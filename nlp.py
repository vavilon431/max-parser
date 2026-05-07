"""
NLP-пайплайн для дашборда: токенізація, лемматизація, NER, тематична фільтрація,
TF-IDF скорінг проти baseline.

Архітектура:
- Natasha завантажується ледаче — модулі не займають RAM поки не викликають tokenize.
- 4 категорії на виході токенайзера: per (персони), loc (локації), org (організації),
  term (тематичні леми, чий корінь є у topical_roots.txt).
- Baseline у SQLite з ключем "category::lemma" (у тій самій таблиці).
- Скорінг: TF-IDF проти baseline — `(1 + log tf) × log((N+1)/(df+1))`.

Швидкісні оптимізації:
- `parse_syntax` вилучено з пайплайна (було ~30% часу, ніде не використовувалось).
- `is_topical` через `startswith(tuple)` — нативний C-цикл замість Python any().
- Інкрементальний кеш лем у таблиці `message_lemmas` — NER кожного поста рахується
  один раз за все життя; запит "топ за період" перетворюється на GROUP BY у SQL.
- Дедуплікація за hash тексту перед NER — репости одного посту обробляються один раз.
- Multi-core опційно через `process_messages_batch_parallel` (ProcessPoolExecutor).
"""
from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# ── Natasha ledge-load ───────────────────────────────────────────────────────

_pipeline_lock = threading.Lock()
_pipeline = None  # tuple(segmenter, morph_vocab, morph_tagger, ner_tagger) або False

# Без VERB — дієслова в "топ слів" військово-політичного дашборду майже ніколи
# не несуть користі (прислать/сообщать/пояснять забивають топ).
_GOOD_POS = {"NOUN", "PROPN", "ADJ"}
_MIN_WORD_LEN = 3

CATEGORIES = ("per", "loc", "org", "term")
_KEY_SEP = "::"

# ── Тематичні корені ─────────────────────────────────────────────────────────

_TOPICAL_ROOTS_FILE = Path(__file__).parent / "topical_roots.txt"
_topical_roots: tuple[str, ...] | None = None
_topical_roots_lock = threading.Lock()


def _load_topical_roots() -> tuple[str, ...]:
    """Завантажує і кешує корені тематичних термінів."""
    global _topical_roots
    with _topical_roots_lock:
        if _topical_roots is not None:
            return _topical_roots
        if not _TOPICAL_ROOTS_FILE.exists():
            print(f"[nlp] WARN: {_TOPICAL_ROOTS_FILE.name} не знайдено", flush=True)
            _topical_roots = ()
            return _topical_roots
        roots: list[str] = []
        for line in _TOPICAL_ROOTS_FILE.read_text(encoding="utf-8").splitlines():
            s = line.strip().lower()
            if not s or s.startswith("#"):
                continue
            roots.append(s)
        # Сортуємо за довжиною спадно — щоб startswith матчив довші корені раніше.
        roots.sort(key=len, reverse=True)
        _topical_roots = tuple(roots)
        print(f"[nlp] завантажено {len(_topical_roots)} тематичних коренів", flush=True)
        return _topical_roots


def reload_topical_roots() -> int:
    """Скинути кеш коренів (для гаряче-перезавантаження). Повертає нову кількість."""
    global _topical_roots
    with _topical_roots_lock:
        _topical_roots = None
    return len(_load_topical_roots())


def is_topical(lemma: str) -> bool:
    """Перевіряє чи починається лема з одного з тематичних коренів.
    Використовує `str.startswith(tuple)` — це нативний C-цикл, в рази швидше
    за Python `any(lemma.startswith(r) for r in roots)`.
    """
    roots = _load_topical_roots()
    if not roots:
        return True  # fallback: якщо файла немає, не фільтруємо
    return lemma.startswith(roots)


# ── Pipeline init ────────────────────────────────────────────────────────────

