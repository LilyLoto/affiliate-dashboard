"""
Собирает статистику из Alanbase API + Google Sheets, считает метрики
по группам (partners/seo/inhouse/cc/inactive) и сохраняет
docs/data/latest.json + docs/data/history.json.

ВАЖНО про Alanbase API:
  /admin/statistic/common      — готовая сводка, но НЕ умеет фильтровать
                                  по цели (goal_keys игнорируется).
  /admin/statistic/conversions — отдаёт список конверсий поштучно, умеет
                                  фильтровать по goal_keys[], но агрегацию
                                  (по партнёру/офферу) нужно считать самим.
  Поэтому здесь используется только /conversions, а суммы по группам
  считаются в Python из списка конверсий.

  Лимиты Alanbase: макс. 1000 записей на страницу, макс. 30 запросов/мин.

Три вкладки Google Sheets:
  - "Партнёры"            — partner_id -> группа (это и дефолт для составных)
  - "Партнёры_по_офферам" — ИСКЛЮЧЕНИЯ: (partner_id, offer_id) -> другая группа
  - "Расходы"              — Spend по неделям, partner_id (+ offer_id для
                              офферов-исключений), период указывается датами

Запускается через .github/workflows/collect.yml по расписанию,
либо вручную: python scripts/collect.py
"""

import os
import json
import csv
import io
import time
import datetime
import urllib.request
import urllib.parse
import urllib.error

# ---------------------------------------------------------------
# НАСТРОЙКИ
# ---------------------------------------------------------------

BASE_URL = "https://lofto.api.alanbase.com/v1"
API_KEY = os.environ["ALANBASE_API_KEY"]
SHEET_ID = os.environ["SHEET_ID"]

TIMEZONE = "Europe/London"
CURRENCY = "USD"

GID_PARTNERS = "172795040"
GID_SPEND = "1069978200"
GID_PARTNER_OFFERS = "89777085"

GROUPS = ["partners", "seo", "inhouse", "cc", "inactive"]

GROUP_ALIASES = {
    "partners": "partners", "партнеры": "partners", "партнёры": "partners",
    "seo": "seo", "сео": "seo",
    "inhouse": "inhouse", "инхаус": "inhouse",
    "cc": "cc", "ccбаннеры": "cc", "баннеры": "cc", "банеры": "cc", "ccbanners": "cc",
    "inactive": "inactive", "неактив": "inactive", "неактивный": "inactive", "неактивные": "inactive",
}

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "docs", "data")
HISTORY_MAX_POINTS = 60

PER_PAGE = 1000
SLEEP_BETWEEN_REQUESTS = 2.2  # секунды; держит нас в пределах 30 запросов/мин с запасом

# Alanbase НЕ конвертирует поле "value" в конверсиях по currency_code —
# оно всегда приходит в исходной валюте продукта (для этого аккаунта — KZT).
# Курс статичный, обновляй вручную по мере изменения реального курса.
# Текущий: 500 KZT = 1.07 USD (задано пользователем 23.07.2026)
CURRENCY_RATES_TO_USD = {
    "USD": 1.0,
    "KZT": 1.07 / 500,
}
_unknown_currencies_warned = set()


def to_usd(value, currency):
    rate = CURRENCY_RATES_TO_USD.get(currency)
    if rate is None:
        if currency not in _unknown_currencies_warned:
            print(f"[warn] неизвестная валюта '{currency}' — считаю как USD 1:1, добавь курс в CURRENCY_RATES_TO_USD")
            _unknown_currencies_warned.add(currency)
        rate = 1.0
    return value * rate


def normalize_group(raw):
    if not raw:
        return None
    key = raw.strip().lower().replace("ё", "е").replace("/", "").replace(" ", "")
    return GROUP_ALIASES.get(key)


# ---------------------------------------------------------------
# ЧТЕНИЕ ТАБЛИЦЫ
# ---------------------------------------------------------------

def read_sheet_tab(gid):
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    rows = list(csv.reader(io.StringIO(raw)))
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]
    result = []
    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        entry = {}
        for i, col in enumerate(header):
            entry[col] = row[i].strip() if i < len(row) else ""
        result.append(entry)
    return result


def load_partner_groups():
    mapping = {}
    for row in read_sheet_tab(GID_PARTNERS):
        pid = row.get("partner_id", "")
        if not pid:
            continue
        group = normalize_group(row.get("группа", ""))
        if group is None:
            print(f"[warn] partner_id {pid}: неизвестная группа '{row.get('группа')}' — пропущен")
            continue
        mapping[pid] = group
    return mapping


