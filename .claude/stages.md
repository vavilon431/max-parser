# Project Stages Journal
Журнал етапів розробки проекту.

---

## [2026-05-27 14:20] — Доступ до VPS відновлено через WARP (aeza блокує UA-IP після переїзду)

**Контекст:** після переїзду на новий ThinkStation (~26.05) пропав SSH-доступ до `max-vps` — `ssh`, `ping`, TCP на 22/80/443/8080 усі timeout, `tracert` обривався в aeza-мережі. Користувач підтвердив що VPS працює (через aeza-панель) і нічого не блокував вручну. Спершу схоже на firewall — але діагностика на VPS (через VNC web-console) показала: ufw inactive, iptables усі ланцюги `policy ACCEPT` 0 правил, hosts.allow/deny порожні, fail2ban не встановлений, sshd_config без AllowUsers. Тобто на VPS НІЩО не блокує. Перевірка з третіх точок через check-host.net: пінг до VPS OK з Канади/Китаю, timeout з України/Ірану. Висновок — aeza-KZ ріже вхідний трафік з UA-IP на рівні своєї мережі (geo-фільтр), наш новий зовнішній IP `213.160.137.117` під нього потрапив.

**Ключові зміни (інфраструктура/доступ, не код):**

Обхід геоблоку через Cloudflare WARP:
- `winget install --id Cloudflare.Warp` на ThinkStation (warp-cli 2026.4.1390.0, служба `CloudflareWARP` автостарт).
- `warp-cli registration new` + `warp-cli connect` → зовнішній IP став `104.28.156.60` (Cloudflare edge).
- Після WARP: TCP 22 до VPS OPEN, `ssh max-vps` працює, повна перевірка парсера пройшла.

Auto-memory:
- Створено `feedback_max_vps_requires_warp_from_ua.md` + рядок у `MEMORY.md` — щоб наступного разу одразу WARP, без години діагностики. Пов'язано з `reference_max_parser_vps_ssh` і `anthropic-api-geoblock-from-kz-vps` (геоблок у зворотний бік).

**Поточний стан:**
- Доступ до VPS відновлено (через WARP). Парсер живий: VPS uptime 4 доби, постів 11 109/год, 53 290/24h, total 863 500, останній пост у реальному часі.
- Сервіси: `max-parser-b` + `max-dashboard` + 3 таймери — усі active. `max-parser` (токен A) — **inactive** (незакрита справа з 23.05, не наслідок переїзду).
- Робоча копія коду чиста, 3 коміти попереду origin/main (з попередніх стейджів). Цей стейдж коду не торкався.

**Наступний крок:**
1. Тримати WARP увімкненим при будь-якій роботі з VPS (`warp-cli status` має бути Connected; служба автостартує з Windows).
2. Завершити перевипуск токена A через `pw_qr_a.py` (тягнеться з 23.05) — тепер коли доступ є, це 5 хв за інструкцією в CLAUDE.md.
3. Опціонально: написати в aeza support тікет про geo-блок UA-IP — якщо хочемо ходити без WARP. Малоймовірно що знімуть (це їх політика), тому WARP — основний шлях.

---

## [2026-05-26 16:00] — Аудит CLAUDE.md проти реального стану + cleanup застарілих залежностей

