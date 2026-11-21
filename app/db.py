"""SQLite persistence layer.

Answers, per-record judgments and per-item category counts are always written
in the same explicit transaction, so a failure leaves all three untouched.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app, g

from .coverage import analyse
from .judge import CATEGORIES, judge
from .version_def import loads

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source TEXT NOT NULL,
    definition TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    record_no TEXT NOT NULL,
    answers TEXT NOT NULL,
    categories TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (version_id, record_no)
);

CREATE INDEX IF NOT EXISTS idx_records_version ON records(version_id);

CREATE TABLE IF NOT EXISTS item_counts (
    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    item_index INTEGER NOT NULL,
    answered INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0,
    exempt INTEGER NOT NULL DEFAULT 0,
    pending INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (version_id, item_index)
);

CREATE TABLE IF NOT EXISTS version_revisions (
    version_id INTEGER PRIMARY KEY REFERENCES versions(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    record_count INTEGER NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_analyses_version ON analyses(version_id, id);

CREATE TRIGGER IF NOT EXISTS versions_immutable_update
BEFORE UPDATE ON versions
BEGIN
    SELECT RAISE(ABORT, 'versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS versions_immutable_delete
BEFORE DELETE ON versions
BEGIN
    SELECT RAISE(ABORT, 'versions are immutable');
END;
"""

CATEGORY_COLUMNS = {
    "answered": "answered",
    "missing": "missing",
    "exempt": "exempt",
    "pending": "pending",
}

# Keep only a few recent analyses per version so repeated runs cannot grow the
# database without bound.  The newest analysis is the one the pages link to.
ANALYSIS_HISTORY = 5


