# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Призначення

Парсер публічних каналів `web.max.ru` (росcійська платформа `MAX`). Збирає всі нові пости з ~3756 каналів у локальну SQLite, далі — Flask-дашборд із FTS5-пошуком і тематичною аналітикою (NER + TF-IDF). Тематика — **виключно військово-політична / російські канали**; NLP-пайплайн заточений під російську морфологію (Natasha).

## Запуск

```bash
pip install -r requirements.txt
playwright install chromium                  # тільки для розвідки
playwright install-deps chromium             # Linux-VPS: системні бібліотеки

# Продакшн (VPS, через systemd):
python ws_parser.py                          # WS polling — пише у matches.db
python dashboard.py                          # Flask UI на :8080

# Setup нового списку каналів:
python resolve_channels.py                   # alias → chatId; пише channels/resolved.json
```

Тестового харнеса нема (`pytest` не використовується, немає `tests/`). Перевірка функціональності — журнал systemd і ручний `curl http://127.0.0.1:8080/api/...`.

```bash
# Скільки каналів зараз моніториться:
journalctl -u max-parser -n 20 | grep 'Каналів'

# Поновлення списку каналів (після правок channels/channels.txt):
systemctl stop max-parser
python resolve_channels.py          # може знадобитись 2-3 запуски через rate-limit MAX API
systemctl start max-parser
```

## Архітектура — великими мазками

Чотири незалежні шари, що спілкуються через `matches.db`:

1. **`resolve_channels.py`** — одноразова setup-фаза. Бере `channels/channels.txt` (по одному alias на рядок), через WS-`op=89` отримує `chatId` і складає `channels/resolved.json`. Виконується раз; парсер це файл лише читає.

2. **`ws_parser.py`** — продакшн збирач. Один процес тримає N WebSocket-з'єднань (по 500 каналів на воркер, ~8 воркерів при 3756 каналах) до `wss://ws-api.oneme.ru/websocket`, виконує `op=75 subscribe` для кожного channelId і чекає push-повідомлення (`cmd=0` з сервера) → `INSERT INTO messages`. Без фільтрації по ключовим словам. Авторизація — LOGIN-токен у `.login_token` (отримується через `ws_auth_scout.py` один раз).

3. **`dashboard.py`** — Flask UI на `:8080`. Один process, threaded. Працює з тим самим SQLite-файлом read-only. Все важке (FTS5 пошук, NLP) кешується в пам'яті процесу.

4. **`nlp.py`** — окремий модуль з ledge-load Natasha-пайплайна. Дашборд викликає його функції; парсер про NLP не знає.

5. **`views_fetcher.py`** — модуль для отримання кількості переглядів постів. Окрема WS-сесія, `op=49` (getMessages), групує пости по `chat_id`. Викликається з `dashboard.py` в рамках reach-задачі. **Не запускається паралельно з парсером** — MAX дозволяє лише одну активну сесію на токен.

6. **`backfill_priority.py`** + **`backfill_missed.py`** — поллінг через op=49 як компенсація WS-пропусків. Push (op=75 subscribe) має непрозорий drop rate і для частини каналів мовчки відхиляється MAX-сервером (підтверджено `probe_subscribe_limit.py`: 2/200 push events за 30 хв на топ-active missed). Backfill через op=49 догоняє всі прогалини, INSERT OR IGNORE через UNIQUE(chat_id, msg_id) робить запуски ідемпотентними. Два systemd timer'и: `backfill-priority.timer` (кожні 30 хв, топ-300 main-flow) і `backfill-missed.timer` (кожні 15 хв, ВСI канали без свіжих постів >10 хв). Обидва через токен B, з stop/start `max-parser-b` навколо запуску (одна сесія на токен).

## Топ слів — як це працює (ядро поточного UX)

NLP-пайплайн категоризує всі значущі токени на 4 групи:

- **NER через `NewsNERTagger`** → `per` / `loc` / `org` (нормалізація через `span.normalize(morph_vocab)`, тому Кремле→Кремль).
- **Решта токенів** проходять жорсткий фільтр:
  - POS ∈ {NOUN, PROPN, ADJ} — **`VERB` повністю викинуто** (свідомо: дієслова забивали топ "прислать/сообщать/пояснять").
  - лема має починатись з одного з ~250 коренів у [topical_roots.txt](topical_roots.txt) (війна, ракета, дрон, санкції, нато, президент, himars і т.д.). Інакше — відсіюється ще до TF-IDF.
- На кожній категорії окремо рахується `(1 + log tf) × log((N+1)/(df+1))` проти baseline. Baseline зберігається в таблиці `baseline_lemma_freq` з ключем `"category::lemma"` — старий формат без `::` авто-визнається застарілим у `nlp.load_baseline()`.

Якщо в топ просочується щось побутове (автомобіль, сніг тощо) — або додавати в `_RU_EXTRA` всередині `dashboard.py`, або через ✕-кнопку UI (пише в `custom_stop_words.txt`). Якщо натомість бракує тематичного терміну — додати корінь у [topical_roots.txt](topical_roots.txt).

