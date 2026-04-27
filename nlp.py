"""
NLP-пайплайн для дашборда: токенізація, лемматизація, NER, тематична фільтрація,
TF-IDF скорінг проти baseline.

Архітектура:
- Natasha завантажується ледаче — moduly не займають RAM поки не викликають tokenize.
- 4 категорії на виході токенайзера: per (персони), loc (локації), org (організації),
  term (тематичні леми, чий корінь є у topical_roots.txt).
- Baseline у SQLite з ключем "category::lemma" (у тій самій таблиці).
- Скорінг: TF-IDF проти baseline — `(1 + log tf) × log((N+1)/(df+1))`.
"""
from __future__ import annotations

import math
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

# ── Natasha ledge-load ───────────────────────────────────────────────────────

_pipeline_lock = threading.Lock()
_pipeline = None  # tuple(segmenter, morph_vocab, morph_tagger, syntax_parser, ner_tagger) або False

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
    """Перевіряє чи починається лема з одного з тематичних коренів."""
    roots = _load_topical_roots()
    if not roots:
        return True  # fallback: якщо файла немає, не фільтруємо
    return any(lemma.startswith(r) for r in roots)


# ── Pipeline init ────────────────────────────────────────────────────────────

def _init_pipeline():
    """Ледача ініціалізація Natasha (segment+morph+syntax+ner). Викликається раз."""
    global _pipeline
    with _pipeline_lock:
        if _pipeline is not None:
            return _pipeline
        try:
            from natasha import (
                Segmenter, MorphVocab, NewsEmbedding,
                NewsMorphTagger, NewsSyntaxParser, NewsNERTagger,
            )
            segmenter = Segmenter()
            morph_vocab = MorphVocab()
            emb = NewsEmbedding()
            morph_tagger = NewsMorphTagger(emb)
            syntax_parser = NewsSyntaxParser(emb)
            ner_tagger = NewsNERTagger(emb)
            _pipeline = (segmenter, morph_vocab, morph_tagger, syntax_parser, ner_tagger)
            print("[nlp] Natasha pipeline (morph+syntax+ner) готовий", flush=True)
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
    segmenter, morph_vocab, morph_tagger, syntax_parser, ner_tagger = pipeline
    stops = extra_stops or set()

    try:
        doc = Doc(text)
        doc.segment(segmenter)
        doc.tag_morph(morph_tagger)
        doc.parse_syntax(syntax_parser)
        doc.tag_ner(ner_tagger)
    except Exception:
        return empty

    out: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    ner_token_ranges: list[tuple[int, int]] = []  # [(start, stop), ...]

    # NER spans → персони / локації / організації; зберігаємо діапазони токенів,
    # щоб не дублювати їх у "term".
    for span in doc.spans:
        try:
            span.normalize(morph_vocab)
        except Exception:
            pass
        norm = _norm_entity(span.normal or span.text or "")
        ner_token_ranges.append((span.start, span.stop))
        if len(norm) < 2 or norm in stops:
            continue
        cat = {"PER": "per", "LOC": "loc", "ORG": "org"}.get(span.type)
        if cat:
            out[cat].append(norm)

    def _in_ner(tok) -> bool:
        for s, e in ner_token_ranges:
            if tok.start >= s and tok.stop <= e:
                return True
        return False

    # Решта значущих токенів → "term" якщо проходить тематичний фільтр.
    for token in doc.tokens:
        if _in_ner(token):
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


def build_baseline(db: sqlite3.Connection, days_back: int = 30,
                   extra_stops: set[str] | None = None) -> tuple[int, int]:
    """
    Перебудовує baseline з БД. Зберігає категоризовані ключі "category::lemma".
    """
    init_baseline_schema(db)
    cutoff_old = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d %H:%M:%S")

    rows = db.execute(
        "SELECT text FROM messages WHERE saved_at < ?", (cutoff_old,)
    ).fetchall()
    if not rows:
        cutoff_today = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        rows = db.execute(
            "SELECT text FROM messages WHERE saved_at < ?", (cutoff_today,)
        ).fetchall()
    if not rows:
        return 0, 0

    key_df: Counter = Counter()
    key_tf: Counter = Counter()
    n_docs = 0

    for (text,) in rows:
        keys = tokenize_lemmas(text, extra_stops)
        if not keys:
            continue
        n_docs += 1
        for k in set(keys):
            key_df[k] += 1
        for k in keys:
            key_tf[k] += 1

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


def compute_period_tf(db: sqlite3.Connection, since: str, row_limit: int,
                      extra_stops: set[str] | None = None,
                      channel: str | None = None) -> dict[str, int]:
    """
    Рахує TF за останніми повідомленнями періоду. Повертає Counter з ключами
    "category::lemma" (per/loc/org/term).

    Якщо передано channel — обмежує вибірку постами з конкретного каналу
    (channel_title рівне переданому значенню).
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
    counter: Counter = Counter()
    for (text,) in rows:
        for key in tokenize_lemmas(text, extra_stops):
            counter[key] += 1
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