def _init_pipeline():
    """Ледача ініціалізація Natasha (segment+morph+ner). Викликається раз.
    `parse_syntax` свідомо вилучено: було ~30% часу пайплайна, але ніде не
    використовується — NER працює на embeddings, POS-фільтр бере дані з morph.
    """
    global _pipeline
    with _pipeline_lock:
        if _pipeline is not None:
            return _pipeline
        try:
            from natasha import (
                Segmenter, MorphVocab, NewsEmbedding,
                NewsMorphTagger, NewsNERTagger,
            )
            segmenter = Segmenter()
            morph_vocab = MorphVocab()
            emb = NewsEmbedding()
            morph_tagger = NewsMorphTagger(emb)
            ner_tagger = NewsNERTagger(emb)
            _pipeline = (segmenter, morph_vocab, morph_tagger, ner_tagger)
            print("[nlp] Natasha pipeline (morph+ner, без syntax) готовий", flush=True)
        except Exception as e:
            print(f"[nlp] Natasha init failed: {e}", flush=True)
            _pipeline = False
        return _pipeline


def nlp_available() -> bool:
    return _init_pipeline() is not False


# ── Категоризований токенайзер ───────────────────────────────────────────────

def _norm_entity(text: str) -> str:
    """Нормалізує сутність для ключа: lower, прибрати зайві пробіли і лапки."""
    return " ".join(text.lower().replace("«", "").replace("»", "")
                        .replace('"', "").replace("'", "").split())


def tokenize_categorized(text: str, extra_stops: set[str] | None = None
                         ) -> dict[str, list[str]]:
    """
    Повертає dict з 4 категоріями:
      - per:  [нормалізовані імена персон з NER]
      - loc:  [нормалізовані локації з NER]
      - org:  [нормалізовані організації з NER]
      - term: [леми NOUN/PROPN/ADJ, чий корінь у topical_roots]

    Токени, що належать NER-spans, НЕ потрапляють у term — щоб не дублювалися.
    """
    empty = {c: [] for c in CATEGORIES}
    pipeline = _init_pipeline()
    if pipeline is False or not text:
        return empty

    from natasha import Doc
    segmenter, morph_vocab, morph_tagger, ner_tagger = pipeline
    stops = extra_stops or set()

    try:
        doc = Doc(text)
        doc.segment(segmenter)
        doc.tag_morph(morph_tagger)
        doc.tag_ner(ner_tagger)
    except Exception:
        return empty

    out: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    ner_token_ids: set[int] = set()  # id() токенів, що належать NER-спанам

    # NER spans → персони / локації / організації; запам'ятовуємо id() токенів
    # span.tokens, щоб у наступному циклі не дублювати їх у "term" (O(1) замість O(N×M)).
    for span in doc.spans:
        try:
            span.normalize(morph_vocab)
        except Exception:
            pass
        norm = _norm_entity(span.normal or span.text or "")
        for tok in span.tokens:
            ner_token_ids.add(id(tok))
        if len(norm) < 2 or norm in stops:
            continue
        cat = {"PER": "per", "LOC": "loc", "ORG": "org"}.get(span.type)
        if cat:
            out[cat].append(norm)

    # Решта значущих токенів → "term" якщо проходить тематичний фільтр.
    for token in doc.tokens:
        if id(token) in ner_token_ids:
            continue
        if token.pos not in _GOOD_POS:
            continue
        try:
            token.lemmatize(morph_vocab)
        except Exception:
            continue
        lemma = (token.lemma or "").lower().strip()
        if len(lemma) < _MIN_WORD_LEN:
            continue
        if not any(c.isalpha() for c in lemma):
            continue
        if lemma in stops:
            continue
        if not is_topical(lemma):
            continue
        out["term"].append(lemma)

    return out


def tokenize_aggregated(text: str, extra_stops: set[str] | None = None
                        ) -> dict[tuple[str, str], int]:
    """Те саме що tokenize_categorized, але одразу агрегує за (cat, lemma) → count.
    Зручно для запису у message_lemmas одним рядком на унікальну пару."""
    cats = tokenize_categorized(text, extra_stops)
    agg: dict[tuple[str, str], int] = {}
    for cat in CATEGORIES:
        for lemma in cats[cat]:
            key = (cat, lemma)
            agg[key] = agg.get(key, 0) + 1
    return agg


