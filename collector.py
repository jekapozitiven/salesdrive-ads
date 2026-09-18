#!/usr/bin/env python3
"""
BBB CLUB — сборщик рекламной статистики по кампаниям (SalesDrive → MyDrop → Firebase).

ЭТАП 1 (этот файл сейчас): подключиться к SalesDrive и показать РЕАЛЬНЫЕ поля заказа —
как называется внешний номер, артикул товара, статус. Запусти с SD_PROBE=1.
По результату докрутим ЭТАП 2 (артикул→категория, джойн с MyDrop за дроп/продажной
ценой и статусом апрув, агрегаты по кампаниям в Firebase).

Секреты/переменные (GitHub → Settings → Secrets and variables → Actions):
  SALESDRIVE_URL      (Variable) — домен, напр. blackstreet.salesdrive.me
  SALESDRIVE_API_KEY  (Secret)   — ключ SalesDrive с правом ЧТЕНИЯ заявок
  MYDROP_API_KEY      (Secret)   — тот же ключ MyDrop, что в stats.py (нужен на этапе 2)
  FIREBASE_DB_URL     (Variable) — как в stats.py (нужен на этапе 2)

Лимиты SalesDrive: 10 запросов/мин, 100/час, 1000/сутки — поэтому пауза между страницами.
"""
import os, sys, json, time, re, datetime as dt
from collections import Counter
import requests

SD_URL    = os.environ.get("SALESDRIVE_URL", "").strip().rstrip("/")
SD_KEY    = os.environ.get("SALESDRIVE_API_KEY", "").strip()
DAYS      = int(os.environ.get("SD_DAYS", "14"))
LIMIT     = int(os.environ.get("SD_LIMIT", "100"))
MAX_PAGES = int(os.environ.get("SD_MAX_PAGES", "50"))
SLEEP     = float(os.environ.get("SD_SLEEP", "7"))   # держим < 10 запросов/мин
TIMEOUT   = int(os.environ.get("SD_TIMEOUT", "40"))
PROBE     = os.environ.get("SD_PROBE", "").strip() in ("1", "true", "yes")


# домен сайта -> магазин. Blink подтверждён из примера; остальные добавим по выводу.
SHOP_DOMAINS = {
    "blink.in.ua": "Blink",
    "black-street.com.ua": "Black-street",
    "bonna-shop.com.ua": "Bonna-shop",
}


# ID сайта в поле sajt -> магазин (надёжнее домена: есть даже без ссылки).
SAJT_TO_SHOP = {"22": "Black-street", "21": "Bonna-shop", "19": "Blink"}

# токены каталогов (категория по артикулу). Имена секретов — как у тебя.
PROM_TOKENS = {
    "Black-street": os.environ.get("TOKEN_BLACKSTREET", "").strip(),
    "Bonna-shop":   os.environ.get("TOKEN_BONNA", "").strip(),
    "Core22":       os.environ.get("TOKEN_CORE22", "").strip(),
    "Street Code":  os.environ.get("TOKEN_STREETCODE", "").strip(),
}
HOROSHOP = {
    "domain":   os.environ.get("HOROSHOP_DOMAIN", "").strip(),
    "login":    os.environ.get("HOROSHOP_LOGIN", "").strip(),
    "password": os.environ.get("HOROSHOP_PASSWORD", "").strip(),
}


def _dom(h):
    m = re.search(r"https?://([^/]+)", h or "")
    return m.group(1).lower().replace("www.", "") if m else ""


def shop_of(order):
    s = str(order.get("sajt"))
    if s in SAJT_TO_SHOP:
        return SAJT_TO_SHOP[s]
    for p in order.get("products") or []:      # запасной вариант — по домену ссылки
        d = _dom(p.get("href"))
        if d in SHOP_DOMAINS:
            return SHOP_DOMAINS[d]
    return "?"


