"""Catalog entry discovery for the Annex Library.

Selectors were derived from the historical sample of the production checkout
(2026-11 revision, 3959 tracked files). See ``docs/CATALOG.md`` for the
evidence table.

``DOI/<bundle>/``
    One entry per DOI bundle, ``DOI-<doi with / replaced by _>``. The catalog
    record is the *glance*: ``<JOURNAL>-<FirstAuthor>-<YYYY-MM-DD>-<Short-Title>/
    <source>-glance.md``. ``*-notes.md``, ``skill/**`` and ``assets/**`` are
    outside the catalog. A bundle with no glance is still a searchable
    attachment-only entry (its metadata is derived from archived filenames).

``ISBN/<bundle>/``
    One entry per book bundle ``ISBN-<isbn>``. Chapter distillation markdown
    (``ch01/verified.md``, ``skills/*/SKILL.md``, ``.cangjie/**``) is not a
    catalog record. Frontmatter is optional here.

``Misc/<YYYYMMDDTHHMMSS.ffffffZ>/``
    One entry per UTC-timestamp bundle; the single topic-named markdown file is
    the record. A stray flat ``Misc/<date>_<slug>.md`` is accepted as a legacy
    entry with a warning.

``Bilibili/``, ``YouTube/``, ``Vimeo/``
    One entry per ``<id>/<作者>_<YYYY-MM-DD>_<短标题>/summary.md``. Transcripts,
    ``.seg/**`` and 要点速览 files are not catalog records.

``SelfHosted/<id>/``
    One entry per document bundle (no markdown; metadata from the filename).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import yaml

from .normalize import date_to_iso, normalize_doi, normalize_tag, normalize_text, parse_iso_date

DOI_BUNDLE_PREFIX = "DOI-"
ISBN_BUNDLE_PREFIX = "ISBN-"
MISC_ID_RE = re.compile(r"^\d{8}T\d{6}\.\d{6}Z$")
MISC_LEGACY_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_")

VIDEO_COLLECTIONS = ("Bilibili", "YouTube", "Vimeo")
COLLECTIONS = ("DOI", "ISBN", "Misc", *VIDEO_COLLECTIONS, "SelfHosted")

NOTE_SUFFIX = "-notes.md"

ATTACHMENT_ROLE_BY_SUFFIX = {
    ".pdf": "document",
    ".epub": "document",
    ".docx": "document",
    ".pptx": "document",
    ".azw3": "document",
    ".txt": "document",
    ".html": "document",
    ".mp4": "video",
    ".mkv": "video",
    ".mp3": "audio",
    ".wav": "audio",
    ".m4a": "audio",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
    ".avif": "image",
    ".tif": "image",
    ".tiff": "image",
    ".json": "sidecar",
    ".vtt": "sidecar",
    ".srt": "sidecar",
}


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class Attachment:
    path: str
    role: str
    annex_tracked: bool = False
    local_present: bool = True
    size: int | None = None
    revision: str = ""

    def as_dict(self, *, include_local: bool = True) -> dict:
        data = {
            "name": self.path.rsplit("/", 1)[-1],
            "path": self.path,
            "role": self.role,
            "annex_tracked": self.annex_tracked,
        }
        if self.size is not None:
            data["size"] = self.size
        if self.revision:
            data["revision"] = self.revision
        if include_local:
            data["local_content"] = "present" if self.local_present else "missing"
        return data


@dataclass
class Entry:
    """One catalog record or attachment-only bundle."""

    entry_id: str
    collection: str
    kind: str
    bundle: str
    record_path: str | None
    revision: str
    revision_kind: str
    title: str
    title_source: str
    doi: str = ""
    work_id: str = ""
    version_type: str = ""
    journal: str = ""
    published: str | None = None
    captured: str | None = None
    captured_date: str | None = None
    tags: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    identifiers: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)
    attachments: list[Attachment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def title_norm(self) -> str:
        return normalize_text(self.title)

    @property
    def authors_norm(self) -> str:
        return normalize_text(" ".join(self.authors))

    @property
    def doi_norm(self) -> str:
        return normalize_doi(self.doi)

    @property
    def tag_norms(self) -> list[str]:
        return [normalize_tag(tag) for tag in self.tags if normalize_tag(tag)]

    @property
    def work_key(self) -> str:
        return self.work_id or self.entry_id

    def as_dict(self, *, include_local: bool = False) -> dict:
        data = {
            "id": self.entry_id,
            "collection": self.collection,
            "kind": self.kind,
            "bundle": self.bundle,
            "record_path": self.record_path,
            "revision": self.revision,
            "title": self.title,
            "work_id": self.work_key,
            "doi": self.doi,
            "version_type": self.version_type,
            "journal": self.journal,
            "published": self.published,
            "captured": self.captured,
            "tags": self.tags,
            "authors": self.authors,
            "identifiers": self.identifiers,
            "attachments": [a.as_dict(include_local=include_local) for a in self.attachments],
        }
        if self.warnings:
            data["warnings"] = self.warnings
        return data


# --------------------------------------------------------------------------- #
# Git plumbing
# --------------------------------------------------------------------------- #


def run_git(root: Path, *args: str, check: bool = True) -> bytes:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout


def head_revision(root: Path) -> str:
    try:
        return run_git(root, "rev-parse", "HEAD").decode().strip()
    except RuntimeError:
        return ""


def tracked_blobs(root: Path) -> dict[str, str]:
    """Return ``{path: blob sha}`` for every tracked file (no filesystem walk)."""
    raw = run_git(root, "ls-files", "-s", "-z")
    result: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        meta, _, path = item.partition(b"\t")
        parts = meta.split(b" ")
        if len(parts) < 3:
            continue
        result[path.decode("utf-8", "surrogateescape")] = parts[1].decode()
    return result


def annex_state(root: Path) -> tuple[set[str], set[str]]:
    """Return ``(present, absent)`` annex-tracked paths via git-annex itself.

    This is the build-time oracle (~0.5 s on the production checkout). Restart
    refreshes use :func:`probe_local_present` instead.
    """
    present = set(
        run_git(root, "annex", "find", "--in=here", check=False)
        .decode("utf-8", "surrogateescape")
        .splitlines()
    )
    absent = set(
        run_git(root, "annex", "find", "--not", "--in=here", check=False)
        .decode("utf-8", "surrogateescape")
        .splitlines()
    )
    return present, absent


def annex_tracked_paths(root: Path, paths: Sequence[str]) -> set[str]:
    """Return the subset of ``paths`` that git-annex tracks (42 ms for 3959)."""
    if not paths:
        return set()
    payload = b"\0".join(path.encode("utf-8", "surrogateescape") for path in paths) + b"\0"
    raw = subprocess.run(
        ["git", "-C", str(root), "check-attr", "--stdin", "-z", "filter"],
        input=payload,
        capture_output=True,
    ).stdout
    parts = raw.split(b"\0")
    tracked: set[str] = set()
    for i in range(0, len(parts) - 2, 3):
        if parts[i + 1] == b"filter" and parts[i + 2] == b"annex":
            tracked.add(parts[i].decode("utf-8", "surrogateescape"))
    return tracked


def probe_local_present(full_path: Path) -> bool:
    """Filesystem probe for git-annex local content.

    Handles both working-tree layouts seen in practice:

    * unlocked adjusted branch: present content is a regular file; absent
      content is a ~100 byte pointer file beginning ``/annex/objects/``;
    * locked branch: present content is a symlink into ``.git/annex/objects``
      and a dropped file leaves a dangling symlink.

    The probe agreed exactly with ``git annex find --in=here`` across the 2692
    annex-tracked production paths (3812 present / 148 absent, zero mismatch).
    """
    try:
        if full_path.is_symlink():
            return full_path.exists()
        stat = full_path.stat()
    except OSError:
        return False
    if stat.st_size < 4096:
        try:
            with full_path.open("rb") as handle:
                head = handle.read(32)
        except OSError:
            return False
        if head.startswith(b"/annex/objects/"):
            return False
    return True


def blob_sha_of_file(path: Path) -> str:
    """Compute a Git blob SHA from worktree bytes without invoking git."""
    data = path.read_bytes()
    sha = hashlib.sha1(b"blob %d\0" % len(data))
    sha.update(data)
    return sha.hexdigest()


# --------------------------------------------------------------------------- #
# Frontmatter and text helpers
# --------------------------------------------------------------------------- #

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.S)
_HEADING_RE = re.compile(r"^#{1,2}\s+(.+)$", re.M)
_AUTHORS_LINE_RE = re.compile(r"^\*\*Authors?[:：]?\*\*\s*[:：]?\s*(.+)$", re.M)


def split_frontmatter(text: str) -> tuple[dict | None, str, str | None]:
    """Return ``(frontmatter mapping or None, body, error message or None)``."""
    if not text.startswith("---"):
        return None, text, None
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None, text, "unterminated frontmatter block"
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return None, text[match.end():], f"unparseable YAML: {_one_line(exc)}"
    if loaded is None:
        return {}, text[match.end():], None
    if not isinstance(loaded, dict):
        return None, text[match.end():], "frontmatter is not a mapping"
    return loaded, text[match.end():], None


def _one_line(exc: Exception) -> str:
    return " ".join(str(exc).split())[:180]


def as_list(value: object) -> list[str]:
    """Coerce a frontmatter value into a list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                loaded = yaml.safe_load(text)
            except yaml.YAMLError:
                loaded = None
            if isinstance(loaded, list):
                return [str(item).strip() for item in loaded if str(item).strip()]
        if ";" in text:
            return [part.strip() for part in text.split(";") if part.strip()]
        return [text]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def as_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    return str(value).strip()


