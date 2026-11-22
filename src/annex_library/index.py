"""Rebuildable SQLite index for the Annex Library catalog.

The index is a *cache*: it lives outside the checkout, it can be deleted at any
time, and a rebuild from the checkout must produce equivalent query results
(``tests/test_search.py::test_delete_and_rebuild_is_equivalent`` asserts this).

Schema notes
------------
* ``entries`` keeps one row per catalog record (glance / Misc note / video
  summary) or attachment-only bundle, with normalised columns for every search
  predicate (``doi_norm``, ``title_norm``, ``authors_norm``, ``tags_norm``,
  ``captured_date``).
* ``entry_tags`` holds one row per tag so exact-tag AND queries are a
  ``EXISTS`` per tag instead of a scan of the JSON column.
* ``attachments`` keeps the full tracked file list per bundle, which is why
  tracked attachments with an annex *placeholder* can still be reported.
* Search uses deterministic SQL predicates (exact DOI, term AND, inclusive date
  range, exact tags) so ranking is stable; there is no full-text index, because
  normalised ``LIKE`` predicates over ~2.3k rows are already sub-millisecond and
  an FTS copy would only add a second thing to keep in sync.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from .catalog import Attachment, ScanResult, Scanner, catalog_scope, head_revision
from .normalize import normalize_doi, normalize_tag, normalize_text

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    entry_id TEXT NOT NULL UNIQUE,
    collection TEXT NOT NULL,
    kind TEXT NOT NULL,
    bundle TEXT NOT NULL,
    record_path TEXT,
    revision TEXT NOT NULL,
    revision_kind TEXT NOT NULL,
    title TEXT NOT NULL,
    title_norm TEXT NOT NULL,
    title_source TEXT NOT NULL,
    doi TEXT NOT NULL,
    doi_norm TEXT NOT NULL,
    work_id TEXT NOT NULL,
    version_type TEXT NOT NULL,
    journal TEXT NOT NULL,
    journal_norm TEXT NOT NULL,
    published TEXT,
    captured TEXT,
    captured_date TEXT,
    tags_json TEXT NOT NULL,
    tags_norm TEXT NOT NULL,
    authors_json TEXT NOT NULL,
    authors_norm TEXT NOT NULL,
    identifiers_json TEXT NOT NULL,
    extra_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    searchable INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS entry_tags (
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    tag_norm TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (entry_id, tag_norm)
);
CREATE INDEX IF NOT EXISTS entry_tags_tag ON entry_tags(tag_norm);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    entry_pk INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    annex_tracked INTEGER NOT NULL DEFAULT 0,
    local_present INTEGER NOT NULL DEFAULT 1,
    size INTEGER,
    UNIQUE (entry_pk, path)
);
CREATE INDEX IF NOT EXISTS attachments_path ON attachments(path);

CREATE TABLE IF NOT EXISTS diagnostics (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS entries_doi ON entries(doi_norm);
CREATE INDEX IF NOT EXISTS entries_captured ON entries(captured_date);
CREATE INDEX IF NOT EXISTS entries_collection ON entries(collection, kind);
CREATE INDEX IF NOT EXISTS entries_work ON entries(work_id);

"""


@dataclass
class IndexStats:
    entries: int
    attachments: int
    diagnostics: int
    duration_ms: float
    revision: str


