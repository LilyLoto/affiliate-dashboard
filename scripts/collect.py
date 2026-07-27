"""
Собирает статистику из Alanbase API + Google Sheets, считает метрики
по группам (partners/seo/inhouse/cc/inactive) — как по сумме группы,
так и в разбивке по каждому партнёру (и по офферам для составных
партнёров) — и сохраняет docs/data/latest.json + docs/data/history.json.

ВАЖНО про Alanbase API:
  /admin/statistic/common      — готовая сводка, но НЕ умеет фильтровать
                                  по цели (goal_keys игнорируется).
  /admin/statistic/conversions — отдаёт список конверсий поштучно, умеет
                                  фильтровать по goal_keys[], но агрегацию
                                  (по партнёру/офферу) нужно считать самим.
  Поэтому здесь используется только /conversions, а суммы считаются
  в Python из списка конверсий.

  Поле "value" в конверсиях НЕ конвертируется по currency_code — оно
  приходит в исходной валюте продукта (для этого аккаунта — KZT),
  конвертация в USD сделана вручную ниже (см. CURRENCY_RATES_TO_USD).

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
    """Вкладка 'Партнёры': {partner_id: group}, {partner_id: partner_name}."""
    group_map = {}
    name_map = {}
    for row in read_sheet_tab(GID_PARTNERS):
        pid = row.get("partner_id", "")
        if not pid:
            continue
        if row.get("partner_name"):
            name_map[pid] = row["partner_name"]
        group = normalize_group(row.get("группа", ""))
        if group is None:
            print(f"[warn] partner_id {pid}: неизвестная группа '{row.get('группа')}' — пропущен")
            continue
        group_map[pid] = group
    return group_map, name_map


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
    raw_samples = []
    for row in read_sheet_tab(GID_SPEND):
        pid = row.get("partner_id", "")
        if not pid:
            continue
        raw_from = row.get("период_с", "")
        raw_to = row.get("период_по", "")
        period_from = normalize_date(raw_from)
        period_to = normalize_date(raw_to)
        if len(raw_samples) < 3:
            raw_samples.append((raw_from, period_from, raw_to, period_to))
        if not period_from or not period_to:
            print(f"[warn] Расходы: у partner_id {pid} (offer {row.get('offer_id','—')}) не распознал дату периода "
                  f"('{raw_from}' / '{raw_to}') — строка пропущена")
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
            "partner_name": row.get("partner_name", ""),
            "spend": spend,
        })
    if raw_samples:
        print(f"[debug] Пример разбора дат из 'Расходы' (сырое -> распознанное): {raw_samples}")
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
                backoff = 3 * (2 ** (attempt - 1))
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
    Тянет ВСЕ конверсии по одной цели за период (с пагинацией).
    Возвращает список словарей {partner_id, partner_name, offer_id, offer_name, value}.
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
                "partner_name": partner.get("full_name") or partner.get("email") or "",
                "offer_id": str(offer.get("id", "")),
                "offer_name": offer.get("name") or "",
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
# СБОРКА МЕТРИК ЗА ПЕРИОД (по группам и в разбивке по партнёрам/офферам)
# ---------------------------------------------------------------

def route_to_group(pid, oid, partner_groups, overrides, override_partner_ids):
    if pid in override_partner_ids:
        return overrides.get((pid, oid), partner_groups.get(pid))
    return partner_groups.get(pid)


def row_key(pid, oid, override_partner_ids):
    """Составные партнёры -> отдельная строка на каждый оффер. Обычные -> одна строка на партнёра."""
    if pid in override_partner_ids:
        return ("offer", pid, oid)
    return ("partner", pid)


def build_metric_aggregates(date_from, date_to, partner_groups, overrides, override_partner_ids):
    aggregates = {g: {"registrations": 0, "ftd": 0, "ggr": 0.0} for g in GROUPS}
    rows = {g: {} for g in GROUPS}
    unknown_partners = set()

    for goal, field in [("registration", "registrations"), ("ftd", "ftd"), ("ggr", "ggr")]:
        conversions = fetch_conversions(goal, date_from, date_to)
        print(f"  {goal}: получено {len(conversions)} конверсий")
        for c in conversions:
            pid, oid = c["partner_id"], c["offer_id"]
            group = route_to_group(pid, oid, partner_groups, overrides, override_partner_ids)
            if not group:
                unknown_partners.add(pid)
                continue

            if field == "ggr":
                aggregates[group]["ggr"] += c["value"]
            else:
                aggregates[group][field] += 1

            key = row_key(pid, oid, override_partner_ids)
            row = rows[group].setdefault(key, {
                "partner_id": pid, "partner_name": c["partner_name"],
                "offer_id": oid if key[0] == "offer" else None,
                "offer_name": c["offer_name"] if key[0] == "offer" else None,
                "registrations": 0, "ftd": 0, "ggr": 0.0,
            })
            if field == "ggr":
                row["ggr"] += c["value"]
            else:
                row[field] += 1

    if unknown_partners:
        print(f"[info] партнёры без группы (нет в 'Партнёры'): {sorted(unknown_partners)}")

    for g in aggregates:
        aggregates[g]["ggr"] = round(aggregates[g]["ggr"], 2)
        for row in rows[g].values():
            row["ggr"] = round(row["ggr"], 2)

    return aggregates, rows


def spend_for_period(spend_rows, date_from, date_to, partner_groups, overrides, override_partner_ids):
    want_from = date_from.split(" ")[0]
    want_to = date_to.split(" ")[0]
    group_totals = {g: 0.0 for g in GROUPS}
    row_totals = {g: {} for g in GROUPS}
    matched_any = False

    for r in spend_rows:
        if r["period_from"] != want_from or r["period_to"] != want_to:
            continue
        matched_any = True
        pid, oid = r["partner_id"], r["offer_id"]
        default_group = partner_groups.get(pid)
        group = overrides.get((pid, oid), default_group) if oid else default_group
        if not group:
            continue
        group_totals[group] += r["spend"]
        key = row_key(pid, oid, override_partner_ids)
        row_totals[group][key] = row_totals[group].get(key, 0.0) + r["spend"]

    if not matched_any and spend_rows:
        available_periods = sorted(set(f"{r['period_from']}..{r['period_to']}" for r in spend_rows))
        print(f"[debug] Spend: искали период {want_from}..{want_to}, в таблице есть периоды: {available_periods}")

    group_totals = {g: round(v, 2) for g, v in group_totals.items()}
    for g in row_totals:
        row_totals[g] = {k: round(v, 2) for k, v in row_totals[g].items()}
    return group_totals, row_totals, matched_any


def week_bounds(weeks_ago=0):
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday() + 7 * weeks_ago)
    end = monday + datetime.timedelta(days=6) if weeks_ago > 0 else today
    return monday.strftime("%Y-%m-%d 00:00"), end.strftime("%Y-%m-%d 23:59"), monday.isoformat()


_DATE_FORMATS = ["%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y"]


def normalize_date(raw):
    """
    Приводит дату из таблицы к единому виду YYYY-MM-DD, независимо от того,
    как Google Sheets её экспортировал (текст, локальный формат, и т.д.).
    Возвращает None, если распознать не удалось.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def build_rows_list(metric_rows, spend_rows, spend_matched, partner_names, group):
    """Сливает метрики и Spend в единый список строк для дашборда, с производными метриками."""
    all_keys = set(metric_rows.get(group, {}).keys()) | set(spend_rows.get(group, {}).keys())
    result = []
    for key in all_keys:
        m = metric_rows.get(group, {}).get(key, {})
        spend = spend_rows.get(group, {}).get(key)
        if spend is None:
            spend = 0.0 if spend_matched else None

        if key[0] == "partner":
            pid = key[1]
            oid = None
            oname = None
        else:
            pid = key[1]
            oid = key[2]
            oname = m.get("offer_name")

        name = m.get("partner_name") or partner_names.get(pid, "") or f"partner {pid}"
        reg = m.get("registrations", 0)
        ftd = m.get("ftd", 0)
        ggr = m.get("ggr", 0.0)
        ngr = round(ggr * 0.6, 2)
        cr_ftd = round(ftd / reg * 100, 2) if reg else 0.0
        cpa_ftd = round(spend / ftd, 2) if (spend is not None and ftd) else None

        result.append({
            "partner_id": pid,
            "partner_name": name,
            "offer_id": oid,
            "offer_name": oname,
            "registrations": reg,
            "ftd": ftd,
            "ggr": ggr,
            "ngr": ngr,
            "spend": spend,
            "cr_ftd": cr_ftd,
            "cpa_ftd": cpa_ftd,
        })

    result.sort(key=lambda r: r["registrations"], reverse=True)
    return result


