"""Acceptance tests for ticket annex-library-crx.1 (annex-lib-search)."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from annex_library.catalog import (
    Scanner,
    annex_state,
    extract_body_authors,
    is_glance_name,
    probe_local_present,
    title_from_filename,
)
from annex_library.index import Index, Query, lookup_by_path, search
from annex_library.normalize import normalize_doi, parse_iso_date
from annex_library.refresh import IndexManager
from annex_library.tools import ToolError, annex_lib_search

from tests.fixtures import build_fixture, drop_annex_content

WILEY_DOI = "10.1002/smll.202311661"
ARXIV_DOI = "10.48550/arXiv.2310.10042"
WORK_ID = "travaglini-conductive-heme-nanowires"
WILEY_GLANCE = (
    "DOI/DOI-10.1002_smll.202311661/"
    "SMALL-Travaglini-2024-04-10-Conductive-Protein-Heme-Nanowires/wiley-glance.md"
)
ARXIV_GLANCE = (
    "DOI/DOI-10.48550_arXiv.2310.10042/"
    "ARXIV-Travaglini-2023-10-15-Conductive-Protein-Heme-Nanowires/arxiv-glance.md"
)
WILEY_PDF = (
    "DOI/DOI-10.1002_smll.202311661/Small - 2024 - Travaglini - Fabrication of "
    "Electronically Conductive Protein%E2%80%90Heme Nanowires.pdf"
)


# --------------------------------------------------------------------------- #
# Catalog selection rules
# --------------------------------------------------------------------------- #


def test_glance_note_and_container_selection(fixture_library: Path) -> None:
    scan = Scanner(fixture_library).scan()
    by_path = {entry.record_path: entry for entry in scan.entries if entry.record_path}

    assert WILEY_GLANCE in by_path
    assert ARXIV_GLANCE in by_path
    # notes are not catalog records
    assert not any(path.endswith("-notes.md") for path in by_path)


def test_glance_name_rules() -> None:
    assert is_glance_name("wiley-glance.md")
    assert not is_glance_name("arxiv-notes.md")
    assert not is_glance_name("glance.md")
    assert not is_glance_name("SKILL.md")


def test_doi_authors_from_body(fixture_library: Path) -> None:
    text = (fixture_library / WILEY_GLANCE).read_text(encoding="utf-8")
    authors = extract_body_authors(text)
    assert "Lorenzo Travaglini" in authors
    assert "Artur Sawicki" in authors


def test_scanner_prefers_frontmatter_then_heading(fixture_library: Path) -> None:
    entry = {e.record_path: e for e in Scanner(fixture_library).scan().entries}[WILEY_GLANCE]
    assert entry.title_source == "heading"
    assert entry.title.startswith("Fabrication of Electronically Conductive")


# --------------------------------------------------------------------------- #
# Acceptance: DOI + coauthor, title, captured range, tags, Misc
# --------------------------------------------------------------------------- #


def test_search_by_doi_and_coauthor(manager: IndexManager) -> None:
    payload = annex_lib_search(manager, doi=WILEY_DOI)
    assert payload["total_works"] == 1
    work = payload["works"][0]
    assert work["work_id"] == WORK_ID
    assert work["matched_version"] == WILEY_GLANCE
    assert {v["entry_id"] for v in work["versions"]} == {WILEY_GLANCE, ARXIV_GLANCE}
    assert payload["works"][0]["versions"][0]["revision"]

    by_author = annex_lib_search(manager, author="Sawicki")
    assert [w["work_id"] for w in by_author["works"]] == [WORK_ID]


def test_search_title_keywords(manager: IndexManager) -> None:
    payload = annex_lib_search(manager, title="conductive nanowires")
    assert payload["total_works"] == 1
    assert payload["works"][0]["work_id"] == WORK_ID


def test_search_captured_range_is_inclusive(manager: IndexManager) -> None:
    """The Misc bundles captured 2026-11-11 and 2026-11-14 are both included."""
    payload = annex_lib_search(
        manager, captured_from="2026-11-11", captured_to="2026-11-14",
        collection="Misc", include_local_status=False,
    )
    captured = sorted(
        version["captured"]
        for work in payload["works"]
        for version in work["versions"]
    )
    assert captured == ["2026-11-11", "2026-11-14"]

    # Excluding the upper bound yields only the earlier record.
    payload = annex_lib_search(
        manager, captured_from="2026-11-11", captured_to="2026-11-13", collection="Misc"
    )
    assert payload["total_works"] == 1
    assert payload["works"][0]["versions"][0]["captured"] == "2026-11-11"


def test_search_exact_tags_combine_with_and(manager: IndexManager) -> None:
    both = annex_lib_search(manager, tags=["gamma-prefoldin", "nanowire"])
    assert both["total_works"] == 1

    single = annex_lib_search(manager, tags=["gamma-prefoldin"])
    assert single["total_works"] == 1  # both versions of the same work

    author_and_tags = annex_lib_search(
        manager, author="Sawicki", tags=["preprint", "gamma-prefoldin"]
    )
    assert author_and_tags["total_works"] == 1
    assert author_and_tags["works"][0]["matched_version"] == ARXIV_GLANCE

    impossible = annex_lib_search(manager, tags=["gamma-prefoldin", "pricing"])
    assert impossible["total_works"] == 0


def test_search_excludes_notes_and_reports_malformed(manager: IndexManager) -> None:
    scan = Scanner(manager.index.library_root).scan()
    assert all(
        not (entry.record_path or "").endswith("-notes.md") for entry in scan.entries
    )
    payload = annex_lib_search(manager, text="malformed")
    assert payload["total_works"] == 0


def test_ambiguous_identifier_is_warned_not_fatal(manager: IndexManager) -> None:
    payload = annex_lib_search(manager, text="邻近标记")
    assert payload["total_works"] >= 2

    duplicates = [
        work["versions"][0]
        for work in payload["works"]
        if work["versions"][0]["identifiers"].get("id") == "20261121T125651.861690Z"
    ]
    assert len(duplicates) == 2, "both records sharing the id stay searchable"
    for version in duplicates:
        assert any("ambiguous identifier" in w for w in version["warnings"])

    # the unrelated record with its own unique id carries no ambiguity warning
    others = [
        work["versions"][0]
        for work in payload["works"]
        if work["versions"][0]["identifiers"].get("id") != "20261121T125651.861690Z"
    ]
    for version in others:
        assert not any("ambiguous identifier" in w for w in version.get("warnings", []))


# --------------------------------------------------------------------------- #
# Acceptance: annex placeholder attachment status
# --------------------------------------------------------------------------- #


def test_wiley_pdf_placeholder_reports_missing(manager: IndexManager) -> None:
    payload = annex_lib_search(manager, doi=WILEY_DOI, include_local_status=True)
    version = payload["works"][0]["versions"][0]
    pdf = [a for a in version["attachments"] if a["path"] == WILEY_PDF][0]
    assert pdf["annex_tracked"] is True
    assert pdf["local_content"] == "present"

    # Local availability for the indexed page is batched with the search.
    detail = json.loads(json.dumps(payload))
    assert detail["index"]["local_status"] == "requested"


def test_placeholder_reports_missing_after_drop(fixture_library: Path, tmp_path: Path) -> None:
    library = tmp_path / "Dropped"
    shutil.copytree(fixture_library, library, symlinks=True)
    drop_annex_content(library, WILEY_PDF)

    index = Index(tmp_path / "dropped.sqlite3", library)
    index.build()
    payload = annex_lib_search(
        IndexManager(index=index, watch=False), doi=WILEY_DOI, include_local_status=True
    )
    version = payload["works"][0]["versions"][0]
    pdf = [a for a in version["attachments"] if a["path"] == WILEY_PDF][0]
    assert pdf["local_content"] == "missing"
    # the tracked placeholder is still returned, which is the point of the field
    assert pdf["annex_tracked"] is True


def test_filesystem_probe_matches_git_annex(tmp_path: Path) -> None:
    library = build_fixture(tmp_path / "Probe")
    present, absent = annex_state(library)
    for rel_path in list(present) + list(absent):
        assert probe_local_present(library / rel_path) is (rel_path in present)


def test_locate_attachments_refreshes_local_state(manager: IndexManager) -> None:
    payload = annex_lib_search(manager, doi=WILEY_DOI, include_local_status=False)
    assert "local_content" not in payload["works"][0]["versions"][0]["attachments"][0]
    record = lookup_by_path(manager.index, WILEY_GLANCE)
    assert record["attachments"]


# --------------------------------------------------------------------------- #
# Acceptance: delete and rebuild the index -> equivalent results
# --------------------------------------------------------------------------- #


def _snapshot(payload: dict) -> dict:
    return {
        "total_works": payload["total_works"],
        "works": [
            {
                "work_id": work["work_id"],
                "matched_version": work["matched_version"],
                "versions": [
                    {
                        "entry_id": version["entry_id"],
                        "revision": version["revision"],
                        "title": version["title"],
                        "attachments": [a["path"] for a in version["attachments"]],
                    }
                    for version in work["versions"]
                ],
            }
            for work in payload["works"]
        ],
    }


def test_delete_and_rebuild_is_equivalent(fixture_library: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "rebuild.sqlite3"
    queries = [
        dict(doi=WILEY_DOI),
        dict(doi=WILEY_DOI, include_local_status=True),
        dict(author="Sawicki"),
        dict(title="conductive nanowires"),
        dict(captured_from="2026-11-11", captured_to="2026-11-14", collection="Misc"),
        dict(tags=["gamma-prefoldin", "nanowire"]),
        dict(text="模型选型"),
    ]
    first: list[dict] = []
    manager = IndexManager(index=Index(index_path, fixture_library), watch=False)
    manager.start()
    for kwargs in queries:
        first.append(_snapshot(annex_lib_search(manager, **kwargs)))
    manager.stop()

    assert index_path.is_file()
    index_path.unlink()
    for suffix in ("-wal", "-shm"):
        candidate = Path(str(index_path) + suffix)
        if candidate.exists():
            candidate.unlink()

    manager = IndexManager(index=Index(index_path, fixture_library), watch=False)
    manager.start()
    second = [_snapshot(annex_lib_search(manager, **kwargs)) for kwargs in queries]
    manager.stop()
    assert first == second


def test_rebuild_keeps_committed_revision(fixture_library: Path, tmp_path: Path) -> None:
    index = Index(tmp_path / "rev.sqlite3", fixture_library)
    index.build()
    revision = index.meta("library_revision")
    assert revision and len(revision) == 40


# --------------------------------------------------------------------------- #
# Freshness and argument validation
# --------------------------------------------------------------------------- #


def test_external_edit_is_reflected_after_refresh(fixture_library: Path, tmp_path: Path) -> None:
    library = tmp_path / "Fresh"
    shutil.copytree(fixture_library, library, symlinks=True)
    manager = IndexManager(index=Index(tmp_path / "fresh.sqlite3", library), watch=False)
    manager.start()
    assert annex_lib_search(manager, title="conductive nanowires")["total_works"] == 1

    glance = library / WILEY_GLANCE
    glance.write_text(
        glance.read_text(encoding="utf-8").replace(
            "# Fabrication of Electronically Conductive Protein-Heme Nanowires for Power Harvesting",
            "# Unrelated retitled glance",
        ),
        encoding="utf-8",
    )
    manager.refresh_now()
    payload = annex_lib_search(manager, title="unrelated retitled")
    assert payload["total_works"] == 1
    manager.stop()


def test_search_requires_a_criterion(manager: IndexManager) -> None:
    with pytest.raises(ToolError) as error:
        annex_lib_search(manager)
    assert error.value.code == "invalid_argument"


def test_search_rejects_invalid_dates(manager: IndexManager) -> None:
    with pytest.raises(ToolError):
        annex_lib_search(manager, captured_from="not-a-date")
    with pytest.raises(ToolError):
        annex_lib_search(manager, captured_from="2026-11-20", captured_to="2026-11-01")


def test_search_limit_is_capped(manager: IndexManager) -> None:
    with pytest.raises(ToolError):
        annex_lib_search(manager, text="library", limit=1000)


# --------------------------------------------------------------------------- #
# Normalisation helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://doi.org/10.1002/SMLL.202311661", "10.1002/smll.202311661"),
        ("doi:10.1002/smll.202311661", "10.1002/smll.202311661"),
        ("10.1002/smll.202311661", "10.1002/smll.202311661"),
    ],
)
def test_normalize_doi(raw: str, expected: str) -> None:
    assert normalize_doi(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2024-04-10", "2024-04-10"),
        ("2024-04-10T00:00:00+08:00", "2024-04-10"),
        ("2026-11-07 1", "2026-11-07"),
        ("unknown", None),
        (None, None),
        ("2026-11", None),
    ],
)
def test_parse_iso_date(raw, expected) -> None:
    parsed = parse_iso_date(raw)
    assert (parsed.isoformat() if parsed else None) == expected


@pytest.mark.parametrize(
    "filename,expected",
    [
        (
            "Small - 2024 - Travaglini - Fabrication of Electronically Conductive "
            "Protein%E2%80%90Heme Nanowires.pdf",
            "Fabrication of Electronically Conductive Protein‐Heme Nanowires",
        ),
        (
            "[Methods in Enzymology (456)] Allison, Scheffler. (2009). "
            "Mitochondrial Function Part A.pdf",
            "Allison, Scheffler. Mitochondrial Function Part A",
        ),
        ("王志军. (2021). 电子技术基础.pdf", "王志军. 电子技术基础"),
    ],
)
def test_title_from_filename(filename: str, expected: str) -> None:
    assert title_from_filename(filename) == expected
