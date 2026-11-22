"""The four public MCP tools of the Annex Library service.

Ticket ``annex-library-crx.1`` ships ``annex-lib-search``. The remaining tools
are declared here so the service has one stable tool surface; they are
implemented by the follow-up tickets (``.2`` check, ``.3`` save, ``.4`` edit /
related versions).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import PurePosixPath

from .catalog import annex_present_paths, probe_local_present
from .index import Index, Query, lookup_by_path, refresh_local_state, search
from .normalize import parse_iso_date
from .refresh import IndexManager
from .validation import check_catalog
from .writes import WriteError, edit_record, save_record

TOOL_SEARCH = "annex-lib-search"
TOOL_SAVE = "annex-lib-save"
TOOL_EDIT = "annex-lib-edit"
TOOL_CHECK = "annex-lib-check"


class ToolError(Exception):
    """Error that is safe to show to an MCP caller."""

    def __init__(self, message: str, *, code: str = "error", detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


def _iso_date(value: str | None, field: str) -> str | None:
    if not value:
        return None
    parsed = parse_iso_date(value)
    if parsed is None:
        raise ToolError(f"{field} is not an ISO date: {value!r}", code="invalid_argument")
    return parsed.isoformat()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------- #
# annex-lib-search
# --------------------------------------------------------------------------- #


def annex_lib_search(
    manager: IndexManager,
    *,
    doi: str | None = None,
    title: str | None = None,
    author: str | None = None,
    tags: list[str] | None = None,
    captured_from: str | None = None,
    captured_to: str | None = None,
    collection: str | None = None,
    text: str | None = None,
    limit: int = 20,
    offset: int = 0,
    include_local_status: bool = False,
    include_attachments: bool = True,
) -> dict:
    """Search the catalog and return works with their versions and attachments."""
    if not any([doi, title, author, tags, captured_from, captured_to, collection, text]):
        raise ToolError(
            "at least one search criterion is required "
            "(doi, title, author, tags, captured_from/captured_to, collection or text)",
            code="invalid_argument",
        )
    if limit < 1 or limit > 100:
        raise ToolError("limit must be between 1 and 100", code="invalid_argument")
    if offset < 0:
        raise ToolError("offset must be >= 0", code="invalid_argument")

    date_from = _iso_date(captured_from, "captured_from")
    date_to = _iso_date(captured_to, "captured_to")
    if date_from and date_to and date_from > date_to:
        raise ToolError("captured_from is after captured_to", code="invalid_argument")

    index = manager.index
    manager.ensure_fresh()

    query = Query(
        doi=doi,
        title=title,
        author=author,
        tags=[tag for tag in (tags or []) if tag and tag.strip()],
        captured_from=date_from,
        captured_to=date_to,
        collection=collection,
        text=text,
        limit=limit,
        offset=offset,
        include_local=include_local_status,
    )
    started = time.perf_counter()
    with index.reading():
        works, summary = search(index, query)
        revision = index.meta("library_revision")
        built_at = index.meta("built_at")
    elapsed_ms = (time.perf_counter() - started) * 1000

    if include_local_status and include_attachments:
        attachments = [attachment for work in works for version in work.versions
                       for attachment in version.get("attachments", [])]
        root = index.library_root
        try:
            present = annex_present_paths(root, sorted({a["path"] for a in attachments if a["annex_tracked"]}))
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            raise ToolError("Unable to query git-annex availability", code="annex_status_failed") from exc
        for attachment in attachments:
            path = attachment["path"]
            available = path in present if attachment["annex_tracked"] else probe_local_present(root / path)
            attachment["local_content"] = "present" if available else "missing"
    if not include_attachments:
        for work in works:
            for version in work.versions:
                version.pop("attachments", None)

    payload = {
        "query": summary["filters"],
        "total_works": summary["total_works"],
        "returned": summary["returned"],
        "offset": summary["offset"],
        "limit": summary["limit"],
        "works": [work.as_dict() for work in works],
        "index": {
            "generation": manager.state.generation,
            "library_revision": revision,
            "built_at": built_at,
            "local_status": "requested" if include_local_status else "not requested",
        },
        "took_ms": round(elapsed_ms, 2),
    }
    if include_local_status and include_attachments:
        payload["index"]["local_status_method"] = "git-annex batch for the returned page"
    return payload


def annex_lib_locate_attachments(
    manager: IndexManager,
    *,
    path: str | None = None,
    entry_id: str | None = None,
    refresh_local: bool = False,
) -> dict:
    """Return tracked attachments for one bundle and their local availability."""
    if not path and not entry_id:
        raise ToolError("path or entry_id is required", code="invalid_argument")
    manager.ensure_fresh()
    with manager.index.reading():
        record = lookup_by_path(manager.index, (path or entry_id or "").rstrip("/"))
        if record is None:
            raise ToolError(f"no catalog record for {path or entry_id!r}", code="not_found")
        if refresh_local:
            refresh_local_state(manager.index, [a["path"] for a in record.get("attachments", [])])
            record = lookup_by_path(manager.index, (path or entry_id or "").rstrip("/")) or record
    attachments = record.get("attachments", [])
    present = sum(1 for a in attachments if a.get("local_content") == "present")
    missing = [
        a for a in attachments
        if a.get("local_content") == "missing" or (a.get("annex_tracked") and not a.get("local_content"))
    ]
    return {
        "entry_id": record["entry_id"],
        "record_path": record["record_path"],
        "bundle": record["bundle"],
        "revision": record["revision"],
        "attachments": attachments,
        "summary": {
            "tracked": len(attachments),
            "local_present": present,
            "local_missing": len(attachments) - present,
        },
        "missing": missing,
    }


# --------------------------------------------------------------------------- #
# Placeholders implemented by later tickets
# --------------------------------------------------------------------------- #


def annex_lib_check(manager: IndexManager, *, path: str | None = None) -> dict:
    selected = PurePosixPath(path or ".")
    if selected.is_absolute() or ".." in selected.parts or "\x00" in str(selected):
        raise ToolError("path must be relative to the Library checkout", code="invalid_argument")
    root = manager.index.library_root
    files, diagnostics = check_catalog(root)
    prefix = selected.as_posix()
    if prefix != ".":
        if not (root / selected).exists() and not any(p == prefix or p.startswith(prefix + "/") for p in files):
            raise ToolError(f"No such archive path: {prefix}", code="not_found")
        diagnostics = [d for d in diagnostics if d["path"] == prefix or d["path"].startswith(prefix + "/")]
    return {
        "path": prefix,
        "ok": not any(d["severity"] == "error" for d in diagnostics),
        "diagnostics": diagnostics,
    }


def annex_lib_save(
    manager: IndexManager, *, metadata: dict, staged_files: list[str] | None = None, request_id: str | None = None
) -> dict:
    try:
        return save_record(manager, metadata, staged_files, request_id)
    except WriteError as exc:
        raise ToolError(str(exc), code=exc.code, detail=exc.detail) from exc


def annex_lib_edit(
    manager: IndexManager, *, path: str, revision: str, field_updates: dict, request_id: str | None = None
) -> dict:
    try:
        return edit_record(manager, path, revision, field_updates, request_id)
    except WriteError as exc:
        raise ToolError(str(exc), code=exc.code, detail=exc.detail) from exc
