"""Acceptance tests for ticket annex-library-5zr.2 (ISBN and SelfHosted glances).

A ``*-glance.md`` file at the bundle root is the catalog record.  Everything
else tracked in the bundle -- including Markdown distillation documents -- is
an attachment, and a bundle without a glance keeps working by deriving metadata
from archived filenames with a warning.
"""

from __future__ import annotations

from pathlib import Path

from annex_library.catalog import catalog_snapshot, is_glance_name
from annex_library.index import Index
from annex_library.refresh import IndexManager
from annex_library.tools import annex_lib_check, annex_lib_search
from annex_library.validation import check_catalog

from tests.fixtures import _pdf_bytes, _run, _write, _write_bytes, build_fixture

ISBN = "978-0-306-40615-7"
ISBN_BUNDLE = f"ISBN/ISBN-{ISBN}"
ISBN_GLANCE = f"{ISBN_BUNDLE}/manual-glance.md"
LEGACY_ISBN_BUNDLE = "ISBN/ISBN-978-0-12-457551-X"

WILEY_GLANCE = (
    "DOI/DOI-10.1002_smll.202311661/"
    "SMALL-Travaglini-2024-04-10-Conductive-Protein-Heme-Nanowires/wiley-glance.md"
)

SELFHOSTED_ID = "20261125T120037.076932Z"
SELFHOSTED_BUNDLE = f"SelfHosted/{SELFHOSTED_ID}"
SELFHOSTED_GLANCE = f"{SELFHOSTED_BUNDLE}/manual-glance.md"

ISBN_GLANCE_TEXT = f"""\
---
title: Quantum Chemistry
authors: [Ira N. Levine]
isbn: {ISBN}
source: publisher
published: 2010
captured: 2026-11-25
stage: library
tags: [chemistry, quantum-chemistry]
publisher: Springer
edition: 5th
language: en
---

# Quantum Chemistry

A distillation of the archived PDF.
"""

SELFHOSTED_GLANCE_TEXT = f"""\
---
id: {SELFHOSTED_ID}
title: Field Report on Catalogs
authors: [Ada Lovelace]
source: internal
captured: 2026-11-25
tags: [report, catalog]
---

# Field Report on Catalogs

Body of the report.
"""


def _manager(root: Path, tmp_path: Path, *, started: bool = False) -> IndexManager:
    manager = IndexManager(Index(tmp_path / "index.sqlite3", root), watch=False)
    if started:
        manager.start()
    return manager


def _commit(root: Path, *paths: str) -> None:
    _run(root, "add", *paths)
    _run(root, "commit", "-qm", "fixture: " + ", ".join(paths))


def _isbn_glance_library(
    tmp_path: Path, *, with_pdf: bool = True, with_chapter: bool = True
) -> Path:
    """Fixture checkout plus the ISBN bundle that carries a glance record."""
    root = build_fixture(tmp_path / "Library")
    _write(root, ISBN_GLANCE, ISBN_GLANCE_TEXT)
    staged = [ISBN_GLANCE]
    if with_pdf:
        _write_bytes(root, f"{ISBN_BUNDLE}/book.pdf", _pdf_bytes("isbn-book"))
        _run(root, "annex", "add", f"{ISBN_BUNDLE}/book.pdf")
    if with_chapter:
        _write(root, f"{ISBN_BUNDLE}/ch01/verified.md", "# chapter distillation\n")
        staged.append(f"{ISBN_BUNDLE}/ch01/verified.md")
    _commit(root, *staged)
    return root


# --------------------------------------------------------------------------- #
# 1. ISBN glance record and its Markdown/PDF attachments
# --------------------------------------------------------------------------- #


def test_isbn_glance_is_the_record_and_keeps_markdown_attachments(tmp_path: Path) -> None:
    root = _isbn_glance_library(tmp_path)
    manager = _manager(root, tmp_path, started=True)
    try:
        payload = annex_lib_search(manager, collection="ISBN")
        work = next(w for w in payload["works"] if w["matched_version"] == ISBN_GLANCE)
        version = work["versions"][0]

        assert version["kind"] == "book"
        assert version["record_path"] == ISBN_GLANCE
        assert version["title"] == "Quantum Chemistry"
        assert version["authors"] == ["Ira N. Levine"]
        assert version["identifiers"]["isbn"] == ISBN
        assert version["journal"] == "Springer"
        assert version["published"] == "2010"
        assert version["captured"] == "2026-11-25"
        assert version["extra"]["stage"] == "library"
        assert version["extra"]["publisher"] == "Springer"
        assert version["extra"]["edition"] == "5th"
        assert version["extra"]["language"] == "en"

        attachments = {a["path"]: a["role"] for a in version["attachments"]}
        assert attachments == {
            f"{ISBN_BUNDLE}/book.pdf": "document",
            f"{ISBN_BUNDLE}/ch01/verified.md": "file",
        }
        assert ISBN_GLANCE not in attachments
    finally:
        manager.stop()


