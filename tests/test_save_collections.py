"""Acceptance tests for tickets annex-library-5zr.1/.3/.4: saving every collection.

``annex_lib_save`` stages Markdown attachments as ordinary Git blobs and every
other attachment through git-annex, then dispatches on ``metadata.collection``:
a DOI paper, an ISBN ``<source>-glance.md`` book record, a server-timestamped
SelfHosted document, or a video ``summary.md`` under its platform directory.
Every test runs against a disposable checkout cloned from ``fixture_library`` so
real Git and git-annex behaviour is exercised instead of mocks.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from annex_library.catalog import split_frontmatter
from annex_library.index import Index
from annex_library.refresh import IndexManager
from annex_library.tools import ToolError, annex_lib_save, annex_lib_search
from tests.fixtures import _pdf_bytes, clone_fixture

ISBN = "978-0-306-40615-7"
ISBN_BUNDLE = f"ISBN/ISBN-{ISBN}"
TIMESTAMP_RE = re.compile(r"\d{8}T\d{6}\.\d{6}Z\Z")
ANNEX_KEY_RE = re.compile(r"SHA256E-s\d+--[0-9a-f]+(?:\.\w+)?\Z")
BOOK_OPTIONAL_FIELDS = (
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
)


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def git_result(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


@pytest.fixture
def writer(fixture_library, tmp_path):
    root = clone_fixture(fixture_library, tmp_path / "Library")
    manager = IndexManager(Index(tmp_path / "index.sqlite3", root), watch=False)
    manager.start()
    yield manager
    manager.stop()


def staged(tmp_path: Path, name: str, content, *, binary: bool = False) -> str:
    """Write a staged file outside the checkout and return its absolute path."""
    path = tmp_path / "staged" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if binary:
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return str(path)


def assert_annex_key(root: Path, path: str) -> None:
    key = git(root, "annex", "lookupkey", path)
    assert ANNEX_KEY_RE.fullmatch(key), key


def assert_git_blob(root: Path, path: str) -> None:
    """The path must be an ordinary 100644 Git object, never an annex key."""
    assert git_result(root, "annex", "lookupkey", path).stdout.strip() == ""
    mode, oid, stage, listed = re.split(r"\s+", git(root, "ls-files", "-s", path), maxsplit=3)
    assert (mode, stage, listed) == ("100644", "0", path)
    assert re.fullmatch(r"[0-9a-f]{40}", oid)


def doi_metadata(**updates) -> dict:
    return dict(
        doi="10.5555/save-collections",
        title="Save Collections Paper",
        source="publisher",
        url="https://doi.org/10.5555/save-collections",
        published="2026-01-01",
        captured="2026-11-25",
        tags=["save-collections", "acceptance"],
        authors=["A. Example"],
        version_type="journal-article",
        body="# Save Collections Paper\n",
        **updates,
    )


def isbn_metadata(**updates) -> dict:
    metadata = dict(
        collection="ISBN",
        isbn=ISBN,
        authors=["John Doe", "Ada Lovelace"],
        title="The Example Book",
        source="Zotero",
        captured="2026-11-25",
        tags=["book", "example"],
        body="# The Example Book\n",
        published="2010",
        url="",
        publisher="Plenum Press",
        edition="2nd",
        language="en",
        series="Series in Applied Physics",
        series_number="3",
        volume="1",
        number_of_volumes="2",
        place="New York",
        pages="123-456",
        short_title="Example",
    )
    metadata.update(updates)
    return metadata


def selfhosted_metadata(**updates) -> dict:
    return dict(
        collection="SelfHosted",
        title="Field Report on Catalogs",
        source="internal",
        url="https://example.org/reports/catalogs",
        published="2026-01-01",
        captured="2026-11-25",
        tags=["report", "catalog"],
        body="# Field Report on Catalogs\n",
        **updates,
    )


def video_metadata(
    collection: str,
    video_id: str,
    *,
    title: str = "Conductive AFM Webinar",
    author: str = "Park Systems",
    published: str = "2026-11-05T18:25:00+08:00",
    **updates,
) -> dict:
    return dict(
        collection=collection,
        video_id=video_id,
        title=title,
        authors=[author, "Second Author"],
        published=published,
        source=collection,
        url=f"https://example.org/video/{video_id}",
        captured="2026-11-25",
        tags=["video", "webinar"],
        body="# Conductive AFM Webinar\n",
        **updates,
    )


# --------------------------------------------------------------------------- #
# 1. Markdown attachments are Git text, everything else stays in git-annex
# --------------------------------------------------------------------------- #


def test_doi_save_commits_markdown_to_git_and_pdf_to_annex(writer, tmp_path: Path) -> None:
    root = writer.index.library_root
    notes = staged(tmp_path, "reading-notes.md", "# Reading notes\n")
    pdf = staged(tmp_path, "paper.pdf", _pdf_bytes("markdown-mixture"), binary=True)

    saved = annex_lib_save(
        writer, metadata=doi_metadata(), staged_files=[notes, pdf], request_id="doi-markdown-mix"
    )
    assert saved["status"] == "committed"
    assert saved["replayed"] is False

    markdown_path = f"{saved['bundle']}/reading-notes.md"
    pdf_path = f"{saved['bundle']}/paper.pdf"
    assert set(git(root, "show", "--format=", "--name-only", saved["commit_sha"]).splitlines()) == {
        saved["record_path"],
        markdown_path,
        pdf_path,
    }

    assert_annex_key(root, pdf_path)
    # The Markdown attachment is a Git blob: no annex key, plain 100644 entry.
    assert_git_blob(root, markdown_path)
    assert git(root, "show", f"{saved['commit_sha']}:{markdown_path}") == "# Reading notes"
    assert git(root, "rev-parse", "HEAD") == saved["commit_sha"]

    # The same staged bytes under a fresh request_id are a no-op, not a second copy.
    again = annex_lib_save(
        writer,
        metadata=doi_metadata(),
        staged_files=[notes, pdf],
        request_id="doi-markdown-mix-again",
    )
    assert again["status"] == "unchanged"
    assert again["commit_sha"] is None
    assert again["record_path"] == saved["record_path"]


def test_doi_save_replays_and_rejects_changed_markdown_bytes(writer, tmp_path: Path) -> None:
    root = writer.index.library_root
    notes = staged(tmp_path, "reading-notes.md", "# Reading notes\n")
    pdf = staged(tmp_path, "paper.pdf", _pdf_bytes("markdown-replay"), binary=True)
    arguments = dict(metadata=doi_metadata(), staged_files=[notes, pdf])

    first = annex_lib_save(writer, **arguments, request_id="doi-markdown-replay")
    assert first["status"] == "committed"

    replay = annex_lib_save(writer, **arguments, request_id="doi-markdown-replay")
    assert replay["replayed"] is True
    assert replay["commit_sha"] == first["commit_sha"]
    assert replay["record_path"] == first["record_path"]

    staged(tmp_path, "reading-notes.md", "# Reading notes, revised\n")
    with pytest.raises(ToolError) as failure:
        annex_lib_save(writer, **arguments, request_id="doi-markdown-replay")
    assert failure.value.code == "conflict"
    assert "different payload" in str(failure.value)

    with pytest.raises(ToolError) as failure:
        annex_lib_save(writer, **arguments, request_id="doi-markdown-revised")
    assert failure.value.code == "conflict"
    assert "different contents" in str(failure.value)
    assert git(root, "rev-parse", "HEAD") == first["commit_sha"]


# --------------------------------------------------------------------------- #
# 2. ISBN books: glance record, Zotero fields, reuse, malformed neighbours
# --------------------------------------------------------------------------- #


def test_isbn_save_writes_a_glance_that_round_trips_zotero_fields(writer) -> None:
    root = writer.index.library_root
    metadata = isbn_metadata()
    saved = annex_lib_save(writer, metadata=metadata, request_id="isbn-zotero")

    assert saved["status"] == "committed"
    assert saved["bundle"] == ISBN_BUNDLE
    assert saved["record_path"] == f"{ISBN_BUNDLE}/zotero-glance.md"

    fields, body, error = split_frontmatter((root / saved["record_path"]).read_text(encoding="utf-8"))
    assert error is None and fields is not None
    assert fields["isbn"] == ISBN
    assert fields["title"] == "The Example Book"
    assert fields["authors"] == ["John Doe", "Ada Lovelace"]
    assert fields["source"] == "Zotero"
    assert fields["captured"] == "2026-11-25"
    assert fields["stage"] == "library"
    assert fields["tags"] == ["book", "example"]
    # A year-only publication date and an empty URL are both valid for a book.
    assert fields["published"] == "2010"
    assert fields["url"] == ""
    for key in BOOK_OPTIONAL_FIELDS:
        assert fields[key] == metadata[key]
    assert "id" not in fields
    assert body == "# The Example Book\n"

    by_title = annex_lib_search(writer, title="Example Book")
    assert by_title["total_works"] == 1
    version = by_title["works"][0]["versions"][0]
    assert version["record_path"] == saved["record_path"]
    assert version["identifiers"]["isbn"] == ISBN

    by_collection = annex_lib_search(writer, collection="ISBN")
    assert saved["record_path"] in {
        nested["record_path"]
        for work in by_collection["works"]
        for nested in work["versions"]
    }


def test_isbn_pure_markdown_save_places_git_attachments_beside_the_glance(writer, tmp_path: Path) -> None:
    root = writer.index.library_root
    notes = staged(tmp_path, "book-notes.md", "# Book notes\n")

    saved = annex_lib_save(
        writer, metadata=isbn_metadata(), staged_files=[notes], request_id="isbn-markdown-only"
    )
    assert saved["status"] == "committed"

    # ISBN attachments live at the bundle root, next to the glance record.
    markdown_path = f"{saved['bundle']}/book-notes.md"
    assert set(git(root, "show", "--format=", "--name-only", saved["commit_sha"]).splitlines()) == {
        saved["record_path"],
        markdown_path,
    }
    assert_git_blob(root, markdown_path)
    assert (root / markdown_path).read_text(encoding="utf-8") == "# Book notes\n"


def test_isbn_save_reuses_the_bundle_for_a_differently_hyphenated_isbn(writer) -> None:
    root = writer.index.library_root
    first = annex_lib_save(writer, metadata=isbn_metadata(), request_id="isbn-reuse-first")
    assert first["bundle"] == ISBN_BUNDLE

    # Same 13 digits, no hyphens: must land in the existing canonical directory.
    again = annex_lib_save(
        writer, metadata=isbn_metadata(isbn="9780306406157"), request_id="isbn-reuse-second"
    )
    assert again["bundle"] == first["bundle"] == ISBN_BUNDLE
    assert again["record_path"] == first["record_path"]
    assert again["status"] == "unchanged"
    assert again["commit_sha"] is None

    bundles = sorted(path.name for path in (root / "ISBN").iterdir() if path.is_dir())
    assert bundles == ["ISBN-978-0-12-457551-X", "ISBN-978-0-306-40615-7"]


@pytest.mark.parametrize(
    "bad_isbn",
    ["978-0-306-40615-6", "9780306406156", "ISBN 978-0-306-40615-6", "not-an-isbn", ""],
)
def test_isbn_save_rejects_an_invalid_check_digit(writer, bad_isbn: str) -> None:
    with pytest.raises(ToolError) as failure:
        annex_lib_save(writer, metadata=isbn_metadata(isbn=bad_isbn), request_id=f"isbn-bad-{bad_isbn}")
    assert failure.value.code == "invalid_argument"
    assert "metadata.isbn" in str(failure.value)
    assert not (writer.index.library_root / ISBN_BUNDLE).exists()


def test_isbn_save_conflicts_with_a_suffixed_bundle_for_the_same_isbn(writer) -> None:
    """A bundle for these digits that is not a canonical directory blocks the save."""
    root = writer.index.library_root
    suffixed = root / f"ISBN/ISBN-{ISBN}-ABC123"
    suffixed.mkdir(parents=True)
    (suffixed / "book.pdf").write_bytes(_pdf_bytes("suffixed-isbn"))
    git(root, "add", "ISBN")
    git(root, "commit", "-qm", "fixture: suffixed ISBN bundle")

    with pytest.raises(ToolError) as failure:
        annex_lib_save(writer, metadata=isbn_metadata(), request_id="isbn-suffixed")
    assert failure.value.code == "conflict"
    assert "unverified suffix" in str(failure.value)
    # No second canonical directory may appear beside the unverified one.
    assert not (root / ISBN_BUNDLE).exists()
    assert sorted(path.name for path in (root / "ISBN").iterdir()) == [
        "ISBN-978-0-12-457551-X",
        f"ISBN-{ISBN}-ABC123",
    ]


def test_isbn_save_is_idempotent_and_writes_a_single_glance(writer) -> None:
    """Repeating an ISBN save under a new request_id must not add a second record."""
    root = writer.index.library_root
    first = annex_lib_save(writer, metadata=isbn_metadata(), request_id="isbn-once-1")
    assert first["record_path"] == f"{ISBN_BUNDLE}/zotero-glance.md"

    again = annex_lib_save(writer, metadata=isbn_metadata(), request_id="isbn-once-2")
    assert again["status"] == "unchanged"
    assert again["commit_sha"] is None
    assert again["record_path"] == first["record_path"]
    assert sorted(path.name for path in (root / ISBN_BUNDLE).glob("*-glance.md")) == ["zotero-glance.md"]
    assert git(root, "show", "--format=", "--name-only", first["commit_sha"]).splitlines() == [
        first["record_path"]
    ]


def test_search_by_isbn_text_finds_the_saved_glance(writer) -> None:
    saved = annex_lib_save(writer, metadata=isbn_metadata(), request_id="isbn-searchable")
    for query in (ISBN, "9780306406157"):
        payload = annex_lib_search(writer, text=query)
        assert payload["total_works"] == 1
        version = payload["works"][0]["versions"][0]
        assert version["record_path"] == saved["record_path"]
        assert version["identifiers"]["isbn"] == ISBN


# --------------------------------------------------------------------------- #
# 3. SelfHosted documents: server-allocated timestamp id and assets/
# --------------------------------------------------------------------------- #


def test_selfhosted_save_allocates_a_utc_timestamp_bundle(writer, tmp_path: Path) -> None:
    root = writer.index.library_root
    transcript = staged(tmp_path, "field-notes.md", "# Field notes\n")
    saved = annex_lib_save(
        writer, metadata=selfhosted_metadata(), staged_files=[transcript], request_id="selfhosted-1"
    )

    assert saved["status"] == "committed"
    assert TIMESTAMP_RE.fullmatch(saved["id"]), saved["id"]
    assert saved["bundle"] == f"SelfHosted/{saved['id']}"
    assert saved["record_path"] == f"{saved['bundle']}/internal-glance.md"

    fields, body, error = split_frontmatter((root / saved["record_path"]).read_text(encoding="utf-8"))
    assert error is None and fields is not None
    assert fields["id"] == saved["id"]
    assert fields["title"] == "Field Report on Catalogs"
    assert fields["url"] == "https://example.org/reports/catalogs"
    assert fields["published"] == "2026-01-01"
    assert fields["stage"] == "library"
    assert body == "# Field Report on Catalogs\n"

    asset = f"{saved['bundle']}/assets/field-notes.md"
    assert set(git(root, "show", "--format=", "--name-only", saved["commit_sha"]).splitlines()) == {
        saved["record_path"],
        asset,
    }
    assert_git_blob(root, asset)

    payload = annex_lib_search(writer, title="Field Report on Catalogs")
    assert payload["total_works"] == 1
    version = payload["works"][0]["versions"][0]
    assert version["record_path"] == saved["record_path"]
    assert version["identifiers"]["id"] == saved["id"]
    assert {nested["path"] for nested in version["attachments"]} == {asset}


def test_selfhosted_save_rejects_a_caller_supplied_id(writer) -> None:
    with pytest.raises(ToolError) as failure:
        annex_lib_save(
            writer,
            metadata=selfhosted_metadata(id="20260101T000000.000000Z"),
            request_id="selfhosted-id",
        )
    assert failure.value.code == "invalid_argument"
    assert "allocates" in str(failure.value)
    assert not (writer.index.library_root / "SelfHosted").exists()


# --------------------------------------------------------------------------- #
# 4. Video platforms: summary.md, transcript under assets/, id conflicts
# --------------------------------------------------------------------------- #

VIDEO_CASES = [
    pytest.param(
        "Bilibili", "BV1SaveCol001", "Park Systems", "2026-11-05T18:25:00+08:00",
        "Park-Systems_2026-11-05_Conductive-AFM-Webinar", id="bilibili",
    ),
    pytest.param(
        "YouTube", "save-col-yt-01", "Ada Lovelace", "2026-11-05",
        "Ada-Lovelace_2026-11-05_Conductive-AFM-Webinar", id="youtube",
    ),
    pytest.param(
        "Vimeo", "77881234", "Grace Hopper", "2026-11-05T09:15:00+08:00",
        "Grace-Hopper_2026-11-05_Conductive-AFM-Webinar", id="vimeo",
    ),
]


@pytest.mark.parametrize("collection, video_id, author, published, folder", VIDEO_CASES)
def test_video_save_writes_summary_and_git_transcript(
    writer, tmp_path: Path, collection: str, video_id: str, author: str, published: str, folder: str
) -> None:
    root = writer.index.library_root
    transcript = staged(tmp_path, "transcript.md", "# Transcript\n")
    metadata = video_metadata(collection, video_id, author=author, published=published)

    saved = annex_lib_save(
        writer, metadata=metadata, staged_files=[transcript], request_id=f"video-{collection}"
    )
    bundle = f"{collection}/{video_id}/{folder}"
    assert saved["status"] == "committed"
    assert saved["bundle"] == bundle
    assert saved["record_path"] == f"{bundle}/summary.md"

    fields, body, error = split_frontmatter((root / saved["record_path"]).read_text(encoding="utf-8"))
    assert error is None and fields is not None
    assert fields["video_id"] == video_id
    assert fields["title"] == "Conductive AFM Webinar"
    assert fields["authors"] == [author, "Second Author"]
    assert str(fields["published"]) == published
    assert fields["url"] == metadata["url"]
    assert fields["stage"] == "library"
    assert body == "# Conductive AFM Webinar\n"

    asset = f"{bundle}/assets/transcript.md"
    assert set(git(root, "show", "--format=", "--name-only", saved["commit_sha"]).splitlines()) == {
        saved["record_path"],
        asset,
    }
    assert_git_blob(root, asset)

    payload = annex_lib_search(writer, title="Conductive AFM Webinar")
    assert payload["total_works"] == 1
    version = payload["works"][0]["versions"][0]
    assert version["kind"] == "video-summary"
    assert version["record_path"] == saved["record_path"]
    assert version["identifiers"]["video_id"] == video_id

    # The identical request under a new request_id changes nothing.
    again = annex_lib_save(
        writer, metadata=metadata, staged_files=[transcript], request_id=f"video-{collection}-again"
    )
    assert again["status"] == "unchanged"
    assert again["commit_sha"] is None
    assert again["record_path"] == saved["record_path"]


@pytest.mark.parametrize(
    "collection, video_id",
    [("Bilibili", "BV1Conflict01"), ("YouTube", "yt-conflict-01"), ("Vimeo", "77881235")],
)
def test_video_save_conflicts_when_the_same_id_has_a_different_folder(
    writer, collection: str, video_id: str
) -> None:
    metadata = video_metadata(collection, video_id)
    first = annex_lib_save(writer, metadata=metadata, request_id=f"video-conflict-{collection}-1")
    assert first["status"] == "committed"

    with pytest.raises(ToolError) as failure:
        annex_lib_save(
            writer,
            metadata={**metadata, "title": "A Different Video Title"},
            request_id=f"video-conflict-{collection}-2",
        )
    assert failure.value.code == "conflict"
    assert video_id in str(failure.value)


@pytest.mark.parametrize("video_id", ["BV1Save/x", "BV1 Save", "BV1Save\tchild", "../escape", ""])
def test_video_save_rejects_identifiers_with_separators_or_whitespace(writer, video_id: str) -> None:
    root = writer.index.library_root
    before = sorted(path.name for path in (root / "Bilibili").iterdir())
    with pytest.raises(ToolError) as failure:
        annex_lib_save(
            writer, metadata=video_metadata("Bilibili", video_id), request_id=f"video-bad-{video_id!r}"
        )
    assert failure.value.code == "invalid_argument"
    assert "metadata.video_id" in str(failure.value)
    assert sorted(path.name for path in (root / "Bilibili").iterdir()) == before


# --------------------------------------------------------------------------- #
# 5. Collection dispatch
# --------------------------------------------------------------------------- #


def test_collection_is_inferred_from_isbn_when_omitted(writer) -> None:
    metadata = isbn_metadata()
    del metadata["collection"]
    saved = annex_lib_save(writer, metadata=metadata, request_id="isbn-inferred")
    assert saved["status"] == "committed"
    assert saved["bundle"] == ISBN_BUNDLE
    assert saved["record_path"] == f"{ISBN_BUNDLE}/zotero-glance.md"


def test_body_fallback_infers_misc_when_no_identifier_names_a_collection(writer) -> None:
    metadata = dict(
        title="Journal Digest",
        source="feed",
        url="https://example.org/digest",
        published="unknown",
        captured="2026-11-25",
        tags=["digest"],
        body="# Journal Digest\n",
    )
    saved = annex_lib_save(writer, metadata=metadata, request_id="misc-inferred")
    assert saved["status"] == "committed"
    assert saved["bundle"] == f"Misc/{saved['id']}"
    assert TIMESTAMP_RE.fullmatch(saved["id"])
    assert saved["record_path"] == f"{saved['bundle']}/Journal-Digest.md"


def test_video_metadata_without_a_collection_or_body_reports_the_missing_collection(writer) -> None:
    metadata = video_metadata("Bilibili", "BV1NoCollection002")
    del metadata["collection"]
    del metadata["body"]
    with pytest.raises(ToolError) as failure:
        annex_lib_save(writer, metadata=metadata, request_id="video-no-collection-body")
    assert failure.value.code == "invalid_argument"
    assert "metadata.collection is required" in str(failure.value)
    assert not (writer.index.library_root / "Bilibili/BV1NoCollection002").exists()


def test_video_metadata_without_a_collection_is_rejected(writer) -> None:
    """A video identifier never falls back to Misc: the platform must be explicit."""
    metadata = video_metadata("Bilibili", "BV1NoCollection001")
    del metadata["collection"]
    with pytest.raises(ToolError) as failure:
        annex_lib_save(writer, metadata=metadata, request_id="video-no-collection")
    assert failure.value.code == "invalid_argument"
    assert "metadata.collection is required" in str(failure.value)
    assert not (writer.index.library_root / "Bilibili/BV1NoCollection001").exists()


def test_isbn_save_rejects_an_attachment_named_like_a_record(writer, tmp_path: Path) -> None:
    """A staged glance beside the record would index as a second catalog record."""
    root = writer.index.library_root
    clash = staged(tmp_path, "manual-glance.md", "# Manual\n")

    with pytest.raises(ToolError) as failure:
        annex_lib_save(
            writer,
            metadata=isbn_metadata(),
            staged_files=[clash],
            request_id="isbn-record-name-clash",
        )
    assert failure.value.code == "conflict"
    assert "cannot be named like a catalog record" in str(failure.value)
    assert not (root / ISBN_BUNDLE).exists()
