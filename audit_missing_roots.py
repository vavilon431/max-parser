"""
Аудит "невидимих" тематичних коренів.

Поточний пайплайн відсіює term-леми які НЕ починаються з одного з ~250 коренів
у topical_roots.txt. Скрипт прогнаює Natasha НА ВСЬОМУ корпусі (sample) БЕЗ цього
фільтра і знаходить леми які потрапили б у топ-300, але зараз пропускаються.

Це список кандидатів — оператор сам вирішує які корені додати у topical_roots.txt
(беруться короткі prefixи, щоб охопити словоформи: "санкц" покриває "санкции",
"санкционный", "санкционировать" тощо).

    python3 /root/audit_missing_roots.py
    # → /root/audit_missing_roots_report.txt
"""
import hashlib
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# Прокидаємо ROOT у sys.path щоб імпортувати nlp/ws_common
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import nlp as nlp_mod
from nlp import _GOOD_POS, _MIN_WORD_LEN, _load_topical_roots, _init_pipeline

DB_FILE     = ROOT / "matches.db"
REPORT_FILE = ROOT / "audit_missing_roots_report.txt"

SAMPLE_SIZE = 5000   # випадкова вибірка постів за період
PERIOD_DAYS = 7
TOP_LIMIT   = 300    # скільки топ-лем оглядаємо
MIN_TF      = 5      # мінімум TF щоб взагалі попасти в розгляд

# Базові ru-стоп-слова, які явно НЕ є кандидатами в корені (повторюємо тут,
# щоб не залежати від `dashboard._RU_EXTRA` — скрипт самодостатній).
_BASIC_STOPS = {
    "который", "это", "весь", "также", "такой", "такая", "такие",
    "сам", "сама", "сами", "год", "годы", "день", "месяц", "неделя",
    "час", "минута", "сегодня", "вчера", "завтра", "новость", "новости",
    "сообщение", "сообщать", "сказать", "говорить", "рассказать",
    "хороший", "плохой", "большой", "маленький", "новый", "старый",
    "первый", "второй", "третий", "последний", "просто", "просто",
    "случай", "часть", "место", "сторона", "вопрос", "ответ", "слово",
    "люди", "человек", "женщина", "мужчина", "ребенок", "дети",
}


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def main():
    print(f"[audit] Завантажую topical_roots...", flush=True)
    current_roots = set(_load_topical_roots())
    print(f"[audit] Поточних коренів: {len(current_roots)}", flush=True)

    print(f"[audit] Беремо випадкову вибірку {SAMPLE_SIZE} постів за {PERIOD_DAYS}d...", flush=True)
    conn = open_db()
    cutoff = (datetime.now() - timedelta(days=PERIOD_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT text FROM messages WHERE saved_at >= ? "
        "ORDER BY RANDOM() LIMIT ?",
        (cutoff, SAMPLE_SIZE)
    ).fetchall()
    conn.close()
    print(f"[audit] Отримано {len(rows)} рядків", flush=True)

    print(f"[audit] Прогрів Natasha...", flush=True)
    if not nlp_mod.nlp_available():
        print("[audit] Natasha недоступна — exit")
        sys.exit(1)
    from natasha import Doc
    pipeline = _init_pipeline()
    segmenter, morph_vocab, morph_tagger, ner_tagger = pipeline

    # Dedup за hash
    by_hash: dict[str, str] = {}
    for r in rows:
        t = (r["text"] or "").strip()
        if not t:
            continue
        h = hashlib.blake2b(t.encode("utf-8"), digest_size=16).hexdigest()
        if h not in by_hash:
            by_hash[h] = t
    print(f"[audit] Унікальних текстів: {len(by_hash)}", flush=True)

    # Рахуємо TF усіх валідних лем (без is_topical фільтра), окремо tracking
    # скільки документів містить лему (df) — для базового TF-IDF-подібного ранкінгу.
    tf_counter: Counter = Counter()
    df_counter: Counter = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    n_processed = 0

    for h, text in by_hash.items():
        n_processed += 1
        if n_processed % 500 == 0:
            print(f"[audit] {n_processed}/{len(by_hash)}", flush=True)
        try:
            doc = Doc(text)
            doc.segment(segmenter)
            doc.tag_morph(morph_tagger)
            doc.tag_ner(ner_tagger)
        except Exception:
            continue

        # id() токенів які належать NER-спанам — їх не беремо в term-кандидати
        ner_ids: set[int] = set()
        for span in doc.spans:
            for tok in span.tokens:
                ner_ids.add(id(tok))

        # Унікальні леми в цьому документі (для df)
        doc_lemmas: set[str] = set()

        for token in doc.tokens:
            if id(token) in ner_ids:
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
            if lemma in _BASIC_STOPS:
                continue
            tf_counter[lemma] += 1
            doc_lemmas.add(lemma)
            # Зберігаємо до 3 прикладів-фрагментів
            if lemma not in examples or len(examples[lemma]) < 3:
                snippet = text[:120].replace("\n", " ")
                examples[lemma].append(snippet)
        for lemma in doc_lemmas:
            df_counter[lemma] += 1

    print(f"[audit] Всього унікальних лем (поза NER, POS-валідних): {len(tf_counter)}", flush=True)

    # Розділяємо: проходять is_topical (in current roots prefix) vs ні
    def passes_topical(lemma: str) -> bool:
        return any(lemma.startswith(r) for r in current_roots)

    missing: list[tuple[str, int, int]] = []  # (lemma, tf, df)
    for lemma, tf in tf_counter.items():
        if tf < MIN_TF:
            continue
        if passes_topical(lemma):
            continue
        missing.append((lemma, tf, df_counter[lemma]))

    missing.sort(key=lambda x: -x[1])  # по TF спадно
    top = missing[:TOP_LIMIT]
    print(f"[audit] Кандидатів (поза topical_roots, TF>={MIN_TF}): {len(missing)}", flush=True)
    print(f"[audit] Записую топ-{TOP_LIMIT} у {REPORT_FILE}", flush=True)

    lines: list[str] = []
    lines.append("# Кандидати на додавання у topical_roots.txt")
    lines.append(f"# Згенеровано: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"# Вибірка: {len(by_hash)} унікальних постів за {PERIOD_DAYS} днів")
    lines.append(f"# Леми поза current topical_roots ({len(current_roots)} коренів)")
    lines.append("# Формат: TF DF lemma | example1 || example2")
    lines.append("")
    for lemma, tf, df in top:
        ex = " || ".join(examples.get(lemma, [])[:2])
        lines.append(f"{tf:5d} {df:4d} {lemma:<30} | {ex}")
    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"[audit] DONE", flush=True)


if __name__ == "__main__":
    main()