def tokenize_lemmas(text: str, extra_stops: set[str] | None = None) -> list[str]:
    """
    Legacy-сумісність: плаский список усіх лем (per+loc+org+term) з префіксом
    "category::". Використовується build_baseline для збереження одного словника.
    """
    cats = tokenize_categorized(text, extra_stops)
    flat: list[str] = []
    for cat in CATEGORIES:
        for lemma in cats[cat]:
            flat.append(f"{cat}{_KEY_SEP}{lemma}")
    return flat


# ── Baseline: SQLite persist ─────────────────────────────────────────────────

BASELINE_TABLE    = "baseline_lemma_freq"
BASELINE_META_KEY = "n_docs"

# ── Інкрементальний кеш лем для постів ───────────────────────────────────────

LEMMA_CACHE_TABLE = "message_lemmas"        # (msg_id, category, lemma, n)
LEMMA_DONE_TABLE  = "message_lemmas_done"   # (msg_id) — мітка обробки (включно з порожніми)


def init_baseline_schema(db: sqlite3.Connection):
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS {BASELINE_TABLE} (
            lemma      TEXT PRIMARY KEY,
            df         INTEGER NOT NULL,
            tf         INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS baseline_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    db.commit()


def init_lemma_cache_schema(db: sqlite3.Connection):
    """Таблиці інкрементального кеша лем. Ідемпотентно.
    `message_lemmas` — одна стрічка на унікальну (msg_id, category, lemma) з лічильником.
    `message_lemmas_done` — мітка що пост оброблено (навіть якщо лем 0).
    """
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS {LEMMA_CACHE_TABLE} (
            msg_id   INTEGER NOT NULL,
            category TEXT    NOT NULL,
            lemma    TEXT    NOT NULL,
            n        INTEGER NOT NULL,
            PRIMARY KEY (msg_id, category, lemma)
        )
    """)
    db.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{LEMMA_CACHE_TABLE}_lemma "
        f"ON {LEMMA_CACHE_TABLE}(category, lemma)"
    )
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS {LEMMA_DONE_TABLE} (
            msg_id INTEGER PRIMARY KEY
        )
    """)
    db.commit()


def lemma_cache_progress(db: sqlite3.Connection) -> tuple[int, int]:
    """Повертає (оброблено_постів, всього_постів) для UI/логів."""
    try:
        done = db.execute(f"SELECT COUNT(*) FROM {LEMMA_DONE_TABLE}").fetchone()[0]
        total = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        return int(done), int(total)
    except sqlite3.OperationalError:
        return 0, 0


def purge_lemma_from_cache(db: sqlite3.Connection, lemma: str) -> int:
    """Видаляє лему з кеша (всі категорії). Викликається після додавання стоп-слова.
    Повертає кількість видалених рядків."""
    cur = db.execute(
        f"DELETE FROM {LEMMA_CACHE_TABLE} WHERE lemma = ?", (lemma,)
    )
    db.commit()
    return cur.rowcount or 0


def reset_lemma_cache(db: sqlite3.Connection):
    """Повне скидання кеша. Використовується якщо помінявся набір тематичних
    коренів і треба все переробити з нуля."""
    db.execute(f"DELETE FROM {LEMMA_CACHE_TABLE}")
    db.execute(f"DELETE FROM {LEMMA_DONE_TABLE}")
    db.commit()