def test_search_by_isbn_finds_the_isbn_glance(tmp_path: Path) -> None:
    root = _isbn_glance_library(tmp_path, with_pdf=False, with_chapter=False)
    manager = _manager(root, tmp_path, started=True)
    try:
        payload = annex_lib_search(manager, text=ISBN)
        assert payload["total_works"] == 1
        assert payload["works"][0]["versions"][0]["identifiers"]["isbn"] == ISBN
    finally:
        manager.stop()


# --------------------------------------------------------------------------- #
# 2. Legacy ISBN bundle without a glance
# --------------------------------------------------------------------------- #


def test_legacy_isbn_bundle_without_a_glance_stays_searchable(manager: IndexManager) -> None:
    payload = annex_lib_search(manager, collection="ISBN")
    assert payload["total_works"] == 1
    work = payload["works"][0]
    assert work["matched_version"] == LEGACY_ISBN_BUNDLE

    version = work["versions"][0]
    assert version["record_path"] is None
    assert version["revision_kind"] == "attachment"
    assert version["title"].startswith("Lowe, Peterson.")
    assert version["warnings"] == [
        "no glance: book metadata derived from archived filenames"
    ]


# --------------------------------------------------------------------------- #
# 3. SelfHosted glance with assets and a legacy bundle beside it
# --------------------------------------------------------------------------- #


def test_selfhosted_glance_is_the_record_with_its_id_and_assets(tmp_path: Path) -> None:
    root = build_fixture(tmp_path / "Library")
    _write(root, SELFHOSTED_GLANCE, SELFHOSTED_GLANCE_TEXT)
    _write_bytes(root, f"{SELFHOSTED_BUNDLE}/assets/report.pdf", _pdf_bytes("selfhosted"))
    _write(root, f"{SELFHOSTED_BUNDLE}/assets/notes.md", "# supporting notes\n")
    _run(root, "annex", "add", f"{SELFHOSTED_BUNDLE}/assets/report.pdf")
    _commit(root, SELFHOSTED_GLANCE, f"{SELFHOSTED_BUNDLE}/assets/notes.md")

    manager = _manager(root, tmp_path, started=True)
    try:
        payload = annex_lib_search(manager, collection="SelfHosted")
        assert payload["total_works"] == 1
        work = payload["works"][0]
        assert work["matched_version"] == SELFHOSTED_GLANCE

        version = work["versions"][0]
        assert version["kind"] == "document"
        assert version["record_path"] == SELFHOSTED_GLANCE
        assert version["identifiers"]["id"] == SELFHOSTED_ID
        assert version["title"] == "Field Report on Catalogs"
        assert version["authors"] == ["Ada Lovelace"]

        roles = {a["path"]: a["role"] for a in version["attachments"]}
        assert roles == {
            f"{SELFHOSTED_BUNDLE}/assets/report.pdf": "document",
            f"{SELFHOSTED_BUNDLE}/assets/notes.md": "file",
        }
        assert SELFHOSTED_GLANCE not in roles
    finally:
        manager.stop()


def test_selfhosted_legacy_bundle_without_a_glance_warns(tmp_path: Path) -> None:
    root = build_fixture(tmp_path / "Library")
    legacy_bundle = "SelfHosted/20190101T000000.000000Z"
    _write_bytes(root, f"{legacy_bundle}/report.pdf", _pdf_bytes("selfhosted-legacy"))
    _run(root, "annex", "add", f"{legacy_bundle}/report.pdf")
    _run(root, "commit", "-qm", "fixture: legacy selfhosted bundle")

    manager = _manager(root, tmp_path, started=True)
    try:
        payload = annex_lib_search(manager, collection="SelfHosted")
        assert payload["total_works"] == 1
        work = payload["works"][0]
        assert work["matched_version"] == legacy_bundle

        version = work["versions"][0]
        assert version["record_path"] is None
        assert version["title"] == "report"
        assert version["warnings"] == [
            "no glance: document metadata derived from archived filenames"
        ]
    finally:
        manager.stop()


# --------------------------------------------------------------------------- #
# 4. Glance discovery: untracked glosses yes, other Markdown no
# --------------------------------------------------------------------------- #


