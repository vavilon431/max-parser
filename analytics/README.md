### analytics/

Канал обміну між дашбордом (на VPS) і cloud routine (на claude.ai).

- `pending/<hash>.md` — дашборд кладе сюди пакет (метадані + system prompt із `../summary.txt` + дамп постів). Hash детермінований від `(q, channel, since_ts, until_ts)`.
- `results/<hash>.md` — cloud routine кладе сюди markdown-відповідь. Дашборд polling'ить цей каталог.

Workflow:
1. Натискання "🧠 Аналітика" → `dashboard.py` формує `pending/<hash>.md` → commit+push.
2. Тригер `max-parser-analytics` routine на claude.ai → клонує репо, обробляє pending, кладе `results/<hash>.md` → commit+push.
3. `dashboard.py` git pull кожні 5с при polling → знаходить `results/<hash>.md` → рендерить.