**Періодичний audit якості:** `python3 audit_missing_roots.py` бере 5000 random постів за 7 днів, прогоняє Natasha БЕЗ topical-фільтра і виводить `audit_missing_roots_report.txt` — топ-300 лем поза `topical_roots.txt` що були б у топі. Дивишся:
- **Реально тематичні** → додавай корінь у `topical_roots.txt`
- **Шум** (місяці, побутове) → додавай у `_RU_EXTRA` в [dashboard.py](dashboard.py)

Після додавання стоп-слів — обов'язково очищай кеш від уже накопиченого: `DELETE FROM message_lemmas WHERE lemma IN (...)` через `sqlite3` з `busy_timeout=120000` (dashboard має бути зупинений або працювати — Python з timeout сам почекає WAL). Після додавання нових коренів повний rebuild не потрібний — нові пости підхопляться автоматично через `lemma_cache_scheduler`.

### Чому `get_top_words` неблокуюча

NER+syntax-пайплайн ~30-60 мс/пост. На 3000 постів (`MAX_ROWS_SCAN`) — ~30-60 с. Тому:

- API завжди повертає миттєво — або кеш, або порожній dict + старт фонового thread (`_start_top_words_compute`).
- `_top_words_inflight` — set періодів, що зараз обчислюються, щоб не паралелити важкий рахунок на той самий період.
- JS у шаблоні авто-перезапитує `/api/top-words` через 15 с якщо отримав порожні списки.
- При старті — `warm_top_words_cache()` послідовно прогріває `day → week → month` (один за одним, не паралельно).

`MAX_ROWS_SCAN = 3000` — навмисний компроміс між повнотою і часом прорахунку. Підвищувати без переходу на швидший токенайзер не рекомендую.

## SQLite-схема (важливе)

- `messages` — основна таблиця, пише `ws_parser`, читає `dashboard`.
- `messages_fts` — FTS5 virtual table з тригерами на INSERT/UPDATE/DELETE (ініціалізується в `dashboard.init_fts()`). Tokenizer `unicode61 remove_diacritics 2` — case-insensitive для кирилиці. Probe-перевірка цілісності при старті, `rebuild` якщо менше половини покрито.
- `baseline_lemma_freq` + `baseline_meta` — TF-IDF baseline; перебудовується раз на 24 год фоновим thread `baseline_scheduler()`.

## Деплой і експлуатація

- **VPS:** alias `max-vps` (host `85.192.56.53`, user `root`). Уся ціль — `/root/` (плоско, не в підпапці): `ws_parser.py`, `dashboard.py`, `nlp.py`, `topical_roots.txt`, `matches.db`. Конфіг systemd — у `systemd/`, інструкція там же ([systemd/README.md](systemd/README.md)).
- **Канал AI-аналітики:** окремий git-клон репо у `/root/max-parser-repo/` (через SSH deploy key `~/.ssh/github_deploy`). Дашборд commit'ить pending pack-файли, cloud routine на claude.ai обробляє і пише результати. Деталі — в секції "AI-аналітика через cloud routine" нижче.
- **Workflow:** код-перший. Усі правки — локально → `scp` на VPS → `systemctl restart`. Жодних ad-hoc правок безпосередньо на сервері.
- **Сервіси:** `max-parser.service` (memory cap 600 МБ), `max-dashboard.service` (cap 800 МБ — Natasha моделі). Логи — systemd journal (`journalctl -u max-dashboard -n 50`).
- **Деплой dashboard:** після `scp` обов'язково `systemctl restart max-dashboard`. Перший запит після рестарту — кеш холодний, ~30-60 с на baseline + перший період.
- **Геообмеження:** `st.max.ru` (JS/CDN) геоблокований за межами RU/CIS. VPS у KZ — основне середовище. Локальна розробка дашборду через VPN/проксі або просто проти копії `matches.db`.

## Функція "Охоплення" (reach)

Дашборд показує суму переглядів по днях (`/api/timeline-reach`). Архітектура — асинхронна: POST стартує фоновий task, GET по `task_id` опитує прогрес.

**Критичне обмеження:** глобальний семафор `_reach_running["busy"]` — одночасно виконується лише один reach-task, бо `views_fetcher` відкриває окрему MAX WS-сесію з тим самим токеном, і паралельні сесії ламають авторизацію. Якщо `busy=True` зависло довше ~5 хв (WS обрив без `finally`) — `systemctl restart max-dashboard`.

## AI-аналітика через cloud routine

Кнопка "🧠 Аналітика" на дашборді — pipeline через GitHub + cloud routine на claude.ai. Дашборд **сам не викликає** Claude API; натомість commit'ить pack у репо, cloud routine його обробляє.