def test_catalog_snapshot_discovers_an_untracked_isbn_glance(tmp_path: Path) -> None:
    root = build_fixture(tmp_path / "Library")
    manager = _manager(root, tmp_path, started=True)
    try:
        assert annex_lib_search(manager, collection="ISBN")["total_works"] == 1

        # Deliberately untracked: a glance written into an existing bundle must
        # become visible without `git add`.
        untracked_glance = f"{LEGACY_ISBN_BUNDLE}/manual-glance.md"
        _write(
            root,
            untracked_glance,
            ISBN_GLANCE_TEXT.replace(f"isbn: {ISBN}", "isbn: 978-0-12-457551-X")
            .replace("Quantum Chemistry", "Recovered Legacy Book"),
        )
        files, _ = catalog_snapshot(root)
        assert untracked_glance in files
        assert files[untracked_glance] == ""

        assert manager.refresh_now()
        payload = annex_lib_search(manager, collection="ISBN")
        assert payload["total_works"] == 1
        version = payload["works"][0]["versions"][0]
        assert version["record_path"] == untracked_glance
        assert version["title"] == "Recovered Legacy Book"
        assert version["identifiers"]["isbn"] == "978-0-12-457551-X"
        assert not version.get("warnings")
    finally:
        manager.stop()


def test_catalog_snapshot_discovers_an_untracked_selfhosted_glance(tmp_path: Path) -> None:
    root = build_fixture(tmp_path / "Library")
    bundle = "SelfHosted/20270101T000000.000000Z"
    _write_bytes(root, f"{bundle}/report.pdf", _pdf_bytes("selfhosted-untracked"))
    _run(root, "annex", "add", f"{bundle}/report.pdf")
    _run(root, "commit", "-qm", "fixture: selfhosted bundle before the glance")

    manager = _manager(root, tmp_path, started=True)
    try:
        assert annex_lib_search(manager, collection="SelfHosted")["total_works"] == 1

        _write(root, f"{bundle}/manual-glance.md",
               SELFHOSTED_GLANCE_TEXT.replace(SELFHOSTED_ID, "20270101T000000.000000Z"))
        files, _ = catalog_snapshot(root)
        assert f"{bundle}/manual-glance.md" in files
        assert manager.refresh_now()

        payload = annex_lib_search(manager, collection="SelfHosted")
        assert payload["total_works"] == 1
        version = payload["works"][0]["versions"][0]
        assert version["record_path"] == f"{bundle}/manual-glance.md"
        assert version["identifiers"]["id"] == "20270101T000000.000000Z"
    finally:
        manager.stop()


def test_catalog_snapshot_ignores_untracked_markdown_that_is_not_a_glance(
    tmp_path: Path,
) -> None:
    root = build_fixture(tmp_path / "Library")
    manager = _manager(root, tmp_path, started=True)
    try:
        assert is_glance_name("notes.md") is False
        before = annex_lib_search(manager, collection="ISBN")["works"]

        _write(root, f"{LEGACY_ISBN_BUNDLE}/notes.md", "# reading notes\n")
        _write(root, f"{LEGACY_ISBN_BUNDLE}/reading-list.md", "- one\n")
        files, _ = catalog_snapshot(root)
        assert f"{LEGACY_ISBN_BUNDLE}/notes.md" not in files
        assert f"{LEGACY_ISBN_BUNDLE}/reading-list.md" not in files

        # Nothing catalog-visible changed, so neither the index nor search moves.
        assert not manager.refresh_now()
        assert annex_lib_search(manager, collection="ISBN")["works"] == before
    finally:
        manager.stop()


# --------------------------------------------------------------------------- #
# 5. Check severities for ISBN directory names
# --------------------------------------------------------------------------- #


def test_check_legacy_isbn_name_is_a_warning_only(tmp_path: Path) -> None:
    root = build_fixture(tmp_path / "Library")
    result = annex_lib_check(_manager(root, tmp_path), path=LEGACY_ISBN_BUNDLE)

    assert result["ok"] is True
    assert {problem["rule"] for problem in result["diagnostics"]} == {"isbn.name"}
    assert {problem["severity"] for problem in result["diagnostics"]} == {"warning"}


def test_check_glance_backed_isbn_name_is_an_error(tmp_path: Path) -> None:
    root = build_fixture(tmp_path / "Library")
    bad_bundle = "ISBN/ISBN-978-0-306-40615-8"
    _write(root, f"{bad_bundle}/manual-glance.md",
           ISBN_GLANCE_TEXT.replace(ISBN, "978-0-306-40615-8"))
    _commit(root, f"{bad_bundle}/manual-glance.md")

    result = annex_lib_check(_manager(root, tmp_path), path=bad_bundle)

    assert result["ok"] is False
    name_problems = [
        problem for problem in result["diagnostics"] if problem["rule"] == "isbn.name"
    ]
    assert name_problems
    assert {problem["severity"] for problem in name_problems} == {"error"}

    # The glance-less fixture bundle with the same class of defect stays a warning.
    legacy = annex_lib_check(_manager(root, tmp_path), path=LEGACY_ISBN_BUNDLE)
    assert legacy["ok"] is True
    assert {problem["severity"] for problem in legacy["diagnostics"]} == {"warning"}


