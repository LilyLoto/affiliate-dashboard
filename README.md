# Affiliate Dashboard — автосбор данных

- `scripts/collect.py` — тянет статистику из Alanbase API + расходы из Google Sheets, считает метрики по группам (partners/seo/inhouse/cc/inactive), сохраняет `docs/data/latest.json` и `docs/data/history.json`
- `.github/workflows/collect.yml` — запускает collect.py каждый день автоматически (GitHub Actions) и коммитит новые данные обратно в репозиторий
- `docs/index.html` — сам дашборд, читает `docs/data/*.json` и рисует KPI, график, таблицу по группам

## Настройка тегов

Если в Alanbase появится новый тег, которого нет в дашборде — допиши его в `scripts/collect.py`, в словарь `TAG_TO_GROUP`.

## NGR

Формула NGR сейчас — приближение (60% от GGR), в `scripts/collect.py`. Уточни коэффициент/формулу по вкладке "Формулы" в Alanbase и поправь строку с `* 0.6`.