_CITATION_PREFIX_RE = re.compile(r"^(?:\*\*|__)(.+?)(?:\*\*|__)")
_CITATION_CUT_RE = re.compile(r"[*(\[]|\s+DOI[\s:]|\s+PMID[\s:]|\s+https?://", re.I)
_NAME_TOKEN_RE = re.compile(r"^[A-Z][\w\u00C0-\u024F'’.\-]{0,24}$")
_INITIAL_RE = re.compile(r"^[A-Z]\.?(?:[A-Z]\.?)?$")
_NAME_PARTICLES = {"de", "del", "der", "van", "von", "da", "di", "la", "le", "dos", "du"}


def _parse_author_list(text: str) -> list[str]:
    """Parse ``Surname I, Surname II.`` into names, or return ``[]`` if unsure."""
    text = re.sub(r"[*_`]", "", text).strip().rstrip(".;")
    candidates = [part.strip() for part in re.split(r",|\band\b|&|;", text) if part.strip()]
    names: list[str] = []
    for candidate in candidates:
        if candidate.lower() in {"et al", "et al.", "..."}:
            continue
        cleaned = candidate.rstrip(".")
        tokens = cleaned.split()
        if not 2 <= len(tokens) <= 5:
            return []
        if not _INITIAL_RE.match(tokens[-1]):
            return []
        for token in tokens[:-1]:
            if not _NAME_TOKEN_RE.match(token) and token.lower() not in _NAME_PARTICLES:
                return []
        names.append(cleaned)
    return names