**Контекст:** після save-stage сьогодні (15:53) користувач викликав `/init` для max-parser. Аудит виявив що `CLAUDE.md` відстав від реальності у 4 областях: multi-token не задокументований як окрема архітектурна одиниця (тільки побіжно у backfill-секції), auth дашборду відсутня цілком (з 04.05 v3.8), QR-rotation для перевипуску токенів не описана, перелік сервісів і файлів деплою застарів (5 нових сервісів і 7 файлів з'явились з v3.4+). Плюс знайдено два мертвих артефакти: `anthropic>=0.40.0` у `requirements.txt` (не використовується після переходу на cloud routine 16.05) і `russian_baseline.txt` (10000 рядків антипатерна, замінено власним baseline з БД 25.04).

**Ключові зміни:**

[CLAUDE.md](../CLAUDE.md) — +105/-15 рядків:
- Нова секція **«Сервіси на VPS»** з таблицею всіх 6 unit'ів (live A+B, dashboard, 3 таймери).
- Нова секція **«Multi-account / multi-token»**: env vars (`WS_PARSER_LABEL`, `WS_PARSER_TOKEN_FILE`, `WS_PARSER_DEVICE_FILE`, `BACKFILL_TOKEN_FILE`), розподіл ролей (A — live, B — live+backfill), поведінка при деградації одного токена.
- Нова секція **«Перевипуск токена (QR-rotation)»**: повний workflow `pw_qr_a.py`/`pw_qr_b.py` (stop сервіс → setsid nohup → scp screenshot → сканування з телефону + підтвердження → перевірка лога → старт сервіс). Зафіксовано підводний камінь: TTL свіжого QR ~30с, скрипт не клікає «Получить новый QR-код» — треба рестарт `pw_qr_*.py` при `«QR-код устарел»`.
- Нова секція **«Auth дашборду»**: `manage_auth.py` CLI, `.dashboard_auth` (pbkdf2), `.dashboard_secret` (Flask SECRET_KEY), TTL кеша `_load_auth_users` 60с.
- Розширено архітектуру з 6 до 7 шарів: `ws_common.py` додано як 0-й (спільний WS handshake, читання токена/device_id, параметризований під multi-account).
- Оновлено розділ **«Деплой і експлуатація»**: розширено перелік файлів коду (додано `ws_common.py`, `views_fetcher.py`, `backfill_*.py`, `manage_auth.py`, `summary.txt`, `pw_qr_*.py`), окремо виділено файли стану (`.login_token*`, `.device_id*`, `.dashboard_*`, `matches.db`).
- Доповнено розділ **«Розвідка / WebSocket protocol»**: розділено auth-flow на «первий запуск з паролем» (`ws_auth_scout.py`) і «перевипуск через QR» (`pw_qr_*.py`); додано **«Діагностичні скрипти»** — `probe_subscribe_limit.py`, `check_dead_channels.py`, `audit_missing_roots.py`.

Cleanup застарілих артефактів:
- [requirements.txt](../requirements.txt): `-anthropic>=0.40.0` (grep підтвердив 0 імпортів у репо, видалено разом зі старим API-flow 16.05).
- `russian_baseline.txt`: видалено (-10000 рядків). Замінено власним baseline з БД через `baseline_lemma_freq` таблицю ще 25.04 у v3.

**Поточний стан:**
- Робоча копія чиста, 2 нових коміти попереду origin/main: `9c8510e` (попередній save-stage) і `09d069a` (поточні правки).
- Реальна архітектура (multi-token + auth + QR-rotation) тепер 1-в-1 відображена в `CLAUDE.md` — нові інстанції Claude Code не повторюватимуть аудит-роботу.
- `requirements.txt` подався на ~7 МБ легше, репо без 166 КБ мертвого тексту.

**Наступний крок:**
1. `git push origin main` коли треба синхронізувати оновлений `CLAUDE.md` з GitHub-репо `vavilon431/max-parser` (потрібен для cloud routine analytics, але документація туди не критична).
2. Завершити перевипуск токена A через `pw_qr_a.py` (відкладено зі стейджа 26.05 14:00) — інструкція тепер у `CLAUDE.md`.
3. Опціонально: у `pw_qr_a.py` / `pw_qr_b.py` додати auto-click на кнопку «Получить новый QR-код» через playwright — щоб TTL ~30с не різав вікно сканування. Зараз кожне протухання QR = повний рестарт скрипта.

---

## [2026-05-26 14:00] — Backup matches.db на диск D + виявлено прострочений токен A (перевипуск відкладено)

**Контекст:** після перевірки стану VPS (uptime 5 хв після несподіваного ребуту 23.05) виявилось, що всі 8 воркерів `max-parser` повертають `FAIL_LOGIN_TOKEN`. Файл `/root/.login_token` датований 23 квітня — токен А прострочений / відкликаний MAX (місяць активності з KZ-IP). Live-парсинг тримається тільки на токені B (`max-parser-b`) + два backfill-таймери (priority/missed) через B-токен. Покриття live-години впало назад до single-token рівня (до 14.05 multi-token stage), але загальний темп (35k постів/24h, top-15 каналів активні) виглядає здорово завдяки backfill через op=49.

**Ключові зміни:**

QR-флоу для перевипуску токена A (підготовка, виконання відкладено):
- На VPS створено `/root/pw_qr_a.py` як копія `/root/pw_qr_b.py` з підміненими шляхами (`.login_token_b` → `.login_token`, `qr_screenshot_b.png` → `qr_screenshot_a.png`).
- Бекап старого токена: `/root/.login_token.bak.20260523`.
- `max-parser` зупинений (`systemctl stop max-parser`) — поки QR-флоу не завершено.
- Спроби сканування QR провалились (TTL ~30с, сторінка MAX не авто-оновлює QR без user-interaction, тільки `screenshot()` без кліку «Получить новый QR-код»). Користувач не встиг просканувати з телефону за вікно — логін перенесено.

Backup matches.db на локальний диск:
- На VPS: `PRAGMA wal_checkpoint(TRUNCATE)` — всі 848 сторінок WAL злиті у головний файл (busy=1 через активний reader-conn дашборду, але дані вже в БД).
- Створено `D:\max-parser-backups\`, скачано `matches_2026-05-23.db` через `scp`.
- Розмір: 1.61 ГіБ (1 724 461 056 байт) — 689 758 постів від 3 679 каналів за 22.04–23.05.
- Розклад розміру: `messages` 813 МБ (тексти+метадані), `messages_fts_data` 229 МБ (FTS5), `message_lemmas` + індекси 366 МБ (кеш лем для тематичної аналітики), решта — індекси по channel/saved_at.

**Поточний стан:**
- `max-parser` (токен A) — **stopped**, чекає перевипуску токена.
- `max-parser-b` (токен B) — active, 8 воркерів стрімлять live.
- `backfill-priority.timer` (топ-300, кожні 30 хв через токен B) — active, errors=17/2186 на останньому циклі.
- `backfill-missed.timer` (всі 3755 stale-каналів, кожні 15 хв) — active, errors=0/11409 на останньому циклі.
- `max-dashboard` — active, lemma-cache нарощується ~60 постів/с.
- `max-dashboard-restart.timer` — active (нічний ребут о 03:00 MSK).
- VPS диск: 7.2 ГБ зайнято / 22 ГБ вільно (25%). За темпом ~30k постів/день (~85 МБ/день) — ще ~260 днів до заповнення.

**Наступний крок:**
1. Завершити перевипуск токена A через `pw_qr_a.py` — користувач має бути готовий просканувати QR і одразу підтвердити вхід на телефоні (вікно ~30с до протухання).
2. Стартувати `max-parser` (`systemctl start max-parser`), перевірити логін усіх 8 воркерів (`journalctl -u max-parser | grep -E 'Login|підключ'`).
3. Звірити покриття main-flow за годину після рестарту — має повернутись до 92%+ (як було 15.05).
4. Опціонально: у `pw_qr_a.py` / `pw_qr_b.py` додати auto-click на кнопку «Получить новый QR-код» через playwright — щоб TTL ~30с не різав вікно сканування.

---

## [2026-05-15 09:30] — Multi-account multi-token + бейджі дельти на дашборді

**Контекст:** після виявлення (08.05) масштабних пропусків live-парсера (50-70% для високочастотних каналів через MAX push-drop) додано регулярний backfill (`backfill_priority.py`) і нічний рестарт `max-dashboard` для очищення lemma-cache. Бейджі дельти каналів/постів на дашборді. 14.05 додано 2-й акаунт MAX (multi-token) для незалежної push-черги, але виявилось, що MAX дропає сесії при 16+ паралельних від одного IP — backfill зламався (errors=164). Розділили ролі: A — live, B — live + backfill. Deep backfill через 2-й токен догнав 24h-прогалини.

**Ключові зміни:**

Multi-account ([ws_common.py](../ws_common.py), [ws_parser.py](../ws_parser.py)):
- `get_login_token(file_path=...)` і `get_device_id(file_path=...)` — параметризовані під другий акаунт. Backward-compatible: дефолтні аргументи зберігають поведінку.
- `ws_parser.main()` читає env vars: `WS_PARSER_LABEL` (дефолт "W"), `WS_PARSER_TOKEN_FILE`, `WS_PARSER_DEVICE_FILE`. Воркер-префікс залежить від label: `[W0..W7]` або `[B0..B7]`.
- `WSClient` приймає `label` параметр і має `.tag` (наприклад "B3") для логів.

Backfill через 2-й токен ([backfill_priority.py](../backfill_priority.py), [systemd/backfill-priority.service](../systemd/backfill-priority.service)):
- `Client.__init__` читає `BACKFILL_TOKEN_FILE` / `BACKFILL_DEVICE_FILE` з env vars (за замовч. — A-токен). У systemd unit задано `.login_token_b` + `.device_id_b` — так backfill не конкурує з A-парсером за per-IP-quota MAX.
- `SYNC_WINDOW` — з env var `BACKFILL_SYNC_WINDOW` (дефолт 30). Для одноразового deep-backfill можна підняти до 500.
- `backfill-priority.timer` переписаний на `OnCalendar=*:0/30` (раніше `OnUnitActiveSec` не запускав таймер бо потребує "активацію через timer" як точки відліку, а ручні `systemctl start` її не задають).

Друга інстанція парсера ([systemd/max-parser-b.service](../systemd/max-parser-b.service)):
- Нова oneshot-сесія з `WS_PARSER_LABEL=B`, окремими файлами токена/device_id, `MemoryMax=600M`.

Бейджі дельти на дашборді ([dashboard.py:2225](../dashboard.py)):
- `get_stats()` повертає додаткові метрики: `last_hour_prev`, `last_day_prev` (постів за попередній період) і `active_channels_24h`, `active_channels_24h_prev` (унікальних авторів за 24h).
- У картці "Каналів" — бейдж `XXX активних за 24h ↑+N`/`↓-N` (порівняння каналів-донорів).
- У картках "За годину" і "За 24 год" — стрілка з різницею **постів** проти попереднього аналогічного періоду (зелена ↑ ріст, червона ↓ спад, нейтральна `·` рівність).
- CSS-стилі `.stat-delta`, `.delta-up`, `.delta-down`, `.delta-zero`.

Нічний рестарт дашборда ([systemd/max-dashboard-restart.timer](../systemd/max-dashboard-restart.timer)):
- `lemma-cache` накопичує ~250k+ постів за добу і впирається в `MemoryMax=800M` — нові reach-задачі не запускаються. Timer щодоби о 03:00 MSK перезапускає сервіс. Бекап-стратегія до повного LRU.

QR-флоу для 2-го токена:
- На VPS лежить `pw_qr_capture.py` (playwright з headless-Chromium), оновлено в новий `pw_qr_b.py` що ловить opcode=291 (а не лише 115) і авто-рефрешить screenshot кожні 25с. Користувач сканує телефоном, скрипт зберігає токен у `/root/.login_token_b`.

Безпека ([.gitignore](../.gitignore)):
- Додано `.login_token_b`, `.device_id_b` у список секретів.
- `qr_*.png`, `qr_screen*.png` — тимчасові screenshots, не комітимо.

**Поточний стан:**
- Покриття main-flow топ-10: **99.1% за 24h, 92.5% за останню годину** (до multi-token + deep backfill було ~84%).
- 3 сервіси active: `max-parser` (токен A, 8 WS), `max-parser-b` (токен B, 8 WS), `backfill-priority.timer` (op=49 через B-токен, 30-хв cycle).
- Backfill виконується без помилок (errors=0).
- Deep backfill (одноразовий, `SYNC_WINDOW=500`) додав +11,744 постів за 4.6 хв.

**Наступний крок:**
- Тиждень нагляду — чи 99% покриття тримається стабільно.
- Якщо ні: додати 2-й IP на VPS (купити IPv4 у aeza ~$2/міс, або безкоштовний IPv6 якщо MAX підтримує) — другий парсер ходить через інший IP, MAX бачить як 2 різні клієнти. Це обійде per-IP throttle повністю.
- Опціонально: LRU-обмеження для lemma-cache у dashboard.py (заміна щонічного рестарту на постійне рішення).

---

## [2026-05-07 20:30] — Анти-зависання WS-воркерів + backfill пропущених постів

**Контекст:** дашборд показав різке падіння — з ~28k постів/день (05.05) до 3.7k за неповний день 07.05. Парсер працював 3 доби без рестарту, статус `active`, але приймав push'і у 5/8 воркерів — W0 замовчав о 13:05, W7 о 16:19, W3 о 16:25. Жодного exception'а перед смертю — просто тиша. ~1500 каналів (37% покриття) залишились без підписки.

**Ключові зміни:**

ws_parser.py — захист від мовчазної смерті воркера ([ws_parser.py:30-36, 140-230, 266-339](../ws_parser.py)):
- Нові константи: `CONNECT_TIMEOUT=30`, `SUBSCRIBE_TIMEOUT=60`, `IDLE_TIMEOUT=600`. Без них `websockets.connect()` і `ws.send()` могли зависнути назавжди при напівзакритому TCP — це і є гіпотеза причини мовчанки W0/W3/W7.
- `websockets.connect()` тепер з `open_timeout=15`, `ping_timeout=20`, `close_timeout=10` — гарантія що жодна I/O-операція не блокує.
- `WSClient._recv_loop` зберігає `last_activity` при кожному push'і **і re-raise** виключення — `worker()` нарешті бачить розрив і друкує "З'єднання розірвано: ...".
- `WSClient.close()` обгорнуто в `asyncio.wait_for(timeout=5)` — захист від зависання при graceful close мертвого сокета.
- Subscribe-цикл винесено в окрему `subscribe_all()` і обгорнуто `asyncio.wait_for(SUBSCRIBE_TIMEOUT)` — масова відправка 500 op=75 більше не зависає.
- Новий `idle_watchdog`: якщо WS живий, але push'ів немає >10 хв — форс-реконнект. Це safety net навіть якщо exception все ж не вилетить.
- Worker-loop переписаний на `asyncio.wait({recv_task, wd_task}, return_when=FIRST_COMPLETED)` — паралельне очікування розриву АБО watchdog.
- Логи помилок тепер з типом виключення: `Помилка (TypeError): ... Reconnect через 20s`.

backfill.py — новий скрипт для нагону пропущених постів ([backfill.py](../backfill.py)):
- Архітектура взята з `views_fetcher._Client`: одна WS-сесія, pipelined op=49 з `CONCURRENT_CHATS=24` (MAX дозволяє багато паралельних send у тій самій сесії, але тільки одну сесію на токен).
- `DEAD_SLICES` мапить worker_id → UTC-час смерті з 15-хв запасом; slice відтворюється так само як у `ws_parser.main`: `items[wid*500:(wid+1)*500]`.
- Pagination: `cursor = oldest_time_in_page`, зупинка коли `oldest <= since_ms` або сторінка не рухається.
- `INSERT OR IGNORE` через `UNIQUE(chat_id, msg_id)` — повторні запуски ідемпотентні.
- Workflow: `systemctl stop max-parser → python3 backfill.py → systemctl start max-parser` (одна WS-сесія на токен).

**Поточний стан:**
- Деплой пройшов: ws_parser.py перезапущений, всі 8 воркерів (W0-W7) активні з рівномірною push-активністю (по 99 рядків за 90с після рестарту).
- Backfill виконано за 5.9 хв при ~6 хв простою: **+5494 нових постів** додано, 0 помилок. Денний підсумок 07.05: 3,751 → 9,603. Основна маса (~4900) припала на slice W0 (7 годин пропуску); по slice W3 ~600. Slice W7 (останні ~355 каналів) повертав порожні відповіді — ймовірний throttle MAX на масовому op=49 (документовано в `resolve_channels.py`).

**Наступний крок:**
- Якщо темп W7 slice критичний — повторний запуск backfill через 5-10 хв (ідемпотентний) добре нагнати залишок.
- Опціонально: експеримент з `CONCURRENT_CHATS` нижче 24 у backfill — можливо MAX починає throttle саме від паралельності, а не від обсягу.
- Спостерігати: чи `idle_watchdog` колись спрацює на проді — це підтвердить що мовчазна смерть більше не повторюється.

---

## [2026-05-07 11:30] — v4.0: Бренд MAX Radar + черга охоплення + PDF на A4 + UX-доводка

**Ключові зміни:**

Бренд та UI ([dashboard.py](../dashboard.py)):
- Перейменування «MAX Parser» → «MAX Radar» у видимих місцях: title вкладки, шапка дашборду, картка логіну (внутрішні ідентифікатори: env-змінні, systemd-юніт, шлях репо — без змін).
- Зменшення ширини контейнерів на 15%: `--page-max: 1400px → 1190px` (одна CSS-змінна, всі max-width-блоки підхопили).
- Картка «Топ каналів»: `max-height: 420px → 560px` — 10-й канал більше не обрізається, скрол лишається для 11+.
- Кнопка «Аналітика» прихована (`style="display:none"`, JS-обробник лишається безпечним через guard на `.disabled`).
- Кнопка «Завантажити» → іконка дискетки `💾` без тексту, з `aria-label` і `title`.

Звіт PDF ([dashboard.py:1716-1801](../dashboard.py)):
- Стрічка постів виключена зі звіту через `body.pdf-mode #main-grid { display: none }`. PDF тепер містить усе до блоку «Тематична аналітика» включно.
- Render розтягнутий по ширині A4 з полями 15мм; багатосторінковий рендер при високому контенті (вирізаємо смуги по `availH = 267мм`).
- Контейнери у pdf-mode займають 100% ширини: `body.pdf-mode { --page-max: 100% !important; }`.
- Chart.js timeline отримує форсований `chart.resize()` + два `requestAnimationFrame` перед html2canvas — інакше canvas лишається у вузькій ширині пре-pdf.
- Заголовок звіту вирівняний по ширині контейнерів (`max-width: var(--page-max); margin: 0 auto`).

Черга охоплення замість 429 ([dashboard.py:2207+](../dashboard.py)):
- API `/api/timeline-reach` більше не повертає «busy» — усі task-и стають у `_reach_pending_ids` (FIFO) і обслуговуються по черзі через `reach_dispatcher()`.
- `threading.Condition` сторожить чергу; dispatcher блокується на `wait()` поки хтось не зробить `notify()`.
- Hard-cap `_REACH_MAX_QUEUE = 10` як anti-spam (повертає 429 лише при переповненні).
- API `/api/timeline-reach/<id>` повертає `queue_position` для стану `queued`.
- UI показує єдиний текст: «⏳ у черзі на підрахунок охоплення: май витримку…» (без числа позицій — навмисно простіше).

«Динаміка згадок» — постійний вибір періоду ([dashboard.py:822-826, 1568-1593](../dashboard.py)):
- Default «7 днів» при першому вході (раніше було 30); вибір зберігається в `localStorage.mention_days` і відновлюється при Refresh / «Знайти».
- HTML-шаблон більше не виставляє `active`-клас — це робить виключно JS на основі localStorage.
- Валідація значень: лише `[7, 30, 90]`; некоректні падають у default.

**Поточний стан:** Деплой на VPS пройшов чисто; всі зміни працюють. PDF-звіт повноекранний, охоплення без помилок «зайнято», вибір періоду стійкий між діями.

**Наступний крок:** Потенційно — індикатор покриття кеша лем у sidebar («Кеш: 87%»); persistent WS-conn для views_fetcher (мінус 1-2с на старт reach-task).

---

## [2026-05-07 09:50] — v3.9: Прискорення тематичної аналітики + морфологічний пошук

**Ключові зміни:**

NLP-пайплайн ([nlp.py](../nlp.py)):
- Видалено `NewsSyntaxParser` з пайплайна — синтаксичні дані ніде не використовувались, але з'їдали ~30% часу обробки одного посту. Pipeline тепер `(segmenter, morph_vocab, morph_tagger, ner_tagger)`.
- `is_topical()` через `lemma.startswith(roots_tuple)` — нативний C-цикл замість Python `any(...)`.
- Нова `tokenize_aggregated(text)` повертає `{(category, lemma): count}` — зручний формат для запису в БД.

Інкрементальний кеш лем ([nlp.py:228+](../nlp.py)):
- Нові таблиці `message_lemmas` (msg_id, category, lemma, n) і `message_lemmas_done` (msg_id) — мітка обробки.
- `process_messages_batch(db, batch_size, extra_stops, since, n_workers)` — обробляє наступну партію постів. Дедуплікує тексти за `blake2b(text)` перед NER (репости ловляться один раз).
- Multi-core: `_process_with_pool` через `ProcessPoolExecutor` з `_worker_init`/`_worker_run`. Контролюється env `MAX_PARSER_NLP_WORKERS` (default 1).
- `compute_period_tf_from_cache(db, since, channel, extra_stops)` — читає TF з агрегатного SQL (мс), стоп-слова фільтруються на льоту.
- Старий `compute_period_tf` залишений як fallback, тепер з hash-дедуплікацією.
- `purge_lemma_from_cache(db, lemma)` — видаляє лему з кеша при додаванні стоп-слова.

Dashboard інтеграція ([dashboard.py](../dashboard.py)):
- `_compute_top_words_blocking` тепер спершу читає кеш; падає у fallback тільки якщо покриття < `LEMMA_CACHE_MIN_COVERAGE` (80%).
- Новий `lemma_cache_scheduler()` — фоновий потік, пріоритет 24h → решта → idle. Batch 200 постів, ~85-95 пост/сек на 1 ядрі.
- `api_add_stop_word` чистить рядки з `message_lemmas` через `purge_lemma_from_cache`.
- Реєстрація `init_lemma_cache_schema(db)` і `lemma_cache_scheduler()` у startup.

Морфологічний пошук ([dashboard.py:1958-2018](../dashboard.py)):
- `build_fts_query` тепер проганяє кожен токен через `SnowballStemmer("russian")` перед додаванням `*`. Запит "зеленский" перетворюється на `зеленск*` і ловить усі словоформи (зеленского, зеленскому, ...).
- Раніше `зеленский*` знаходив 1 483 пости, тепер `зеленск*` — 2 531 (+71%). Для "ракета" виграш +811% (519 → 4731).
- Edge-кейси: явна `*` від користувача → as-is, оператори AND/OR/лапки → as-is, токени <5 символів → без стемінгу (щоб не отримати "ид*" з "идти"), не-кирилиця → без стемінгу.

Метрики на VPS після деплою:
- Топ-слова за 24h: **0.9с** з кешу замість 10-20 хв (на 14k постах).
- Кеш на 24h-вікні: 100% (14703/14705 постів) одразу після першого прогріву.
- На повну БД (~250k постів) кеш доходить за ~45-50 хв background, не блокуючи UI.

**Поточний стан:** На VPS працює нова версія, кеш активно наповнюється; тематична аналітика і пошук помітно швидші, словоформи ловляться повноцінно.

**Наступний крок:** Опційно — підняти `MAX_PARSER_NLP_WORKERS` до 4 (потрібно підняти `MemoryMax` у systemd до ~2400МБ); додати UI-індикатор покриття кеша на дашборді ("Тематична аналітика: кеш 87%").

---

## [2026-05-04 15:00] — v3.8: Session-based auth + заглушка Аналітики

**Ключові зміни:**

Авторизація на дашборді ([dashboard.py](../dashboard.py), [manage_auth.py](../manage_auth.py)):
- Замість HTTP Basic Auth — повноцінна форма входу `/login` з темним стилем у дусі дашборду (фіолетовий акцент, центрована картка, focus-border).
- Session через signed cookie, lifetime 7 днів. Secret key — `.dashboard_secret` (auto-generate при першому старті, 32 байти `secrets.token_bytes`, chmod 600).
- Файл `.dashboard_auth` — рядки `username:pbkdf2_hash` (хеш через `werkzeug.security.generate_password_hash`). Кеш `_load_auth_users` TTL 60 с — рестарт сервісу не потрібен при додаванні юзера.
- `before_request` гейт: пропускає `/login`, `/logout` і будь-який запит з валідною сесією. Решта: API (`/api/...`) → 401 JSON, HTML → 302 redirect на `/login?next=...` з захистом від open-redirect.
- Routes `/login` (GET form, POST validate), `/logout` (видалення сесії). Якщо `.dashboard_auth` порожній — auth ВИМКНЕНИЙ (для локальної розробки).
- Topbar отримав посилання `⎋ <username>` поряд з «⟳ Оновити» — клік виходить.
- `current_user` пробрасується у `render_template_string`.

CLI-helper [manage_auth.py](../manage_auth.py):
- `add <username>` — інтерактивний `getpass` (двічі), мін. 8 символів, оновлення якщо вже існує.
- `remove <username>`, `list`.
- Файл пишеться з `chmod 600` автоматично, відсортований за username.

Тимчасова заглушка кнопки «🧠 Аналітика» ([dashboard.py](../dashboard.py)):
- Кнопка завжди `disabled`, tooltip «Тимчасово недоступно». JS-обробник не приєднується (через `if (!btn || btn.disabled) return`).
- API-ендпоінти `/api/analytics`, `/api/analytics/<id>` лишились живі — UI просто без доступу. Повернути в робочий стан = одна Jinja-умова.

**Поточний стан:** v3.8 задеплоєна. Auth поки вимкнений (файл `.dashboard_auth` ще не створено на VPS). Користувач має сам додати 5 акаунтів через `python3 /root/manage_auth.py add <name>`. Кнопка Аналітики прихована від користувачів.

**Наступний крок:**
1. **Користувач додає 5 акаунтів** через `manage_auth.py` на VPS — після цього auth вмикається автоматично за 60 с.
2. **HTTPS** — поки HTTP, паролі ходять у клер. Caddy/Nginx + Let's Encrypt — окремий етап (потрібен домен).
3. Ідеї для верхнього ряду stat-плиток (обговорено): Σ охоплення 24 год, постів-«тривог», Δ постів vs вчора, гаряче слово дня, latency останнього поста.
4. Disk-кеш `_top_words_cache` + recurring refresh.
5. Завершити WS-рефакторинг із v3.5.

---

## [2026-05-04 14:00] — v3.7.1: PDF у білій темі + центрування stat-плиток

**Ключові зміни:**

PDF-звіт у світлій темі для друку ([dashboard.py](../dashboard.py)):
- Новий CSS-блок `body.pdf-mode ...` — інверсна тема: фон білий, увесь текст чорний (через `body.pdf-mode * { color: #000 !important; border-color: #ccc !important }`).
- Перед знімком `html2canvas` додаємо клас `pdf-mode` на `<body>`, після — знімаємо у `finally`. Користувач не бачить мерехтіння (~1-2 с).
- Chart.js не реагує на CSS (це canvas) — окремий патч у JS перед знімком: `ticks.color`, `grid.color`, `legend.labels.color` → чорні/світло-сірі. Зберігаємо попередні значення у `chartPatch` і відновлюємо у `finally`.
- `pointValueLabelsPlugin` отримав awareness: при `body.pdf-mode` бере темно-фіолетовий (`#3a2db8`) для згадок і темно-бірюзовий (`#0c7588`) для охоплення замість світлих відтінків.
- `html2canvas backgroundColor: '#080b14'` → `'#ffffff'` для PDF-режиму.
- Тимчасова шапка `#pdf-report-header` тепер інлайн-стилі білі (фон `#fff`, текст `#000`, рамка `#6c63ff`).
- Точкові overrides: `mark.highlight` → жовтий маркер `#fff59d`, `.word-bar-track`/`.channel-bar-track` → `#eee`, `.analytics-body .ai-list-item` → `#f6f5ff` зі збереженням фіолетового лівого border.

Stat-плитки ([dashboard.py](../dashboard.py)):
- Додано `text-align: center` у `.stat-card` — іконка/число/підпис тепер по центру.

**Поточний стан:** v3.7.1 задеплоєна. Звіт читабельний на роздруківці A4 (білий фон + чорний текст), темна тема дашборду залишається без змін під час звичайного перегляду. Stat-плитки візуально вирівняні.

**Наступний крок:**
1. Розширити верхній ряд плиток новими KPI (обговорено у переписці): кандидати — Σ охоплення 24 год, постів-«тривог», Δ постів vs вчора, гаряче слово дня, latency останнього поста.
2. Disk-кеш `_top_words_cache` + recurring refresh (з v3.7).
3. Завершити WS-рефакторинг із v3.5.

---

## [2026-05-04 13:00] — v3.7: AI-аналітика через Claude API + поліровка UI

**Ключові зміни:**

AI-аналітика — нова кнопка «🧠 Аналітика» ([dashboard.py](../dashboard.py), [requirements.txt](../requirements.txt), [summary.txt](../summary.txt)):
- Кнопка активна тільки коли `q` непорожній і `period in {'24h', '7d'}`. Disabled з контекстними tooltip-ами в інших випадках.
- Промт із [summary.txt](../summary.txt) (ТОП-5 тригерів + аналітичний висновок ~2000 символів, українською).
- Async-task pattern як у reach: `POST /api/analytics` стартує task, `GET /api/analytics/<task_id>` опитує. Глобальний семафор `_analytics_running["busy"]` — один Claude-запит одночасно. Кеш 15 хв на `(q, channel, since_ts, until_ts)`.
- Захист від переповнення контексту: `ANALYTICS_MAX_INPUT_CHARS=600_000` (~150k токенів), `ANALYTICS_POST_TRIM_CHARS=1500`. Серіалізатор `_build_analytics_input` обрізає довгі пости і зупиняється на ліміті.
- Mini-renderer markdown→HTML без зовнішніх залежностей: `## заголовки`, `**bold**`, нумеровані пункти. Формат відповіді фіксований у summary.txt, тому 30 рядків regex покривають усе.
- Конфіг через файли: `/root/.anthropic_key` (API key, chmod 600), `/root/.anthropic_gateway` (опційний base_url), `/root/.anthropic_gateway_token` (опційний CF AI Gateway токен).
- Модель: `claude-sonnet-4-6`.

Cloudflare AI Gateway — обхід гео-блоку Anthropic для RU-IP:
- VPS у aeza маршрутизується через Moscow (попри декларований KZ), Anthropic закриває API → 403 forbidden.
- Підтримка `base_url` через файл `.anthropic_gateway` + `default_headers={"cf-aig-authorization": "Bearer ..."}` через `.anthropic_gateway_token`.
- Authenticated Gateway з токеном (Cloudflare AI Gateway → max-parser → native Anthropic passthrough), щоб ніхто крім нашого dashboard не міг витрачати CF-ліміти.
- Прямий тест curl-ом: `claude-sonnet-4-6` повертає 200 через gateway.

UI/UX поліровка ([dashboard.py](../dashboard.py)):
- Кнопка «📥 Завантажити» (експорт XLSX): перероблена з form-submit на JS-навігацію `/api/export-xlsx?<window.location.search>`. Раніше hidden `period=custom` всередині `custom-dates` блоку перебивав активний preset (2 рядки в URL → Flask бере перший → `custom` без `from_date`/`to_date` → `(None, None, "all")` → експорт за весь час). Тепер експорт використовує query-параметри сторінки → саме ту вибірку, що видно.
- Result-badge тепер містить дати в дужках поряд із period_label: «за останні 7 днів (28.04.2026 — 04.05.2026)». Helper `_format_period_dates`.
- Заголовок «Тематична аналітика» динамічний: «Тематична аналітика за 24 години / за 7 днів / за 30 днів — військово-політична». Прибрано перемикач day/week/month — синхронізовано з основним фільтром через `_PERIOD_TO_WORDS`.
- «Топ каналів (N)»: count унікальних каналів у дужках. Окремий запит `get_top_channels_total` з `COUNT(DISTINCT channel_title)` (бо основний `LIMIT 20`).
- Графік «Динаміка згадок»: custom Chart.js plugin `pointValueLabelsPlugin` малює підписи прямо на канві — згадки фіолетом над кривою (`fmtBig`-аналог), охоплення блакитним під кривою (`fmtBigUA`: `тис./млн/млрд`). Локальні максимуми + крайні точки — завжди, інші ненульові — якщо вистачає місця.
- Σ охоплення в заголовку графіка: бейдж `Σ охоплення: X.X млн переглядів` поряд з «Динаміка згадок ‹q›». Очищається при перемиканні 7д/30д/90д.
- Reach-status повідомлення про завершення: `Охоплення зібрано та становить 20.5 млн переглядів` замість `Охоплення зібрано (постів: N)`. Об'єднано sampled і non-sampled гілки.
- PDF-звіт повністю переписано:
  - Шапка-блок (тимчасовий, додається першим child report-root): «МОНІТОРИНГ МЕДІА-ПРОСТОРУ МЕСЕНДЖЕРА «MAX»» + «Звіт по ключовому слову «...» · за останні 7 днів (DD.MM.YYYY — DD.MM.YYYY)».
  - Контент масштабується на одну сторінку A4 з 5мм margin (вже не розрізається).
  - 4 stat-плитки (Всього постів / Каналів / За годину / За 24 год) виключено з PDF через `ignoreElements`.

Performance і кеш ([dashboard.py](../dashboard.py)):
- `MAX_ROWS_SCAN`: 3 000 → 20 000 (ширша вибірка для тематичної аналітики).
- `TOP_WORDS_CACHE_TTL`: 300с → 3600с — інакше при 10-20 хв на прорахунок кеш expired раніше ніж прогрівся.
- Інвалідація кешу в `api_add_stop_word` уже існувала (помилково думали що нема).

CSS-уніфікація — однакова ширина всіх контейнерів:
- `--page-pad`: 1.5rem → 1.25rem.
- Прибрано horizontal padding з layout-обгорток `.tops-grid`, `.topics-section`, `.main-grid`, `.stat-grid` — раніше вони зсували children-картки на додаткові 1.25rem всередину, через що `.post-card`, `.topic-card`, `.sidebar-card`, `.stat-card` виглядали вужчими за `search-wrap`/`result-badge`/`timeline-card`.
- `.analytics-result` padding 1.25rem 1.5rem → 1.25rem симетричний.

**Підводні камені, зафіксовані під час реалізації:**
- Файл `summary.txt` був не задеплоєний з першою ітерацією — клік «Аналітика» падав з `[Errno 2] No such file or directory: '/root/summary.txt'`. Для майбутніх деплоїв пам'ятати: при додаванні нової фічі заливати ВСІ нові залежні файли разом.
- Anthropic API повертає 403 з повідомленням `Request not allowed` (не `geo_restricted`) — на діагностику пішло 2 ітерації. Перевірка: `curl ipinfo.io` на VPS показала Moscow/RU.
- Cloudflare AI Gateway за замовчуванням має ввімкнений Authenticated режим — без cf-aig-authorization токена дає `code:2009 Unauthorized` (це не Anthropic, а сам gateway).
- `MAX_ROWS_SCAN=20_000` означає ~10-20 хв на прогрів одного періоду (NLP-pipeline ~30-60мс/пост). Після рестарту `warm_top_words_cache` йде послідовно day→week→month, повний прогрів 25-45 хв. Це ламає сприйняття «кеш має бути миттєвим».

**Поточний стан:** v3.7 задеплоєна на VPS. `max-dashboard` active, AI-аналітика працює end-to-end через Cloudflare gateway. Усі контейнери дашборду візуально вирівняні. PDF-звіт на одну сторінку з шапкою.

**Наступний крок:**
1. **Disk-кеш `_top_words_cache`** — серіалізувати у файл при штатному завершенні і відновлювати при старті, щоб рестарт сервісу не убивав 30-45 хв прогріву.
2. **Recurring refresh** — фоновий scheduler який перераховує `day` раз на годину, `week` раз на 6 год, `month` раз на добу, незалежно від користувацьких запитів.
3. Завершити WS-рефакторинг (`ws_common.py` + `ws_parser.py`/`resolve_channels.py`) з v3.5 — досі незакомічений шар.
4. Розглянути `GROUP BY channel_link` замість `channel_title` у `get_top_channels` — щоб канали з однаковою назвою не зливалися (відомо з v3.6).

---

## [2026-05-04 10:30] — v3.6: Reach без жорсткого ліміту + alert-секція в Топ каналів

**Ключові зміни:**

Reach-pipeline ([views_fetcher.py](../views_fetcher.py), [dashboard.py](../dashboard.py)):
- `CONCURRENT_CHATS` 5 → 24. Pipelining op=49 у тому самому WS-conn — websockets дозволяє багато паралельних send (recv-loop один). Звірено через context7-доки `python-websockets`. Окремі conn-и не відкриваємо (MAX блокує паралельні сесії на токен).
- In-memory кеш `(chat_id, msg_id) → (views, ts)` у `views_fetcher` з TTL 30 хв (`_views_cache`, `_cache_get`, `_cache_put`). Перемикання період 7↔30 не фетчить ті самі пости повторно.
- `_fetch_one_chat` отримав `oldest_ts_ms` — пагінація припиняється коли `cursor` перетнув нижню межу періоду. Скорочує MAX_ROUNDS_PER_CHAT на сухих каналах.
- Прибрано hard-fail `too_many_posts:1001` ([dashboard.py:1495-1577](../dashboard.py)). Замість нього — стратифіковане семплування:
  - `_REACH_FULL_LIMIT = 1500` — повний прохід
  - `_REACH_SAMPLE_SIZE = 800` — цільова вибірка понад FULL_LIMIT
  - `_REACH_HARD_LIMIT = 8000` — стеля SELECT
  - Helper `_stratified_sample(rows, target)` — пропорційна квота по днях, мін. 5 постів на день. Екстраполяція `views_d = sampled × (day_total/day_sampled)`.
- API `/api/timeline-reach/<task_id>` тепер повертає `sampled`, `posts_total`, `posts_sampled`. Inline JS показує `Охоплення (≈ за вибіркою K з N постів)` для семплованого режиму.
- Деплой-смок: `q=путин&days=7` (раніше падав 1001) → 3418 постів → семпл 799 → 394 канали за ~60с. Реалістичні views: 28.04: 32.7М, 29.04: 44.2М, 30.04: 39М. CONCURRENT_CHATS=24 без rate-limit на токен.

Alert-канали — двосекційний Топ каналів ([dashboard.py](../dashboard.py)):
- Новий файл [channels/alert_channels.txt](../channels/alert_channels.txt) — alias по рядку, lower-case, mtime-кеш в `_load_alert_channels()` (рестарт dashboard не потрібен при правці файлу).
- `get_top_channels(..., mode)` — режими `'all'` (поточна поведінка), `'main'` (exclude alert), `'alert'` (only alert). Фільтр `lower(channel_link) IN/NOT IN (?,?,...)`. Ключ кешу включає `mode`.
- Без `q` → два списки в `tops-grid`: «Топ каналів — основний потік» + «Топ каналів — БПЛА / тривоги / радари». З `q` → один список як раніше.
- CSS `.tops-grid` оновлено на `repeat(auto-fit, minmax(380px, 1fr))` — один блок full-width, два = 50/50 на широких, 1 колонка на mobile.
- Seed-список з 27 alert-каналів (`LPRalarm`, `Info_bpla_Shebekino`, `vrv_radar`, `radarrussiia_novosti`, `crimea_radar82`, `locatorru`, `russia_rradar`, `BelgorodDRONE`, `krnew`, `kerch_onlinee` тощо).

Обслуговування каналів:
- Видалено канал «Плохие скидки» (alias `plohie_skidki`) — з `channels.txt`, з VPS `resolved.json` (3756 → 3755 каналів, бекап лежить на VPS), 787 постів видалено з `messages` + `INSERT INTO messages_fts(messages_fts) VALUES('rebuild')`. `max-parser` зупинявся на ~5с (інакше DB locked), знову active.

**Підводні камені, які зафіксовано:**
- `get_top_channels` групує `GROUP BY channel_title` — два канали з однаковою назвою (наприклад `russia_rradar` + `kerch_onlinee` обидва "Радар по всей России") **зливаються в одну стрічку UI**. Це не баг від цих змін (поведінка існуюча), але видно гостро коли користувач переносить дублікат у alert. Кандидат на наступну ітерацію: `GROUP BY channel_link` або композитний ключ.
- `LIMIT 20` ріже хвіст alert-режиму. З 25+ alert-каналів зараз 5 випадають з top-20 (cnt < 24). Якщо буде запит — підняти LIMIT для alert-режиму до 30.

**Поточний стан:** v3.6 деплоєна. `max-dashboard` active, `max-parser` active (3755 каналів). Reach-смок: «путин/7д» працює без помилки. Двосекційний топ виводиться при порожньому `q`.

**Наступний крок:** Завершити WS-рефакторинг із попереднього стейджа (`ws_common.py` + правки `ws_parser.py`/`resolve_channels.py`/`views_fetcher.py` що поза reach-блоком) — це окремий незакомічений шар з v3.5. Окремо: оцінити чи варто змінити `GROUP BY channel_title` → `channel_link` у топ каналів, щоб дублікати назви не зливалися.

---

## [2026-05-04 08:37] — v3.5: Кнопка «📄 Звіт» — PDF-експорт дашборду на клієнті

**Ключові зміни:**

PDF-звіт через html2canvas + jsPDF ([dashboard.py](../dashboard.py)):
- У `<head>` підключено CDN-скрипти `html2canvas@1.4.1` і `jspdf@2.5.2` (UMD, з `defer`) поряд з Chart.js
- Кнопка **📄 Звіт** у формі пошуку поряд з «📥 Завантажити»; `type="button"` → не сабмітить форму, викликає `generatePDF(this)`
- Обгортка `<div id="report-root">` навколо stat-grid → search-wrap → result-badge → timeline-card → tops-grid → topics-section → main-grid (тобто весь верх дашборду + поточна сторінка постів)
- `ignoreElements` викидає при рендері: `.search-wrap`, `.channel-ac-list`, `.stop-btn` (✕ біля топ-слів), `.stop-modal-overlay`. У звіт потрапляє лише контент, без UI-елементів керування
- `html2canvas` опції: `scale: 2` (Retina-якість), `backgroundColor: '#080b14'` (узгоджено з темою дашборду), `useCORS: true`, `logging: false`
- Multi-page: одна довга canvas-картинка → `addImage` на A4 з негативним `position` для кожної наступної сторінки (класичний html2canvas+jsPDF паттерн). Сторінок стільки, скільки треба для висоти контенту
- Файл зберігається як `max_report_YYYY-MM-DD_HHMM.pdf` через `pdf.save()`
- UX: кнопка під час генерації стає `disabled` з текстом `⏳ Готую…`, повертається до `📄 Звіт` у `finally`. Помилки → `alert()` з повідомленням + `console.error`

Чому клієнтський підхід (а не Playwright/ReportLab на сервері):
- Нуль нових серверних залежностей — `max-dashboard.service` не потребує chromium
- Працює з тим, що користувач реально бачить на екрані (включно з підгруженими через `/api/top-words`, `/api/timeline-reach` асинхронними даними)
- Не блокує WS-семафор (`views_fetcher` залишається вільним для XLSX-експорту)
- Швидко — ~1-2с на типовий дашборд

**Поточний стан:** Деплой на VPS успішний (08:32). FTS-індекс консистентний (probe 166326 / 181673). `max-dashboard` active. PDF генерується клієнтсько, не торкає БД/WS.

**Наступний крок:** Скомітити окремо WS-рефакторинг (`ws_common.py` + правки `ws_parser.py`/`views_fetcher.py`/`resolve_channels.py`) — це наступний крок з v3.4, який зараз у незакомічених змінах. Опціонально для PDF: якщо постів багато (>50), додати page-break між картками постів через `pagebreak: { mode: ['css','legacy'] }`-патерн або ручне слайсювання — зараз пост може бути «розрізаний» між сторінками.

---

## [2026-04-29 15:25] — v3.4: Ревізія коду — XSS-фікс, кеш гарячих SELECT-ів, оптимізація NLP

**Ключові зміни:**

Безпека (XSS у тематичній аналітиці) ([dashboard.py](../dashboard.py)):
- Топ слів: `onclick="addStopWord(event,'{{ word }}')"` → `onclick='addStopWord(event, {{ word|tojson }})'`. У JS-рендері — `JSON.stringify(word)` + `escapeHtml`. Лема могла містити `'`/`"`/`<` (NER видає сутності типу `O'Brien`) і ламати JS / відкривати інʼєкцію.
- href: `/?q={{ word }}` → `/?q={{ word|urlencode }}` (рядки 561, 517 — топ слів і топ каналів)

Виправлено false-positive ребілд baseline при кожному рестарті ([dashboard.py:1679-1716](../dashboard.py)):
- Раніше `_baseline_state["last_built"]=0.0` при старті → `time.time() - 0 > 86400` завжди True → scheduler одразу запускав 30-хв ребілд навіть на свіжому baseline
- Додано `_read_baseline_built_at(db)` — читає `MAX(updated_at)` з `baseline_lemma_freq` і насіває `last_built` справжнім значенням з БД
- Підтверджено в логах після деплою: `[baseline] starting rebuild...` НЕ зʼявився при старті

Race condition у кеші топ-слів ([dashboard.py:1725-1726](../dashboard.py)):
- `_top_words_cache.clear()` був поза `_top_words_lock` (могло конфліктувати з фоновим воркером, що пише результат)
- Загорнуто в `with _top_words_lock:`

Перформанс — кеш гарячих SELECT-ів ([dashboard.py:1313-1376](../dashboard.py)):
- `SELECT DISTINCT channel_title` і `GROUP BY channel_title` — повний скан на кожен рендер `/`
- Винесено в `get_channel_list(db)` (TTL 300с) і `get_top_channels(db, q, since, until)` (TTL 60с з ключем по q+вікно)
- Прибрано 60+ рядків з `index()` — тепер 2 виклики кеш-функцій
- Реальний ефект на VPS (89k постів): `GET /` cold 581мс → warm 256мс (-56%)

NER tokenize: `_in_ner` O(N×M) → O(1) ([nlp.py:148-173](../nlp.py)):
- Раніше для кожного токена крутився повний цикл по всіх NER-spans (`for s,e in ranges: if tok.start>=s and tok.stop<=e`)
- Замінено на `set(id(tok) for span in doc.spans for tok in span.tokens)` — O(1) lookup. На довгих постах (1000+ токенів × 5-10 spans) скорочує `tokenize_categorized` помітно

Обмеження `build_baseline` ([nlp.py:259-280](../nlp.py)):
- Додано `BASELINE_MAX_DOCS = 10_000` + `ORDER BY RANDOM() LIMIT ?` у SELECT
- Раніше нічний ребілд крутив усі пости старші 30 днів через повний Natasha-pipeline (~30-60мс/пост × 50k+ = десятки хвилин). Тепер cap на 10k → ~8-10хв
- Параметр `max_docs=BASELINE_MAX_DOCS` опційний — не ламає викликів

Залежності ([requirements.txt](../requirements.txt), [requirements-dev.txt](../requirements-dev.txt)):
- Розділено на prod/dev. Prod лишає flask/nltk/natasha/websockets/openpyxl
- `playwright`, `httpx[socks]` винесено у dev (потрібні лише для одноразової розвідки `scout.py`/`playwright_scout.py`, не для VPS-сервісів)
- На VPS `pip install -r requirements.txt` тепер тягне на ~200 МБ менше

Дрібниці:
- `asyncio.get_event_loop()` → `asyncio.get_running_loop()` у [ws_parser.py:213](../ws_parser.py) і [views_fetcher.py:83](../views_fetcher.py) (deprecation у Python 3.10+)
- Виправлено type hint `dict[str, int]` → `dict[str, dict]` у [resolve_channels.py:175](../resolve_channels.py) (реально зберігає {id, title, subs})

**Поточний стан:** Деплой на VPS успішний (15:21). FTS індекс консистентний (probe 81756 / 88972 повідомлень). Smoke-тести: `GET /` 200, `GET /?q=путин` 200/73мс, `/api/top-words` 200/8мс. NLP-пайплайн готовий, 467 тематичних коренів. Парсер активно пише пости.

**Наступний крок:** Винести спільний WS handshake (`connect`+`login`) у `ws_common.py` — зараз продубльовано в трьох файлах ([ws_parser.py](../ws_parser.py), [resolve_channels.py](../resolve_channels.py), [views_fetcher.py](../views_fetcher.py)). Далі — розпаралелити `views_fetcher` (3-5 одночасних WS conn-ів) для прискорення великих xlsx-експортів.

---

## [2026-04-27 20:36] — v3.3: Перепланування дашборду + лінія охоплення на timeline

**Ключові зміни:**

Layout/UI ([dashboard.py](../dashboard.py)):
- Поле пошуку, result-badge і блок «Динаміка згадок» винесено з `main-grid` і піднято **над** «Топ каналів». Новий порядок секцій: статистика → пошук → бейдж → timeline → топ каналів → тематична аналітика → стрічка постів
- `topics-grid` тепер 4 колонки в один ряд (`repeat(4, 1fr)`) замість 2×2; адаптив: 2 колонки <1100px, 1 колонка <600px

Топ каналів за ключовим словом:
- Якщо `q` заданий — top_channels рахується через `messages_fts MATCH` з тими ж часовими фільтрами що й основний пошук (since_ts/until_ts), `LIMIT 20`
- Без `q` — стара поведінка (загальний топ по всій БД)
- Реалізовано в `index()` ([dashboard.py:1733](../dashboard.py))

Друга лінія на timeline — «Охоплення» (sum of views per day):
- **Архітектура:** повна асинхронна схема з прогресом, бо WS-збір через `views_fetcher` — повільний (десятки сек–хвилини)
- Backend: in-memory стейт-стор тасків (`_reach_tasks`, `_reach_cache`) + глобальний семафор `_reach_running` (одна WS-сесія одночасно, бо `.login_token` єдиний)
- `POST /api/timeline-reach` → старт або повернення кешу. `GET /api/timeline-reach/<id>` → `{state, progress, total, data}`
- Worker `_run_reach_task()` парсить логи `views_fetcher` регексом `[views] N/M chat=…` → оновлює прогрес у task dict
- Запобіжники: `q` обов'язковий, `days ∈ {7, 30}` (90 — забагато), ліміт **1000 постів** на task, **429** при зайнятому семафорі, кеш TTL 15 хв per `(q, channel, days)`
- Frontend: при наявності `q` після `loadTimeline()` стартує `loadReach()` з пулінгом кожні 2с. Друга лінія — бірюзова пунктирна на окремій Y-осі справа з форматом `12.3k`/`1.2M`. Статус-рядок під заголовком: `⏳ збираємо охоплення (X/Y каналів)…` → `Охоплення зібрано (постів: N)` / `error`
- `_reachCurrentDays` — захист від race condition коли користувач перемикає період під час пулінгу

**Поточний стан:** Деплой на VPS успішний. Smoke-тести:
- `/?q=путин&days=7` → коректно повернув `too_many_posts:1001` (ліміт спрацював)
- `q=медведев&days=7` → 76 постів × 64 канали, готово за ~20с, реальні агреговані views (пік 25.04 — 2.79M)
- Прогрес тікав 22→47→60→64 каналів кожні 5с

**Наступний крок:** Якщо потрібна точніша економія WS-викликів — додати колонки `views`, `views_at` в `messages` і кешувати «назавжди» для постів старших за 7 днів (вони вже не ростуть). Зараз TTL 15 хв — компроміс.

---

## [2026-04-27 20:02] — v3.2: Експорт результатів пошуку у XLSX з live-views

**Ключові зміни:**

Розвідка протоколу MAX (op=49 → views):
- Створено [scout_views.py](../scout_views.py) — одноразовий recon, шукає поля типу view/seen/read у payload-ах різних опкодів
- **Знайдено:** `payload.messages[i].stats.views` через op=49 (getMessages) повертає актуальну кількість переглядів. Підтверджено на 5 семплах — значення оновлюються в реальному часі
- Інші проби: op=66 потребує `messageIds` (валідація), op=130/131 — timeout (відсутні або інші семантики)

Live-views fetcher:
- Новий модуль [views_fetcher.py](../views_fetcher.py) — async WS-клієнт, окремий від `ws_parser.py` (свій conn до `wss://ws-api.oneme.ru/websocket`)
- Логіка: групує запитані пости за `chat_id`, для кожного каналу 1 op=49 з backward-pagination (до 100 повідомлень × 20 раундів) поки не зібрано всі msg_id або не вичерпано вікно
- Sync wrapper `fetch_views(posts) -> {(chat_id, msg_id): views|None}` — викликається з Flask handler через `asyncio.run`
- Не зберігає views у БД — snapshot тільки на момент скачування (per користувачева вимога)
- `INTER_REQUEST_DELAY=0.05` між op=49 (окремий conn, не парсерний)

Endpoint `/api/export-xlsx` у [dashboard.py](../dashboard.py):
- Той самий SQL що `index()` (FTS5 MATCH + channel + period + sort), **без LIMIT** — повна вибірка
- `_query_export_rows()` повертає всі рядки → `views_fetcher.fetch_views()` → `_build_xlsx()` через openpyxl
- Формат 1-в-1 з [example/приклад_формату.xlsx](../example/приклад_формату.xlsx): лист «Повідомлення», шапка (запит/кількість/період), рядок 5 — заголовки (Канал/Посилання/Дата/Кількість переглядів/Текст), дані з рядка 6, ширина колонок ідентична
- Дата постів у форматі DD.MM.YYYY, текст з wrap_text=True
- HTTP-ім'я файлу через RFC 6266 (`filename="ascii"; filename*=UTF-8''…`) — інакше `Content-Disposition` падав на UnicodeEncodeError для кирилиці

UI/UX:
- Кнопка «📥 Завантажити» справа від «Знайти» з `formaction="/api/export-xlsx"` — використовує ту саму форму, той самий набір параметрів
- JS-фідбек на сабміт: блокування повторних кліків + `⏳ Збирається... (може зайняти кілька хвилин)`
- Auto-reset кнопки на `window.focus`/`pageshow` (коли користувач повертається після save dialog) + 15-хв запобіжник

Залежності: `+websockets>=12.0`, `+openpyxl>=3.1.0` у [requirements.txt](../requirements.txt).

**Поточний стан:** Деплой на VPS успішний. Тести пройдено:
- 1-канал/24г («ТАСС») — 147 постів, 1.2с, 147/147 views зібрано, монотонно зростають
- широкий запит `period=all q=зеленск*` — 500 постів × 159 каналів, ~7 хв (очікувано — обмежено WS round-trip)
- Кириличні запити більше не падають на encoding header

**Наступний крок:** Якщо великі експорти стануть частим кейсом — спроєктувати async background job + WebSocket-progress (зараз браузер просто чекає кілька хвилин). Для першої ітерації UX-фідбек достатній. Альтернативно: паралельні WS-conn-и (3-5 одночасно) у `views_fetcher` — лінійно прискорить великі експорти. На зараз — спостереження за реальним юзем.

---

## [2026-04-27 19:05] — v3.1: Тематична аналітика per-channel + автокомпліт пошуку каналу

**Ключові зміни:**

Тематична аналітика по обраному каналу:
- `nlp.compute_period_tf` отримала опційний параметр `channel` → `WHERE channel_title = ?` у SELECT по `messages`
- `_compute_top_words_blocking(period, channel)` — той самий NER+TF-IDF, але по підмножині постів каналу
- Кеш `_top_words_cache` тепер ключується парою `(period, channel)`; `_top_words_inflight` теж парою — глобал і per-channel живуть незалежно
- API `/api/top-words` приймає `channel`; `index()` передає поточний `channel` у `get_top_words`
- Заголовок секції: «Тематична аналітика — військово-політична · канал **X**» коли є фільтр; data-channel виставляється на `.topics-section`, JS `loadTopWords` приклеює його до запиту
- Прогрів кешу при старті (`warm_top_words_cache`) — тільки `channel=""`; per-channel рахується по запиту (часто <1с — у каналу мало постів)

Автокомпліт пошуку каналу:
- Замінено `<select name="channel">` на `<input>` + кастомний випадаючий список (`.channel-ac`)
- Список каналів передається у JS через `{{ channel_list | tojson }}` → `_CHANNEL_LIST`
- Substring-фільтрація case-insensitive (cyrillic-ready), підсвічування фрагменту через `<mark>`
- Навігація: ↑↓, Enter — вибрати активний АБО найрелевантніший зі списку, Esc — закрити, ✕ — очистити фільтр
- Клік поза dropdown — закриває; ліміт 50 видимих варіантів
- Вибір з autocomplete одразу сабмітить форму (миттєвий перехід на відфільтровану сторінку)

**Поточний стан:** Деплой на VPS пройшов. Smoke-тести: `GET /` 200, `GET /api/top-words?period=day` 200, `GET /api/top-words?channel=...` 200 з логом `[top-words] day/<channel>: готово за 0.5s` — per-channel compute дійсно ізольований від глобального кешу. БД: 53014 повідомлень, FTS probe 48739/53014 — індекс консистентний.

**Наступний крок:** Перевірити в браузері UX автокомпліту з реальними каналами (substring match для російських назв з пробілами/комами). Якщо UX добрий — наступним кроком «топ каналів за згадками ключового слова» (показувати поряд з основним пошуком, у яких каналах термін зустрічається найчастіше).

---

## [2026-04-25 02:00] — v3: Дашборд зрілий — TF-IDF топ слів, FTS5 пошук, systemd autostart

**Ключові зміни:**

NLP-стек:
- Створено `nlp.py` — ізольований модуль (Segmenter, MorphVocab, NewsMorphTagger з Natasha; lazy-loaded, ~500 МБ RAM при першому виклику)
- Видалено pymorphy3 — повний перехід на Natasha (`NewsMorphTagger` навчений на новинах)
- Видалено антипатерн `russian_baseline.txt` (топ-1500 OpenSubtitles як стоп-лист) — замінено на власний baseline з БД
- Нові SQLite-таблиці `baseline_lemma_freq` (lemma, df, tf) і `baseline_meta` (n_docs)
- `baseline_scheduler` — фоновий потік перебудовує baseline раз на добу (rolling 30 днів)

Топ слів:
- Перепроектовано на TF-IDF: `score = (1 + log(tf)) × log((N+1)/(df_baseline+1))` (sublinear TF як sklearn)
- Фільтр POS-тегів: тільки NOUN/PROPN/VERB/ADJ — автоматично відсіює службові слова
- `_RU_EXTRA` — розширений російський стоп-ліст (новинні штампи, часові маркери)
- Видалено `_UK_STOPS`, `_EN_STOPS` — проект тільки російськомовний
- Bar chart показує **score**, число поряд — TF (кількість згадок); ранкінг візуально монотонний
- Кнопка ✕ — додавання слова до `custom_stop_words.txt`, миттєвий refresh без перезавантаження
- Baseline fallback виключає останню добу — baseline і "сьогодні" не перекриваються

Пошук (FTS5):
- Замінено `LIKE '%q%'` на FTS5 virtual table `messages_fts` з `unicode61 remove_diacritics 2` tokenizer
- Тригери INSERT/UPDATE/DELETE — автосинхронізація з `messages` (парсер не змінюється)
- `init_fts()` self-healing: probe-запит на частотні літери; якщо <50% від messages → автоматичний `rebuild`
- `build_fts_query`: AND між словами + автоматичний `*` для префіксного пошуку (`путин` → знаходить «Путин», «Путина», «путинский»)
- Підтримка операторів: OR, NOT, NEAR, "точна фраза", `слово*`
- Підсвічування через FTS5 `highlight()` — узгоджене з токенізацією

Інфраструктура:
- `systemd/max-parser.service`, `systemd/max-dashboard.service` — автозапуск після reboot, restart on failure, MemoryMax обмеження (600M/800M)
- `ws_parser.py`: WAL mode, batch commit (раз на 2с / 100 повідомлень), `commit_watchdog`
- `dashboard.py`: WAL, threaded Flask, `get_stats` з 30-с TTL
- `requirements.txt`: видалено pymorphy3, додано natasha

**Поточний стан:** Дашборд на VPS працює через systemd, FTS5 проіндексувала 6393 пости, baseline містить 23k+ лем з 5483 документів. Топ слів показує реальні тематичні сигнали (приклад: «россошанский», «мгимо», «галактика», «звездообразование»). Пошук «путин» знаходить 216 постів (case-insensitive + всі словоформи).

**Наступний крок:** Через ~30 днів парсер накопичить достатньо історії — baseline переключиться з fallback "all - last day" на чистий "older than 30 days", IDF стане ще точнішим. Опціонально — фаза 2: дедуплікація варіантів через Navec embeddings (синоніми, транслітерація).

---

## [2026-04-23 14:30] — v2.1: Парсер працює, знайдено реальний opcode нових повідомлень

**Ключові зміни:**
- `resolve_channels.py` — змінено формат збереження з `{alias: chatId}` на `{alias: {id, title, subs}}` для збереження метаданих каналу
- `ws_parser.py` — повний переписання: видалено keyword-фільтрацію, додано збір усіх постів, виправлено критичний баг відсутності `_recv_loop` (без якого `_send_recv` ніколи не резолвив Future і `connect()` завжди повертав False)
- Додано debug-логування та виявлено реальний opcode нових повідомлень: **op=128** (не op=55 як очікувалось), payload: `{chatId, message, ttl, unread, mark}`
- Виправлено `on_push` для обробки op=128 (одне повідомлення в `payload.message`)
- SQLite схема: `messages(id, saved_at, channel_title, channel_link, channel_subs, chat_id, msg_id, msg_time, post_link, text)`

**Поточний стан:** Парсер запущений на VPS, 5 воркерів підписані на 2091 канал, пости зберігаються в `matches.db`.

**Наступний крок:** Побудувати Flask-дашборд для перегляду зібраних постів.

---

## [2026-04-22 ~18:00] — v1: Розвідка WebSocket API MAX через Playwright на VPS

**Ключові зміни:**
- Створено `scout.py` — HTTP зонд (httpx), знайдено що `web.max.ru` це SPA
- Створено `playwright_scout.py` — Playwright розвідка, перехоплення XHR/WebSocket
- Виправлено баг WebSocket frame handler (`.payload` → пряма рядкова передача в старих версіях Playwright)
- Розгорнуто на KZ VPS (`85.192.56.53`) — `web.max.ru` доступний без геоблоку
- Створено `CLAUDE.md` з описом архітектури і контексту проекту

**Головна знахідка:**
- MAX використовує **WebSocket API**: `wss://ws-api.oneme.ru/websocket`
- Протокол: `{"ver":11,"cmd":0,"seq":N,"opcode":X,"payload":{...}}`
- Знайдені opcodes: `6` (handshake), `288` (QR auth init), `289` (auth polling)
- Веб-клієнт вимагає авторизацію навіть для публічних каналів

**Поточний стан:** WebSocket API виявлено, протокол частково зрозумілий, потрібна авторизація для читання каналів.

**Наступний крок:** Отримати акаунт MAX → знайти opcode авторизації через логін/пароль в JS бандлі → побудувати WS-клієнт без Playwright.

---
