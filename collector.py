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
    # "<домен black-street>": "Black-street",
    # "<домен bonna>":        "Bonna-shop",
}


def _dom(h):
    m = re.search(r"https?://([^/]+)", h or "")
    return m.group(1).lower().replace("www.", "") if m else ""


def shop_of(order):
    for p in order.get("products") or []:
        d = _dom(p.get("href"))
        if d:
            return SHOP_DOMAINS.get(d, "?:" + d)  # неизвестный домен покажем как ?:домен
    return "?"


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

    # --- разобранный вид: основное + допродажа отдельно, магазин по домену ---
    print("\n=== РАЗБОР ЗАКАЗА (первые 12) ===")
    for x in orders[:12]:
        e = extract(x)
        print(f"ext={e['externalId']!r} магазин={e['shop']} дата={e['date']} статус={e['statusId']} "
              f"sku={e['skus']} сумма_осн={e['mainSum']} допродажа={e['upsells']} сумма_доп={e['upsellSum']}")
    # какие домены встретились (чтобы дополнить карту магазинов)
    dc = Counter()
    for x in orders:
        for p in x.get("products") or []:
            d = _dom(p.get("href"))
            if d:
                dc[d] += 1
    print("\n=== ДОМЕНЫ САЙТОВ (какой = какой магазин) ===")
    for d, n in dc.most_common():
        print(f"  {d}: {n}  -> {SHOP_DOMAINS.get(d,'НЕ ЗНАЮ, скажи какой магазин')}")


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