def load_baseline(db: sqlite3.Connection) -> tuple[dict[str, int], int]:
    """Повертає (key_df, n_baseline_docs). Пусте якщо baseline ще не побудовано.
    Ключі мають формат "category::lemma" (тематично-категоризований словник).

    Якщо baseline був побудований старою версією (ключі без "::"), він вважається
    несумісним: повертаємо порожній словник + 0 docs, щоб scheduler ініціював
    перебудову автоматично.
    """
    try:
        rows = db.execute(f"SELECT lemma, df FROM {BASELINE_TABLE}").fetchall()
    except sqlite3.OperationalError:
        return {}, 0
    if not rows:
        return {}, 0
    # Швидка перевірка сумісності: якщо перший рядок без "::", схема стара.
    if _KEY_SEP not in rows[0][0]:
        print("[nlp] застарілий baseline (без категорій) — буде перебудовано", flush=True)
        return {}, 0
    baseline_df = {r[0]: r[1] for r in rows}
    meta_row = db.execute(
        "SELECT value FROM baseline_meta WHERE key = ?", (BASELINE_META_KEY,)
    ).fetchone()
    n_docs = int(meta_row[0]) if meta_row else 0
    return baseline_df, n_docs


BASELINE_MAX_DOCS = 10_000  # cap: повний пайплайн ~30-60мс/пост, 10k≈8-10хв.


def build_baseline(db: sqlite3.Connection, days_back: int = 30,
                   extra_stops: set[str] | None = None,
                   max_docs: int = BASELINE_MAX_DOCS) -> tuple[int, int]:
    """
    Перебудовує baseline з БД. Зберігає категоризовані ключі "category::lemma".
    Якщо постів старших за period > max_docs — беремо рівномірну випадкову вибірку
    (SQLite ORDER BY RANDOM() LIMIT) щоб не блокувати Natasha-pipeline на години.
    """
    init_baseline_schema(db)
    cutoff_old = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d %H:%M:%S")

    sql = ("SELECT text FROM messages WHERE saved_at < ? "
           "ORDER BY RANDOM() LIMIT ?")
    rows = db.execute(sql, (cutoff_old, max_docs)).fetchall()
    if not rows:
        cutoff_today = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        rows = db.execute(sql, (cutoff_today, max_docs)).fetchall()
    if not rows:
        return 0, 0

    # Дедуплікація: репости одного тексту — один прохід через NER.
    unique_texts: dict[str, list[str]] = defaultdict(list)
    for (text,) in rows:
        h = hashlib.blake2b((text or "").encode("utf-8"), digest_size=16).hexdigest()
        unique_texts[h].append(text or "")

    key_df: Counter = Counter()
    key_tf: Counter = Counter()
    n_docs = 0

    for h, texts in unique_texts.items():
        keys = tokenize_lemmas(texts[0], extra_stops)
        dup = len(texts)
        n_docs += dup
        if not keys:
            continue
        unique_keys = set(keys)
        for k in unique_keys:
            key_df[k] += dup
        per_text_tf: Counter = Counter(keys)
        for k, c in per_text_tf.items():
            key_tf[k] += c * dup

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute(f"DELETE FROM {BASELINE_TABLE}")
    db.executemany(
        f"INSERT INTO {BASELINE_TABLE}(lemma, df, tf, updated_at) VALUES(?, ?, ?, ?)",
        [(k, df, key_tf[k], now) for k, df in key_df.items()]
    )
    db.execute(
        "INSERT OR REPLACE INTO baseline_meta(key, value) VALUES(?, ?)",
        (BASELINE_META_KEY, str(n_docs))
    )
    db.commit()
    return n_docs, len(key_df)


# ── TF-IDF scoring ───────────────────────────────────────────────────────────

def _score_one(tf: int, df_b: int, log_n: float) -> float:
    idf = log_n - math.log(df_b + 1)
    return (1.0 + math.log(tf)) * idf


