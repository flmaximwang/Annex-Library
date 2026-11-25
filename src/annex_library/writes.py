"""Scoped Git writes and replay for MCP catalog operations."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import yaml

from .catalog import (
    COLLECTIONS,
    VIDEO_COLLECTIONS,
    Scanner,
    catalog_scope,
    catalog_snapshot,
    is_glance_name,
    split_frontmatter,
)
from .normalize import normalize_doi, parse_iso_date
from .refresh import IndexManager
from .validation import (
    DOI,
    ISBN_NAME,
    canonical_isbn,
    isbn_digits,
    validate_metadata,
)


class WriteError(Exception):
    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass
class WritePlan:
    locator: dict
    records: dict[str, bytes] = field(default_factory=dict)
    attachments: dict[str, Path] = field(default_factory=dict)
    texts: dict[str, Path] = field(default_factory=dict)


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", "--literal-pathspecs", "-C", str(root), *args], capture_output=True)
    if check and result.returncode:
        raise WriteError("git_failed", f"git {args[0]} failed: {result.stderr.decode(errors='replace').strip()}")
    return result


def _json(data: object) -> bytes:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def _digest(path: Path, algorithm: str = "sha256") -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, algorithm).hexdigest()


def _ensure_safe(root: Path, relative: str) -> Path:
    dest = root / relative
    if not dest.is_relative_to(root) or ".." in Path(relative).parts or any(
        component.is_symlink() for component in (dest.parent, *dest.parent.parents) if component.is_relative_to(root)
    ):
        raise WriteError("conflict", f"Unsafe archive path: {relative}")
    return dest


def _clean_path(root: Path, path: str) -> None:
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", path).stdout
    target = _ensure_safe(root, path)
    tracked = _git(root, "ls-files", "-z", "--", path).stdout
    if status or ((target.exists() or target.is_symlink()) and not tracked):
        raise WriteError("conflict", f"Archive path has local changes: {path}", path=path)


def _committed(root: Path, request_key: str, payload: str) -> dict | None:
    marker = f"Annex-Library-Request: {request_key}"
    history = _git(root, "log", "--first-parent", "-z", "--format=%H%x00%B", "--fixed-strings", f"--grep={marker}").stdout
    parts = history.decode().split("\0")
    for sha, message in zip(parts[::2], parts[1::2]):
        lines = message.splitlines()
        if marker not in lines:
            continue
        if f"Annex-Library-Payload: {payload}" not in lines:
            raise WriteError("conflict", "request_id was already committed with a different payload")
        encoded = next(line.removeprefix("Annex-Library-Locator: ") for line in lines if line.startswith("Annex-Library-Locator: "))
        return {"commit_sha": sha, **json.loads(base64.urlsafe_b64decode(encoded))}
    return None


def _publish(manager: IndexManager, result: dict, *, replayed: bool) -> dict:
    try:
        manager.refresh_now()
    except Exception as exc:
        return {"status": "committed_index_pending", "replayed": replayed, **result, "index_error": str(exc)}
    return {"status": "committed", "replayed": replayed, **result}


def _save_attachments_md(name: str) -> bool:
    """Markdown attachments are Git text objects, not annex content."""
    return name.casefold().endswith(".md")


def execute_write(
    manager: IndexManager,
    operation: str,
    request_id: str | None,
    payload: dict,
    prepare: Callable[[Path], WritePlan],
) -> dict:
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 256:
        raise WriteError("invalid_argument", "A nonempty request_id of at most 256 characters is required")
    root = manager.index.library_root
    git_dir = Path(os.fsdecode(_git(root, "rev-parse", "--absolute-git-dir").stdout.rstrip(b"\n")))
    if _git(root, "rev-parse", "--show-prefix").stdout.strip():
        raise WriteError("invalid_argument", "Archive writes require the root of a Git checkout")
    request_key = hashlib.sha256(request_id.encode()).hexdigest()
    try:
        payload_digest = hashlib.sha256(_json({"operation": operation, **payload})).hexdigest()
    except (ValueError, TypeError) as exc:
        raise WriteError("invalid_argument", "The request must contain JSON-compatible values") from exc

    with (git_dir / "annex-library-write.lock").open("a+b") as lock, manager._lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        replay = _committed(root, request_key, payload_digest)
        if replay is not None:
            return _publish(manager, replay, replayed=True)
        if any((git_dir / name).exists() for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")):
            raise WriteError("conflict", "Finish the active Git operation before writing the archive")
        if _git(root, "ls-files", "--unmerged").stdout:
            raise WriteError("conflict", "Resolve the unmerged Git index before writing the archive")
        plan = prepare(root)
        if not plan.records and not plan.attachments and not plan.texts:
            manager.refresh_now()
            return {"status": "unchanged", "commit_sha": None, **plan.locator}
        sha = _commit(root, plan, operation, request_key, payload_digest)
        return _publish(manager, {"commit_sha": sha, **plan.locator}, replayed=False)


def _commit(root: Path, plan: WritePlan, operation: str, request_key: str, payload: str) -> str:
    paths = sorted(plan.records.keys() | plan.attachments.keys() | plan.texts.keys())
    backups: dict[str, bytes | None] = {}
    made_dirs = set()
    for path in paths:
        _clean_path(root, path)
        dest = root / path
        if dest.is_symlink() or (dest.exists() and not stat.S_ISREG(dest.stat().st_mode)):
            raise WriteError("conflict", f"Expected a regular archive file at {path}")
        backups[path] = dest.read_bytes() if dest.exists() else None

    touched = []
    before_head = _git(root, "rev-parse", "HEAD").stdout
    encoded = base64.urlsafe_b64encode(_json(plan.locator)).decode()
    message = (
        f"Annex Library: {operation}\n\n"
        f"Annex-Library-Request: {request_key}\n"
        f"Annex-Library-Payload: {payload}\n"
        f"Annex-Library-Locator: {encoded}"
    )
    try:
        for relative in paths:
            dest = root / relative
            for folder in dest.parents:
                if folder == root or folder.exists():
                    break
                made_dirs.add(folder)
            dest.parent.mkdir(parents=True, exist_ok=True)
            touched.append(relative)
            if relative in plan.records:
                dest.write_bytes(plan.records[relative])
            elif relative in plan.texts:
                shutil.copyfile(plan.texts[relative], dest)
            else:
                shutil.copyfile(plan.attachments[relative], dest)
        if plan.records:
            _git(root, "add", "--", *plan.records)
        if plan.texts:
            _git(root, "add", "--", *plan.texts)
        if plan.attachments:
            _git(root, "annex", "add", "--force-large", "--", *plan.attachments)
            for relative in plan.attachments:
                if not _git(root, "annex", "lookupkey", "--", relative).stdout.strip():
                    raise WriteError("git_failed", f"Attachment was not staged in git-annex: {relative}")
        _git(root, "commit", "--only", "-m", message, "--", *paths)
    except BaseException as exc:
        committed = _committed(root, request_key, payload)
        if committed is not None:
            return committed["commit_sha"]
        if touched:
            if _git(root, "rev-parse", "HEAD").stdout != before_head:
                raise WriteError("recovery_required", "HEAD changed while the archive write failed", paths=touched) from exc
            rollback = _git(root, "reset", "--quiet", "HEAD", "--", *touched, check=False)
            try:
                if rollback.returncode:
                    raise OSError(rollback.stderr.decode(errors="replace"))
                for relative in touched:
                    dest = root / relative
                    previous = backups[relative]
                    if previous is None:
                        dest.unlink(missing_ok=True)
                    else:
                        dest.write_bytes(previous)
                for folder in sorted(made_dirs, key=lambda path: len(path.parts), reverse=True):
                    if folder.is_dir() and not any(folder.iterdir()):
                        folder.rmdir()
            except OSError as cleanup:
                raise WriteError("recovery_required", str(cleanup), paths=touched) from exc
        if isinstance(exc, OSError):
            raise WriteError("write_failed", str(exc)) from exc
        raise
    return _git(root, "rev-parse", "HEAD").stdout.decode().strip()


def _slug(value: str, default: str) -> str:
    words = re.findall(r"[^\W_]+", value, flags=re.UNICODE)
    return "-".join(words)[:60].rstrip("-") or default


def render_record(fields: dict, body: str) -> bytes:
    frontmatter = yaml.safe_dump(fields, allow_unicode=True, sort_keys=False)
    return f"---\n{frontmatter}---\n{body}".encode()


def _existing_attachment_matches(root: Path, path: str, staged: Path) -> bool:
    key = _git(root, "annex", "lookupkey", "--", path, check=False).stdout.decode().strip()
    match = re.match(r"(SHA(?:1|224|256|384|512))E?(?:-s\d+)?--([0-9a-f]+)(?:\.|$)", key)
    if match:
        return _digest(staged, match[1].lower()) == match[2]
    if key:
        raise WriteError("conflict", f"Cannot verify content equality for annex key {key}", path=path)
    original = root / path
    return original.is_file() and not original.is_symlink() and _digest(original) == _digest(staged)


def save_record(manager: IndexManager, metadata: dict, staged_files: list[str] | None, request_id: str | None) -> dict:
    fields = dict(metadata)
    collection = fields.pop("collection", None)
    body = fields.pop("body", None)
    if collection is None:
        if "doi" in fields:
            collection = "DOI"
        elif "isbn" in fields:
            collection = "ISBN"
        elif "video_id" in fields:
            raise WriteError(
                "invalid_argument",
                "metadata.collection is required for the video collections",
            )
        elif isinstance(body, str):
            collection = "Misc"
        else:
            raise WriteError("invalid_argument", "metadata.collection is required for this save")
    if not isinstance(collection, str) or collection not in COLLECTIONS:
        raise WriteError("invalid_argument", f"Save supports the collections: {', '.join(COLLECTIONS)}")
    if not isinstance(body, str) or not body.strip():
        raise WriteError("invalid_argument", "metadata.body must be a nonempty Markdown string")
    if "id" in fields:
        raise WriteError("invalid_argument", "The service allocates Misc and SelfHosted IDs")
    body = body if body.endswith("\n") else body + "\n"
    fields.setdefault("stage", "library")
    if collection == "DOI":
        doi = fields.get("doi")
        normalized = normalize_doi(doi) if isinstance(doi, str) else ""
        if not DOI.fullmatch(normalized) or "\\" in normalized or "\x00" in normalized:
            raise WriteError("invalid_argument", "metadata.doi must be a valid archive DOI")
        fields["doi"] = normalized
    elif collection == "ISBN":
        isbn = canonical_isbn(fields.get("isbn"))
        if isbn is None:
            raise WriteError("invalid_argument", "metadata.isbn must be a valid ISBN-10 or ISBN-13")
        fields["isbn"] = isbn
    elif collection in VIDEO_COLLECTIONS:
        video_id = fields.get("video_id")
        if not isinstance(video_id, str) or not _valid_video_id(video_id):
            raise WriteError("invalid_argument", "metadata.video_id must be a platform identifier without separators")

    with tempfile.TemporaryDirectory(prefix="annex-library-stage-") as temp:
        staged = {}
        for raw in staged_files or []:
            source = Path(raw)
            if not source.is_absolute() or not source.is_file():
                raise WriteError("invalid_argument", f"Expected an absolute path to a readable staged file: {raw}")
            if source.name.startswith(".") or source.name in {".", ".."} or any(ord(ch) < 32 for ch in source.name):
                raise WriteError("invalid_argument", f"Invalid staged attachment name: {source.name}")
            if source.name in staged:
                raise WriteError("conflict", f"Duplicate staged attachment name: {source.name}")
            copy = Path(temp) / source.name
            try:
                shutil.copyfile(source, copy)
            except OSError as exc:
                raise WriteError("invalid_argument", f"Cannot read staged attachment: {raw}") from exc
            staged[source.name] = copy
        encoded_payload = {
            "collection": collection, "fields": fields, "body": body,
            "attachments": {name: _digest(path) for name, path in staged.items()},
        }
        return execute_write(manager, "save", request_id, encoded_payload,
                             lambda root: _prepare_save(root, collection, fields, body, staged))


def _valid_video_id(value: str) -> bool:
    return (
        bool(value)
        and value not in {".", ".."}
        and not any(char in value for char in "/\\\x00")
        and not any(char.isspace() for char in value)
    )


def _record_plan(root: Path, plan: WritePlan, record: str, fields: dict, body: str) -> None:
    """Add the record when absent, or reuse an equivalent one."""
    dest = _ensure_safe(root, record)
    if not (dest.exists() or dest.is_symlink()):
        plan.records[record] = render_record(fields, body)
        return
    _clean_path(root, record)
    if not dest.is_file() or dest.is_symlink():
        raise WriteError("conflict", f"Expected Markdown at {record}")
    current_fields, current_body, error = split_frontmatter(dest.read_text())
    if error or current_fields is None or current_body != body:
        # YAML date scalars may deserialize as date objects in existing records.
        equivalent = current_fields is not None and all(
            str(current_fields.get(key)) == str(value) if key in {"published", "captured"} else current_fields.get(key) == value
            for key, value in fields.items()
        )
        if error or not equivalent or current_body != body:
            raise WriteError("conflict", "A different record already exists at this archive path", record_path=record)


def _plan_attachments(
    root: Path, plan: WritePlan, prefix: str, staged: dict[str, Path], *, record: str
) -> None:
    """Plan staged files: markdown as Git text, everything else as annex content."""
    record_dir = PurePosixPath(record).parent.as_posix()
    for name, source in staged.items():
        path = f"{prefix}/{name}"
        if path == record:
            raise WriteError("conflict", "An attachment name collides with the record", path=path)
        if is_glance_name(name) and PurePosixPath(path).parent.as_posix() == record_dir:
            # A glance beside the record would be indexed as a second record for
            # this bundle, so the catalog would report a collision on every check.
            raise WriteError(
                "conflict",
                "An attachment cannot be named like a catalog record",
                path=path,
            )
        dest = _ensure_safe(root, path)
        if dest.exists() or dest.is_symlink():
            _clean_path(root, path)
            if not _existing_attachment_matches(root, path, source):
                raise WriteError("conflict", f"Attachment name already exists with different contents: {name}", path=path)
        elif _save_attachments_md(name):
            plan.texts[path] = source
        else:
            plan.attachments[path] = source


def _prepare_save(root: Path, collection: str, fields: dict, body: str, staged: dict[str, Path]) -> WritePlan:
    fields = dict(fields)
    title = _slug(fields.get("title") if isinstance(fields.get("title"), str) else "", "Untitled")
    if collection == "Misc":
        misc_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        fields["id"] = misc_id
        bundle = f"Misc/{misc_id}"
        if (root / bundle).exists():
            raise WriteError("conflict", "Allocated timestamp is already in use. Retry the request.")
        record = f"{bundle}/{title}.md"
        asset_prefix = f"{bundle}/assets"
    elif collection == "ISBN":
        bundle = _isbn_bundle(root, fields["isbn"])
        record = f"{bundle}/{_slug(str(fields.get('source', '')), 'source').lower()}-glance.md"
        asset_prefix = bundle
    elif collection == "SelfHosted":
        bundle_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        fields["id"] = bundle_id
        bundle = f"SelfHosted/{bundle_id}"
        if (root / bundle).exists():
            raise WriteError("conflict", "Allocated timestamp is already in use. Retry the request.")
        record = f"{bundle}/{_slug(str(fields.get('source', '')), 'source').lower()}-glance.md"
        asset_prefix = f"{bundle}/assets"
    elif collection in VIDEO_COLLECTIONS:
        bundle = _video_bundle(root, collection, fields)
        record = f"{bundle}/summary.md"
        asset_prefix = f"{bundle}/assets"
    else:
        oid = fields["doi"].replace("/", "_")
        oid = oid.replace("10.48550_arxiv.", "10.48550_arXiv.", 1)
        paper_root = root / "DOI"
        _ensure_safe(root, "DOI/placeholder")
        candidates = [p.name for p in paper_root.iterdir() if p.name.casefold() == f"doi-{oid.casefold()}"] if paper_root.is_dir() else []
        if len(candidates) > 1:
            raise WriteError("conflict", "Multiple archive directories have this DOI")
        bundle = f"DOI/{candidates[0] if candidates else 'DOI-' + oid}"
        files, _ = catalog_snapshot(root)
        glances = [path for path in files if path.startswith(bundle + "/") and path.count("/") == 3 and is_glance_name(Path(path).name)]
        if len(glances) > 1:
            raise WriteError("conflict", "Multiple glances already exist for this DOI")
        authors = fields.get("authors")
        author = authors[0].split()[-1] if isinstance(authors, list) and authors and isinstance(authors[0], str) and authors[0].split() else "Author"
        published = str(fields.get("published", ""))[:10]
        date = published if re.fullmatch(r"\d{4}-\d{2}-\d{2}", published) else "unknown-date"
        journal = _slug(str(fields.get("journal", "")), "Paper")
        source = _slug(str(fields.get("source", "")), "source").lower()
        record = glances[0] if glances else f"{bundle}/{journal}-{_slug(author, 'Author')}-{date}-{title}/{source}-glance.md"
        asset_prefix = bundle

    problems = validate_metadata(record, collection, fields, writing=True)
    if problems:
        raise WriteError("validation_failed", "Record metadata failed validation", diagnostics=problems)
    locator = {"bundle": bundle, "record_path": record}
    if collection in {"Misc", "SelfHosted"}:
        locator["id"] = fields["id"]
    plan = WritePlan(locator)
    _record_plan(root, plan, record, fields, body)
    _plan_attachments(root, plan, asset_prefix, staged, record=record)
    return plan


def _isbn_bundle(root: Path, isbn: str) -> str:
    """Resolve the ISBN bundle, reusing one canonical directory per ISBN."""
    digits = isbn_digits(isbn)
    book_root = root / "ISBN"
    _ensure_safe(root, "ISBN/placeholder")
    if not book_root.is_dir():
        return f"ISBN/ISBN-{isbn}"
    matches = []
    malformed = []
    for name in sorted(p.name for p in book_root.iterdir() if p.is_dir()):
        if not name.startswith("ISBN-"):
            continue
        candidate = name[5:]
        if isbn_digits(candidate) != digits and not isbn_digits(candidate).startswith(digits):
            continue
        if ISBN_NAME.fullmatch(name):
            matches.append(name)
        else:
            malformed.append(name)
    if len(matches) > 1:
        raise WriteError("conflict", f"Multiple archive directories hold this ISBN: {', '.join(matches)}")
    if matches:
        return f"ISBN/{matches[0]}"
    if malformed:
        raise WriteError(
            "conflict",
            f"An ISBN directory with an unverified suffix already exists: {', '.join(malformed)}",
        )
    return f"ISBN/ISBN-{isbn}"


def _video_bundle(root: Path, collection: str, fields: dict) -> str:
    """Resolve ``<Platform>/<video_id>/<author>_<date>_<title>`` deterministically."""
    video_id = fields["video_id"]
    authors = fields.get("authors")
    author = authors[0] if isinstance(authors, list) and authors and isinstance(authors[0], str) else ""
    author_slug = _slug(author, "Author")
    published = str(fields.get("published", ""))
    match = re.search(r"\d{4}-\d{2}-\d{2}", published)
    if match is None or parse_iso_date(match[0]) is None:
        raise WriteError("invalid_argument", "metadata.published must carry a YYYY-MM-DD publication date")
    date = match[0]
    title_slug = _slug(str(fields.get("title", "")), "Untitled")
    folder = f"{author_slug}_{date}_{title_slug}"
    bundle = f"{collection}/{video_id}/{folder}"
    _ensure_safe(root, f"{collection}/{video_id}/placeholder")
    parent = root / collection / video_id
    if parent.is_dir():
        existing = sorted(p.name for p in parent.iterdir() if p.is_dir())
        if existing and existing != [folder]:
            raise WriteError(
                "conflict",
                f"Video {video_id} is already archived as {existing[0]}",
                bundle=f"{collection}/{video_id}/{existing[0]}",
            )
    return bundle


def edit_record(
    manager: IndexManager, path: str, revision: str, field_updates: dict, request_id: str | None
) -> dict:
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts) or "\x00" in path or not catalog_scope(path):
        raise WriteError("invalid_argument", "Expected a Library-relative catalog record path")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise WriteError("invalid_argument", "Expected the content revision returned by search")
    immutable = {"doi", "DOI", "id", "pmid", "arxiv_id", "video_id", "isbn"}
    if immutable & field_updates.keys():
        raise WriteError("invalid_argument", "Catalog identifiers are fixed by the archive path")
    return execute_write(
        manager, "edit", request_id,
        {"path": path, "revision": revision, "field_updates": field_updates},
        lambda root: _prepare_edit(root, path, revision, field_updates),
    )


def _prepare_edit(root: Path, path: str, revision: str, field_updates: dict) -> WritePlan:
    source = _ensure_safe(root, path)
    if source.is_symlink() or not source.is_file():
        raise WriteError("conflict", "The selected record moved or was removed", path=path)
    data = source.read_bytes()
    current_revision = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
    if revision != current_revision:
        raise WriteError("conflict", "The record changed since search", path=path, revision=current_revision)

    files, _ = catalog_snapshot(root)
    prefix = catalog_scope(path) + "/"
    scoped = {name: blob for name, blob in files.items() if name == path or name.startswith(prefix)}
    entry = next((item for item in Scanner(root).scan(scoped).entries if item.record_path == path), None)
    if entry is None:
        raise WriteError("invalid_argument", "The selected path is outside the editable catalog", path=path)
    _clean_path(root, path)
    try:
        fields, body, error = split_frontmatter(data.decode())
    except UnicodeError as exc:
        raise WriteError("validation_failed", "Record must contain UTF-8 Markdown", path=path) from exc
    if fields is None or error:
        raise WriteError("validation_failed", error or "Record has no YAML frontmatter", path=path)

    updated = {**fields, **field_updates}
    existing = validate_metadata(path, entry.collection, fields, writing=True)
    problems = [problem for problem in validate_metadata(path, entry.collection, updated, writing=True)
                if problem not in existing or problem["rule"].rsplit(".", 1)[-1] in field_updates]
    if problems:
        raise WriteError("validation_failed", "Record metadata failed validation", diagnostics=problems)
    locator = {"bundle": entry.bundle, "record_path": path}
    return WritePlan(locator, {path: render_record(updated, body)} if updated != fields else {})
