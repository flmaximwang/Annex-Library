"""Focused tests for the ISBN identifier search criterion (annex-library-5zr.6).

The catalog stores identifiers in ``entries.identifiers_json`` rather than in a
normalised column, so these tests pin the two behaviours that matter: an ISBN
matches regardless of hyphenation, in both directions, and a malformed ``isbn``
argument is rejected before any query runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from annex_library.index import Index
from annex_library.refresh import IndexManager
from annex_library.tools import ToolError, annex_lib_search

from tests.fixtures import _run, _write, build_fixture, clone_fixture

ISBN = "978-0-306-40615-7"
ISBN_DIGITS = "9780306406157"
ISBN_BUNDLE = f"ISBN/ISBN-{ISBN}"
ISBN_GLANCE = f"{ISBN_BUNDLE}/manual-glance.md"

GLANCE_TEXT = """\
---
title: Quantum Chemistry
authors: [Ira N. Levine]
isbn: {isbn}
source: publisher
captured: 2026-11-25
published: 2010
stage: library
tags: [chemistry, quantum-chemistry]
---

# Quantum Chemistry

A distillation of the archived PDF.
"""


def _isbn_library(root: Path, isbn_field: str = ISBN) -> None:
    """Add one ISBN glance whose ``isbn`` frontmatter is ``isbn_field``."""
    _write(root, ISBN_GLANCE, GLANCE_TEXT.format(isbn=isbn_field))
    _run(root, "add", ISBN_GLANCE)
    _run(root, "commit", "-qm", f"fixture: ISBN glance {isbn_field}")


def _manager(root: Path, tmp_path: Path) -> IndexManager:
    manager = IndexManager(Index(tmp_path / "index.sqlite3", root), watch=False)
    manager.start()
    return manager


# --------------------------------------------------------------------------- #
# 1. Hyphen-insensitive matching, both directions
# --------------------------------------------------------------------------- #


def test_isbn_matches_regardless_of_hyphenation_in_either_direction(tmp_path: Path) -> None:
    root = build_fixture(tmp_path / "Library")
    # Stored hyphenated (the production shape): a digits-only query must find it.
    _isbn_library(root, ISBN)
    manager = _manager(root, tmp_path)
    try:
        digits_only = annex_lib_search(manager, isbn=ISBN_DIGITS)
        assert digits_only["total_works"] == 1
        assert digits_only["works"][0]["matched_version"] == ISBN_GLANCE
        assert digits_only["query"]["isbn"] == ISBN_DIGITS
        assert digits_only["works"][0]["versions"][0]["identifiers"]["isbn"] == ISBN

        # Spaced form is the same identifier as well.
        spaced = annex_lib_search(manager, isbn="978 0 306 40615 7")
        assert spaced["total_works"] == 1
    finally:
        manager.stop()

    # Stored digits-only: a hyphenated query must find it, from a cloned checkout.
    digits_root = clone_fixture(root, tmp_path / "DigitsLibrary")
    _write(digits_root, ISBN_GLANCE, GLANCE_TEXT.format(isbn=f'"{ISBN_DIGITS}"'))
    _run(digits_root, "add", ISBN_GLANCE)
    _run(digits_root, "commit", "-qm", "fixture: digits-only ISBN")
    manager = _manager(digits_root, tmp_path / "digits")
    try:
        hyphenated = annex_lib_search(manager, isbn=ISBN)
        assert hyphenated["total_works"] == 1
        assert hyphenated["works"][0]["matched_version"] == ISBN_GLANCE
        assert hyphenated["works"][0]["versions"][0]["identifiers"]["isbn"] == ISBN_DIGITS
    finally:
        manager.stop()


# --------------------------------------------------------------------------- #
# 2. Free text also matches identifier values
# --------------------------------------------------------------------------- #


def test_free_text_search_finds_the_book_by_its_isbn(tmp_path: Path) -> None:
    root = build_fixture(tmp_path / "Library")
    _isbn_library(root, ISBN)
    manager = _manager(root, tmp_path)
    try:
        for query in (ISBN, ISBN_DIGITS):
            payload = annex_lib_search(manager, text=query)
            assert payload["total_works"] == 1
            version = payload["works"][0]["versions"][0]
            assert version["record_path"] == ISBN_GLANCE
            assert version["identifiers"]["isbn"] == ISBN
    finally:
        manager.stop()


# --------------------------------------------------------------------------- #
# 3. Argument validation and unknown identifiers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["12345", "not-an-isbn", ISBN[:12]])
def test_invalid_isbn_argument_raises_invalid_argument(tmp_path: Path, value: str) -> None:
    root = build_fixture(tmp_path / "Library")
    _isbn_library(root, ISBN)
    manager = _manager(root, tmp_path)
    try:
        with pytest.raises(ToolError) as failure:
            annex_lib_search(manager, isbn=value)
        assert failure.value.code == "invalid_argument"
        assert "isbn is not a 10- or 13-digit ISBN" in str(failure.value)
    finally:
        manager.stop()


def test_unknown_isbn_returns_no_works(tmp_path: Path) -> None:
    root = build_fixture(tmp_path / "Library")
    _isbn_library(root, ISBN)
    manager = _manager(root, tmp_path)
    try:
        # A valid ISBN-13 with a correct check digit that the catalog never saw.
        payload = annex_lib_search(manager, isbn="978-1-4028-9462-6")
        assert payload["total_works"] == 0
        assert payload["works"] == []
    finally:
        manager.stop()