def load_partner_offer_overrides():
    mapping = {}
    override_partner_ids = set()
    for row in read_sheet_tab(GID_PARTNER_OFFERS):
        pid = row.get("partner_id", "")
        oid = row.get("offer_id", "")
        if not pid or not oid:
            continue
        override_partner_ids.add(pid)
        group = normalize_group(row.get("группа", ""))
        if group is None:
            print(f"[warn] partner_id {pid} offer_id {oid}: неизвестная группа '{row.get('группа')}' — пропущен из исключений")
            continue
        mapping[(pid, oid)] = group
    return mapping, override_partner_ids


def load_spend_rows():
    rows = []
    for row in read_sheet_tab(GID_SPEND):
        pid = row.get("partner_id", "")
        if not pid:
            continue
        period_from = row.get("период_с", "")
        period_to = row.get("период_по", "")
        if not period_from or not period_to:
            print(f"[warn] Расходы: у partner_id {pid} (offer {row.get('offer_id','—')}) нет дат периода — строка пропущена")
            continue
        raw_spend = (row.get("spend", "") or "0").replace("$", "").replace(",", "").strip()
        try:
            spend = float(raw_spend) if raw_spend else 0.0
        except ValueError:
            spend = 0.0
        rows.append({
            "period_from": period_from,
            "period_to": period_to,
            "partner_id": pid,
            "offer_id": row.get("offer_id", "").strip(),
            "spend": spend,
        })
    return rows


# ---------------------------------------------------------------
# ALANBASE API — /admin/statistic/conversions
# ---------------------------------------------------------------

MAX_RETRIES = 5
RETRYABLE_HTTP_CODES = {500, 502, 503, 504}


def _request_page(goal_key, date_from, date_to, page):
    """Один запрос одной страницы, с повтором при временных ошибках сервера (5xx)."""
    params = {
        "timezone": TIMEZONE,
        "date_from": date_from,
        "date_to": date_to,
        "currency_code": CURRENCY,
        "goal_keys[]": [goal_key],
        "per_page": PER_PAGE,
        "page": page,
    }
    url = f"{BASE_URL}/admin/statistic/conversions?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={
        "API-KEY": API_KEY,
        "Content-Type": "application/json",
    })

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        time.sleep(SLEEP_BETWEEN_REQUESTS)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code in RETRYABLE_HTTP_CODES and attempt < MAX_RETRIES:
                backoff = 3 * (2 ** (attempt - 1))  # 3s, 6s, 12s, 24s...
                print(f"  [warn] {goal_key} стр.{page}: HTTP {e.code}, повтор через {backoff}с (попытка {attempt}/{MAX_RETRIES})")
                time.sleep(backoff)
                continue
            raise
        except urllib.error.URLError as e:
            last_error = e
            if attempt < MAX_RETRIES:
                backoff = 3 * (2 ** (attempt - 1))
                print(f"  [warn] {goal_key} стр.{page}: {e}, повтор через {backoff}с (попытка {attempt}/{MAX_RETRIES})")
                time.sleep(backoff)
                continue
            raise
    raise last_error


def fetch_conversions(goal_key, date_from, date_to):
    """
    Тянет ВСЕ конверсии по одной цели за период (с пагинацией),
    возвращает список словарей {partner_id, offer_id, value}.
    Автоматически повторяет запрос при временных сбоях сервера (502/503/504).
    """
    results = []
    page = 1
    while True:
        body = _request_page(goal_key, date_from, date_to, page)

        rows = body.get("data", [])
        for row in rows:
            partner = row.get("partner") or {}
            offer = row.get("offer") or {}
            raw_value = row.get("value") or row.get("revenue") or 0
            currency = row.get("value_currency") or "USD"
            value_usd = to_usd(raw_value, currency)
            results.append({
                "partner_id": str(partner.get("id", "")),
                "offer_id": str(offer.get("id", "")),
                "value": value_usd,
            })

        meta = body.get("meta", {})
        last_page = meta.get("last_page", 1) or 1
        if page == 1 or page % 10 == 0 or page >= last_page:
            print(f"  {goal_key}: страница {page}/{last_page}, собрано {len(results)}")
        if page >= last_page or not rows:
            break
        page += 1

    return results


# ---------------------------------------------------------------
# СБОРКА МЕТРИК ЗА ПЕРИОД
# ---------------------------------------------------------------

def route_to_group(pid, oid, partner_groups, overrides, override_partner_ids):
    if pid in override_partner_ids:
        return overrides.get((pid, oid), partner_groups.get(pid))
    return partner_groups.get(pid)


