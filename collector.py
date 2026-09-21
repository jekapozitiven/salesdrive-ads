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
from urllib.parse import quote
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

# магазин заказа -> ключ магазина в воркере «Товары» (чтобы категория тянулась
# СТРОГО из каталога этого магазина, а не из склеенной межмагазинной карточки).
SHOP_TO_STORE = {"Black-street": "blackstreet", "Bonna-shop": "bonna", "Blink": "blink"}


def product_of(sku, store=None):
    """Артикул -> карточка товара из «Товары» (/lookup). Кэш по (магазин, артикул).
    Возвращает {category, name, img} (или None-поля, если не найдено)."""
    key = (sku or "").strip()
    if not key:
        return {"category": None, "name": "", "img": ""}
    ck = f"{store or ''}|{key}"
    if ck in _cat_cache:
        return _cat_cache[ck]
    out = {"category": None, "name": "", "img": ""}
    try:
        body = {"article": key}
        if store:
            body["store"] = store
        r = requests.post(WORKER_URL + "/lookup", json=body, timeout=20)
        if r.status_code == 200:
            j = r.json()
            if j.get("found"):
                p = j.get("product") or {}
                imgs = p.get("images") or []
                img = ""
                if imgs:
                    first = imgs[0]
                    img = first.get("url") if isinstance(first, dict) else str(first)
                out = {"category": p.get("category") or None,
                       "name": p.get("name") or "", "img": img or ""}
    except Exception as e:
        print(f"  lookup error {key!r}: {e}")
    _cat_cache[ck] = out
    return out


def category_of(sku, store=None):
    """Артикул -> категория (обёртка над product_of)."""
    return product_of(sku, store)["category"]


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
        "mainItems": [{"sku": p.get("sku"), "price": amt(p),
                       "href": p.get("href") or "", "name": p.get("text") or p.get("name") or ""}
                      for p in main],
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
    for attempt in range(4):
        r = requests.get(_base() + "/api/order/list/", params=params,
                         headers={"Form-Api-Key": SD_KEY}, timeout=TIMEOUT)
        if r.status_code == 200:
            break
        if r.status_code in (429, 500, 502, 503, 504):
            wait = 15 * (attempt + 1)
            print(f"SalesDrive {r.status_code} (ліміт?) — чекаю {wait}s, спроба {attempt + 1}/4")
            time.sleep(wait)
            continue
        raise SystemExit(f"SalesDrive HTTP {r.status_code}: {r.text[:400]}")
    else:
        print("SalesDrive: ліміт не відпустив — пропускаю прогін (наступний за розкладом добере).")
        sys.exit(0)   # м'який вихід, джоб не червоний
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