**Flow:**
1. Користувач натискає кнопку → JS POST'ить `/api/analytics/start` (`q` + `period=24h|7d`).
2. Дашборд формує `<hash>.md` (метадані + system prompt із [summary.txt](summary.txt) + дамп постів), кладе у `/root/max-parser-repo/analytics/pending/`, `git commit + push origin main`.
3. JS polling `/api/analytics/result/<hash>` кожні 10с (timeout 65 хв).
4. Cloud routine `trig_01BuHdcdyvecVXpjMnbmDesM` (claude.ai, cron `7 * * * *` UTC) клонує репо, обробляє pending, commit'ить `results/<hash>.md`, push, видаляє pending.
5. Дашборд `git pull` під час polling → бачить result → рендерить через `_md_to_html`.

**Hash** детермінований від `(q, channel, since_ts, until_ts)` — повторний клік із тими самими фільтрами одразу віддає кешований результат із репо.

**Тригер routine:**
- **GitHub Action `.github/workflows/fire-routine.yml`** — основний шлях. Спрацьовує на push до `analytics/pending/**`, викликає `POST /v1/claude_code/routines/<id>/fire` через `secrets.ROUTINE_FIRE_TOKEN`. Action в Microsoft infra, тому НЕ геоблокований (на відміну від VPS у KZ — `api.anthropic.com` оттуди 403).
- **Cron `7 * * * *` UTC** — safety net якщо Action провалився.
- **Manual через UI** — JS показує посилання на `https://claude.ai/code/routines/<id>` у pending-screen для fallback (Run now на тій сторінці).

**Авторизація для push із routine:** Anthropic GitHub integration (Connectors → GitHub Integration на claude.ai) має тільки **read** доступ — push з routine 403. Тому в `prompt` routine вшито fine-grained PAT (`Contents: Read+Write` на `vavilon431/max-parser`), яким routine робить `git remote set-url origin "https://x-access-token:PAT@github.com/..."` перед push. Token зберігається в Anthropic logах routine — обмежений scope мінімізує ризик.

**Дані обміну:** `/root/max-parser-repo/` — окремий git-клон (через SSH deploy key). Не плутати з runtime-каталогом `/root/` (там `dashboard.py`, `matches.db`). Дашборд читає з / пише в `analytics/pending/` і `analytics/results/` через `subprocess` + `git`.

**Що НЕ потрібне:** `.anthropic_key`, `.anthropic_gateway`, `.anthropic_gateway_token`, пакет `anthropic`. Видалено разом зі старим API-flow. Якщо файли ще є локально — можна видалити, не використовуються.

**Ліміти:** 600k символів пакету (~150k токенів), пости > 1500 символів обрізаються. Періоди — тільки `24h` або `7d`.

## resolve_channels.py — нюанси

MAX API обмежує кількість `op=89`-запитів на сесію. Під час великого resolve (1000+ каналів) WS-з'єднання може впасти посередині — `знайдено` перестає зростати, `не знайдено` швидко біжить до кінця. Симптом: у логах `знайдено` стабілізується на певному значенні і вже не росте.

**Рішення:** запускати `resolve_channels.py` повторно — він пропускає вже резолвлені (`if a not in resolved`) і добирає решту. Зазвичай 2-3 запуски закривають усе охоплення.

`channels/failed.txt` після кожного запуску — лише ті, що дійсно не знайшлись. Для аналізу причин — `analyze_failed.py` (перевіряє кожен через API і категоризує), `fix_report.py` (постобробка: розбиває hash-invite від звичайних username).

## Розвідка / WebSocket protocol (довідково)

Базова структура повідомлення:
```json
{"ver": 11, "cmd": 0, "seq": N, "opcode": X, "payload": {...}}
```
`cmd=0` — запит клієнта; `cmd=1` — відповідь сервера на seq; `cmd=0` від сервера — push.

Ключові opcodes (повний список — у git-історії старого CLAUDE.md, тут — лише ті, що використовує продакшн):

| op | напр | призначення |
|----|------|-------------|
| 6  | →←   | handshake; `userAgent` має бути об'єктом, не рядком |
| 19 | →    | login за токеном `{"token": LOGIN_TOKEN}` |
| 49 | →←   | get channel messages: `{chatId, from: ts_ms, backward: 30, getMessages: true}` |
| 75 | →    | subscribe to channel updates: `{chatId, subscribe: true}` |
| 89 | →←   | resolve channel by link: `{"link": "https://max.ru/ALIAS"}` → `chat.chatId` |

Авторизаційний flow (op=288/289/291/115) — лише для одноразового отримання LOGIN-токена через QR; реалізація в [ws_auth_scout.py](ws_auth_scout.py). Парсер у продакшні не виконує QR — читає готовий `.login_token`.

Розвідувальні скрипти — `scout.py` (HTTP), `playwright_scout.py` (XHR/WS interception) — використовувались на етапі reverse-engineering API, у продакшн-pipeline не входять. Лишилися для повторної розвідки якщо протокол зміниться.