def extract_body_authors(body: str) -> list[str]:
    """Read the author line of a glance.

    Three shapes occur in the archive, all handled here:

    * a labelled line, ``**Authors:** Lorenzo Travaglini, Nga T. Lam``;
    * a bold citation opener, ``**Popp D, Iwasa M, Narita A** (Biopolymers. 2009)``;
    * a bare citation opener, ``Kollipara L, Zahedi RP. *Protein carbamylation*``.
    """
    labelled = _AUTHORS_LINE_RE.search(body)
    if labelled:
        text = re.sub(r"[*_`]", "", labelled.group(1))
        return [part.strip().rstrip(".") for part in re.split(r",|\band\b|&|;", text) if part.strip()]

    for line in body.splitlines()[:12]:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "|", ">")):
            continue
        bold = _CITATION_PREFIX_RE.match(stripped)
        chunk = bold.group(1) if bold else stripped
        cut = _CITATION_CUT_RE.search(chunk)
        if cut:
            chunk = chunk[: cut.start()]
        names = _parse_author_list(chunk)
        if names:
            return names
        if bold:
            continue
    return []


def extract_heading(body: str) -> str:
    for match in _HEADING_RE.finditer(body):
        title = re.sub(r"[#*_`]", "", match.group(1)).strip()
        if title:
            return title
    return ""


_DOC_SUFFIXES = (".pdf", ".epub", ".azw3", ".docx", ".pptx", ".txt", ".mp4", ".mp3", ".m4a")