def _num(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def order_categories(e):
    """Список уникальных категорий по артикулам основного товара заказа.
    Категория берётся из каталога ИМЕННО магазина заказа (store)."""
    store = SHOP_TO_STORE.get(e["shop"])
    cats = []
    for sku in e["skus"]:
        c = category_of(sku, store)
        if c and c not in cats:
            cats.append(c)
    return cats


def aggregate(orders, margin_by_ext=None):
    """Агрегаты по источник -> день -> категория(кампания). Смешанные -> отдельная корзина.
    margin_by_ext: {внешний_номер: маржа} из MyDrop (по externalId заказа).
    Возвращает (agg, articles), где articles: источник -> катКлюч -> {артикул: кол-во}
    (для раскрытия категории до списка артикулов во вкладке «Реклама»)."""
    mbe = margin_by_ext or {}
    agg = {}
    articles = {}
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
        srckey = fbkey(e["source"])
        cell = agg.setdefault(srckey, {}).setdefault(day, {}).setdefault(catkey, {
            "cat": catname, "leads": 0, "approved": 0, "sum": 0.0,
            "upsCount": 0, "upsSum": 0.0, "margin": 0.0, "extIds": [],
        })
        cell["leads"] += 1
        cell["sum"] += e["mainSum"]
        if str(x.get("statusId")) in APRUV_STATUS:
            cell["approved"] += 1
        if e["upsells"]:
            cell["upsCount"] += 1
            cell["upsSum"] += e["upsellSum"]
        _m = mbe.get(str(e["externalId"]))
        cell["margin"] += (_m.get("gross", 0.0) if isinstance(_m, dict) else (_m or 0.0))
        if e["externalId"] and len(cell["extIds"]) < 500:
            cell["extIds"].append(str(e["externalId"]))
        # артикулы этой категории (по магазину) — для раскрытия в приложении
        abucket = articles.setdefault(srckey, {}).setdefault(catkey, {})
        for sku in e["skus"]:
            s = (sku or "").strip()
            if not s:
                continue
            if s in abucket or len(abucket) < 800:
                abucket[s] = abucket.get(s, 0) + 1
    return agg, articles


def push_ads_firebase(agg):
    """PUT агрегатов ПО ДНЯМ в shop-reports/ads/<src>/<day> — история накапливается (старое не стираем)."""
    if not FIREBASE_DB_URL:
        print("FIREBASE_DB_URL пуст — агрегаты не записаны (только показ).")
        return
    n = 0
    for src, days in agg.items():
        for day, cells in days.items():
            url = f"{FIREBASE_DB_URL}/shop-reports/ads/{quote(src, safe='')}/{day}.json"
            requests.put(url, data=json.dumps(cells, ensure_ascii=False).encode("utf-8"),
                         headers={"Content-Type": "application/json"}, timeout=60)
            n += 1
    print(f"Firebase ads: записано дней {n} по {len(agg)} источникам")


def build_ord(orders, mbe=None):
    """Пер-заказная детализация (навсегда): srckey -> day -> oid -> запись с товарами.
    Запись: {catKey,catName,approved,sum,upsSum,upsCount,margin,drop,items,ext}.
    items: [{sku,name,img,href,price}]. drop = продажна − маржа (по факту MyDrop)."""
    mbe = mbe or {}
    out = {}
    for x in orders:
        e = extract(x)
        if e["source"] not in WANTED_SOURCES:
            continue
        day = e["date"]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", day or ""):
            continue
        oid = str(x.get("id") or "")
        if not oid:
            continue
        store = SHOP_TO_STORE.get(e["shop"])
        cats, items = [], []
        for it in e["mainItems"]:
            p = product_of(it["sku"], store)
            if p["category"] and p["category"] not in cats:
                cats.append(p["category"])
            items.append({"sku": it["sku"] or "", "name": it["name"] or p["name"] or it["sku"] or "",
                          "img": p["img"] or "", "href": it["href"] or "", "price": it["price"]})
        if len(cats) > 1:
            catkey, catname = MIXED_KEY, "Потребує розподілу (кілька категорій)"
        elif cats:
            catkey, catname = fbkey(cats[0]), cats[0]
        else:
            catkey, catname = "_no_cat", "(без категорії)"
        # маржа/апрув/выкуп из сматченного MyDrop-заказа (по внешнему номеру);
        # маржа = ВАЛОВА (продажна − дроп). Если MyDrop не сматчился — апрув по SalesDrive, маржа 0.
        m = mbe.get(str(e["externalId"]))
        if isinstance(m, dict):
            margin = m.get("gross", 0.0); drop = m.get("drop", 0.0)
            approved = m.get("approved", 0); sold = m.get("sold", 0)
        else:
            margin = 0.0; drop = 0.0; sold = 0
            approved = 1 if str(x.get("statusId")) in APRUV_STATUS else 0
        out.setdefault(fbkey(e["source"]), {}).setdefault(day, {})[oid] = {
            "catKey": catkey, "catName": catname,
            "approved": approved, "sold": sold,
            "sum": e["mainSum"], "upsSum": e["upsellSum"], "upsCount": 1 if e["upsells"] else 0,
            "margin": margin, "drop": drop,
            "items": items, "ext": str(e["externalId"] or ""),
        }
    return out


# --- телефонные заказы с сайта: из MyDrop по примечанию оператора ---
MD_SHOP_FROM_NOTE = [
    (re.compile(r"блек|блэк|black", re.I), "Сайт-Black-street", "blackstreet"),
    (re.compile(r"бонна|bonna", re.I),     "Сайт-Bonna-shop",   "bonna"),
    (re.compile(r"блін?к|блин?к|blink", re.I), "Хор-Blink",     "blink"),
]
ARTICLE_RE = re.compile(r"[A-Za-zА-Яа-яЇїІіЄєҐґ][A-Za-zА-Яа-яЇїІіЄєҐґ0-9.]*-[A-Za-z0-9./]+")
MD_APPROVE_WORDS = ("підтвер", "подтвер", "продаж", "прода", "відправл", "отправл",
                    "виконан", "выполн", "доставл", "видан", "выдан", "оплач")


def _md_approved(m):
    t = str((m.get("orderStatus") or {}).get("title") or "").lower()
    return 1 if any(w in t for w in MD_APPROVE_WORDS) else 0


def build_phone_ord(md_orders):
    """Телефонные заказы с сайта: MyDrop-заказы, где в примечании (description) есть
    «с сайта <магазин>». Магазин из примечания, категория из артикула (в примечании
    или из товара), дроп/маржа/сумма из заказа MyDrop. Ключ oid = md<id> (не двоит)."""
    out = {}
    for m in md_orders:
        note = str(m.get("description") or "")
        if not re.search(r"с\s*сайт", note, re.I):
            continue
        shop = None
        for rx, src, store in MD_SHOP_FROM_NOTE:
            if rx.search(note):
                shop = (src, store); break
        if not shop:
            continue
        src, store = shop
        day = str(m.get("dateTime") or m.get("date") or "")[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
            continue
        oid = "md" + str(m.get("id") or "")
        if oid == "md":
            continue
        total = _num(m.get("total"))
        prods = m.get("products") or []
        arts = ARTICLE_RE.findall(note)
        if not arts:
            arts = [((p.get("product") or {}).get("sku") or p.get("sku") or "") for p in prods]
        cat, item = None, None
        for a in arts:
            a = (a or "").strip()
            if not a:
                continue
            p = product_of(a, store)
            nm = p["name"] or (prods and (prods[0].get("product") or {}).get("title")) or a
            if not item:
                item = {"sku": a, "name": nm, "img": p["img"] or "", "href": "", "price": total}
            if p["category"]:
                cat = p["category"]
                item = {"sku": a, "name": nm, "img": p["img"] or "", "href": "", "price": total}
                break
        appr, sold = _md_flags(m)
        drop = _num(m.get("dropPrice"))
        gross = round(total - drop, 2) if appr else 0.0   # валова (продажна − дроп) на апрувнутих
        out.setdefault(fbkey(src), {}).setdefault(day, {})[oid] = {
            "catKey": fbkey(cat) if cat else "_no_cat", "catName": cat or "(без категорії)",
            "approved": appr, "sold": sold, "sum": total, "upsSum": 0.0, "upsCount": 0,
            "margin": gross, "drop": drop,
            "items": [item] if item else [], "ext": str(m.get("id") or ""), "phone": 1,
        }
    return out


def push_ord_firebase(ordtree):
    """PATCH пер-заказной детализации ПО ДНЯМ в ads-ord/<src>/<day> — мерж (не стираем свежие из вебхука),
    история навсегда (старые дни не трогаем)."""
    if not FIREBASE_DB_URL:
        return
    n = 0
    for src, days in ordtree.items():
        for day, ords in days.items():
            url = f"{FIREBASE_DB_URL}/shop-reports/ads-ord/{quote(src, safe='')}/{day}.json"
            requests.patch(url, data=json.dumps(ords, ensure_ascii=False).encode("utf-8"),
                           headers={"Content-Type": "application/json"}, timeout=60)
            n += len(ords)
    print(f"Firebase ads-ord: заказов записано {n}")


def reaggregate_ads_from_ord(ordtree):
    """Пересобрать ads/<src>/<day> ИЗ ads-ord (там и SalesDrive, и телефонные из MyDrop) —
    единый источник правды, чтобы телефонные заказы не затирались."""
    if not FIREBASE_DB_URL:
        return
    n = 0
    for src, days in ordtree.items():
        for day in days:
            try:
                ords = requests.get(
                    f"{FIREBASE_DB_URL}/shop-reports/ads-ord/{quote(src, safe='')}/{day}.json",
                    timeout=40).json() or {}
            except Exception:
                ords = {}
            cells = {}
            for oid, c in ords.items():
                if not c:
                    continue
                ck = c.get("catKey", "_no_cat")
                cell = cells.setdefault(ck, {"cat": c.get("catName") or ck, "leads": 0,
                                             "approved": 0, "sold": 0, "sum": 0.0,
                                             "upsCount": 0, "upsSum": 0.0, "margin": 0.0})
                cell["leads"] += 1
                cell["approved"] += c.get("approved", 0) or 0
                cell["sold"] += c.get("sold", 0) or 0
                cell["sum"] += c.get("sum", 0) or 0
                cell["upsCount"] += c.get("upsCount", 0) or 0
                cell["upsSum"] += c.get("upsSum", 0) or 0
                cell["margin"] += c.get("margin", 0) or 0
            requests.put(f"{FIREBASE_DB_URL}/shop-reports/ads/{quote(src, safe='')}/{day}.json",
                         data=json.dumps(cells, ensure_ascii=False).encode("utf-8"),
                         headers={"Content-Type": "application/json"}, timeout=60)
            n += 1
    print(f"Firebase ads: пересобрано дней {n} из ads-ord")


def push_articles_firebase(articles):
    """PUT списка артикулов по категориям в shop-reports/ads-articles (для раскрытия категории).
    ВАЖНО: артикулы содержат / . ( ) — их НЕЛЬЗЯ использовать как ключи Firebase.
    Поэтому пишем СПИСКОМ пар [артикул, кол-во] (по убыванию кол-ва)."""
    if not FIREBASE_DB_URL:
        return
    out = {}
    for src, cats in (articles or {}).items():
        o = {}
        for ck, d in cats.items():
            o[ck] = [[sku, n] for sku, n in sorted(d.items(), key=lambda kv: -kv[1])]
        out[src] = o
    url = f"{FIREBASE_DB_URL}/shop-reports/ads-articles.json"
    r = requests.put(url, data=json.dumps(out, ensure_ascii=False).encode("utf-8"),
                     headers={"Content-Type": "application/json"}, timeout=60)
    print(f"Firebase ads-articles: HTTP {r.status_code}, источников={len(out)}")


MYDROP_KEY = os.environ.get("MYDROP_API_KEY", "").strip()
MYDROP_URL = os.environ.get("MYDROP_BASE_URL",
                            "https://backend.mydrop.com.ua/dropshipper/api/orders").strip()
MARGIN_KEYS = ("realMargin", "margin", "real_margin")


def mydrop_fetch(days, max_pages=80):
    """Заказы MyDrop за последние `days` дней (для джойна по внешнему номеру)."""
    if not MYDROP_KEY:
        print("Нет MYDROP_API_KEY — MyDrop пропускаю.")
        return []
    headers = {"X-API-KEY": MYDROP_KEY, "Accept": "application/json"}
    date_from = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out, page = [], 1
    while page <= max_pages:
        params = {"date_type": "period", "date_start": date_from,
                  "date_end": dt.date.today().isoformat(), "page": page}
        r = requests.get(MYDROP_URL, headers=headers, params=params, timeout=40)
        if r.status_code != 200:
            print(f"MyDrop HTTP {r.status_code}: {r.text[:200]}")
            break
        j = r.json()
        if isinstance(j, list):
            batch, meta = j, {}
        else:
            batch = j.get("results") or j.get("data") or j.get("orders") or []
            meta = j.get("meta") or {}
        out += batch
        tp = int(meta.get("totalPages") or meta.get("total_pages") or 0) or None
        if not batch:
            break
        if tp and page >= tp:
            break
        if not tp and len(batch) < 20:
            break
        page += 1
        time.sleep(0.2)
    return out


def _extnum(detail):
    """Внешний номер заказа из карточки MyDrop (первое непустое из четырёх полей)."""
    d = detail.get("data") if isinstance(detail.get("data"), dict) else detail
    for k in ("externalOrderId", "promOrderId", "horoshopOrderId", "externalId"):
        v = d.get(k)
        if v not in (None, "", 0, "0"):
            return str(v)
    return ""


def mydrop_detail(oid, headers):
    url = f"{MYDROP_URL.rstrip('/')}/{oid}"
    try:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code == 200:
            return r.json() or {}
    except Exception:
        pass
    return {}


def build_margin_index(md_orders):
    """Строит {внешний_номер: маржа}. Кэш id->внешний_номер в Firebase (тянем карточки
    только для новых заказов, маржу берём из списка — она дозревает)."""
    from concurrent.futures import ThreadPoolExecutor
    headers = {"X-API-KEY": MYDROP_KEY, "Accept": "application/json"}
    cache = {}
    if FIREBASE_DB_URL:
        try:
            r = requests.get(f"{FIREBASE_DB_URL}/shop-reports/ads-md-index.json", timeout=40)
            if r.status_code == 200:
                cache = r.json() or {}
        except Exception:
            pass
    ids = [str(m.get("id")) for m in md_orders if m.get("id")]
    missing = [i for i in ids if i not in cache]
    cap = int(os.environ.get("MD_DETAIL_CAP", "700"))
    missing = missing[:cap]
    print(f"MyDrop-индекс: заказов {len(ids)}, в кэше {len(ids) - len([i for i in ids if i not in cache])}, "
          f"дотягиваю карточек {len(missing)}")
    if missing:
        def work(i):
            return i, _extnum(mydrop_detail(i, headers))
        new = {}
        with ThreadPoolExecutor(max_workers=10) as ex:
            for i, ext in ex.map(work, missing):
                if ext:                       # кэшируем только удачные; пустые перепробуем позже
                    cache[i] = ext
                    new[i] = ext
        print(f"  новых номеров закэшировано: {len(new)}")
        if FIREBASE_DB_URL and new:
            try:
                requests.patch(f"{FIREBASE_DB_URL}/shop-reports/ads-md-index.json",
                               data=json.dumps(new, ensure_ascii=False).encode("utf-8"),
                               headers={"Content-Type": "application/json"}, timeout=60)
            except Exception as e:
                print(f"кэш индекса не сохранён: {e}")
    mbe = {}
    for m in md_orders:
        ext = cache.get(str(m.get("id")))
        if ext:
            total = _num(m.get("total"))
            drop = _num(m.get("dropPrice"))
            appr, sold = _md_flags(m)
            mbe[ext] = {
                "total": total, "drop": drop,
                "gross": round(total - drop, 2) if appr else 0.0,  # валова на апрувнутих
                "approved": appr, "sold": sold,
            }
    return mbe


# --- статусы MyDrop для трекера рекламы ---
# «Апрув» = заказ прошёл колл-центр и ушёл в исполнение (всё, кроме Новый/Недозвон/Вайбер/Дубль/…).
MD_NOT_APPROVED = ("новый", "новые", "новий", "недозвон", "вайбер", "дубл",
                   "не апрув", "не учит", "обмен", "обмін", "хорошоп")


def _md_flags(m):
    """(approved, sold) по статусу MyDrop. sold = финальный успешный (выкуплен)."""
    st = m.get("orderStatus") or {}
    title = str(st.get("title") or "").strip().lower()
    final = st.get("final") is True
    typ = str(st.get("type") or "").strip().lower()
    approved = 0 if any(w in title for w in MD_NOT_APPROVED) else 1
    sold = 1 if (final and typ == "success") else 0
    return approved, sold


def _flat_str_values(o, prefix="", depth=0, acc=None):
    """Плоский разбор: путь-к-полю -> множество строковых значений (до глубины 2)."""
    if acc is None:
        acc = {}
    if depth > 2 or not isinstance(o, dict):
        return acc
    for k, v in o.items():
        p = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, (str, int)):
            acc.setdefault(p, set()).add(str(v))
        elif isinstance(v, dict):
            _flat_str_values(v, p, depth + 1, acc)
    return acc


def mydrop_probe(days):
    # externalId сайтовых заказов SalesDrive
    sd = fetch_all((dt.date.today() - dt.timedelta(days=days)).isoformat())
    site_ext = {str(extract(x)["externalId"]) for x in sd
                if extract(x)["source"] in WANTED_SOURCES and extract(x)["externalId"]}
    print(f"SalesDrive: сайтовых заказов с externalId = {len(site_ext)}")

    md = mydrop_fetch(days)
    print(f"MyDrop: заказов = {len(md)}")
    if not md:
        return
    o = md[0]
    print("\n=== ПРИМЕР MyDrop-ЗАКАЗА (ключи) ===")
    print(", ".join(sorted(str(k) for k in o.keys())))
    print("\n=== JSON (обрезан) ===")
    print(json.dumps(o, ensure_ascii=False)[:2500])

    # авто-детект поля связи: какое поле MyDrop пересекается с externalId SalesDrive
    field_vals = {}
    for m in md:
        for p, vals in _flat_str_values(m).items():
            field_vals.setdefault(p, set()).update(vals)
    hits = [(len(v & site_ext), p) for p, v in field_vals.items() if (v & site_ext)]
    hits.sort(reverse=True)
    print("\n=== ПОЛЯ MyDrop, СОВПАДАЮЩИЕ с externalId SalesDrive ===")
    if hits:
        for ov, p in hits[:8]:
            print(f"  {p}: совпадений {ov}/{len(site_ext)}")
    else:
        print("  совпадений не найдено — externalId в MyDrop, видимо, лежит иначе (см. JSON выше)")

    for mk in MARGIN_KEYS:
        if mk in o:
            print(f"\nМаржа: поле '{mk}' = {o.get(mk)}")
            break

    # --- проверка джойна по телефону и телефон+артикул ---
    def norm_phone(p):
        d = re.sub(r"\D", "", str(p or ""))
        return d[-9:] if len(d) >= 9 else d

    def sd_phone(x):
        srcs = [(x.get("primaryContact") or {}).get("phone")]
        srcs += [c.get("phone") for c in (x.get("contacts") or [])]
        for s in srcs:
            v = s[0] if isinstance(s, list) and s else s
            n = norm_phone(v)
            if n:
                return n
        return ""

    def md_skus(m):
        out = set()
        for p in (m.get("products") or []):
            sku = (p.get("product") or {}).get("sku") or p.get("sku") or ""
            n = norm_sku(sku)
            if n:
                out.add(n)
        return out

    md_by_phone = {}
    for m in md:
        ph = norm_phone(m.get("phone"))
        if ph:
            md_by_phone.setdefault(ph, []).append(m)

    tot = m_ph = m_phsku = 0
    for x in sd:
        e = extract(x)
        if e["source"] not in WANTED_SOURCES:
            continue
        tot += 1
        cands = md_by_phone.get(sd_phone(x), [])
        if cands:
            m_ph += 1
            sdsk = {norm_sku(s) for s in e["skus"] if s}
            if any(sdsk & md_skus(m) for m in cands):
                m_phsku += 1
    print(f"\nДЖОЙН: телефон {m_ph}/{tot}; телефон+артикул {m_phsku}/{tot}")

    # --- выгрузка образца в Firebase: поле «Источник трафика», товары, внешний номер ---
    if FIREBASE_DB_URL:
        srcfields = {}
        for m in md:
            for p, vals in _flat_str_values(m).items():
                if re.search(r"источник|джерел|трафик|traffic|source|utm|канал", p, re.I):
                    srcfields.setdefault(p, set()).update(vals)
        srcfields = {k: sorted(v)[:30] for k, v in srcfields.items()}
        # статистика маржи: продажна−дроп (валовая) против realMargin, наличие dropPrice
        n_drop = s_total = s_drop = s_real = 0
        for m in md:
            t = _num(m.get("total")); dp = _num(m.get("dropPrice")); rm = _num(m.get("realMargin"))
            if dp > 0:
                n_drop += 1
            s_total += t; s_drop += dp; s_real += rm
        margin_stats = {"orders": len(md), "withDropPrice": n_drop,
                        "sumTotal": round(s_total), "sumDrop": round(s_drop),
                        "grossMargin_total_minus_drop": round(s_total - s_drop),
                        "sumRealMargin": round(s_real)}
        dbg = {"at": dt.datetime.now().isoformat(), "count": len(md),
               "allKeys": sorted(str(k) for k in md[0].keys()),
               "trafficFields": srcfields, "marginStats": margin_stats, "samples": md[:3]}
        try:
            requests.put(f"{FIREBASE_DB_URL}/shop-reports/mydrop-debug.json",
                         data=json.dumps(dbg, ensure_ascii=False, default=str).encode("utf-8"),
                         headers={"Content-Type": "application/json"}, timeout=60)
            print("MyDrop debug -> shop-reports/mydrop-debug")
        except Exception as e:
            print(f"mydrop-debug: {e}")


def _prom_groups(token):
    """Категорії з Prom (groups/list) за токеном магазину."""
    if not token:
        return []
    cats, url, headers, last = {}, "https://my.prom.ua/api/v1/groups/list", {"Authorization": "Bearer " + token}, None
    for _ in range(60):
        params = {"limit": 100}
        if last:
            params["last_id"] = last
        r = requests.get(url, headers=headers, params=params, timeout=40)
        if r.status_code != 200:
            print(f"Prom groups HTTP {r.status_code}: {r.text[:150]}")
            break
        groups = (r.json() or {}).get("groups") or (r.json() or {}).get("data") or []
        if not groups:
            break
        for g in groups:
            nm = ((g.get("name_multilang") or {}).get("uk")) or g.get("name")
            if nm and str(nm).strip():
                cats[str(nm).strip()] = 1
        nl = max((g.get("id") or 0) for g in groups)
        if nl == last or len(groups) < 100:
            break
        last = nl
        time.sleep(0.2)
    return sorted(cats.keys())


def _horoshop_categories(cap=12000):
    """Категорії Blink з Horoshop (беремо з поля parent товарів)."""
    domain = os.environ.get("HOROSHOP_DOMAIN", "").strip().replace("https://", "").replace("http://", "").strip("/")
    login = os.environ.get("HOROSHOP_LOGIN", "").strip()
    pw = os.environ.get("HOROSHOP_PASSWORD", "").strip()
    if not (domain and login and pw):
        print(f"Horoshop: пусті секрети (domain={bool(domain)}, login={bool(login)}, pass={bool(pw)})")
        return []
    try:
        r = requests.post(f"https://{domain}/api/auth/", json={"login": login, "password": pw}, timeout=30)
        j = r.json()
        tok = (j.get("response") or {}).get("token") if isinstance(j.get("response"), dict) else None
        tok = tok or j.get("token") or ((j.get("data") or {}).get("token") if isinstance(j.get("data"), dict) else None)
    except Exception as e:
        print(f"Horoshop auth error: {e}"); return []
    if not tok:
        print(f"Horoshop {domain}: токен НЕ отримано. HTTP {r.status_code}, відповідь: {str(j)[:250]}")
        return []
    print(f"Horoshop {domain}: авторизація ок")
    cats, offset, first = set(), 0, True
    while offset < cap:
        try:
            r = requests.post(f"https://{domain}/api/catalog/export/",
                              json={"token": tok, "limit": 500, "offset": offset}, timeout=60)
        except Exception as e:
            print(f"Horoshop export error: {e}"); break
        if r.status_code != 200:
            print(f"Horoshop export HTTP {r.status_code}: {r.text[:200]}"); break
        j = r.json()
        resp = j.get("response") if isinstance(j.get("response"), (dict, list)) else j
        if isinstance(resp, dict):
            prods = resp.get("products") or resp.get("items") or []
        elif isinstance(resp, list):
            prods = resp
        else:
            prods = []
        if first:
            print(f"Horoshop export: товарів={len(prods)}")
            first = False
        if not prods:
            break
        for p in prods:
            # категорія Blink: parent = {"id":.., "value":"Чоловічий одяг/Футболки"}
            # додаємо ВСІ рівні шляху (і батьківські групи, і підгрупи)
            c = p.get("parent")
            if isinstance(c, dict):
                c = c.get("value") or c.get("title") or c.get("name")
            if isinstance(c, str):
                for seg in re.split(r"[\\/]", c):
                    seg = seg.strip()
                    if seg and not seg.isdigit():
                        cats.add(seg)
        offset += len(prods)
        if len(prods) < 500:
            break
        time.sleep(0.3)
    return sorted(cats)


def write_catlists():
    """Майстер-список категорій ПО КОЖНОМУ магазину -> Firebase shop-reports/ads-catlist."""
    out = {}
    bs = _prom_groups(os.environ.get("TOKEN_BLACKSTREET", "").strip())
    if bs:
        out["Сайт-Black-street"] = bs
    bn = _prom_groups(os.environ.get("TOKEN_BONNA", "").strip())
    if bn:
        out["Сайт-Bonna-shop"] = bn
    bl = _horoshop_categories()
    if bl:
        out["Хор-Blink"] = bl
    if out and FIREBASE_DB_URL:
        requests.patch(f"{FIREBASE_DB_URL}/shop-reports/ads-catlist.json",
                       data=json.dumps(out, ensure_ascii=False).encode("utf-8"),
                       headers={"Content-Type": "application/json"}, timeout=60)
    print("Майстер-список категорій: " + ", ".join(f"{k.split('-')[-1]}={len(v)}" for k, v in out.items()))


def main():
    if os.environ.get("SD_MYDROP", "").strip() in ("1", "true", "yes"):
        mydrop_probe(DAYS)
        return
    if not SD_URL or not SD_KEY:
        raise SystemExit("Нет SALESDRIVE_URL или SALESDRIVE_API_KEY — положи в Secrets/Variables.")
    date_from = (dt.date.today() - dt.timedelta(days=DAYS)).isoformat()
    print(f"SalesDrive: заказы с {date_from}, по {LIMIT}/страницу.")

    if PROBE:
        probe(fetch_page(1, date_from))
        return

    orders = fetch_all(date_from)
    print(f"Всего заказов за период: {len(orders)}")

    # маржа из MyDrop по внешнему номеру (если задан ключ)
    mbe = {}
    md = []
    if MYDROP_KEY:
        md = mydrop_fetch(DAYS)
        print(f"MyDrop: заказов {len(md)}")
        mbe = build_margin_index(md)
        site = [extract(x) for x in orders]
        site = [e for e in site if e["source"] in WANTED_SOURCES]
        cov = sum(1 for e in site if str(e["externalId"]) in mbe)
        print(f"Маржа сматчена: {cov}/{len(site)} сайтовых заказов")

    agg, articles = aggregate(orders, mbe)

    # мастер-список каталога больше НЕ тянем: категории берём из рекламируемых
    # product_type Google Ads (узел ads-google-cat, пишется Google-скриптом).

    # сводка в лог (проверка перед записью)
    print("\n=== СВОДКА ПО КАМПАНИЯМ ===")
    for src in sorted(agg):
        tot = {}
        for day, cats in agg[src].items():
            for ck, c in cats.items():
                t = tot.setdefault(ck, {"cat": c["cat"], "leads": 0, "approved": 0, "sum": 0.0, "upsSum": 0.0, "margin": 0.0})
                t["leads"] += c["leads"]; t["approved"] += c["approved"]
                t["sum"] += c["sum"]; t["upsSum"] += c["upsSum"]; t["margin"] += c.get("margin", 0)
        print(f"\n[{src}]")
        for ck, t in sorted(tot.items(), key=lambda kv: -kv[1]["leads"]):
            print(f"  {t['cat'][:42]:<42} заявок={t['leads']:>4} апрув={t['approved']:>4} "
                  f"сума={t['sum']:>9.0f} маржа={t['margin']:>8.0f} допрод={t['upsSum']:>7.0f}")

    if not APRUV_STATUS:
        print("\n(!) APRUV_STATUS не задан — 'апрув' везде 0. Добавь секрет APRUV_STATUS "
              "со списком id статусов-апрув через запятую.")
    # поордерная детализация (товары, дроп, маржа) — постоянная история;
    # ads собираем ИЗ ads-ord, чтобы учитывались и телефонные заказы из MyDrop.
    try:
        ordtree = build_ord(orders, mbe)
        # телефонные заказы с сайта из MyDrop (по примечанию) — в ту же ветку
        phone_tree = build_phone_ord(md)
        nph = 0
        for src, days in phone_tree.items():
            for day, ords in days.items():
                ordtree.setdefault(src, {}).setdefault(day, {}).update(ords)
                nph += len(ords)
        if nph:
            print(f"Телефонные с сайта (MyDrop, по примечанию): {nph}")
        push_ord_firebase(ordtree)
        reaggregate_ads_from_ord(ordtree)
    except Exception as e:
        print(f"ads-ord: {e}")
        push_ads_firebase(agg)   # запасной путь, если поордерная сборка упала


if __name__ == "__main__":
    main()