def build_metric_aggregates(date_from, date_to, partner_groups, overrides, override_partner_ids):
    aggregates = {g: {"registrations": 0, "ftd": 0, "ggr": 0.0} for g in GROUPS}
    unknown_partners = set()

    for goal, field in [("registration", "registrations"), ("ftd", "ftd"), ("ggr", "ggr")]:
        conversions = fetch_conversions(goal, date_from, date_to)
        print(f"  {goal}: получено {len(conversions)} конверсий")
        for row in conversions:
            group = route_to_group(row["partner_id"], row["offer_id"], partner_groups, overrides, override_partner_ids)
            if not group:
                unknown_partners.add(row["partner_id"])
                continue
            if field == "ggr":
                aggregates[group]["ggr"] += row["value"]
            else:
                aggregates[group][field] += 1

    if unknown_partners:
        print(f"[info] партнёры без группы (нет в 'Партнёры'): {sorted(unknown_partners)}")

    for g in aggregates:
        aggregates[g]["ggr"] = round(aggregates[g]["ggr"], 2)
    return aggregates


def spend_for_period(spend_rows, date_from, date_to, partner_groups, overrides):
    want_from = date_from.split(" ")[0]
    want_to = date_to.split(" ")[0]
    aggregates = {g: 0.0 for g in GROUPS}
    matched_any = False
    for row in spend_rows:
        if row["period_from"] != want_from or row["period_to"] != want_to:
            continue
        matched_any = True
        pid, oid = row["partner_id"], row["offer_id"]
        default_group = partner_groups.get(pid)
        group = overrides.get((pid, oid), default_group) if oid else default_group
        if not group:
            continue
        aggregates[group] += row["spend"]
    return {g: round(v, 2) for g, v in aggregates.items()}, matched_any


def week_bounds(weeks_ago=0):
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday() + 7 * weeks_ago)
    end = monday + datetime.timedelta(days=6) if weeks_ago > 0 else today
    return monday.strftime("%Y-%m-%d 00:00"), end.strftime("%Y-%m-%d 23:59")


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    cur_from, cur_to = week_bounds(0)
    prev_from, prev_to = week_bounds(1)
    print(f"Текущая неделя: {cur_from} .. {cur_to}")
    print(f"Прошлая неделя: {prev_from} .. {prev_to}")

    partner_groups = load_partner_groups()
    overrides, override_partner_ids = load_partner_offer_overrides()
    spend_rows = load_spend_rows()
    print(f"Загружено: {len(partner_groups)} партнёров, {len(overrides)} офферов-исключений, "
          f"{len(override_partner_ids)} составных партнёров, {len(spend_rows)} строк расходов")

    print("Тянем конверсии за текущую неделю...")
    current_metrics = build_metric_aggregates(cur_from, cur_to, partner_groups, overrides, override_partner_ids)
    print("Тянем конверсии за прошлую неделю...")
    previous_metrics = build_metric_aggregates(prev_from, prev_to, partner_groups, overrides, override_partner_ids)

    current_spend, cur_spend_matched = spend_for_period(spend_rows, cur_from, cur_to, partner_groups, overrides)
    previous_spend, prev_spend_matched = spend_for_period(spend_rows, prev_from, prev_to, partner_groups, overrides)

    if not cur_spend_matched:
        print(f"[warn] в 'Расходы' нет строк с периодом {cur_from.split(' ')[0]}..{cur_to.split(' ')[0]} — Spend за эту неделю будет 0")

    latest_groups = {}
    for g in GROUPS:
        cur = dict(current_metrics[g])
        cur["spend"] = current_spend.get(g, 0.0)
        cur["ngr"] = round(cur["ggr"] * 0.6, 2)  # приближение, см. README

        prev = dict(previous_metrics[g])
        prev["spend"] = previous_spend.get(g, 0.0) if prev_spend_matched else None
        prev["ngr"] = round(prev["ggr"] * 0.6, 2)

        latest_groups[g] = {"current": cur, "previous": prev}

    latest = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "period": {"from": cur_from, "to": cur_to},
        "previous_period": {"from": prev_from, "to": prev_to},
        "groups": latest_groups,
        "spend_stale_warnings": {},
    }

    with open(os.path.join(DATA_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=2)
    print("Записан docs/data/latest.json")

    history_path = os.path.join(DATA_DIR, "history.json")
    history = {}
    if os.path.exists(history_path):
        with open(history_path, "r", encoding="utf-8") as f:
            history = json.load(f)

    today_str = datetime.date.today().isoformat()
    for g in GROUPS:
        history.setdefault(g, [])
        points = [p for p in history[g] if p["date"] != today_str]
        points.append({
            "date": today_str,
            "registrations": current_metrics[g]["registrations"],
            "ftd": current_metrics[g]["ftd"],
            "ggr": current_metrics[g]["ggr"],
            "spend": current_spend.get(g, 0.0),
        })
        history[g] = points[-HISTORY_MAX_POINTS:]

    with open(history_path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    print("Записан docs/data/history.json")


if __name__ == "__main__":
    main()
