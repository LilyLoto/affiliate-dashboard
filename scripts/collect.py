"""
Собирает статистику из Alanbase API (по partner_id и offer_id)
+ читает три вкладки Google Sheets:
  - "Партнёры"            — какой partner_id в какой группе (обычные)
  - "Партнёры_по_офферам" — для "составных" партнёров, у которых разные
                            офферы относятся к разным группам
  - "Расходы"              — Spend по неделям, по partner_id (и offer_id
                             для составных партнёров)

Сохраняет docs/data/latest.json + docs/data/history.json.
Запускается через .github/workflows/collect.yml по расписанию,
либо вручную: python scripts/collect.py
"""

import os
import json
import csv
import io
import re
import datetime
import urllib.request
import urllib.parse

# ---------------------------------------------------------------
# НАСТРОЙКИ
# ---------------------------------------------------------------

BASE_URL = "https://lofto.api.alanbase.com/v1"
API_KEY = os.environ["ALANBASE_API_KEY"]
SHEET_ID = os.environ["SHEET_ID"]

TIMEZONE = "Asia/Almaty"
CURRENCY = "USD"

# gid каждой вкладки — если пересоздашь вкладку, gid поменяется,
# тогда поправь тут
GID_PARTNERS = "172795040"
GID_SPEND = "1069978200"
GID_PARTNER_OFFERS = "89777085"

GROUPS = ["partners", "seo", "inhouse", "cc", "inactive"]

# нормализация написания групп: разные варианты записи -> канонический ключ
GROUP_ALIASES = {
    "partners": "partners", "партнеры": "partners", "партнёры": "partners",
    "seo": "seo", "сео": "seo",
    "inhouse": "inhouse", "инхаус": "inhouse",
    "cc": "cc", "ccбаннеры": "cc", "баннеры": "cc", "банеры": "cc", "ccbanners": "cc",
    "inactive": "inactive", "неактив": "inactive", "неактивный": "inactive", "неактивные": "inactive",
}

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "docs", "data")
HISTORY_MAX_POINTS = 60


def normalize_group(raw):
    if not raw:
        return None
    key = raw.strip().lower().replace("ё", "е").replace("/", "").replace(" ", "")
    return GROUP_ALIASES.get(key)


# ---------------------------------------------------------------
# ЧТЕНИЕ ТАБЛИЦЫ (три вкладки, каждая по своему gid)
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
    """Вкладка 'Партнёры': {partner_id: group}. Пропускает нераспознанные группы."""
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
    """Вкладка 'Партнёры_по_офферам': {(partner_id, offer_id): group} + set составных partner_id."""
    mapping = {}
    override_partner_ids = set()
    for row in read_sheet_tab(GID_PARTNER_OFFERS):
        pid = row.get("partner_id", "")
        oid = row.get("offer_id", "")
        if not pid or not oid:
            continue
        group = normalize_group(row.get("группа", ""))
        if group is None:
            print(f"[warn] partner_id {pid} offer_id {oid}: неизвестная группа '{row.get('группа')}' — пропущен")
            continue
        mapping[(pid, oid)] = group
        override_partner_ids.add(pid)
    return mapping, override_partner_ids


def load_spend_rows():
    """Вкладка 'Расходы' — список строк с датами периода, partner_id, offer_id (может быть пустым), spend."""
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
# ALANBASE API
# ---------------------------------------------------------------

