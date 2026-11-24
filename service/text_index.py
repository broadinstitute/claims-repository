"""The text index behind GET /v1/objects/search?q=: object_text holds, for every
stored object, the words of each text field it has (statement, name,
description). One definition shared by every write path, the backfill and the
audit, as service/refs.py is for object_refs, so the index cannot disagree
with itself.

Matching, the same on both backends: every word of the query must begin a word
of ONE searched field of the object. Words are runs of letters and digits,
compared case-insensitively; everything else, the underscore included, only
separates them. So `nod2 bowel` finds "The gene NOD2 is associated with
inflammatory bowel disease.", `inflam` finds it too, `od2` does not, and
`abnormal glucose` finds the gene set mp_abnormal_glucose_homeostasis.

`words` stores a field's words lowercased, each preceded by one space, so
"begins a word" is `words LIKE '% <word>%'` on SQLite and MySQL alike. On MySQL
a FULLTEXT index over the same column narrows the rows first; it is only an
accelerator, and the LIKE conditions decide every match. For a word that is in
nearly every row the index is slower than reading the table, so the search
gives the index a short budget and then reads the table instead
(`fulltext=False`).
"""
from __future__ import annotations

import json
import re

# Top-level string slots that are searched, in the order a result is labelled.
TEXT_FIELDS = ("statement", "name", "description")
MAX_WORDS_CHARS = 20_000      # of one field's words; a longer text is indexed up to here
MAX_QUERY_WORDS = 12
_WORD = re.compile(r"[^\W_]+")

# What InnoDB's FULLTEXT index holds of `words`: tokens of at least
# innodb_ft_min_token_size (3) characters that are not on its default stopword
# list. A query word outside that (short, a stopword, or not plain a-z0-9, whose
# tokenization is not worth relying on) is enforced by LIKE alone.
_FULLTEXT_TERM = re.compile(r"[a-z0-9]{3,80}")
INNODB_STOPWORDS = frozenset(
    "a about an are as at be by com de en for from how i in is it la of on or that the this to was what when "
    "where who will with und www".split())
MAX_FULLTEXT_TERMS = 8


def tokens(text: str) -> list[str]:
    """The words of a text, lowercased, in order."""
    return _WORD.findall(text.casefold())


def words(text) -> str | None:
    """A text's `words` value (" the gene nod2 ..."), or None when it has no word."""
    if not isinstance(text, str):
        return None
    found = tokens(text)
    if not found:
        return None
    value = " " + " ".join(found)
    if len(value) > MAX_WORDS_CHARS:
        value = value[:value.rfind(" ", 0, MAX_WORDS_CHARS)]
    return value or None


def text_rows(object_id: str, class_name: str, payload: dict) -> list[tuple[str, str, str, str]]:
    """[(object_id, class_name, field, words)] of one document: a row per text field it has."""
    rows = []
    for field in TEXT_FIELDS:
        value = words(payload.get(field))
        if value:
            rows.append((object_id, class_name, field, value))
    return rows


def index_objects(con, objects) -> None:
    """Index [(object_id, class_name, payload dict)]. Idempotent: a stored object's document never changes, so a
    row already there is left as it is."""
    rows = [row for object_id, class_name, payload in objects for row in text_rows(object_id, class_name, payload)]
    if rows:
        con.executemany(con.dialect.insert_text, rows)


def query_words(q: str) -> list[str]:
    """The distinct words of a query, in order, at most MAX_QUERY_WORDS."""
    return list(dict.fromkeys(tokens(q)))[:MAX_QUERY_WORDS]


def fulltext_query(found: list[str]) -> str | None:
    """The boolean-mode FULLTEXT query that narrows rows for these words, or None when no word can use the index."""
    terms = [word for word in found if _FULLTEXT_TERM.fullmatch(word) and word not in INNODB_STOPWORDS][:MAX_FULLTEXT_TERMS]
    return " ".join(f"+{word}*" for word in terms) or None


def match_sql(con, q: str, *, fields=None, class_name=None, alias: str = "t", fulltext: bool = True):
    """(SQL condition over object_text `alias`, params, whether the FULLTEXT index is used) for query `q`, or None
    when q has no word (nothing matches). `fulltext=False` leaves the index out: the same rows, found by reading."""
    from . import db

    found = query_words(q)
    if not found:
        return None
    where, params, indexed = [], [], False
    if fulltext and not con.dialect.sqlite:
        narrowed = fulltext_query(found)
        if narrowed:
            where.append(f"MATCH({alias}.words) AGAINST (? IN BOOLEAN MODE)")
            params.append(narrowed)
            indexed = True
    for word in found:
        where.append(f"{alias}.words LIKE ? ESCAPE '!'")
        params.append(f"% {db.like_escape(word)}%")
    if fields:
        where.append(f"{alias}.field IN ({', '.join('?' * len(fields))})")
        params.extend(fields)
    if class_name:
        where.append(f"{alias}.class_name = ?")
        params.append(class_name)
    return " AND ".join(where), params, indexed


_FIELD_SQL = {
    True: "CASE WHEN json_type(payload, '$.{f}') = 'text' THEN json_extract(payload, '$.{f}') END",
    False: "CASE WHEN JSON_TYPE(JSON_EXTRACT(payload, '$.{f}')) = 'STRING' THEN JSON_UNQUOTE(JSON_EXTRACT(payload, '$.{f}')) END",
}


def rebuild_object_text(con, batch: int = 5000, progress=None) -> int:
    """Index every stored document that is not indexed yet (backfill after the migration, or repair). Reads only
    the text fields, in id order, a batch at a time; each batch is its own short transaction. Returns the number of
    index rows offered (already-present rows are ignored)."""
    select = ", ".join(_FIELD_SQL[con.dialect.sqlite].format(f=field) + f" AS {field}" for field in TEXT_FIELDS)
    written, last = 0, ""
    while True:
        rows = con.execute(
            f"SELECT id, class_name, {select} FROM dapper_objects WHERE id > ? ORDER BY id LIMIT ?", (last, batch)).fetchall()
        if not rows:
            break
        last = rows[-1]["id"]
        found = [row for r in rows for row in text_rows(r["id"], r["class_name"], {f: r[f] for f in TEXT_FIELDS})]
        if found:
            con.executemany(con.dialect.insert_text, found)
            written += len(found)
        con.commit()   # a short transaction per batch, and a fresh read snapshot for the next
        if progress:
            progress(last, written)
    return written


def expected_rows(con, batch: int = 5000):
    """Yield every index row the stored documents imply, for the audit."""
    last = ""
    while True:
        rows = con.execute("SELECT id, class_name, payload FROM dapper_objects WHERE id > ? ORDER BY id LIMIT ?", (last, batch)).fetchall()
        if not rows:
            return
        last = rows[-1]["id"]
        for r in rows:
            yield from text_rows(r["id"], r["class_name"], json.loads(r["payload"]))