def strip_document_suffix(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    lowered = base.lower()
    for suffix in _DOC_SUFFIXES:
        if lowered.endswith(suffix):
            return base[: -len(suffix)]
    return base


def title_from_filename(name: str) -> str:
    """Derive a readable title from an archived filename.

    Handles the two library-wide shapes:

    * ``[Methods in Enzymology (456)] Allison, Scheffler. (2009). Mitochondrial Function Part A.pdf``
    * ``Small - 2024 - Travaglini - Fabrication of ... Nanowires.pdf``
    """
    stem = strip_document_suffix(name)
    stem = stem.replace("%E2%80%90", "‐").replace("%20", " ")
    stem = re.sub(r"^\[[^\]]*\]\s*", "", stem)
    parts = [part.strip() for part in stem.split(" - ")]
    if len(parts) >= 4 and re.fullmatch(r"(19|20)\d{2}", parts[1]):
        stem = " - ".join(parts[3:])
    # a publication year is metadata, not part of the title
    stem = re.sub(r"\s*\((?:19|20)\d{2}\)\s*\.?\s*", " ", stem)
    stem = re.sub(r"[_\uFFFD]", " ", stem)
    return re.sub(r"\s+", " ", stem).strip(" .-–_")


def authors_from_filename(name: str) -> list[str]:
    """Best-effort author list from an archived filename's leading segment."""
    stem = re.sub(r"^\[[^\]]*\]\s*", "", strip_document_suffix(name))
    match = re.match(r"([^()]{2,90}?)\s*(?:\.\s*\(|,\s*\(|\(|\.\s)", stem)
    if not match:
        return []
    text = match.group(1).strip().strip(".,")
    if not text or len(text.split()) > 14:
        return []
    return [part.strip() for part in re.split(r",|\band\b|&", text) if part.strip()]


def is_glance_name(name: str) -> bool:
    return name.endswith("-glance.md")


def doi_from_bundle(bundle: str) -> str:
    return bundle[len(DOI_BUNDLE_PREFIX):] if bundle.startswith(DOI_BUNDLE_PREFIX) else bundle


def isbn_from_bundle(bundle: str) -> str:
    return bundle[len(ISBN_BUNDLE_PREFIX):] if bundle.startswith(ISBN_BUNDLE_PREFIX) else bundle


def attachment_role(name: str) -> str:
    return ATTACHMENT_ROLE_BY_SUFFIX.get(Path(name).suffix.lower(), "file")


# --------------------------------------------------------------------------- #
# Scanner
# --------------------------------------------------------------------------- #


@dataclass
class ScanResult:
    entries: list[Entry]
    diagnostics: list[dict]
    revision: str
    tracked_count: int


class Scanner:
    """Turn one Library checkout into catalog entries.

    ``annex_present`` / ``annex_absent`` come from :func:`annex_state` at build
    time; when omitted, attachment availability is probed from the worktree.
    """

    def __init__(
        self,
        root: Path,
        *,
        annex_present: set[str] | None = None,
        annex_absent: set[str] | None = None,
        annex_tracked: set[str] | None = None,
    ) -> None:
        self.root = root
        self.annex_present = annex_present
        self.annex_absent = annex_absent
        self._annex_tracked = annex_tracked
        self._revision_cache: dict[str, str] = {}

    # -- public -----------------------------------------------------------

    def scan(self) -> ScanResult:
        tracked_map = tracked_blobs(self.root)
        tracked = sorted(tracked_map)
        if self._annex_tracked is None:
            self._annex_tracked = annex_tracked_paths(self.root, tracked)
        entries: list[Entry] = []
        diagnostics: list[dict] = []
        entries.extend(self._scan_doi(tracked, tracked_map, diagnostics))
        entries.extend(self._scan_misc(tracked, tracked_map, diagnostics))
        entries.extend(self._scan_videos(tracked, tracked_map, diagnostics))
        entries.extend(self._scan_isbn(tracked, tracked_map, diagnostics))
        entries.extend(self._scan_selfhosted(tracked, tracked_map, diagnostics))
        return ScanResult(
            entries=entries,
            diagnostics=diagnostics,
            revision=head_revision(self.root),
            tracked_count=len(tracked),
        )

    # -- DOI --------------------------------------------------------------

    def _scan_doi(
        self, tracked: Sequence[str], tracked_map: dict[str, str], diagnostics: list[dict]
    ) -> list[Entry]:
        bundles: dict[str, list[str]] = {}
        for path in tracked:
            parts = path.split("/")
            if len(parts) > 1 and parts[0] == "DOI":
                bundles.setdefault(parts[1], []).append(path)
        entries: list[Entry] = []
        for bundle in sorted(bundles):
            files = bundles[bundle]
            bundle_rel = f"DOI/{bundle}"
            glances = [p for p in files if p.count("/") == 3 and is_glance_name(p.rsplit("/", 1)[-1])]
            notes = [p for p in files if p.endswith(NOTE_SUFFIX)]
            attachments = self._attachments(files, skip_markdown=True)
            if glances:
                for glance in sorted(glances):
                    entries.append(self._doi_entry(bundle, glance, tracked_map, attachments))
                if len(glances) > 1:
                    diagnostics.append(
                        {
                            "path": bundle_rel,
                            "severity": "warning",
                            "message": f"{len(glances)} glances in one bundle; "
                                       "each is indexed as its own version",
                        }
                    )
                continue
            derived = self._derived_entry(
                collection="DOI",
                bundle=bundle_rel,
                files=files,
                tracked_map=tracked_map,
                attachments=attachments,
                extra={"slug_doi": doi_from_bundle(bundle).replace("_", "/"), "record_kind": "attachment-only"},
                warning="no glance: metadata derived from archived filename",
            )
            if derived is not None:
                entries.append(derived)
            elif notes:
                diagnostics.append(
                    {
                        "path": bundle_rel,
                        "severity": "info",
                        "message": f"{len(notes)} note file(s) and no glance or attachment; skipped",
                    }
                )
        return entries

    def _doi_entry(
        self,
        bundle: str,
        glance: str,
        tracked_map: dict[str, str],
        attachments: list[Attachment],
    ) -> Entry:
        text, read_error = self._read(glance)
        front, body, fm_error = split_frontmatter(text)
        warnings = [message for message in (read_error, fm_error) if message]
        fm = front or {}
        doi = as_str(fm.get("doi")) or as_str(fm.get("DOI"))
        fm_title = as_str(fm.get("title"))
        heading = extract_heading(body)
        if fm_title:
            title, title_source = fm_title, "frontmatter"
        elif heading:
            title, title_source = heading, "heading"
        else:
            title, title_source = self._attachment_title(attachments, bundle), "attachment-filename"
        authors = as_list(fm.get("authors")) or extract_body_authors(body)
        if not authors:
            authors = authors_from_filename(self._primary_document(attachments) or "")
        captured = as_str(fm.get("captured"))
        captured_date = date_to_iso(parse_iso_date(fm.get("captured")))
        if not captured_date:
            warnings.append("no parsable captured date; date-range filters skip this entry")
        published_raw = fm.get("published")
        published_date = date_to_iso(parse_iso_date(published_raw))
        published = published_date or (as_str(published_raw) or None)
        identifiers = {
            key: as_str(fm.get(key))
            for key in ("pmid", "pmcid", "doi_reported", "arxiv_id")
            if as_str(fm.get(key))
        }
        return Entry(
            entry_id=glance,
            collection="DOI",
            kind="glance",
            bundle=f"DOI/{bundle}",
            record_path=glance,
            revision=self._record_revision(glance, tracked_map),
            revision_kind="record",
            title=title,
            title_source=title_source,
            doi=doi,
            work_id=as_str(fm.get("work_id")),
            version_type=as_str(fm.get("version_type")) or as_str(fm.get("version")),
            journal=as_str(fm.get("journal")),
            published=published,
            captured=captured or None,
            captured_date=captured_date,
            tags=as_list(fm.get("tags")),
            authors=authors,
            identifiers=identifiers,
            extra={
                "slug_doi": doi_from_bundle(bundle).replace("_", "/"),
                "record_kind": "glance",
                "type": as_str(fm.get("type")),
                "reading_level": as_str(fm.get("reading_level")),
                "source": as_str(fm.get("source")),
                "url": as_str(fm.get("url")),
                "stage": as_str(fm.get("stage")),
                "glance_dir": glance.rsplit("/", 1)[0],
            },
            attachments=list(attachments),
            warnings=warnings,
        )

    # -- Misc -------------------------------------------------------------

    def _scan_misc(
        self, tracked: Sequence[str], tracked_map: dict[str, str], diagnostics: list[dict]
    ) -> list[Entry]:
        bundles: dict[str, list[str]] = {}
        flat: list[str] = []
        for path in tracked:
            parts = path.split("/")
            if len(parts) < 2 or parts[0] != "Misc":
                continue
            if len(parts) == 2 and path.endswith(".md"):
                flat.append(path)
            else:
                bundles.setdefault(parts[1], []).append(path)
        entries: list[Entry] = []
        by_id: dict[str, list[Entry]] = {}
        for bundle in sorted(bundles):
            files = bundles[bundle]
            records = [p for p in files if p.count("/") == 2 and p.endswith(".md")]
            if not MISC_ID_RE.match(bundle):
                diagnostics.append(
                    {
                        "path": f"Misc/{bundle}",
                        "severity": "warning",
                        "message": "Misc bundle directory is not a UTC timestamp id",
                    }
                )
            if not records:
                diagnostics.append(
                    {
                        "path": f"Misc/{bundle}",
                        "severity": "info",
                        "message": "Misc bundle has no markdown record",
                    }
                )
                continue
            if len(records) > 1:
                diagnostics.append(
                    {
                        "path": f"Misc/{bundle}",
                        "severity": "warning",
                        "message": f"{len(records)} markdown files in one Misc bundle",
                    }
                )
            for record in sorted(records):
                entry = self._misc_entry(bundle, record, tracked_map, files)
                entries.append(entry)
                by_id.setdefault(entry.extra.get("misc_id", ""), []).append(entry)
        for path in flat:
            entry = self._flat_misc_entry(path, tracked_map)
            entries.append(entry)
            by_id.setdefault(entry.extra.get("misc_id", ""), []).append(entry)
        for misc_id, group in by_id.items():
            if misc_id and len(group) > 1:
                for entry in group:
                    entry.warnings.append(
                        f"ambiguous identifier {misc_id}: {len(group)} catalog records share it"
                    )
                diagnostics.append(
                    {
                        "path": misc_id,
                        "severity": "warning",
                        "message": f"ambiguous Misc id shared by {len(group)} records: "
                                   + ", ".join(filter(None, (g.record_path for g in group))),
                    }
                )
        return entries

    def _misc_entry(
        self, bundle: str, record: str, tracked_map: dict[str, str], files: list[str]
    ) -> Entry:
        text, read_error = self._read(record)
        front, body, fm_error = split_frontmatter(text)
        warnings = [message for message in (read_error, fm_error) if message]
        fm = front or {}
        misc_id = as_str(fm.get("id")) or (bundle if MISC_ID_RE.match(bundle) else "")
        fm_title = as_str(fm.get("title"))
        heading = extract_heading(body)
        if fm_title:
            title, title_source = fm_title, "frontmatter"
        elif heading:
            title, title_source = heading, "heading"
        else:
            title, title_source = title_from_filename(record), "filename"
        captured_date = date_to_iso(parse_iso_date(fm.get("captured")))
        if not captured_date and MISC_ID_RE.match(bundle):
            captured_date = date_to_iso(_date_from_misc_id(bundle))
        published_raw = fm.get("published")
        published = date_to_iso(parse_iso_date(published_raw)) or (as_str(published_raw) or None)
        attachments = self._attachments(files, skip_markdown=True)
        return Entry(
            entry_id=record,
            collection="Misc",
            kind="misc-note" if MISC_ID_RE.match(bundle) else "misc-legacy",
            bundle=f"Misc/{bundle}",
            record_path=record,
            revision=self._record_revision(record, tracked_map),
            revision_kind="record",
            title=title,
            title_source=title_source,
            doi=as_str(fm.get("doi")),
            work_id=as_str(fm.get("work_id")),
            version_type=as_str(fm.get("version_type")),
            published=published,
            captured=as_str(fm.get("captured")) or None,
            captured_date=captured_date,
            tags=as_list(fm.get("tags")),
            authors=as_list(fm.get("authors")) or extract_body_authors(body),
            identifiers={"id": misc_id} if misc_id else {},
            extra={
                "misc_id": misc_id,
                "source": as_str(fm.get("source")),
                "url": as_str(fm.get("url")),
                "legacy_path": as_str(fm.get("legacy_path")),
                "stage": as_str(fm.get("stage")),
            },
            attachments=attachments,
            warnings=warnings,
        )

    def _flat_misc_entry(self, record: str, tracked_map: dict[str, str]) -> Entry:
        text, read_error = self._read(record)
        front, body, fm_error = split_frontmatter(text)
        warnings = [message for message in (read_error, fm_error) if message]
        warnings.append("legacy Misc note: flat file outside a timestamp bundle")
        fm = front or {}
        fm_title = as_str(fm.get("title"))
        heading = extract_heading(body)
        if fm_title:
            title, title_source = fm_title, "frontmatter"
        elif heading:
            title, title_source = heading, "heading"
        else:
            title, title_source = title_from_filename(record), "filename"
        captured_date = date_to_iso(parse_iso_date(fm.get("captured")))
        if not captured_date:
            match = MISC_LEGACY_DATE_RE.match(record.rsplit("/", 1)[-1])
            if match:
                captured_date = match.group(1)
        published_raw = fm.get("published")
        return Entry(
            entry_id=record,
            collection="Misc",
            kind="misc-legacy",
            bundle=record,
            record_path=record,
            revision=self._record_revision(record, tracked_map),
            revision_kind="record",
            title=title,
            title_source=title_source,
            doi=as_str(fm.get("doi")),
            work_id=as_str(fm.get("work_id")),
            version_type=as_str(fm.get("version_type")),
            published=date_to_iso(parse_iso_date(published_raw)) or (as_str(published_raw) or None),
            captured=as_str(fm.get("captured")) or None,
            captured_date=captured_date,
            tags=as_list(fm.get("tags")),
            authors=as_list(fm.get("authors")) or extract_body_authors(body),
            identifiers={"id": as_str(fm.get("id"))} if as_str(fm.get("id")) else {},
            extra={
                "misc_id": as_str(fm.get("id")),
                "source": as_str(fm.get("source")),
                "url": as_str(fm.get("url")),
                "legacy_path": as_str(fm.get("legacy_path")),
            },
            attachments=[],
            warnings=warnings,
        )

    # -- video platforms --------------------------------------------------

    def _scan_videos(
        self, tracked: Sequence[str], tracked_map: dict[str, str], diagnostics: list[dict]
    ) -> list[Entry]:
        entries: list[Entry] = []
        for collection in VIDEO_COLLECTIONS:
            bundle_map: dict[str, list[str]] = {}
            for path in tracked:
                parts = path.split("/")
                if len(parts) >= 4 and parts[0] == collection:
                    bundle_map.setdefault("/".join(parts[:3]), []).append(path)
            for bundle, files in sorted(bundle_map.items()):
                summaries = [p for p in files if p.endswith("/summary.md")]
                if not summaries:
                    diagnostics.append(
                        {
                            "path": bundle,
                            "severity": "info",
                            "message": "video bundle without summary.md is not a catalog entry",
                        }
                    )
                    continue
                entries.append(self._video_entry(collection, bundle, summaries[0], tracked_map, files))
        return entries

    def _video_entry(
        self,
        collection: str,
        bundle: str,
        record: str,
        tracked_map: dict[str, str],
        files: list[str],
    ) -> Entry:
        text, read_error = self._read(record)
        front, body, fm_error = split_frontmatter(text)
        warnings = [message for message in (read_error, fm_error) if message]
        fm = front or {}
        parts = bundle.split("/")
        video_id = parts[1] if len(parts) > 1 else ""
        fm_title = as_str(fm.get("title"))
        heading = extract_heading(body)
        if fm_title:
            title, title_source = fm_title, "frontmatter"
        elif heading:
            title, title_source = heading, "heading"
        else:
            title, title_source = title_from_filename(parts[-1]), "bundle-name"
        captured_date = date_to_iso(parse_iso_date(fm.get("captured")))
        published_raw = fm.get("published")
        return Entry(
            entry_id=record,
            collection=collection,
            kind="video-summary",
            bundle=bundle,
            record_path=record,
            revision=self._record_revision(record, tracked_map),
            revision_kind="record",
            title=title,
            title_source=title_source,
            doi=as_str(fm.get("doi")),
            work_id=as_str(fm.get("work_id")),
            version_type=as_str(fm.get("version_type")),
            published=date_to_iso(parse_iso_date(published_raw)) or (as_str(published_raw) or None),
            captured=as_str(fm.get("captured")) or None,
            captured_date=captured_date,
            tags=as_list(fm.get("tags")),
            authors=as_list(fm.get("authors")) or extract_body_authors(body),
            identifiers={"video_id": video_id} if video_id else {},
            extra={
                "platform": collection,
                "source": as_str(fm.get("source")),
                "url": as_str(fm.get("url")),
                "stage": as_str(fm.get("stage")),
                "bundle_name": parts[-1],
            },
            attachments=self._attachments(files, skip_markdown=True),
            warnings=warnings,
        )

    # -- ISBN --------------------------------------------------------------

    def _scan_isbn(
        self, tracked: Sequence[str], tracked_map: dict[str, str], diagnostics: list[dict]
    ) -> list[Entry]:
        bundles: dict[str, list[str]] = {}
        for path in tracked:
            parts = path.split("/")
            if len(parts) > 1 and parts[0] == "ISBN":
                bundles.setdefault(parts[1], []).append(path)
        entries: list[Entry] = []
        for bundle in sorted(bundles):
            files = bundles[bundle]
            attachments = self._attachments(files, skip_markdown=True)
            warnings: list[str] = []
            if bundle != bundle.strip():
                warnings.append("book bundle directory name has leading/trailing whitespace")
            entry = self._derived_entry(
                collection="ISBN",
                bundle=f"ISBN/{bundle}",
                files=files,
                tracked_map=tracked_map,
                attachments=attachments,
                extra={
                    "isbn": isbn_from_bundle(bundle),
                    "record_kind": "book-bundle",
                    "distillation_docs": len([p for p in files if p.endswith(".md")]),
                },
                warning=None,
            )
            if entry is None:
                diagnostics.append(
                    {
                        "path": f"ISBN/{bundle}",
                        "severity": "info",
                        "message": "book bundle without a document attachment",
                    }
                )
                continue
            entry.warnings.extend(warnings)
            entries.append(entry)
        return entries

    # -- SelfHosted -------------------------------------------------------

    def _scan_selfhosted(
        self, tracked: Sequence[str], tracked_map: dict[str, str], diagnostics: list[dict]
    ) -> list[Entry]:
        bundles: dict[str, list[str]] = {}
        for path in tracked:
            parts = path.split("/")
            if len(parts) > 1 and parts[0] == "SelfHosted":
                bundles.setdefault(parts[1], []).append(path)
        entries: list[Entry] = []
        for bundle in sorted(bundles):
            files = bundles[bundle]
            entry = self._derived_entry(
                collection="SelfHosted",
                bundle=f"SelfHosted/{bundle}",
                files=files,
                tracked_map=tracked_map,
                attachments=self._attachments(files, skip_markdown=True),
                extra={"record_kind": "document"},
                warning="no markdown record: metadata derived from archived filename",
            )
            if entry is None:
                diagnostics.append(
                    {
                        "path": f"SelfHosted/{bundle}",
                        "severity": "info",
                        "message": "document bundle without an attachment",
                    }
                )
                continue
            entries.append(entry)
        return entries

    # -- helpers ----------------------------------------------------------

    def _derived_entry(
        self,
        *,
        collection: str,
        bundle: str,
        files: Sequence[str],
        tracked_map: dict[str, str],
        attachments: list[Attachment],
        extra: dict,
        warning: str | None,
    ) -> Entry | None:
        """Build an attachment-only entry from archived filenames."""
        documents = [a for a in attachments if a.role == "document"] or list(attachments)
        if not documents:
            return None
        primary = documents[0]
        warnings = [warning] if warning else []
        return Entry(
            entry_id=bundle,
            collection=collection,
            kind={"DOI": "paper-archive", "ISBN": "book", "SelfHosted": "document"}.get(
                collection, "attachment"
            ),
            bundle=bundle,
            record_path=None,
            revision=tracked_map.get(primary.path, ""),
            revision_kind="attachment",
            title=title_from_filename(primary.path),
            title_source="attachment-filename",
            authors=authors_from_filename(primary.path),
            extra=extra,
            attachments=list(attachments),
            warnings=warnings,
        )

    def _primary_document(self, attachments: Sequence[Attachment]) -> str:
        for attachment in attachments:
            if attachment.role == "document":
                return attachment.path
        return ""

    def _attachment_title(self, attachments: Sequence[Attachment], bundle: str) -> str:
        document = self._primary_document(attachments)
        return title_from_filename(document) if document else bundle

    def _read(self, rel_path: str) -> tuple[str, str | None]:
        try:
            return (self.root / rel_path).read_text(encoding="utf-8", errors="replace"), None
        except OSError as exc:
            return "", f"cannot read record: {exc.strerror}"

    def _record_revision(self, rel_path: str, tracked_map: dict[str, str]) -> str:
        """Content revision of a record: the worktree blob SHA.

        Equal to the committed blob when the file is clean, so an edit can state
        a precondition that detects external modification as well as commits.
        """
        cached = self._revision_cache.get(rel_path)
        if cached is not None:
            return cached
        try:
            revision = blob_sha_of_file(self.root / rel_path)
        except OSError:
            revision = tracked_map.get(rel_path, "")
        self._revision_cache[rel_path] = revision
        return revision

    def _attachments(self, files: Sequence[str], *, skip_markdown: bool) -> list[Attachment]:
        result: list[Attachment] = []
        for path in files:
            name = path.rsplit("/", 1)[-1]
            if skip_markdown and name.endswith(".md"):
                continue
            role = attachment_role(name)
            annex_tracked = path in (self._annex_tracked or ())
            present = self._local_present(path)
            size = None
            if present:
                try:
                    size = (self.root / path).stat().st_size
                except OSError:
                    size = None
            result.append(
                Attachment(
                    path=path,
                    role=role,
                    annex_tracked=annex_tracked,
                    local_present=present,
                    size=size,
                    revision=self._revision_cache.get(path) or "",
                )
            )
        return result

    def _local_present(self, rel_path: str) -> bool:
        if self.annex_absent is not None and rel_path in self.annex_absent:
            return False
        if self.annex_present is not None and rel_path in self.annex_present:
            return True
        return probe_local_present(self.root / rel_path)


def _date_from_misc_id(misc_id: str) -> _dt.date | None:
    try:
        return _dt.datetime.strptime(misc_id[:15], "%Y%m%dT%H%M%S").date()
    except ValueError:
        return None
