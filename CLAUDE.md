# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Призначення

Парсер публічних каналів `web.max.ru` (росcійська платформа `MAX`). Збирає всі нові пости з ~3756 каналів у локальну SQLite, далі — Flask-дашборд із FTS5-пошуком і тематичною аналітикою (NER + TF-IDF). Тематика — **виключно військово-політична / російські канали**; NLP-пайплайн заточений під російську морфологію (Natasha).

## Запуск

```bash
pip install -r requirements.txt
playwright install chromium                  # тільки для розвідки і QR-rotation
playwright install-deps chromium             # Linux-VPS: системні бібліотеки

# Setup нового списку каналів:
python resolve_channels.py                   # alias → chatId; пише channels/resolved.json
```

Тестового харнеса нема (`pytest` не використовується, немає `tests/`). Перевірка функціональності — журнал systemd і ручний `curl http://127.0.0.1:8080/api/...`.

### Сервіси на VPS (всі через systemd)

| Unit | Що робить | Memory cap |
|---|---|---|
| `max-parser.service` | live WS-парсинг через токен A (`WS_PARSER_LABEL=W`, 8 воркерів по 500 каналів). Дефолтні `.login_token` + `.device_id`. | 600 МБ |
| `max-parser-b.service` | live WS-парсинг через токен B (`WS_PARSER_LABEL=B`, env `WS_PARSER_TOKEN_FILE=.login_token_b`). Незалежна push-черга. | 600 МБ |
| `max-dashboard.service` | Flask UI на `:8080`, NLP, FTS5-пошук, AI-аналітика через cloud routine. | 800 МБ |
| `backfill-priority.timer` | op=49 догон топ-300 main-flow через токен B, кожні 30 хв. | — |
| `backfill-missed.timer` | op=49 догон ВСІХ stale-каналів (без свіжих постів >10 хв) через токен B, кожні 15 хв. | — |
| `max-dashboard-restart.timer` | щонічний рестарт о 03:00 MSK для очищення `lemma-cache` від накопичення (800МБ cap → нові reach-задачі не стартують). | — |

```bash
# Скільки каналів зараз моніториться:
journalctl -u max-parser -n 20 | grep 'Каналів'

# Поновлення списку каналів (після правок channels/channels.txt):
systemctl stop max-parser max-parser-b
python resolve_channels.py          # може знадобитись 2-3 запуски через rate-limit MAX API
systemctl start max-parser max-parser-b
```

## Архітектура — великими мазками

Сім незалежних шарів, що спілкуються через `matches.db` + спільний `ws_common.py`:

0. **`ws_common.py`** — спільний WS handshake (`op=6` + `op=19 login`) + читання `.login_token` / `.device_id`. Параметризований під multi-account через `file_path` у `get_login_token()` / `get_device_id()`. Використовується усіма WS-споживачами (`ws_parser`, `resolve_channels`, `views_fetcher`, `backfill_*`) — змінювати handshake тут один раз, а не у п'яти місцях.

1. **`resolve_channels.py`** — одноразова setup-фаза. Бере `channels/channels.txt` (по одному alias на рядок), через WS-`op=89` отримує `chatId` і складає `channels/resolved.json`. Виконується раз; парсер це файл лише читає.

2. **`ws_parser.py`** — продакшн збирач. Один процес тримає N WebSocket-з'єднань (по 500 каналів на воркер, ~8 воркерів при 3756 каналах) до `wss://ws-api.oneme.ru/websocket`, виконує `op=75 subscribe` для кожного channelId і чекає push-повідомлення (`cmd=0` з сервера) → `INSERT INTO messages`. Без фільтрації по ключовим словам. Авторизація — LOGIN-токен (отримується через `ws_auth_scout.py` один раз або `pw_qr_*.py` для перевипуску — див. секцію [QR-rotation](#перевипуск-токена-qr-rotation)). **Запускається у двох systemd-інстанціях** (`max-parser` і `max-parser-b`), див. секцію [Multi-account](#multi-account--multi-token).

3. **`dashboard.py`** — Flask UI на `:8080`. Один process, threaded. Працює з тим самим SQLite-файлом read-only. Все важке (FTS5 пошук, NLP) кешується в пам'яті процесу. Auth — див. секцію [Auth дашборду](#auth-дашборду).

4. **`nlp.py`** — окремий модуль з ledge-load Natasha-пайплайна. Дашборд викликає його функції; парсер про NLP не знає.

5. **`views_fetcher.py`** — модуль для отримання кількості переглядів постів. Окрема WS-сесія, `op=49` (getMessages), групує пости по `chat_id`. Викликається з `dashboard.py` в рамках reach-задачі. **Не запускається паралельно з парсером** — MAX дозволяє лише одну активну сесію на токен.