def api_get(path, params):
    all_rows = []
    page = 1
    while True:
        q = dict(params)
        q["page"] = page
        q["per_page"] = 100
        url = f"{BASE_URL}{path}?" + urllib.parse.urlencode(q, doseq=True)
        req = urllib.request.Request(url, headers={
            "API-KEY": API_KEY,
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        rows = body.get("data", [])
        all_rows.extend(rows)
        meta = body.get("meta", {})
        last_page = meta.get("last_page", 1) or 1
        if page >= last_page or not rows:
            break
        page += 1
    return all_rows


def fetch_by_partner(goal_key, date_from, date_to):
    """group_by=partner — для обычных партнёров. Возвращает {partner_id: {count, value}}."""
    rows = api_get("/admin/statistic/common", {
        "group_by": "partner",
        "timezone": TIMEZONE,
        "date_from": date_from,
        "date_to": date_to,
        "currency_code": CURRENCY,
        "goal_keys[]": [goal_key],
    })
    result = {}
    for row in rows:
        fields = row.get("group_fields", [])
        pid = str(fields[0]["id"]) if fields else "unknown"
        total = row.get("conversions", {}).get("total", {})
        result[pid] = {
            "count": total.get("count", 0),
            "value": total.get("value", 0) or total.get("revenue", 0) or 0,
        }
    return result


def fetch_by_offer_for_partner(goal_key, partner_id, date_from, date_to):
    """group_by=offer, отфильтровано по одному partner_id — для составных партнёров."""
    rows = api_get("/admin/statistic/common", {
        "group_by": "offer",
        "timezone": TIMEZONE,
        "date_from": date_from,
        "date_to": date_to,
        "currency_code": CURRENCY,
        "goal_keys[]": [goal_key],
        "partner_ids[]": [partner_id],
    })
    result = {}
    for row in rows:
        fields = row.get("group_fields", [])
        oid = str(fields[0]["id"]) if fields else "unknown"
        total = row.get("conversions", {}).get("total", {})
        result[oid] = {
            "count": total.get("count", 0),
            "value": total.get("value", 0) or total.get("revenue", 0) or 0,
        }
    return result


# ---------------------------------------------------------------
# СБОРКА МЕТРИК ЗА ПЕРИОД
# ---------------------------------------------------------------

def build_metric_aggregates(date_from, date_to, partner_groups, overrides, override_partner_ids):
    aggregates = {g: {"registrations": 0, "ftd": 0, "ggr": 0.0} for g in GROUPS}

    # обычные партнёры — одним запросом на всех
    for goal, field in [("registration", "registrations"), ("ftd", "ftd"), ("ggr", "ggr")]:
        by_partner = fetch_by_partner(goal, date_from, date_to)
        for pid, data in by_partner.items():
            if pid in override_partner_ids:
                continue  # у составных партнёров считаем по офферам ниже
            group = partner_groups.get(pid)
            if not group:
                continue  # partner_id не найден в справочнике "Партнёры" — пропускаем
            value = data["value"] if field == "ggr" else data["count"]
            aggregates[group][field] += value

    # составные партнёры — по офферам, отдельным запросом на каждого
    for pid in override_partner_ids:
        for goal, field in [("registration", "registrations"), ("ftd", "ftd"), ("ggr", "ggr")]:
            by_offer = fetch_by_offer_for_partner(goal, pid, date_from, date_to)
            for oid, data in by_offer.items():
                group = overrides.get((pid, oid))
                if not group:
                    print(f"[info] partner_id {pid} offer_id {oid}: нет в 'Партнёры_по_офферам' — пропущен")
                    continue
                value = data["value"] if field == "ggr" else data["count"]
                aggregates[group][field] += value

    for g in aggregates:
        aggregates[g]["ggr"] = round(aggregates[g]["ggr"], 2)
    return aggregates


def spend_for_period(spend_rows, date_from, date_to, partner_groups, overrides):
    """Ищет в Расходы строки, чей период ТОЧНО совпадает с запрошенной неделей."""
    want_from = date_from.split(" ")[0]
    want_to = date_to.split(" ")[0]
    aggregates = {g: 0.0 for g in GROUPS}
    matched_any = False
    for row in spend_rows:
        if row["period_from"] != want_from or row["period_to"] != want_to:
            continue
        matched_any = True
        pid, oid = row["partner_id"], row["offer_id"]
        if oid:
            group = overrides.get((pid, oid))
        else:
            group = partner_groups.get(pid)
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
    print(f"Загружено: {len(partner_groups)} партнёров, {len(overrides)} офферов-исключений, {len(spend_rows)} строк расходов")

    current_metrics = build_metric_aggregates(cur_from, cur_to, partner_groups, overrides, override_partner_ids)
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
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
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