def test_check_isbn_collision_requires_shared_digits(tmp_path: Path) -> None:
    root = build_fixture(tmp_path / "Library")
    _write(root, ISBN_GLANCE, ISBN_GLANCE_TEXT)
    _commit(root, ISBN_GLANCE)
    manager = _manager(root, tmp_path)

    single = annex_lib_check(manager, path=ISBN_BUNDLE)
    assert single["ok"] is True
    assert not [p for p in single["diagnostics"] if p["rule"].startswith("isbn.")]

    # Same digits, different hyphenation: a directory-level collision.
    second = "ISBN/ISBN-9780306406157"
    _write(root, f"{second}/manual-glance.md",
           ISBN_GLANCE_TEXT.replace(f"isbn: {ISBN}", 'isbn: "9780306406157"'))
    _commit(root, f"{second}/manual-glance.md")

    problems = check_catalog(root)[1]
    collisions = [p for p in problems if p["rule"] == "isbn.collision"]
    assert {p["path"] for p in collisions} == {ISBN_BUNDLE, second}
    assert {p["severity"] for p in collisions} == {"error"}
    assert all("ISBN-9780306406157" in p["message"] for p in collisions)

    # by_identifier keys on the raw identifier string, so hyphenation keeps
    # these two records apart even though their digits are identical.
    assert not [
        p for p in problems
        if p["rule"] == "identifier.collision" and p["path"].startswith("ISBN/")
    ]


# --------------------------------------------------------------------------- #
# 6. Identifier collisions
# --------------------------------------------------------------------------- #


def test_isbn_glance_entry_does_not_collide_with_its_own_bundle(tmp_path: Path) -> None:
    root = _isbn_glance_library(tmp_path, with_pdf=False, with_chapter=False)
    result = annex_lib_check(_manager(root, tmp_path), path=ISBN_BUNDLE)

    assert result["ok"] is True
    assert result["diagnostics"] == []
    assert not [
        p for p in check_catalog(root)[1]
        if p["rule"] == "identifier.collision" and p["path"].startswith("ISBN/")
    ]


def test_identifier_collision_reports_a_doi_shared_across_bundles(tmp_path: Path) -> None:
    root = build_fixture(tmp_path / "Library")
    second_bundle = "DOI/DOI-10.1002_other.202400001/SECOND-Example-2024-01-01-Other-Paper"
    second_glance = f"{second_bundle}/second-glance.md"
    _write(
        root,
        second_glance,
        """\
---
type: paper
doi: 10.1002/smll.202311661
source: Crossref
published: 2024-01-01
captured: 2026-11-25
stage: library
tags: [fixture]
---

# Second paper that reuses the Wiley DOI
""",
    )
    _commit(root, second_glance)

    problems = check_catalog(root)[1]
    hits = [
        p for p in problems
        if p["rule"] == "identifier.collision"
        and "10.1002/smll.202311661" in p["message"]
    ]
    assert {p["path"] for p in hits} == {WILEY_GLANCE, second_glance}
    assert {p["severity"] for p in hits} == {"error"}


def test_identifier_collision_reports_a_shared_isbn_value(tmp_path: Path) -> None:
    root = build_fixture(tmp_path / "Library")
    other_bundle = "ISBN/ISBN-978-0-13-110362-7"
    other_glance = f"{other_bundle}/manual-glance.md"
    _write(root, ISBN_GLANCE, ISBN_GLANCE_TEXT)
    _write(root, other_glance,
           ISBN_GLANCE_TEXT.replace(f"isbn: {ISBN}", f'isbn: "{ISBN}"'))
    _commit(root, ISBN_GLANCE, other_glance)

    problems = check_catalog(root)[1]
    hits = [
        p for p in problems
        if p["rule"] == "identifier.collision"
        and f"ISBN identifier {ISBN}" in p["message"]
    ]
    assert {p["path"] for p in hits} == {ISBN_GLANCE, other_glance}
    assert {p["severity"] for p in hits} == {"error"}
    # The second record's frontmatter disagrees with its bundle name.
    assert any(
        p["rule"] == "isbn.bundle" and p["path"] == other_glance for p in problems
    )
    # Distinct directory digits: no directory-level collision.
    assert not [p for p in problems if p["rule"] == "isbn.collision"]