6. **`backfill_priority.py`** + **`backfill_missed.py`** — поллінг через op=49 як компенсація WS-пропусків. Push (op=75 subscribe) має непрозорий drop rate і для частини каналів мовчки відхиляється MAX-сервером (підтверджено `probe_subscribe_limit.py`: 2/200 push events за 30 хв на топ-active missed). Backfill через op=49 догоняє всі прогалини, INSERT OR IGNORE через UNIQUE(chat_id, msg_id) робить запуски ідемпотентними. Два systemd timer'и: `backfill-priority.timer` (кожні 30 хв, топ-300 main-flow) і `backfill-missed.timer` (кожні 15 хв, ВСI канали без свіжих постів >10 хв). Обидва через токен B (env `BACKFILL_TOKEN_FILE=/root/.login_token_b`), стартують напряму без stop/start `max-parser-b` — `max-parser-b` тримає `op=75 subscribe`, що сумісно з op=49 на тій самій сесії токена.

## Multi-account / multi-token

Два MAX-акаунти підвищують live-покриття через незалежні push-черги (MAX дропає push на 16+ паралельних WS від одного IP — split по 8+8). До 14.05 покриття main-flow топ-10 за останню годину тримало ~84%; після multi-token + deep backfill — 99.1% за 24h, 92.5% за останню годину.

**Файли токенів і device_id** (всі у `/root/`, chmod 600):
- `.login_token` + `.device_id` — токен A (live через `max-parser.service`)
- `.login_token_b` + `.device_id_b` — токен B (live через `max-parser-b.service` + ВСІ backfill-таймери)

**env vars** (читає `ws_parser.py:main()` і `backfill_*.py:Client.__init__`):
- `WS_PARSER_LABEL` — префікс воркерів у логах (`W` дефолт для A; `B` у `max-parser-b.service`)
- `WS_PARSER_TOKEN_FILE` — кастомний шлях до токена (дефолт `/root/.login_token`)
- `WS_PARSER_DEVICE_FILE` — кастомний шлях до device_id (дефолт `/root/.device_id`)
- `BACKFILL_TOKEN_FILE` / `BACKFILL_DEVICE_FILE` — аналог для backfill-сервісів

