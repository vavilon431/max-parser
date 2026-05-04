# Project Stages Journal
Журнал етапів розробки проекту.

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