class Index:
    """Owns the SQLite file and the build/refresh lifecycle."""

    def __init__(self, path: Path, library_root: Path) -> None:
        self.path = path
        self.library_root = library_root
        self._conn: sqlite3.Connection | None = None
        self._conn_lock = threading.RLock()

    # -- connection -------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self.path), timeout=30.0, check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=30000")
        return self._conn

    def close(self) -> None:
        with self._conn_lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        with self._conn_lock:
            yield self.conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._conn_lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    # -- lifecycle --------------------------------------------------------

    def exists(self) -> bool:
        return self.path.is_file()

    def meta(self, key: str) -> str | None:
        try:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return None
        return row[0] if row else None

    def build(self, *, files: dict[str, str] | None = None) -> IndexStats:
        """Rebuild the index from scratch, atomically.

        The old file is kept until the new one is complete: the build writes to
        a temporary sibling and swaps it in, so a failed build never leaves the
        service without an index.
        """
        started = time.perf_counter()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".building")
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(tmp_path) + suffix)
            if candidate.exists():
                candidate.unlink()
        scan = Scanner(self.library_root).scan(files)

        conn = sqlite3.connect(str(tmp_path))
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute("PRAGMA synchronous=OFF")
            conn.executescript(SCHEMA)
            counts = _populate(conn, scan)
            revision = scan.revision or head_revision(self.library_root)
            meta = {
                "schema_version": str(SCHEMA_VERSION),
                "library_root": str(self.library_root),
                "library_revision": revision,
                "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "entry_count": str(counts[0]),
                "attachment_count": str(counts[1]),
                "diagnostic_count": str(counts[2]),
                "local_state": "probe",
            }
            conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", meta.items())
            conn.commit()
        finally:
            conn.close()
        with self._conn_lock:
            self.close()
            os.replace(tmp_path, self.path)
        stats = IndexStats(
            entries=counts[0],
            attachments=counts[1],
            diagnostics=counts[2],
            duration_ms=(time.perf_counter() - started) * 1000,
            revision=revision,
        )
        return stats

    def refresh(self, files: dict[str, str], scopes: set[str], revision: str) -> IndexStats:
        """Publish only changed bundles, including Misc identifier peers."""
        started = time.perf_counter()
        misc_changed = any(scope.startswith("Misc/") for scope in scopes)
        selected = {
            path: oid for path, oid in files.items()
            if catalog_scope(path) in scopes or (misc_changed and path.startswith("Misc/"))
        }
        scan = Scanner(self.library_root).scan(selected)
        with self.transaction() as conn:
            if misc_changed:
                conn.execute("DELETE FROM entries WHERE collection = 'Misc'")
                conn.execute("DELETE FROM diagnostics WHERE path LIKE 'Misc/%'")
            for scope in sorted(scopes):
                conn.execute("DELETE FROM entries WHERE bundle = ? OR record_path = ?", (scope, scope))
                conn.execute("DELETE FROM diagnostics WHERE path = ?", (scope,))
            _populate(conn, scan)
            counts = tuple(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                           for table in ("entries", "attachments", "diagnostics"))
            meta = {
                "library_revision": revision,
                "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "entry_count": str(counts[0]),
                "attachment_count": str(counts[1]),
                "diagnostic_count": str(counts[2]),
            }
            conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", meta.items())
        return IndexStats(*counts, (time.perf_counter() - started) * 1000, revision)

    def ensure(self) -> IndexStats | None:
        """Build the index when missing or built for a different checkout."""
        if not self.exists():
            return self.build()
        if self.meta("schema_version") != str(SCHEMA_VERSION):
            return self.build()
        if self.meta("library_root") != str(self.library_root):
            return self.build()
        return None


def _populate(conn: sqlite3.Connection, scan: ScanResult) -> tuple[int, int, int]:
    entry_rows: list[tuple] = []
    tag_rows: list[tuple] = []
    attachment_rows: list[tuple] = []

    first_pk = conn.execute("SELECT coalesce(max(id), 0) + 1 FROM entries").fetchone()[0]
    for index, entry in enumerate(scan.entries, start=first_pk):
        doi_norm = entry.doi_norm
        entry_rows.append(
            (
                index,
                entry.entry_id,
                entry.collection,
                entry.kind,
                entry.bundle,
                entry.record_path,
                entry.revision,
                entry.revision_kind,
                entry.title,
                entry.title_norm,
                entry.title_source,
                entry.doi,
                doi_norm,
                entry.work_id,
                entry.version_type,
                entry.journal,
                normalize_text(entry.journal),
                entry.published,
                entry.captured,
                entry.captured_date,
                json.dumps(entry.tags, ensure_ascii=False),
                "|".join(entry.tag_norms),
                json.dumps(entry.authors, ensure_ascii=False),
                entry.authors_norm,
                json.dumps(entry.identifiers, ensure_ascii=False, sort_keys=True),
                json.dumps(entry.extra, ensure_ascii=False, sort_keys=True),
                json.dumps(entry.warnings, ensure_ascii=False),
                1,
            )
        )
        for position, tag in enumerate(entry.tag_norms):
            tag_rows.append((index, tag, position))
        for attachment in entry.attachments:
            attachment_rows.append(
                (
                    index,
                    attachment.path,
                    attachment.path.rsplit("/", 1)[-1],
                    attachment.role,
                    int(attachment.annex_tracked),
                    int(attachment.local_present),
                    attachment.size,
                )
            )
        identifiers_text = " ".join(
            f"{key} {value}" for key, value in sorted(entry.identifiers.items())
        )

    conn.executemany(
        "INSERT INTO entries VALUES (" + ",".join("?" * 28) + ")",
        entry_rows,
    )
    conn.executemany("INSERT OR IGNORE INTO entry_tags VALUES (?, ?, ?)", tag_rows)
    conn.executemany("INSERT INTO attachments VALUES (NULL, ?, ?, ?, ?, ?, ?, ?)", attachment_rows)
    conn.executemany(
        "INSERT INTO diagnostics(path, severity, message) VALUES (?, ?, ?)",
        [(d["path"], d["severity"], d["message"]) for d in scan.diagnostics],
    )
    return len(entry_rows), len(attachment_rows), len(scan.diagnostics)