**Розподіл ролей:**
- A — лише live push (через `max-parser.service`)
- B — live push (`max-parser-b.service`) + ВСІ op=49 backfill (бо одна сесія на токен; A зайнятий своїм 8-WS push'ем)

**При деградації одного токена** (наприклад `FAIL_LOGIN_TOKEN` через TTL) — другий тримає live; backfill догоняє через op=49 решту. До перевипуску система працює, лише з нижчим live-покриттям. Перевипуск — див. наступну секцію.

## Перевипуск токена (QR-rotation)

Симптом протухлого токена: у `journalctl -u max-parser` (або `-b`) кожні 20с `Login FAIL: FAIL_LOGIN_TOKEN`, всі воркери в reconnect-loop. Сервіс `active`, але нічого не парсить.

Workflow перевипуску (для токена A; для B — дзеркально через `pw_qr_b.py`):

```bash
# 1. Зупинити інстанцію з мертвим токеном (інакше після збереження нового
#    MAX може видати ще один токен паралельно):
systemctl stop max-parser

# 2. Бекап старого токена (на випадок rollback):
cp /root/.login_token /root/.login_token.bak.$(date +%Y%m%d)

# 3. Запустити QR-flow у фоні (відкриває headless Chromium, ловить opcode=291/115):
setsid nohup python3 /root/pw_qr_a.py < /dev/null > /tmp/pw_qr_a.log 2>&1 & disown

# 4. Скачати QR-screenshot локально і показати користувачу:
#    scp max-vps:/root/qr_screenshot_a.png ./
#    Користувач сканує з телефону через MAX → Налаштування → Сесії → Додати
#    Підтверджує вхід на телефоні (без цього op=291/115 не приходить).
#    Скрипт оновлює screenshot кожні 25с (REFRESH_EVERY) до 15 хв (TIMEOUT_SEC).
#    УВАГА: TTL свіжого QR на сторінці MAX ~30с, потім "QR-код устарел"
#    і потрібен restart pw_qr_*.py (скрипт сам не клікає "Получить новый QR-код").

# 5. Перевірити що токен зловлено:
tail -5 /tmp/pw_qr_a.log     # має бути [OK] token len=...  +  [SAVED] /root/.login_token

# 6. Старт парсера, перевірка логіну:
systemctl start max-parser
journalctl -u max-parser -n 30 | grep -E 'Login|підключ'   # має бути "Login OK" замість FAIL
```

Скрипти `pw_qr_a.py` / `pw_qr_b.py` — копії одна одної з підміненими `TOKEN_OUT` / `SCREEN_OUT`. Лежать на VPS, у репо не зберігаються.

## Auth дашборду

Сесійна auth через signed-cookie (Flask `SECRET_KEY` у `/root/.dashboard_secret`, lifetime 7 днів). Користувачі — у `/root/.dashboard_auth` (рядки `username:pbkdf2_hash`, chmod 600). Якщо файл порожній/відсутній — auth ВИМКНЕНИЙ (для локальної розробки).

```bash
# CLI на VPS — додавання / видалення користувача:
python3 /root/manage_auth.py add <username>      # двічі getpass, мін. 8 символів
python3 /root/manage_auth.py remove <username>
python3 /root/manage_auth.py list

# Кеш `_load_auth_users` TTL 60с — рестарт сервісу не потрібен.
```

Routes: `/login` (GET form + POST validate), `/logout`. Решта (`/api/*`, `/`) — гейт у `before_request`: API → 401 JSON, HTML → 302 на `/login?next=...` (захищено від open-redirect).

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

- **VPS:** alias `max-vps` (host `85.192.56.53`, user `root`). Уся ціль — `/root/` (плоско, не в підпапці).
- **Файли коду, що деплояться:** `ws_parser.py`, `ws_common.py`, `dashboard.py`, `nlp.py`, `views_fetcher.py`, `backfill_priority.py`, `backfill_missed.py`, `manage_auth.py`, `topical_roots.txt`, `summary.txt`, `custom_stop_words.txt` (опц.). На VPS додатково: `pw_qr_a.py` / `pw_qr_b.py` для перевипуску токенів (у репо не лежать).
- **Файли стану (тільки на VPS, у репо не лежать):** `.login_token`, `.login_token_b`, `.device_id`, `.device_id_b`, `.dashboard_auth`, `.dashboard_secret`, `matches.db` (+`-wal`/`-shm`).
- **Канал AI-аналітики:** окремий git-клон репо у `/root/max-parser-repo/` (через SSH deploy key `~/.ssh/github_deploy`). Дашборд commit'ить pending pack-файли, cloud routine на claude.ai обробляє і пише результати. Деталі — в секції "AI-аналітика через cloud routine" нижче.
- **Workflow:** код-перший. Усі правки — локально → `scp` на VPS → `systemctl restart`. Жодних ad-hoc правок безпосередньо на сервері.
- **Конфіг systemd:** у [systemd/](systemd/), інструкція [systemd/README.md](systemd/README.md). Memory caps: `max-parser`/`max-parser-b` — 600 МБ, `max-dashboard` — 800 МБ (Natasha моделі). Логи — systemd journal (`journalctl -u max-dashboard -n 50`).
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

Авторизаційний flow (op=288/289/291/115) — лише для одноразового отримання LOGIN-токена через QR. Два режими:
- **Перший запуск з телефонним номером** — [ws_auth_scout.py](ws_auth_scout.py) (`--phone --password`, op=19 login).
- **Перевипуск через QR** — `pw_qr_a.py` / `pw_qr_b.py` на VPS (headless Chromium → ловить opcode=291/115). Див. [Перевипуск токена](#перевипуск-токена-qr-rotation).

Парсер у продакшні не виконує QR — читає готовий `.login_token` через `ws_common.get_login_token()`.

**Розвідувальні скрипти** (reverse-engineering API, у продакшн не входять, лишилися на випадок зміни протоколу): `scout.py` (HTTP), `playwright_scout.py` (XHR/WS interception), `scout_views.py` (пошук view-полів через op-зонди).

**Діагностичні скрипти** (для розбору падінь покриття):
- [probe_subscribe_limit.py](probe_subscribe_limit.py) — підраховує реальний push-rate на 200 топ-каналів за 30 хв. Використати щоб довести: drop-rate `op=75 subscribe` на стороні MAX непрозорий і саме тому потрібен backfill.
- [check_dead_channels.py](check_dead_channels.py) — пробігає `channels/resolved.json` через op=49, виявляє канали з нульовою активністю (видалені, перейменовані, приватні).
- [audit_missing_roots.py](audit_missing_roots.py) — 5000 random постів за 7 днів через Natasha без `topical_roots.txt` фільтра → `audit_missing_roots_report.txt` (топ-300 лем поза коренями). Періодичний audit якості (детальніше в секції "Топ слів").