def score_top_categorized(tf_period: dict[str, int],
                          baseline_df: dict[str, int],
                          n_baseline_docs: int,
                          min_tf: int = 3,
                          limit_per_cat: int = 25
                          ) -> dict[str, list[tuple[str, int, float]]]:
    """
    TF-IDF ранкінг з sublinear TF scaling, з розбивкою за категоріями.
    Вхід: tf_period з ключами "category::lemma".
    Вихід: {"per": [(lemma, tf, score), ...], "loc": [...], "org": [...], "term": [...]}
    """
    by_cat: dict[str, list[tuple[str, int, float]]] = {c: [] for c in CATEGORIES}
    log_n = math.log(n_baseline_docs + 1) if n_baseline_docs > 0 else 0.0

    for key, tf in tf_period.items():
        if tf < min_tf:
            continue
        cat, sep, lemma = key.partition(_KEY_SEP)
        if not sep or cat not in by_cat:
            continue
        if log_n > 0:
            df_b = baseline_df.get(key, 0)
            score = _score_one(tf, df_b, log_n)
        else:
            score = float(tf)
        by_cat[cat].append((lemma, tf, score))

    for cat in by_cat:
        by_cat[cat].sort(key=lambda x: -x[2])
        by_cat[cat] = by_cat[cat][:limit_per_cat]
    return by_cat


# ── Інкрементальна обробка постів у фоні ─────────────────────────────────────

def _select_pending_msgs(db: sqlite3.Connection, batch_size: int,
                         since: str | None = None
                         ) -> list[tuple[int, str]]:
    """Бере пости яких ще нема в `message_lemmas_done`. Якщо since задано —
    обмежує цим періодом (для пріоритетного прогріву "за 24h")."""
    if since:
        sql = (
            f"SELECT m.id, m.text FROM messages m "
            f"LEFT JOIN {LEMMA_DONE_TABLE} d ON d.msg_id = m.id "
            f"WHERE d.msg_id IS NULL AND m.saved_at >= ? "
            f"ORDER BY m.id DESC LIMIT ?"
        )
        rows = db.execute(sql, (since, batch_size)).fetchall()
    else:
        sql = (
            f"SELECT m.id, m.text FROM messages m "
            f"LEFT JOIN {LEMMA_DONE_TABLE} d ON d.msg_id = m.id "
            f"WHERE d.msg_id IS NULL "
            f"ORDER BY m.id DESC LIMIT ?"
        )
        rows = db.execute(sql, (batch_size,)).fetchall()
    return [(int(r[0]), r[1] or "") for r in rows]


def _persist_batch_results(db: sqlite3.Connection,
                           msg_ids: list[int],
                           results_by_id: dict[int, dict[tuple[str, str], int]]):
    """Записує результати NER одного batch-у у `message_lemmas` + мітки в `_done`."""
    rows: list[tuple[int, str, str, int]] = []
    for mid in msg_ids:
        agg = results_by_id.get(mid, {})
        for (cat, lemma), n in agg.items():
            rows.append((mid, cat, lemma, n))
    if rows:
        db.executemany(
            f"INSERT OR REPLACE INTO {LEMMA_CACHE_TABLE}"
            f"(msg_id, category, lemma, n) VALUES (?, ?, ?, ?)",
            rows
        )
    db.executemany(
        f"INSERT OR IGNORE INTO {LEMMA_DONE_TABLE}(msg_id) VALUES (?)",
        [(mid,) for mid in msg_ids]
    )
    db.commit()


def _process_with_dedup(items: list[tuple[int, str]],
                        extra_stops: set[str] | None
                        ) -> dict[int, dict[tuple[str, str], int]]:
    """Sequential-варіант. Дедуплікує тексти за blake2b-hash, проганяє Natasha
    один раз на унікальний текст, копіює результат на всі msg_id."""
    by_hash_msgs: dict[str, list[int]] = defaultdict(list)
    by_hash_text: dict[str, str] = {}
    for mid, text in items:
        h = hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()
        by_hash_msgs[h].append(mid)
        if h not in by_hash_text:
            by_hash_text[h] = text

    results: dict[int, dict[tuple[str, str], int]] = {}
    for h, mids in by_hash_msgs.items():
        text = by_hash_text[h]
        if not text.strip():
            for mid in mids:
                results[mid] = {}
            continue
        agg = tokenize_aggregated(text, extra_stops)
        for mid in mids:
            results[mid] = agg
    return results


# ProcessPool worker — module-level, щоб pickle-нувся.

_worker_stops: set[str] | None = None


