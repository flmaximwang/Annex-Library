"""Validation shared by archive checks and catalog writes."""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from pathlib import Path

from .catalog import MISC_ID_RE, VIDEO_COLLECTIONS, Scanner, catalog_snapshot, doi_from_bundle, split_frontmatter
from .normalize import normalize_doi, parse_iso_date

DOI = re.compile(r"10\.\d{4,}/[^\s/]+(?:/[^\s/]+)*\Z", re.I)
ISBN_NAME = re.compile(r"ISBN-([0-9]+(?:-[0-9]+)*(?:-[Xx]|[Xx])?)\Z")
ISBN_PREFIX = re.compile(r"ISBN-([0-9]+(?:-[0-9]+)*[Xx]?)")
VIDEO_NAME = re.compile(r".+_(\d{4}-\d{2}-\d{2})_.+\Z")

BOOK_OPTIONAL_STRINGS = {
    "publisher",
    "edition",
    "language",
    "series",
    "series_number",
    "volume",
    "number_of_volumes",
    "place",
    "pages",
    "short_title",
}


def isbn_digits(value: str) -> str:
    """Return the ISBN digits with hyphens removed and ``X`` upper-cased."""
    return re.sub(r"\s+", "", value.replace("-", "")).upper()


def isbn_checksum_valid(value: str) -> bool:
    """Verify an ISBN-10 or ISBN-13 check digit (hyphens allowed)."""
    digits = isbn_digits(value)
    if len(digits) == 10:
        if not digits[:9].isdigit() or digits[9] not in "0123456789X":
            return False
        total = sum((10 - position) * (10 if char == "X" else int(char))
                    for position, char in enumerate(digits))
        return total % 11 == 0
    if len(digits) == 13:
        if not digits.isdigit():
            return False
        total = sum((1 if position % 2 == 0 else 3) * int(char)
                    for position, char in enumerate(digits))
        return total % 10 == 0
    return False


def canonical_isbn(value: object) -> str | None:
    """Normalise a caller-supplied ISBN or return ``None`` when it is invalid.

    Whitespace is dropped, a trailing ``x`` becomes ``X`` and a redundant
    ``ISBN`` label is removed; the caller's hyphens are preserved.
    """
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", "", value)
    if text[:4].casefold() == "isbn":
        text = text[4:].lstrip(":-")
    text = text.upper()
    if not re.fullmatch(r"[0-9]+(?:-[0-9]+)*[X]?", text):
        return None
    return text if isbn_checksum_valid(text) else None


def issue(path: str, rule: str, message: str, repair: str, *, severity: str = "error") -> dict:
    return {"path": path, "rule": rule, "severity": severity, "message": message, "repair": repair}