class DataError(Exception):
    """Validation or business error shown to the user."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | os.PathLike) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.isolation_level = None
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def init_db(path: str | os.PathLike) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = connect(path)
    try:
        connection.executescript(SCHEMA)
    finally:
        connection.close()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exception=None) -> None:
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def _in_transaction(connection: sqlite3.Connection) -> bool:
    return connection.in_transaction


def _commit(connection: sqlite3.Connection) -> None:
    connection.execute("COMMIT")


# ---------------------------------------------------------------------------
# Versions


def create_version(name: str, source: str, definition: dict) -> int:
    connection = get_db()
    created_at = utcnow()
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "INSERT INTO versions (name, source, definition, created_at)"
            " VALUES (?, ?, ?, ?)",
            (
                name,
                source,
                json.dumps(definition, ensure_ascii=False, separators=(",", ":")),
                created_at,
            ),
        )
        _commit(connection)
        return int(cursor.lastrowid)
    except Exception:
        if _in_transaction(connection):
            connection.execute("ROLLBACK")
        raise


def list_versions() -> list[dict]:
    connection = get_db()
    rows = connection.execute(
        """
        SELECT v.id, v.name, v.created_at,
               (SELECT COUNT(*) FROM records r WHERE r.version_id = v.id)
                   AS record_count
        FROM versions v
        ORDER BY v.id DESC
        """
    ).fetchall()
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "created_at": row["created_at"],
            "record_count": row["record_count"],
        }
        for row in rows
    ]


def get_version(version_id: int) -> dict | None:
    connection = get_db()
    row = connection.execute(
        "SELECT * FROM versions WHERE id = ?", (version_id,)
    ).fetchone()
    if row is None:
        return None
    record_count = connection.execute(
        "SELECT COUNT(*) FROM records WHERE version_id = ?", (version_id,)
    ).fetchone()[0]
    return {
        "id": row["id"],
        "name": row["name"],
        "source": row["source"],
        "definition": loads(row["definition"]),
        "created_at": row["created_at"],
        "record_count": record_count,
    }


def get_counts(version_id: int, item_count: int) -> list[dict]:
    connection = get_db()
    rows = connection.execute(
        "SELECT * FROM item_counts WHERE version_id = ?", (version_id,)
    ).fetchall()
    by_index = {row["item_index"]: row for row in rows}
    counts = []
    for index in range(item_count):
        row = by_index.get(index)
        entry = {category: 0 for category in CATEGORIES}
        if row is not None:
            for category in CATEGORIES:
                entry[category] = row[CATEGORY_COLUMNS[category]]
        counts.append(entry)
    return counts


# ---------------------------------------------------------------------------
# Rule coverage analyses


def _ensure_revision_row(connection: sqlite3.Connection, version_id: int) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO version_revisions (version_id, revision)"
        " VALUES (?, 0)",
        (version_id,),
    )


def get_revision(version_id: int) -> int:
    connection = get_db()
    row = connection.execute(
        "SELECT revision FROM version_revisions WHERE version_id = ?",
        (version_id,),
    ).fetchone()
    return int(row["revision"]) if row is not None else 0


def _bump_revision(connection: sqlite3.Connection, version_id: int) -> None:
    _ensure_revision_row(connection, version_id)
    connection.execute(
        "UPDATE version_revisions SET revision = revision + 1"
        " WHERE version_id = ?",
        (version_id,),
    )


def run_analysis(version_id: int) -> dict:
    """Analyse stored records under one consistent snapshot.

    Reads the definition and all records inside a single read transaction,
    and only afterwards writes the result row.  The result carries the
    revision observed while reading; the exact same revision is written back
    if no correction has happened meanwhile, so a stale snapshot can never be
    stored as current.
    """
    connection = get_db()
    try:
        connection.execute("BEGIN")
        version_row = connection.execute(
            "SELECT definition FROM versions WHERE id = ?", (version_id,)
        ).fetchone()
        if version_row is None:
            raise DataError("表版不存在。")
        definition = loads(version_row["definition"])
        revision_row = connection.execute(
            "SELECT revision FROM version_revisions WHERE version_id = ?",
            (version_id,),
        ).fetchone()
        revision = int(revision_row["revision"]) if revision_row is not None else 0
        records = [
            (row["record_no"], json.loads(row["answers"]))
            for row in connection.execute(
                "SELECT record_no, answers FROM records"
                " WHERE version_id = ? ORDER BY id",
                (version_id,),
            ).fetchall()
        ]
        connection.execute("COMMIT")
    except Exception:
        if _in_transaction(connection):
            connection.execute("ROLLBACK")
        raise

    snapshot = analyse(definition, records)
    result = dict(snapshot)
    result["version_id"] = version_id
    result["revision"] = revision

    try:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_revision_row(connection, version_id)
        current = int(
            connection.execute(
                "SELECT revision FROM version_revisions WHERE version_id = ?",
                (version_id,),
            ).fetchone()["revision"]
        )
        if current != revision:
            connection.execute("ROLLBACK")
            raise DataError(
                "分析期间有答案被更正，未保存本次结果，请重新分析。"
            )
        cursor = connection.execute(
            "INSERT INTO analyses"
            " (version_id, revision, record_count, result, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                version_id,
                revision,
                snapshot["record_count"],
                json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                utcnow(),
            ),
        )
        new_id = int(cursor.lastrowid)
        connection.execute(
            "DELETE FROM analyses WHERE version_id = ? AND id NOT IN ("
            " SELECT id FROM analyses WHERE version_id = ?"
            " ORDER BY id DESC LIMIT ?)",
            (version_id, version_id, ANALYSIS_HISTORY),
        )
        _commit(connection)
        return {"id": new_id, "result": result}
    except Exception:
        if _in_transaction(connection):
            connection.execute("ROLLBACK")
        raise


def _analyse_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "version_id": row["version_id"],
        "revision": row["revision"],
        "record_count": row["record_count"],
        "created_at": row["created_at"],
        "result": json.loads(row["result"]),
    }


def get_latest_analysis(version_id: int) -> dict | None:
    connection = get_db()
    row = connection.execute(
        "SELECT * FROM analyses WHERE version_id = ? ORDER BY id DESC LIMIT 1",
        (version_id,),
    ).fetchone()
    return _analyse_row(row) if row is not None else None


def get_analysis(version_id: int, analysis_id: int) -> dict | None:
    connection = get_db()
    row = connection.execute(
        "SELECT * FROM analyses WHERE id = ? AND version_id = ?",
        (analysis_id, version_id),
    ).fetchone()
    return _analyse_row(row) if row is not None else None


# ---------------------------------------------------------------------------
# Records / import


def _serialise_answers(answer_list: list[str]) -> str:
    return json.dumps(answer_list, ensure_ascii=False, separators=(",", ":"))


def _serialise_categories(categories: list[str]) -> str:
    return json.dumps(categories, ensure_ascii=False, separators=(",", ":"))


def import_records(
    version_id: int, rows: list[tuple[str, list[str]]], definition: dict
) -> int:
    """Import validated rows atomically; returns number of records."""
    items = definition["items"]
    connection = get_db()
    counts = [{category: 0 for category in CATEGORIES} for _ in items]
    prepared: list[tuple] = []
    stamp = utcnow()

    for record_no, answers in rows:
        answer_map = {
            index: value
            for index, value in enumerate(answers)
            if value in ("是", "否")
        }
        categories = judge(definition, answer_map)
        for index, category in enumerate(categories):
            counts[index][category] += 1
        prepared.append(
            (
                version_id,
                record_no,
                _serialise_answers(answers),
                _serialise_categories(categories),
                stamp,
            )
        )

    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT COUNT(*) FROM records WHERE version_id = ?", (version_id,)
        ).fetchone()[0]
        if existing:
            raise DataError("该表版已导入记录，不能重复导入。")

        connection.executemany(
            "INSERT INTO records"
            " (version_id, record_no, answers, categories, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            prepared,
        )
        connection.executemany(
            "INSERT INTO item_counts"
            " (version_id, item_index, answered, missing, exempt, pending)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    version_id,
                    index,
                    entry["answered"],
                    entry["missing"],
                    entry["exempt"],
                    entry["pending"],
                )
                for index, entry in enumerate(counts)
            ],
        )
        _ensure_revision_row(connection, version_id)
        _commit(connection)
        return len(prepared)
    except Exception:
        if _in_transaction(connection):
            connection.execute("ROLLBACK")
        raise


def get_record(version_id: int, record_no: str) -> dict | None:
    connection = get_db()
    row = connection.execute(
        "SELECT * FROM records WHERE version_id = ? AND record_no = ?",
        (version_id, record_no),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "version_id": row["version_id"],
        "record_no": row["record_no"],
        "answers": json.loads(row["answers"]),
        "categories": json.loads(row["categories"]),
        "updated_at": row["updated_at"],
    }


def correct_answer(
    version_id: int, record_no: str, item_index: int, value: str
) -> list[str]:
    """Change one answer, then re-judge only that record and update counts.

    Returns the new categories.  The record update and the count update share
    one transaction; on failure the exception propagates and nothing changes.
    The version's analysis revision is bumped in the same transaction so any
    existing coverage analysis is reported as stale.
    """
    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")

        version_row = connection.execute(
            "SELECT definition FROM versions WHERE id = ?", (version_id,)
        ).fetchone()
        if version_row is None:
            raise DataError("表版不存在。")
        definition = loads(version_row["definition"])

        row = connection.execute(
            "SELECT answers, categories FROM records"
            " WHERE version_id = ? AND record_no = ?",
            (version_id, record_no),
        ).fetchone()
        if row is None:
            raise DataError("记录号不存在。")

        if not 0 <= item_index < len(definition["items"]):
            raise DataError("检查项不存在。")
        if value not in ("是", "否", ""):
            raise DataError("答案只能是是、否或留空。")

        answers = json.loads(row["answers"])
        old_categories = json.loads(row["categories"])
        while len(answers) < len(definition["items"]):
            answers.append("")
        answers[item_index] = value

        answer_map = {
            index: answer
            for index, answer in enumerate(answers)
            if answer in ("是", "否")
        }
        new_categories = judge(definition, answer_map)

        connection.execute(
            "UPDATE records SET answers = ?, categories = ?, updated_at = ?"
            " WHERE version_id = ? AND record_no = ?",
            (
                _serialise_answers(answers),
                _serialise_categories(new_categories),
                utcnow(),
                version_id,
                record_no,
            ),
        )

        for index, (old, new) in enumerate(zip(old_categories, new_categories)):
            if old == new:
                continue
            connection.execute(
                f"UPDATE item_counts SET {CATEGORY_COLUMNS[old]} ="
                f" {CATEGORY_COLUMNS[old]} - 1, {CATEGORY_COLUMNS[new]} ="
                f" {CATEGORY_COLUMNS[new]} + 1"
                " WHERE version_id = ? AND item_index = ?",
                (version_id, index),
            )

        _bump_revision(connection, version_id)

        _commit(connection)
        return new_categories
    except Exception:
        if _in_transaction(connection):
            connection.execute("ROLLBACK")
        raise