# --------------------------------------------------------------------------- #
# Query layer
# --------------------------------------------------------------------------- #


@dataclass
class Query:
    doi: str | None = None
    title: str | None = None
    author: str | None = None
    tags: Sequence[str] = ()
    captured_from: str | None = None
    captured_to: str | None = None
    collection: str | None = None
    text: str | None = None
    limit: int = 20
    offset: int = 0
    include_local: bool = False
    include_notes: bool = False
    include_derived: bool = True


@dataclass
class WorkResult:
    work_id: str
    matched_version: str
    versions: list[dict]

    def as_dict(self) -> dict:
        return {
            "work_id": self.work_id,
            "matched_version": self.matched_version,
            "versions": self.versions,
        }


def search(index: Index, query: Query) -> tuple[list[WorkResult], dict]:
    """Run one search and return grouped works plus a query summary."""
    clauses: list[str] = []
    params: list[object] = []

    if query.doi:
        clauses.append("idx.doi_norm = ?")
        params.append(normalize_doi(query.doi))
    if query.title:
        terms = [term for term in normalize_text(query.title).split() if term]
        for term in terms:
            clauses.append("idx.title_norm LIKE ?")
            params.append(f"%{term}%")
    if query.author:
        terms = [term for term in normalize_text(query.author).split() if term]
        for term in terms:
            clauses.append("idx.authors_norm LIKE ?")
            params.append(f"%{term}%")
    if query.tags:
        for tag in query.tags:
            clauses.append(
                "EXISTS (SELECT 1 FROM entry_tags t WHERE t.entry_id = idx.id AND t.tag_norm = ?)"
            )
            params.append(normalize_tag(tag))
    if query.captured_from:
        clauses.append("idx.captured_date IS NOT NULL AND idx.captured_date >= ?")
        params.append(query.captured_from)
    if query.captured_to:
        clauses.append("idx.captured_date IS NOT NULL AND idx.captured_date <= ?")
        params.append(query.captured_to)
    if query.collection:
        collections = [part.strip() for part in query.collection.split(",") if part.strip()]
        clauses.append("idx.collection IN (" + ",".join("?" * len(collections)) + ")")
        params.extend(collections)
    if not query.include_notes:
        clauses.append("idx.kind NOT IN ('note')")
    if not query.include_derived:
        clauses.append("idx.kind IN ('glance','misc-note','misc-legacy','video-summary')")
    if query.text:
        terms = [term for term in normalize_text(query.text).split() if term]
        for term in terms:
            clauses.append(
                "(idx.title_norm LIKE ? OR idx.authors_norm LIKE ? OR idx.tags_norm LIKE ?)"
            )
            params.extend([f"%{term}%"] * 3)

    where = " AND ".join(clauses) if clauses else "1"
    base_sql = f"""
        SELECT * FROM entries idx
        WHERE {where}
        ORDER BY (idx.kind = 'glance') DESC, idx.captured_date DESC, idx.title COLLATE NOCASE, idx.entry_id
    """
    rows = index.conn.execute(base_sql, params).fetchall()

    # A work matches when any of its versions matches; when searching by DOI the
    # other versions of the same work are returned too, and the matched one is
    # identified explicitly.
    grouped: dict[str, list[sqlite3.Row]] = {}
    order: list[str] = []
    matched: dict[str, sqlite3.Row] = {}
    for row in rows:
        key = row["work_id"] or row["entry_id"]
        if key not in grouped:
            order.append(key)
            matched[key] = row
        grouped.setdefault(key, []).append(row)

    # Fill in sibling versions of a matched work (versions that did not match
    # the predicate themselves, e.g. searching the journal DOI).
    work_keys = list(grouped)
    if work_keys:
        placeholders = ",".join("?" * len(work_keys))
        siblings = index.conn.execute(
            f"""
            SELECT * FROM entries
            WHERE COALESCE(NULLIF(work_id, ''), entry_id) IN ({placeholders})
            ORDER BY (kind = 'glance') DESC, captured_date DESC, title COLLATE NOCASE, entry_id
            """,
            work_keys,
        ).fetchall()
        for row in siblings:
            key = row["work_id"] or row["entry_id"]
            if all(existing["entry_id"] != row["entry_id"] for existing in grouped.get(key, [])):
                grouped[key].append(row)

    total = len(grouped)
    page = order[query.offset: query.offset + query.limit]
    results: list[WorkResult] = []
    for key in page:
        versions = [_row_to_dict(index, row, query.include_local) for row in grouped[key]]
        matched_id = matched[key]["entry_id"]
        results.append(
            WorkResult(work_id=key, matched_version=matched_id, versions=versions)
        )
    summary = {
        "total_works": total,
        "returned": len(results),
        "offset": query.offset,
        "limit": query.limit,
        "filters": {
            "doi": query.doi,
            "title": query.title,
            "author": query.author,
            "tags": list(query.tags),
            "captured_from": query.captured_from,
            "captured_to": query.captured_to,
            "collection": query.collection,
            "text": query.text,
        },
    }
    return results, summary


