# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Призначення

Парсер публічних каналів `web.max.ru` (rosсійська платформа `MAX`). Збирає всі нові пости з ~2000 каналів у локальну SQLite, далі — Flask-дашборд із FTS5-пошуком і тематичною аналітикою (NER + TF-IDF). Тематика — **виключно військово-політична / російські канали**; NLP-пайплайн заточений під російську морфологію (Natasha).

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

## Архітектура — великими мазками

Чотири незалежні шари, що спілкуються через `matches.db`:

1. **`resolve_channels.py`** — одноразова setup-фаза. Бере `channels/channels.txt` (по одному alias на рядок), через WS-`op=89` отримує `chatId` і складає `channels/resolved.json`. Виконується раз; парсер це файл лише читає.

2. **`ws_parser.py`** — продакшн збирач. Один процес тримає N WebSocket-з'єднань (по 500 каналів на воркер) до `wss://ws-api.oneme.ru/websocket`, виконує `op=75 subscribe` для кожного channelId і чекає push-повідомлення (`cmd=0` з сервера) → `INSERT INTO messages`. Без фільтрації по ключовим словам. Авторизація — LOGIN-токен у `.login_token` (отримується через `ws_auth_scout.py` один раз).

3. **`dashboard.py`** — Flask UI на `:8080`. Один process, threaded. Працює з тим самим SQLite-файлом read-only. Все важке (FTS5 пошук, NLP) кешується в пам'яті процесу.

4. **`nlp.py`** — окремий модуль з ledge-load Natasha-пайплайна. Дашборд викликає його функції; парсер про NLP не знає.

## Топ слів — як це працює (ядро поточного UX)

NLP-пайплайн категоризує всі значущі токени на 4 групи:

- **NER через `NewsNERTagger`** → `per` / `loc` / `org` (нормалізація через `span.normalize(morph_vocab)`, тому Кремле→Кремль).
- **Решта токенів** проходять жорсткий фільтр:
  - POS ∈ {NOUN, PROPN, ADJ} — **`VERB` повністю викинуто** (свідомо: дієслова забивали топ "прислать/сообщать/пояснять").
  - лема має починатись з одного з ~250 коренів у [topical_roots.txt](topical_roots.txt) (війна, ракета, дрон, санкції, нато, президент, himars і т.д.). Інакше — відсіюється ще до TF-IDF.
- На кожній категорії окремо рахується `(1 + log tf) × log((N+1)/(df+1))` проти baseline. Baseline зберігається в таблиці `baseline_lemma_freq` з ключем `"category::lemma"` — старий формат без `::` авто-визнається застарілим у `nlp.load_baseline()`.

Якщо в топ просочується щось побутове (автомобіль, сніг тощо) — або додавати в `_RU_EXTRA` всередині `dashboard.py`, або через ✕-кнопку UI (пише в `custom_stop_words.txt`). Якщо натомість бракує тематичного терміну — додати корінь у [topical_roots.txt](topical_roots.txt).

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
- **Workflow:** код-перший. Усі правки — локально → `scp` на VPS → `systemctl restart`. Жодних ad-hoc правок безпосередньо на сервері.
- **Сервіси:** `max-parser.service` (memory cap 600 МБ), `max-dashboard.service` (cap 800 МБ — Natasha моделі). Логи — systemd journal (`journalctl -u max-dashboard -n 50`).
- **Деплой dashboard:** після `scp` обов'язково `systemctl restart max-dashboard`. Перший запит після рестарту — кеш холодний, ~30-60 с на baseline + перший період.
- **Геообмеження:** `st.max.ru` (JS/CDN) геоблокований за межами RU/CIS. VPS у KZ — основне середовище. Локальна розробка дашборду через VPN/проксі або просто проти копії `matches.db`.

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