def prom_catalog(token, cap=80000):
    """Артикул -> категория (group.name) из Prom API."""
    idx, url = {}, "https://my.prom.ua/api/v1/products/list"
    headers = {"Authorization": "Bearer " + token}
    last, got = None, 0
    while got < cap:
        params = {"limit": 100}
        if last:
            params["last_id"] = last
        r = requests.get(url, headers=headers, params=params, timeout=40)
        if r.status_code != 200:
            print(f"  Prom HTTP {r.status_code}: {r.text[:200]}")
            break
        prods = (r.json() or {}).get("products", [])
        if not prods:
            break
        for p in prods:
            sku = (p.get("sku") or "").strip()
            if sku:
                idx[sku] = (p.get("group") or {}).get("name")
        got += len(prods)
        new_last = max((p.get("id") or 0) for p in prods)   # курсор ВПЕРЁД: наибольший id
        if new_last == last:                                # прогресса нет — стоп
            break
        last = new_last
        if len(prods) < 100:
            break
        time.sleep(0.3)
    return idx


def norm_sku(s):
    """База артикула без вариаций. База — заглавными, вариации — строчными/в скобках."""
    s = (s or "").strip()
    s = re.sub(r"\s*\(.*$", "", s)     # обрезать от первой "(" — (B), (коп1)L …
    s = s.split("/")[0].strip()        # компаунд "Rap-RD266/RD015" -> "Rap-RD266"
    s = re.sub(r"[a-z]\d*$", "", s)     # хвост-вариация строчными: q2, q, l (базы — ЗАГЛАВНЫМИ)
    return re.sub(r"\s+", "", s).lower()


def _hs_cat(p):
    """Достаём имя категории из товара Horoshop (parent — путь/объект/список/id)."""
    cat = p.get("parent")
    if isinstance(cat, dict):
        cat = cat.get("title") or cat.get("name") or cat.get("id")
    if isinstance(cat, list):
        cat = cat[-1] if cat else None
        if isinstance(cat, dict):
            cat = cat.get("title") or cat.get("name") or cat.get("id")
    if isinstance(cat, str) and ("\\" in cat or "/" in cat):
        cat = re.split(r"[\\/]", cat)[-1].strip()
    return cat


def horoshop_catalog(domain, login, password, cap=40000):
    """Полный каталог сайта Horoshop: артикул -> категория."""
    idx = {}
    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    try:
        r = requests.post(f"https://{domain}/api/auth/",
                          json={"login": login, "password": password}, timeout=30)
        j = r.json()
        tok = (j.get("response") or {}).get("token") if isinstance(j.get("response"), dict) else None
        tok = tok or j.get("token")
    except Exception as e:
        print(f"  Horoshop {domain}: ошибка авторизации ({e})")
        return idx
    if not tok:
        print(f"  Horoshop {domain}: не пришёл токен. Ответ: {str(j)[:200]}")
        return idx
    offset = 0
    while offset < cap:
        try:
            r = requests.post(f"https://{domain}/api/products/get/",
                              json={"token": tok, "limit": 500, "offset": offset}, timeout=60)
        except Exception as e:
            print(f"  Horoshop {domain}: ошибка запроса товаров ({e})"); break
        if r.status_code != 200:
            print(f"  Horoshop {domain} HTTP {r.status_code}: {r.text[:200]}"); break
        j = r.json()
        prods = (j.get("response") or {}).get("products") if isinstance(j.get("response"), dict) else None
        prods = prods or j.get("products") or []
        if not prods:
            break
        for p in prods:
            art = (p.get("article") or p.get("parent_article") or "").strip()
            if art:
                idx[art] = _hs_cat(p)
        offset += len(prods)
        if len(prods) < 500:
            break
        time.sleep(0.3)
    return idx


# Источник в MyDrop по логике: сайт (домен) + Витрати (expensesAmount).
# Витрати пусто -> "Сайт-<магазин>" (Google-трафик, НУЖЕН);
# Витрати есть  -> "Prom-<магазин>" (Пром, НЕ нужен);
# Blink -> всегда "Хор-Blink".
WANTED_SOURCES = {"Сайт-Black-street", "Сайт-Bonna-shop", "Хор-Blink"}