def validate_metadata(path: str, collection: str, fields: dict, *, writing: bool = False) -> list[dict]:
    """Validate a record's own fields. Legacy records allow missing write fields."""
    problems = []

    def report(rule: str, message: str, fix: str) -> None:
        problems.append(issue(path, rule, message, fix, severity="error" if writing else "warning"))

    common = {"source", "url", "published", "captured", "stage", "tags"}
    required = common | {"title"}
    optional_url = False
    if collection == "DOI":
        required |= {"authors", "version_type"}
        bundle = path.split("/")[1]
        identifier = fields.get("doi", fields.get("DOI"))
        if bundle.startswith("DOI-"):
            required.add("doi")
            if identifier and (not isinstance(identifier, str) or not DOI.fullmatch(normalize_doi(identifier))
                               or normalize_doi(identifier).replace("/", "_") != bundle[4:].casefold()):
                report("doi.bundle", "DOI differs from the bundle identifier or is invalid", "Use the DOI encoded in the bundle name.")
        elif bundle.startswith("PMID-"):
            required.add("pmid")
        elif bundle.startswith("arXiv-"):
            required.add("arxiv_id")
    elif collection == "Misc":
        required.add("id")
        if "id" in fields and (not isinstance(fields["id"], str) or not MISC_ID_RE.fullmatch(fields["id"]) or fields["id"] != path.split("/")[1]):
            report("misc.id", "Misc id must equal its UTC timestamp directory", "Preserve the timestamp directory and set id to its name.")
    elif collection == "ISBN":
        required |= {"isbn", "authors"}
        required.discard("url")
        optional_url = True
        bundle = path.split("/")[1]
        value = canonical_isbn(fields.get("isbn"))
        if "isbn" in fields and value is None:
            report("isbn.invalid", "isbn must be a valid ISBN-10 or ISBN-13", "Use the ISBN printed in the book, hyphens allowed.")
        elif value is not None and not bundle.startswith("ISBN-"):
            report("isbn.bundle", "book bundle must be named ISBN-<isbn>", "Rename the bundle after the ISBN it archives.")
        elif value is not None and isbn_digits(bundle[4:]) != isbn_digits(value):
            report("isbn.bundle", "isbn differs from the bundle identifier", "Use the ISBN encoded in the bundle name.")
    elif collection == "SelfHosted":
        if "id" in fields and fields["id"] != path.split("/")[1]:
            report("selfhosted.id", "SelfHosted id must equal its bundle directory", "Preserve the server-assigned timestamp directory.")
    elif collection in VIDEO_COLLECTIONS:
        required.add("video_id")
        if "video_id" in fields and fields["video_id"] != path.split("/")[1]:
            report("video.id", "Video id differs from the platform directory", "Use the platform identifier encoded in the path.")
    for key in sorted(required - fields.keys()):
        if not writing and (key in {"work_id", "version_type", "video_id", "authors", "title", "isbn"}):
            continue
        if key == "doi" and "DOI" in fields and not writing:
            continue
        report(f"frontmatter.required.{key}", f"Missing {key}", f"Set {key} in the YAML frontmatter.")

    scalar_fields = {"title", "source", "url", "stage", "doi", "DOI", "id", "video_id", "journal", "work_id", "version_type", "legacy_path", "pmid", "arxiv_id", "isbn"} | BOOK_OPTIONAL_STRINGS
    for key in sorted(scalar_fields & fields.keys()):
        value = fields[key]
        if optional_url and key == "url" and value == "":
            continue
        if not isinstance(value, str) or not value.strip():
            report(f"frontmatter.type.{key}", f"{key} must be a nonempty string", f"Specify {key} as a YAML string.")
    for key in ("tags", "authors"):
        if key not in fields:
            continue
        values = fields[key]
        if not isinstance(values, list) or not all(isinstance(item, str) and item.strip() for item in values) or (key == "authors" and not values):
            report(f"frontmatter.type.{key}", f"{key} must be a list of nonempty strings", f"Write {key} as a YAML list.")
    if "stage" in fields and fields["stage"] != "library":
        report("frontmatter.stage", "stage must be library", "Set stage to library.")
    if "captured" in fields:
        value = fields["captured"]
        parsed = parse_iso_date(value)
        if parsed is None or (writing and str(value) != parsed.isoformat()):
            report("frontmatter.captured", "captured must be a YYYY-MM-DD date", "Set captured to the actual capture date.")
    if "published" in fields and writing:
        value = fields["published"]
        partial = collection == "ISBN"
        if value != "unknown" and not _valid_publication(value, partial_dates=partial):
            report(
                "frontmatter.published",
                "published must be an ISO date or zoned timestamp, or unknown"
                if not partial
                else "published must be YYYY, YYYY-MM, an ISO date or zoned timestamp, or unknown",
                "Use the source publication date or the literal unknown.",
            )
    return problems


def _valid_publication(value: object, *, partial_dates: bool = False) -> bool:
    if isinstance(value, dt.datetime):
        return value.utcoffset() is not None
    if isinstance(value, dt.date):
        return True
    if isinstance(value, str):
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                dt.date.fromisoformat(value)
                return True
            if partial_dates and re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", value):
                dt.date.fromisoformat(f"{value}-01")
                return True
            if partial_dates and re.fullmatch(r"\d{4}", value):
                return True
            return "T" in value and dt.datetime.fromisoformat(value).utcoffset() is not None
        except ValueError:
            pass
    return False


