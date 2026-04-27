# Project Stages Journal
Журнал етапів розробки проекту.

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
