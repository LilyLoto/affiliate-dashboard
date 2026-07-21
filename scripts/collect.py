"""
Собирает статистику из Alanbase API + расходы из Google Sheets,
считает метрики по группам (partners/seo/inhouse/cc/inactive)
и сохраняет docs/data/latest.json + docs/data/history.json.

Запускается автоматически по расписанию через GitHub Actions
(.github/workflows/collect.yml), либо вручную:
    python scripts/collect.py
"""

import os
import json
import csv
import io
import datetime
import urllib.request
import urllib.parse

# ---------------------------------------------------------------
# НАСТРОЙКИ — правь здесь, если что-то поменяется на стороне Alanbase / таблицы
# ---------------------------------------------------------------

BASE_URL = "https://lofto.api.alanbase.com/v1"
API_KEY = os.environ["ALANBASE_API_KEY"]
SHEET_ID = os.environ["SHEET_ID"]

TIMEZONE = "Asia/Almaty"
CURRENCY = "USD"

# Какие теги Alanbase относятся к какой группе дашборда.
# Если появится новый тег, которого нет в этом словаре — он попадёт
# в лог как "неизвестный тег", просто допиши его сюда.
TAG_TO_GROUP = {
    "рся": "partners",
    "fb": "partners",
    "сео": "seo",
    "inhouse": "inhouse",
    "cc": "cc",
    "неактивный": "inactive",
}
GROUPS = ["partners", "seo", "inhouse", "cc", "inactive"]

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "docs", "data")
HISTORY_MAX_POINTS = 60


# ---------------------------------------------------------------
# ALANBASE API
# ---------------------------------------------------------------

def api_get(path, params):
    """GET-запрос к Alanbase со всеми страницами результата."""
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


def fetch_goal_by_tag(goal_key, date_from, date_to):
    """Возвращает {tag: {"count": int, "value": float}} для одной цели (goal)."""
    rows = api_get("/admin/statistic/common", {
        "group_by": "tags",
        "timezone": TIMEZONE,
        "date_from": date_from,
        "date_to": date_to,
        "currency_code": CURRENCY,
        "goal_keys[]": [goal_key],
    })
    result = {}
    for row in rows:
        fields = row.get("group_fields", [])
        tag = fields[0]["label"] if fields else "unknown"
        total = row.get("conversions", {}).get("total", {})
        result[tag] = {
            "count": total.get("count", 0),
            "value": total.get("value", 0) or total.get("revenue", 0) or 0,
        }
    return result


# ---------------------------------------------------------------
# GOOGLE SHEETS (расходы) — читаем публичный CSV-экспорт, без OAuth
# ---------------------------------------------------------------

def read_spend_by_tag():
    """
    Пытается найти в таблице колонку с тегом и колонку с расходом
    и вернуть {tag: spend}. Если структура таблицы не совпала —
    возвращает {} и печатает предупреждение, остальной сбор не ломается.
    """
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[warn] не смог скачать Google Sheet: {e}")
        return {}

    reader = list(csv.reader(io.StringIO(raw)))
    if not reader:
        return {}

    header = [h.strip().lower() for h in reader[0]]

    def find_col(*candidates):
        for i, h in enumerate(header):
            if any(c in h for c in candidates):
                return i
        return None

    tag_col = find_col("тег", "tag")
    spend_col = find_col("spend", "расход", "затрат")

    if tag_col is None or spend_col is None:
        print("[warn] не нашёл колонки тег/расход в таблице — проверь заголовки")
        return {}

    spend_by_tag = {}
    for row in reader[1:]:
        if len(row) <= max(tag_col, spend_col):
            continue
        tag = row[tag_col].strip().lower()
        raw_val = row[spend_col].replace("$", "").replace(",", "").strip()
        if not tag or not raw_val:
            continue
        try:
            spend_by_tag[tag] = spend_by_tag.get(tag, 0) + float(raw_val)
        except ValueError:
            continue
    return spend_by_tag


# ---------------------------------------------------------------
# СБОРКА СНИМКА ЗА ПЕРИОД
# ---------------------------------------------------------------

def build_group_aggregates(date_from, date_to):
    reg_by_tag = fetch_goal_by_tag("registration", date_from, date_to)
    ftd_by_tag = fetch_goal_by_tag("ftd", date_from, date_to)
    ggr_by_tag = fetch_goal_by_tag("ggr", date_from, date_to)
    spend_by_tag = read_spend_by_tag()

    all_tags = set(reg_by_tag) | set(ftd_by_tag) | set(ggr_by_tag)
    for tag in all_tags:
        if tag.lower() not in TAG_TO_GROUP and tag not in TAG_TO_GROUP:
            print(f"[info] неизвестный тег из Alanbase: '{tag}' — допиши в TAG_TO_GROUP, если нужно")

    aggregates = {g: {"registrations": 0, "ftd": 0, "ggr": 0.0, "spend": 0.0} for g in GROUPS}

    for tag in all_tags:
        group = TAG_TO_GROUP.get(tag) or TAG_TO_GROUP.get(tag.lower())
        if not group:
            continue
        aggregates[group]["registrations"] += reg_by_tag.get(tag, {}).get("count", 0)
        aggregates[group]["ftd"] += ftd_by_tag.get(tag, {}).get("count", 0)
        aggregates[group]["ggr"] += ggr_by_tag.get(tag, {}).get("value", 0)
        aggregates[group]["spend"] += spend_by_tag.get(tag.lower(), 0)

    # NGR — точную формулу считает Alanbase во вкладке "Формулы".
    # Пока берём приблизительно 60% от GGR — поправь коэффициент,
    # когда сверишь с реальным отчётом.
    for g in aggregates:
        aggregates[g]["ngr"] = round(aggregates[g]["ggr"] * 0.6, 2)
        aggregates[g]["ggr"] = round(aggregates[g]["ggr"], 2)
        aggregates[g]["spend"] = round(aggregates[g]["spend"], 2)

    return aggregates


def week_bounds(weeks_ago=0):
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday() + 7 * weeks_ago)
    end = monday + datetime.timedelta(days=6) if weeks_ago > 0 else today
    return (
        monday.strftime("%Y-%m-%d 00:00"),
        end.strftime("%Y-%m-%d 23:59"),
    )


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    cur_from, cur_to = week_bounds(0)
    prev_from, prev_to = week_bounds(1)

    print(f"Текущая неделя: {cur_from} .. {cur_to}")
    print(f"Прошлая неделя: {prev_from} .. {prev_to}")

    current = build_group_aggregates(cur_from, cur_to)
    previous = build_group_aggregates(prev_from, prev_to)

    latest = {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "period": {"from": cur_from, "to": cur_to},
        "previous_period": {"from": prev_from, "to": prev_to},
        "groups": {
            g: {"current": current[g], "previous": previous[g]} for g in GROUPS
        },
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
            "registrations": current[g]["registrations"],
            "ftd": current[g]["ftd"],
            "ggr": current[g]["ggr"],
        })
        history[g] = points[-HISTORY_MAX_POINTS:]

    with open(history_path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    print("Записан docs/data/history.json")


if __name__ == "__main__":
    main()