def site_source(o):
    shop = shop_of(o)                      # Black-street / Bonna-shop / Blink / "?..."
    vyt = o.get("expensesAmount") or 0
    if shop == "Blink":
        return "Хор-Blink"
    if shop in ("Black-street", "Bonna-shop"):
        return ("Prom-" if vyt else "Сайт-") + shop
    return None                            # неизвестный сайт


def extract(o):
    prods = o.get("products") or []
    main = [p for p in prods if not p.get("upsell")]
    ups = [p for p in prods if p.get("upsell")]
    amt = lambda p: (p.get("price") or 0) * (p.get("amount") or 1)
    return {
        "id": o.get("id"),
        "externalId": o.get("externalId"),
        "date": (o.get("orderTime") or "")[:10],
        "statusId": o.get("statusId"),
        "shop": shop_of(o),
        "sajt": o.get("sajt"),
        "expenses": o.get("expensesAmount"),
        "source": site_source(o),
        "skus": [p.get("sku") for p in main],
        "mainSum": sum(amt(p) for p in main),
        "upsells": [{"name": p.get("text"), "price": p.get("price")} for p in ups],
        "upsellSum": sum(amt(p) for p in ups),
    }


def _base():
    u = SD_URL
    if not u.startswith("http"):
        u = "https://" + u
    return u