def check_catalog(root: Path) -> tuple[set[str], list[dict]]:
    files, _ = catalog_snapshot(root)
    scanner = Scanner(root)
    scan = scanner.scan(files)
    problems = []
    isbn_groups = defaultdict(set)
    isbn_bundles = {"/".join(path.split("/")[:2]) for path in files if path.startswith("ISBN/")}
    records = {entry.bundle for entry in scan.entries if entry.record_path}
    for bundle in sorted(isbn_bundles):
        name = bundle.split("/")[1]
        # Legacy book bundles predate ISBN validation: their directory names
        # stay readable and only warn, so an old checkout is never un-checkable.
        severity = "error" if bundle in records else "warning"
        prefix = ISBN_PREFIX.match(name)
        if prefix:
            isbn_groups[prefix[1].replace("-", "").upper()].add(bundle)
        digits = prefix[1].replace("-", "").upper() if prefix else ""
        if (
            not ISBN_NAME.fullmatch(name)
            or len(digits) not in {10, 13}
            or not isbn_checksum_valid(digits)
        ):
            suggestion = prefix[0] if prefix else "ISBN-<10- or 13-digit ISBN>"
            problems.append(
                issue(
                    bundle,
                    "isbn.name",
                    "Invalid ISBN directory name, check digit, or suffix",
                    f"Verify the ISBN and reconcile the bundle under {suggestion}.",
                    severity=severity,
                )
            )
    for peers in isbn_groups.values():
        if len(peers) > 1:
            bundle_names = sorted(peers)
            canonical = min((ISBN_PREFIX.match(p.split("/")[1])[0] for p in bundle_names), key=len)
            for bundle in bundle_names:
                problems.append(
                    issue(
                        bundle,
                        "isbn.collision",
                        f"Multiple bundles for {canonical}: {', '.join(bundle_names)}",
                        f"Reconcile attachments into one verified {canonical} bundle.",
                        severity="error" if bundle in records else "warning",
                    )
                )

    by_identifier = defaultdict(list)
    for entry in scan.entries:
        path = entry.record_path
        if not path:
            continue
        text, read_error = scanner._read(path)
        fields, _, yaml_error = split_frontmatter(text)
        error = read_error or yaml_error
        if error:
            problems.append(issue(path, "frontmatter.yaml", error, "Repair the delimited YAML mapping."))
        else:
            problems.extend(validate_metadata(path, entry.collection, fields or {}))
        if entry.collection == "DOI":
            bundle_name = entry.bundle.split("/")[1]
            if not ((bundle_name.startswith("DOI-") and DOI.fullmatch(doi_from_bundle(bundle_name).replace("_", "/", 1))) or re.fullmatch(r"PMID-\d+|arXiv-[^/\s]+", bundle_name)):
                problems.append(issue(path, "doi.name", "Invalid paper bundle identifier", "Use DOI-<DOI with / encoded as _>, PMID-<id>, or arXiv-<id>."))
            identifier = entry.doi_norm or bundle_name.casefold()
        elif entry.collection == "Misc":
            if entry.kind == "misc-legacy":
                problems.append(issue(path, "misc.layout", "Legacy flat Misc record", "Use a timestamp bundle for new entries.", severity="warning"))
            identifier = entry.identifiers.get("id")
        elif entry.collection in VIDEO_COLLECTIONS:
            name = entry.bundle.split("/")[-1]
            match = VIDEO_NAME.fullmatch(name)
            if not match or parse_iso_date(match[1]) is None:
                problems.append(issue(path, "video.name", "Video directory lacks author, date or title", "Use <author>_<YYYY-MM-DD>_<title>/summary.md."))
            identifier = entry.identifiers.get("video_id")
        elif entry.collection == "ISBN":
            identifier = entry.identifiers.get("isbn")
        else:
            identifier = entry.identifiers.get("id")
        if identifier:
            by_identifier[entry.collection, identifier].append(path)
    for (collection, identifier), records in by_identifier.items():
        if len(records) > 1:
            for path in records:
                problems.append(issue(path, "identifier.collision", f"{collection} identifier {identifier} is shared by {', '.join(records)}", "Verify which records represent distinct versions and reconcile the identifier."))

    for diagnostic in scan.diagnostics:
        if "ambiguous Misc id" in diagnostic["message"] or "glances in one bundle" in diagnostic["message"]:
            continue
        problems.append(issue(diagnostic["path"], "bundle.layout", diagnostic["message"], "Review the archive directory against its collection layout.", severity=diagnostic["severity"]))
    problems.sort(key=lambda item: (item["path"], item["rule"], item["message"]))
    return set(files), problems
