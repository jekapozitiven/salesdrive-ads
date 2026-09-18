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

# Категорию по артикулу берём из уже готового индекса приложения «Товары»
# (Cloudflare Worker + KV). Он сам ходит в Prom/Horoshop, нормализует артикулы
# и связывает магазины по алиасам. Токены магазинов сборщику НЕ нужны.
WORKER_URL = os.environ.get(
    "TOVARY_WORKER_URL", "https://cold-sea-36e7tovary.bonnashops.workers.dev").rstrip("/")

_cat_cache = {}


def category_of(sku):
    """Артикул -> категория через /lookup воркера «Товары». С кэшем по артикулу."""
    key = (sku or "").strip()
    if not key:
        return None
    if key in _cat_cache:
        return _cat_cache[key]
    cat = None
    try:
        r = requests.post(WORKER_URL + "/lookup", json={"article": key}, timeout=20)
        if r.status_code == 200:
            j = r.json()
            if j.get("found"):
                cat = (j.get("product") or {}).get("category") or None
    except Exception as e:
        print(f"  lookup error {key!r}: {e}")
    _cat_cache[key] = cat
    return cat


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


def norm_sku(s):
    """База артикула без вариаций. База — заглавными, вариации — строчными/в скобках."""
    s = (s or "").strip()
    s = re.sub(r"\s*\(.*$", "", s)     # обрезать от первой "(" — (B), (коп1)L …
    s = s.split("/")[0].strip()        # компаунд "Rap-RD266/RD015" -> "Rap-RD266"
    s = re.sub(r"[a-z]\d*$", "", s)     # хвост-вариация строчными: q2, q, l (базы — ЗАГЛАВНЫМИ)
    return re.sub(r"\s+", "", s).lower()


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

    # распределение statusId + справочник статусов из meta (id -> назва)
    print("\n=== statusId В ЗАКАЗАХ ===")
    for sid, n in Counter(x.get("statusId") for x in orders).most_common():
        print(f"  {sid}: {n}")
    print("\n=== СПРАВОЧНИК СТАТУСОВ (id -> назва) ===")
    fields = (resp.get("meta") or {}).get("fields") or {}
    stf = None
    for fk, fv in fields.items():
        lab = str((fv or {}).get("label", "")).lower()
        if fk.lower() in ("statusid", "status") or "статус" in lab or "status" in lab:
            stf = (fk, fv)
            break
    if stf:
        fk, fv = stf
        print(f"поле: {fk} ({fv.get('label')})")
        for opt in (fv.get("options") or []):
            print(f"  id={opt.get('value')} -> {opt.get('text')}")
    else:
        print("поле статуса не нашёл. Ключи meta.fields:", list(fields.keys()))

    # === КАТЕГОРИЯ ПО АРТИКУЛУ через воркер «Товары» (/lookup) ===
    # Воркер сам нормализует артикул и связывает магазины по алиасам.
    print(f"\n=== КАТЕГОРИЯ ПО АРТИКУЛУ (через {WORKER_URL}/lookup) ===")
    total = matched = shown = 0
    per_t = Counter()
    per_m = Counter()
    ex_nf = []
    for x in orders:
        e = extract(x)
        if e["source"] not in ("Сайт-Black-street", "Сайт-Bonna-shop", "Хор-Blink"):
            continue
        for sku in e["skus"]:
            if not sku:
                continue
            total += 1
            per_t[e["shop"]] += 1
            cat = category_of(sku)
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


FIREBASE_DB_URL = os.environ.get("FIREBASE_DB_URL", "").strip().rstrip("/")
# статусы SalesDrive, которые считаем «апрув» (через запятую в секрете APRUV_STATUS).
APRUV_STATUS = {s.strip() for s in os.environ.get("APRUV_STATUS", "").split(",") if s.strip()}
MIXED_KEY = "__потребує_розподілу__"


def fbkey(s):
    """Ключ Firebase без запрещённых символов . $ # / [ ]."""
    return re.sub(r"[.$#/\[\]]", "_", str(s)).strip() or "_"