def _worker_init(stops: set[str] | None):
    """Викликається в дочірньому процесі один раз. Прогріває Natasha-пайплайн
    і зберігає stop-set в global для подальших викликів _worker_run."""
    global _worker_stops
    _worker_stops = stops or set()
    _init_pipeline()


def _worker_run(text: str) -> dict[tuple[str, str], int]:
    """Викликається на кожному унікальному тексті у дочірньому процесі."""
    return tokenize_aggregated(text, _worker_stops)


def _process_with_pool(items: list[tuple[int, str]],
                       extra_stops: set[str] | None,
                       n_workers: int
                       ) -> dict[int, dict[tuple[str, str], int]]:
    """Multi-core варіант з ProcessPoolExecutor. Дедуплікує тексти і шарить
    обчислення між n_workers. Кожен worker тримає Natasha-пайплайн у пам'яті
    (тому оверхед — лише на холодний старт пула)."""
    from concurrent.futures import ProcessPoolExecutor

    by_hash_msgs: dict[str, list[int]] = defaultdict(list)
    by_hash_text: dict[str, str] = {}
    for mid, text in items:
        h = hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()
        by_hash_msgs[h].append(mid)
        if h not in by_hash_text:
            by_hash_text[h] = text

    hashes = [h for h, t in by_hash_text.items() if t.strip()]
    texts = [by_hash_text[h] for h in hashes]

    results: dict[int, dict[tuple[str, str], int]] = {}
    if not texts:
        for mids in by_hash_msgs.values():
            for mid in mids:
                results[mid] = {}
        return results

    with ProcessPoolExecutor(max_workers=n_workers,
                              initializer=_worker_init,
                              initargs=(extra_stops,)) as ex:
        for h, agg in zip(hashes, ex.map(_worker_run, texts, chunksize=4)):
            for mid in by_hash_msgs[h]:
                results[mid] = agg

    # Тексти з порожнім stripped — не пройшли в pool, маркуємо як оброблені з {}.
    for h, t in by_hash_text.items():
        if not t.strip():
            for mid in by_hash_msgs[h]:
                results[mid] = {}
    return results


def process_messages_batch(db: sqlite3.Connection,
                           batch_size: int,
                           extra_stops: set[str] | None = None,
                           since: str | None = None,
                           n_workers: int = 1) -> int:
    """Обробляє наступну партію постів які ще не в кеші.
    `since` — пріоритезувати пости з періоду (для прогріву "24h" спершу).
    `n_workers` — 1 (sequential, default) або >1 (ProcessPoolExecutor).
    Повертає кількість оброблених постів (0 — більше нема pending)."""
    if not nlp_available():
        return 0
    items = _select_pending_msgs(db, batch_size, since=since)
    if not items:
        return 0

    if n_workers > 1:
        results = _process_with_pool(items, extra_stops, n_workers)
    else:
        results = _process_with_dedup(items, extra_stops)

    msg_ids = [mid for mid, _ in items]
    _persist_batch_results(db, msg_ids, results)
    return len(items)


# ── TF за період: швидко з кеша + fallback на on-the-fly ─────────────────────