def _row_to_dict(index: Index, row: sqlite3.Row, include_local: bool) -> dict:
    from .catalog import Attachment

    attachments = _attachments_for(index, row["id"], include_local)
    data = {
        "entry_id": row["entry_id"],
        "collection": row["collection"],
        "kind": row["kind"],
        "bundle": row["bundle"],
        "record_path": row["record_path"],
        "revision": row["revision"],
        "revision_kind": row["revision_kind"],
        "title": row["title"],
        "doi": row["doi"],
        "work_id": row["work_id"],
        "version_type": row["version_type"],
        "journal": row["journal"],
        "published": row["published"],
        "captured": row["captured"],
        "tags": json.loads(row["tags_json"]),
        "authors": json.loads(row["authors_json"]),
        "identifiers": json.loads(row["identifiers_json"]),
        "extra": json.loads(row["extra_json"]),
        "attachments": attachments,
    }
    warnings = json.loads(row["warnings_json"])
    if warnings:
        data["warnings"] = warnings
    return data


def _attachments_for(index: Index, entry_pk: int, include_local: bool) -> list[dict]:
    rows = index.conn.execute(
        "SELECT path, name, role, annex_tracked, local_present, size FROM attachments "
        "WHERE entry_pk=? ORDER BY path",
        (entry_pk,),
    ).fetchall()
    result = []
    for row in rows:
        item = {
            "path": row["path"],
            "name": row["name"],
            "role": row["role"],
            "annex_tracked": bool(row["annex_tracked"]),
        }
        if row["size"] is not None:
            item["size"] = row["size"]
        if include_local:
            item["local_content"] = "present" if row["local_present"] else "missing"
        result.append(item)
    return result


def lookup_by_path(index: Index, rel_path: str) -> dict | None:
    row = index.conn.execute(
        "SELECT idx.* FROM entries idx WHERE idx.record_path = ? "
        "OR idx.bundle = ? OR idx.entry_id = ?",
        (rel_path, rel_path, rel_path),
    ).fetchone()
    return _row_to_dict(index, row, True) if row else None


def refresh_local_state(index: Index, paths: Sequence[str]) -> int:
    """Update attachment availability for the given paths in one transaction."""
    from .catalog import probe_local_present

    updated = 0
    with index.transaction() as conn:
        for rel_path in paths:
            present = int(probe_local_present(index.library_root / rel_path))
            cursor = conn.execute(
                "UPDATE attachments SET local_present=? WHERE path=?",
                (present, rel_path),
            )
            updated += cursor.rowcount
    return updated