WEEKS_TO_KEEP = 12  # цель хранения — но набирается постепенно, см. ниже
INITIAL_BURST_WEEKS = 4  # эти 4 недели (текущая + 3 прошлые) можно посчитать сразу одним прогоном
MAX_NEW_HISTORICAL_WEEKS_PER_RUN = 1  # а вот сверх этих 4 — добираем не больше 1 НОВОЙ недели за прогон,
                                       # чтобы не повторить 11-часовой прогон при попытке достать все 12 разом


def load_json_file(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json_file(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def collect_period_snapshot(date_from, date_to, partner_groups, overrides, override_partner_ids, spend_rows, partner_names):
    """Считает полный снимок (метрики + строки по партнёрам) для одного произвольного периода."""
    metrics, rows_by_group = build_metric_aggregates(date_from, date_to, partner_groups, overrides, override_partner_ids)
    spend_totals, spend_rows_by_group, spend_matched = spend_for_period(
        spend_rows, date_from, date_to, partner_groups, overrides, override_partner_ids)

    snapshot = {}
    for g in GROUPS:
        cur = dict(metrics[g])
        cur["spend"] = spend_totals.get(g, 0.0) if spend_matched else None
        cur["ngr"] = round(cur["ggr"] * 0.6, 2)  # приближение, см. README
        cur["rows"] = build_rows_list(rows_by_group, spend_rows_by_group, spend_matched, partner_names, g)
        snapshot[g] = cur
    snapshot["_period"] = {"from": date_from, "to": date_to}
    snapshot["_spend_matched"] = spend_matched
    return snapshot


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    partner_groups, partner_names = load_partner_groups()
    overrides, override_partner_ids = load_partner_offer_overrides()
    spend_rows = load_spend_rows()
    print(f"Загружено: {len(partner_groups)} партнёров, {len(overrides)} офферов-исключений, "
          f"{len(override_partner_ids)} составных партнёров, {len(spend_rows)} строк расходов")

    weeks_path = os.path.join(DATA_DIR, "weeks.json")
    weeks_cache = load_json_file(weeks_path)

    # ---- НЕДЕЛИ: текущая всегда пересчитывается; из прошлых — досчитываем не больше
    #      MAX_NEW_HISTORICAL_WEEKS_PER_RUN новых за один прогон, чтобы не пытаться
    #      добрать все 12 недель разом (именно это привело к 11-часовому прогону раньше).
    #      Кэш заполняется постепенно, по чуть-чуть, пока не наберёт полное окно.
    week_keys_wanted = []
    new_historical_computed = 0
    for weeks_ago in range(WEEKS_TO_KEEP):
        date_from, date_to, week_id = week_bounds(weeks_ago)
        week_keys_wanted.append(week_id)

        if weeks_ago == 0:
            print(f"Считаем неделю {week_id} (текущая)...")
            weeks_cache[week_id] = collect_period_snapshot(
                date_from, date_to, partner_groups, overrides, override_partner_ids, spend_rows, partner_names)
        elif week_id not in weeks_cache:
            within_burst = weeks_ago < INITIAL_BURST_WEEKS
            if within_burst or new_historical_computed < MAX_NEW_HISTORICAL_WEEKS_PER_RUN:
                reason = "первичный набор недель" if within_burst else f"пополнение истории, {new_historical_computed + 1}/{MAX_NEW_HISTORICAL_WEEKS_PER_RUN} за этот прогон"
                print(f"Считаем неделю {week_id} ({reason})...")
                weeks_cache[week_id] = collect_period_snapshot(
                    date_from, date_to, partner_groups, overrides, override_partner_ids, spend_rows, partner_names)
                if not within_burst:
                    new_historical_computed += 1
            else:
                print(f"Неделя {week_id} ещё не в кэше, но лимит новых недель за прогон исчерпан — добьём в следующий раз")
        else:
            print(f"Неделя {week_id} уже в кэше — пропускаем повторный запрос к Alanbase")

    for key in list(weeks_cache.keys()):
        if key not in week_keys_wanted:
            del weeks_cache[key]

    save_json_file(weeks_path, weeks_cache)
    print(f"Записан docs/data/weeks.json ({len(weeks_cache)} из {WEEKS_TO_KEEP} недель в кэше)")

    # ---- history.json для графика — берём из уже посчитанной текущей недели, без доп. запросов ----
    current_week_id = week_keys_wanted[0]
    current_week_snapshot = weeks_cache[current_week_id]

    history_path = os.path.join(DATA_DIR, "history.json")
    history = load_json_file(history_path)
    today_str = datetime.date.today().isoformat()
    for g in GROUPS:
        history.setdefault(g, [])
        points = [p for p in history[g] if p["date"] != today_str]
        points.append({
            "date": today_str,
            "registrations": current_week_snapshot[g]["registrations"],
            "ftd": current_week_snapshot[g]["ftd"],
            "ggr": current_week_snapshot[g]["ggr"],
            "spend": current_week_snapshot[g]["spend"] if current_week_snapshot[g]["spend"] is not None else 0.0,
        })
        history[g] = points[-HISTORY_MAX_POINTS:]
    save_json_file(history_path, history)
    print("Записан docs/data/history.json")

    # ---- latest.json — только метаданные: что доступно и когда обновлялось ----
    latest = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "available_weeks": sorted(week_keys_wanted, reverse=True),
        "spend_stale_warnings": {},
    }
    save_json_file(os.path.join(DATA_DIR, "latest.json"), latest)
    print("Записан docs/data/latest.json")


if __name__ == "__main__":
    main()