def fetch_page(page, date_from):
    params = {
        "page": page,
        "limit": LIMIT,
        "filter[orderTime][from]": date_from,
        "filter[statusId]": "__ALL__",
    }
    r = requests.get(_base() + "/api/order/list/", params=params,
                     headers={"Form-Api-Key": SD_KEY}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise SystemExit(f"SalesDrive HTTP {r.status_code}: {r.text[:400]}")
    try:
        return r.json()
    except Exception:
        raise SystemExit(f"SalesDrive вернул не-JSON: {r.text[:400]}")


def orders_from(resp):
    if isinstance(resp, dict):
        for k in ("data", "orders", "result", "items"):
            if isinstance(resp.get(k), list):
                return resp[k]
    if isinstance(resp, list):
        return resp
    return []


def fetch_all(date_from):
    out, page = [], 1
    while page <= MAX_PAGES:
        resp = fetch_page(page, date_from)
        batch = orders_from(resp)
        out += batch
        meta = resp if isinstance(resp, dict) else {}
        tp = (meta.get("totals", {}) or {}).get("pages") \
            or (meta.get("pagination", {}) or {}).get("totalPages") \
            or (meta.get("meta", {}) or {}).get("totalPage")
        if tp:
            if page >= int(tp):
                break
        elif len(batch) < LIMIT:
            break
        page += 1
        time.sleep(SLEEP)
    return out


def probe(resp):
    print("Верхний уровень ответа, ключи:",
          list(resp.keys()) if isinstance(resp, dict) else type(resp).__name__)
    orders = orders_from(resp)
    print(f"Заказов на странице: {len(orders)}")
    if not orders:
        print("Пусто. Проверь: домен SALESDRIVE_URL, ключ, что за период есть заказы.")
        # покажем весь ответ, чтобы понять структуру
        print(json.dumps(resp, ensure_ascii=False, indent=2)[:2000])
        return
    o = orders[0]
    print("\n=== ПРИМЕР ЗАКАЗА — ключи верхнего уровня ===")
    print(", ".join(sorted(o.keys())))
    print("\n=== ПРИМЕР ЗАКАЗА — полный JSON (обрезан) ===")
    print(json.dumps(o, ensure_ascii=False, indent=2)[:4500])

    # кандидаты: внешний номер
    ext = [k for k in o if "extern" in k.lower()
           or k.lower() in ("externalid", "orderid", "ttn", "sajt", "number")]
    print("\nВозможные поля ВНЕШНЕГО НОМЕРА:", ext or "— не нашёл, гляну по JSON выше")

    # кандидаты: список товаров + артикул
    prod_key = None
    for k, v in o.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            kk = [str(x).lower() for x in v[0].keys()]
            if any(("sku" in x or "articul" in x or "артик" in x or "vendor" in x) for x in kk):
                prod_key = k
                break
    if prod_key:
        print(f"Товары в поле '{prod_key}'. Ключи позиции:",
              ", ".join(sorted(o[prod_key][0].keys())))
    else:
        print("Список товаров с артикулом авто не нашёл — определим по JSON выше.")

    # кандидаты: статус
    st = [k for k in o if "status" in k.lower()]
    print("Возможные поля СТАТУСА:", st)

    # частота полей по всем заказам
    c = Counter()
    for x in orders:
        if isinstance(x, dict):
            c.update(x.keys())
    print("\n=== ЧАСТОТА ПОЛЕЙ (топ-40) ===")
    for k, n in c.most_common(40):
        print(f"  {k}: {n}")

    # --- значения ключевых полей по первым заказам (для выбора схемы привязки) ---
    def dom(h):
        m = re.search(r"https?://([^/]+)", h or "")
        return m.group(1) if m else ""
    print("\n=== КЛЮЧЕВЫЕ ПОЛЯ (первые 10 заказов, без апселов) ===")
    for x in orders[:10]:
        prods = [p for p in (x.get("products") or []) if not p.get("upsell")]
        skus = [p.get("sku", "") for p in prods]
        doms = sorted({dom(p.get("href")) for p in prods if p.get("href")})
        print(f"id={x.get('id')} externalId={x.get('externalId')!r} sajt={x.get('sajt')!r} "
              f"formId={x.get('formId')} statusId={x.get('statusId')} "
              f"utmSource={x.get('utmSource')!r} utmMedium={x.get('utmMedium')!r} "
              f"utmCampaign={x.get('utmCampaign')!r} campaignId={x.get('campaignId')!r} "
              f"shops={doms} sku={skus}")
    n_ext = sum(1 for x in orders if x.get("externalId"))
    print(f"\nИз {len(orders)} заказов: с externalId={n_ext} (ключ для джойна с MyDrop)")

    # --- разбор: сайт + Витрати -> источник, основное + допродажа ---
    print("\n=== РАЗБОР ЗАКАЗА (первые 15) ===")
    for x in orders[:15]:
        e = extract(x)
        print(f"ext={e['externalId']!r} sajt={e['sajt']!r} Витрати={e['expenses']!r} -> ИСТОЧНИК={e['source']} "
              f"| магазин={e['shop']} sku={e['skus']} осн={e['mainSum']} доп={e['upsellSum']}")

    # сколько заказов в каждый источник (нам нужны только 3 сайтовых)
    sc = Counter(extract(x)["source"] for x in orders)
    print("\n=== ИСТОЧНИКИ (по логике сайт+Витрати) ===")
    for s, n in sc.most_common():
        mark = "  <- НУЖЕН" if s in WANTED_SOURCES else ""
        print(f"  {s}: {n}{mark}")

    # какие значения принимает поле sajt
    sj = Counter(str(x.get("sajt")) for x in orders)
    print("\n=== ЗНАЧЕНИЯ ПОЛЯ 'sajt' ===")
    for s, n in sj.most_common(10):
        print(f"  {s!r}: {n}")

    # === КАТЕГОРИИ: у каждого магазина свой каталог ===
    #   Black-street, Bonna-shop = Prom (магазины на своём домене) -> TOKEN_*
    #   Blink = Horoshop (blink.in.ua) -> HOROSHOP_*
    shop_idx = {}   # магазин -> {норм.артикул: категория}

    for shop, tok in (("Black-street", PROM_TOKENS["Black-street"]),
                      ("Bonna-shop", PROM_TOKENS["Bonna-shop"])):
        if not tok:
            print(f"\n[Prom] нет токена для {shop} — пропускаю")
            continue
        print(f"\n[Prom] тяну каталог {shop}…")
        raw = prom_catalog(tok)
        ni = {}
        for sku, cat in raw.items():
            ni.setdefault(norm_sku(sku), cat)
        shop_idx[shop] = ni
        print(f"[Prom] {shop}: товаров={len(raw)}, уникальных баз={len(ni)}")

    if HOROSHOP["domain"] and HOROSHOP["login"] and HOROSHOP["password"]:
        print(f"\n[Horoshop] тяну каталог {HOROSHOP['domain']} (Blink)…")
        raw = horoshop_catalog(HOROSHOP["domain"], HOROSHOP["login"], HOROSHOP["password"])
        if raw:
            print("  пример:", dict(list(raw.items())[:3]))
        ni = {}
        for sku, cat in raw.items():
            ni.setdefault(norm_sku(sku), cat)
        shop_idx["Blink"] = ni
        print(f"[Horoshop] Blink: товаров={len(raw)}, уникальных баз={len(ni)}")
    else:
        print("\n[Horoshop] нет доступа для Blink (HOROSHOP_* пустые)")

    # резолв категорий по каждому заказу через каталог его магазина
    print("\n=== КАТЕГОРИЯ ПО АРТИКУЛУ (у каждого свой каталог) ===")
    total = matched = shown = 0
    per_t = Counter()
    per_m = Counter()
    ex_nf = []
    for x in orders:
        e = extract(x)
        if e["source"] not in ("Сайт-Black-street", "Сайт-Bonna-shop", "Хор-Blink"):
            continue
        ni = shop_idx.get(e["shop"], {})
        for sku in e["skus"]:
            if not sku:
                continue
            total += 1
            per_t[e["shop"]] += 1
            cat = ni.get(norm_sku(sku))
            if cat:
                matched += 1
                per_m[e["shop"]] += 1
                tag = cat
            else:
                tag = "(нет категории)"
                if len(ex_nf) < 15:
                    ex_nf.append(f"{e['shop']}:{sku!r}")
            if shown < 18:
                print(f"  {e['shop']}: {sku!r} -> {tag}")
                shown += 1
    print(f"\nВсего артикулов: {total} | с категорией: {matched} | без: {total - matched}")
    for shop in per_t:
        print(f"  {shop}: {per_m[shop]}/{per_t[shop]}")
    if ex_nf:
        print("Без категории (примеры):", ", ".join(ex_nf))


def main():
    if not SD_URL or not SD_KEY:
        raise SystemExit("Нет SALESDRIVE_URL или SALESDRIVE_API_KEY — положи в Secrets/Variables.")
    date_from = (dt.date.today() - dt.timedelta(days=DAYS)).isoformat()
    print(f"SalesDrive: заказы с {date_from}, по {LIMIT}/страницу.")

    if PROBE:
        probe(fetch_page(1, date_from))
        return

    # обычный прогон (пока только выгрузка — этап 2 добавим после сверки полей)
    orders = fetch_all(date_from)
    print(f"Всего заказов за период: {len(orders)}")
    with open("orders_sample.json", "w", encoding="utf-8") as f:
        json.dump(orders[:50], f, ensure_ascii=False, indent=2)
    print("Сохранил orders_sample.json (первые 50) — для сверки полей.")
    # --- ЭТАП 2 (добавим, когда подтвердим имена полей и появятся API магазинов) ---
    # 1) для каждого заказа: внешний_№, артикул(ы), статус, дата
    # 2) артикул -> категория (API магазина Prom/Horoshop, кэш)
    # 3) категория -> кампания (маппинг из Firebase, редактируется во вкладке)
    # 4) джойн по внешнему_№ с MyDrop -> дроп-цена, продажная, маржа, апрув
    # 5) агрегаты по кампаниям/дням -> Firebase shop-reports/ads/<магазин>/<кампания>/<день>
    # 6) заказы со смешанными категориями -> в узел "нужно распределить"


if __name__ == "__main__":
    main()