def compute_period_tf_from_cache(db: sqlite3.Connection,
                                 since: str,
                                 extra_stops: set[str] | None = None,
                                 channel: str | None = None
                                 ) -> tuple[dict[str, int], int, int]:
    """Читає TF з `message_lemmas` за період (агрегатний SQL — мікросекунди).
    Поверне ({"category::lemma": tf}, постів_у_кеші, постів_всього_у_періоді).

    `extra_stops` фільтруються на льоту (важливо: коли користувач додає стоп через
    UI, нові леми ще можуть лишитись у кеші до наступного `purge_lemma_from_cache`,
    тому перестраховуємось і тут).
    """
    stops = extra_stops or set()

    if channel:
        sql = (
            f"SELECT l.category, l.lemma, SUM(l.n) AS tf "
            f"FROM {LEMMA_CACHE_TABLE} l "
            f"JOIN messages m ON m.id = l.msg_id "
            f"WHERE m.saved_at >= ? AND m.channel_title = ? "
            f"GROUP BY l.category, l.lemma"
        )
        params: tuple = (since, channel)

        sql_done = (
            f"SELECT COUNT(*) FROM messages m "
            f"JOIN {LEMMA_DONE_TABLE} d ON d.msg_id = m.id "
            f"WHERE m.saved_at >= ? AND m.channel_title = ?"
        )
        sql_total = (
            "SELECT COUNT(*) FROM messages "
            "WHERE saved_at >= ? AND channel_title = ?"
        )
    else:
        sql = (
            f"SELECT l.category, l.lemma, SUM(l.n) AS tf "
            f"FROM {LEMMA_CACHE_TABLE} l "
            f"JOIN messages m ON m.id = l.msg_id "
            f"WHERE m.saved_at >= ? "
            f"GROUP BY l.category, l.lemma"
        )
        params = (since,)
        sql_done = (
            f"SELECT COUNT(*) FROM messages m "
            f"JOIN {LEMMA_DONE_TABLE} d ON d.msg_id = m.id "
            f"WHERE m.saved_at >= ?"
        )
        sql_total = "SELECT COUNT(*) FROM messages WHERE saved_at >= ?"

    counter: Counter = Counter()
    try:
        rows = db.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        rows = []

    for cat, lemma, tf in rows:
        if not cat or not lemma:
            continue
        if lemma in stops:
            continue
        counter[f"{cat}{_KEY_SEP}{lemma}"] += int(tf)

    try:
        done_in_period = int(db.execute(sql_done, params).fetchone()[0])
        total_in_period = int(db.execute(sql_total, params).fetchone()[0])
    except sqlite3.OperationalError:
        done_in_period, total_in_period = 0, 0

    return dict(counter), done_in_period, total_in_period


def compute_period_tf(db: sqlite3.Connection, since: str, row_limit: int,
                      extra_stops: set[str] | None = None,
                      channel: str | None = None) -> dict[str, int]:
    """
    Fallback (повільний): рахує TF за останніми повідомленнями періоду шляхом
    запуску NER на льоту. Дедуплікує тексти за hash. Використовується якщо
    кеш ще порожній або частково заповнений.
    """
    if channel:
        rows = db.execute(
            "SELECT text FROM messages WHERE saved_at >= ? AND channel_title = ? "
            "ORDER BY id DESC LIMIT ?",
            (since, channel, row_limit)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT text FROM messages WHERE saved_at >= ? ORDER BY id DESC LIMIT ?",
            (since, row_limit)
        ).fetchall()

    by_hash_dup: dict[str, int] = defaultdict(int)
    by_hash_text: dict[str, str] = {}
    for (text,) in rows:
        t = text or ""
        h = hashlib.blake2b(t.encode("utf-8"), digest_size=16).hexdigest()
        by_hash_dup[h] += 1
        if h not in by_hash_text:
            by_hash_text[h] = t

    counter: Counter = Counter()
    for h, dup in by_hash_dup.items():
        text = by_hash_text[h]
        for key in tokenize_lemmas(text, extra_stops):
            counter[key] += dup
    return counter


# ── Backward-compat обгортка (на випадок зовнішніх викликів) ─────────────────

def score_top(tf_period: dict[str, int],
              baseline_df: dict[str, int],
              n_baseline_docs: int,
              min_tf: int = 3,
              limit: int = 200) -> list[tuple[str, int, float]]:
    """
    Legacy: повертає плаский ранкінг по всіх категоріях разом.
    Лема віддається з префіксом "category::" — викликач сам розділяє якщо треба.
    """
    by_cat = score_top_categorized(tf_period, baseline_df, n_baseline_docs,
                                   min_tf=min_tf, limit_per_cat=limit)
    flat: list[tuple[str, int, float]] = []
    for cat in CATEGORIES:
        for lemma, tf, score in by_cat[cat]:
            flat.append((f"{cat}{_KEY_SEP}{lemma}", tf, score))
    flat.sort(key=lambda x: -x[2])
    return flat[:limit]