def order_categories(e):
    """Список уникальных категорий по артикулам основного товара заказа."""
    cats = []
    for sku in e["skus"]:
        c = category_of(sku)
        if c and c not in cats:
            cats.append(c)
    return cats


def aggregate(orders):
    """Агрегаты по источник -> день -> категория(кампания). Смешанные -> отдельная корзина."""
    agg = {}
    for x in orders:
        e = extract(x)
        if e["source"] not in WANTED_SOURCES:
            continue
        day = e["date"]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", day or ""):
            continue
        cats = order_categories(e)
        if len(cats) > 1:
            catkey, catname = MIXED_KEY, "Потребує розподілу (кілька категорій)"
        elif cats:
            catkey, catname = fbkey(cats[0]), cats[0]
        else:
            catkey, catname = "_no_cat", "(без категорії)"
        cell = agg.setdefault(fbkey(e["source"]), {}).setdefault(day, {}).setdefault(catkey, {
            "cat": catname, "leads": 0, "approved": 0, "sum": 0.0,
            "upsCount": 0, "upsSum": 0.0, "extIds": [],
        })
        cell["leads"] += 1
        cell["sum"] += e["mainSum"]
        if str(x.get("statusId")) in APRUV_STATUS:
            cell["approved"] += 1
        if e["upsells"]:
            cell["upsCount"] += 1
            cell["upsSum"] += e["upsellSum"]
        if e["externalId"] and len(cell["extIds"]) < 500:
            cell["extIds"].append(str(e["externalId"]))
    return agg


def push_ads_firebase(agg):
    """PUT агрегатов в shop-reports/ads (только эта ветка)."""
    if not FIREBASE_DB_URL:
        print("FIREBASE_DB_URL пуст — агрегаты не записаны (только показ).")
        return
    url = f"{FIREBASE_DB_URL}/shop-reports/ads.json"
    r = requests.put(url, data=json.dumps(agg, ensure_ascii=False).encode("utf-8"),
                     headers={"Content-Type": "application/json"}, timeout=60)
    print(f"Firebase ads: HTTP {r.status_code}, источников={len(agg)}")


def main():
    if not SD_URL or not SD_KEY:
        raise SystemExit("Нет SALESDRIVE_URL или SALESDRIVE_API_KEY — положи в Secrets/Variables.")
    date_from = (dt.date.today() - dt.timedelta(days=DAYS)).isoformat()
    print(f"SalesDrive: заказы с {date_from}, по {LIMIT}/страницу.")

    if PROBE:
        probe(fetch_page(1, date_from))
        return

    orders = fetch_all(date_from)
    print(f"Всего заказов за период: {len(orders)}")
    agg = aggregate(orders)

    # сводка в лог (проверка перед записью)
    print("\n=== СВОДКА ПО КАМПАНИЯМ ===")
    for src in sorted(agg):
        tot = {}
        for day, cats in agg[src].items():
            for ck, c in cats.items():
                t = tot.setdefault(ck, {"cat": c["cat"], "leads": 0, "approved": 0, "sum": 0.0, "upsSum": 0.0})
                t["leads"] += c["leads"]; t["approved"] += c["approved"]
                t["sum"] += c["sum"]; t["upsSum"] += c["upsSum"]
        print(f"\n[{src}]")
        for ck, t in sorted(tot.items(), key=lambda kv: -kv[1]["leads"]):
            print(f"  {t['cat'][:45]:<45} заявок={t['leads']:>4} апрув={t['approved']:>4} "
                  f"сума={t['sum']:>10.0f} допрод={t['upsSum']:>8.0f}")

    if not APRUV_STATUS:
        print("\n(!) APRUV_STATUS не задан — 'апрув' везде 0. Добавь секрет APRUV_STATUS "
              "со списком id статусов-апрув через запятую.")
    push_ads_firebase(agg)


if __name__ == "__main__":
    main()
