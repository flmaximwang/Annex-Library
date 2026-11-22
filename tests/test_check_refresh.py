"""Validation and watchdog acceptance on isolated Library repositories."""

import time

from annex_library.index import Index
from annex_library.refresh import IndexManager
from annex_library.tools import annex_lib_check, annex_lib_search
from tests.fixtures import _run, _write, build_fixture


def test_check_isbn_collisions_bad_yaml_and_ignored_notes(tmp_path):
    root = build_fixture(tmp_path / "Library")
    manager = IndexManager(Index(tmp_path / "index.sqlite3", root), watch=False)
    valid = ("ISBN-978-0-08-087776-1", "ISBN-978-0-08-102256-6")
    invalid = ("ISBN-978-0-12-227410-7-C5XKA6VZ", "ISBN-978-0-12-227410-7-YI73UQM9")
    for name in (*valid, *invalid):
        _write(root, f"ISBN/{name}/fixture.pdf", "Fixture")
    _run(root, "add", "ISBN")
    for name in valid:
        assert annex_lib_check(manager, path=f"ISBN/{name}")["ok"]
    for name in invalid:
        result = annex_lib_check(manager, path=f"ISBN/{name}")
        assert {problem["rule"] for problem in result["diagnostics"]} == {"isbn.name", "isbn.collision"}
        assert all("ISBN-978-0-12-227410-7" in p["repair"] for p in result["diagnostics"])
    try:
        manager.start()
        before = annex_lib_check(manager)
        search = annex_lib_search(manager, doi="10.1002/smll.202311661")["works"]
        bundle = "DOI/DOI-10.1002_smll.202311661/SMALL-Fixture-2024-01-01-Example"
        _write(root, f"{bundle}/one-notes.md", "---\ntags: [bad\n---\n")
        _write(root, f"{bundle}/two-notes.md", "# Reading notes\n")
        _run(root, "add", f"{bundle}/one-notes.md", f"{bundle}/two-notes.md")
        status = _run(root, "status", "--porcelain").stdout
        assert not manager.refresh_now()
        assert annex_lib_check(manager) == before
        assert annex_lib_search(manager, doi="10.1002/smll.202311661")["works"] == search
        assert _run(root, "status", "--porcelain").stdout == status

        broken = f"{bundle}/extra-glance.md"
        _write(root, broken, "---\ntags: [bad\n---\n# Extra Fixture\n")
        assert {d["rule"] for d in annex_lib_check(manager, path=broken)["diagnostics"]} >= {"frontmatter.yaml"}
    finally:
        manager.stop()


def test_watch_force_rebuild_equivalence_after_external_changes(tmp_path):
    upstream = build_fixture(tmp_path / "Upstream")
    root = tmp_path / "Library"
    _run(tmp_path, "clone", str(upstream), str(root))
    manager = IndexManager(Index(tmp_path / "index.sqlite3", root), poll_interval=0.1)
    manager.start()
    path = "Misc/20261121T125651.861607Z/proximity-labeling-datasets-for-virtual-cells.md"
    original = (root / path).read_text()

    def expect(title, count):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = annex_lib_search(manager, title=title)
            if result["total_works"] == count:
                return result["works"]
            time.sleep(0.025)
        raise AssertionError((title, count, manager.state.last_error))

    def populate(filename, title):
        _write(root, filename, original.replace("邻近标记能为虚拟细胞提供什么数据集", title))

    try:
        populate(path, "FirstLocalTitle")
        expect("FirstLocalTitle", 1)
        populate(path, "SecondLocalTitle")
        expect("SecondLocalTitle", 1)
        renamed = path.rsplit("/", 1)[0] + "/renamed.md"
        (root / path).rename(root / renamed)
        populate(renamed, "AfterRenameTitle")
        assert expect("AfterRenameTitle", 1)[0]["matched_version"] == renamed
        expect("SecondLocalTitle", 0)
        (root / renamed).unlink()
        expect("AfterRenameTitle", 0)
        _run(root, "restore", path)

        new_path = "Misc/20261121T125651.861607Z/from-pull.md"
        _write(upstream, new_path, original.replace("邻近标记能为虚拟细胞提供什么数据集", "PulledTitle"))
        _run(upstream, "add", new_path)
        _run(upstream, "commit", "-m", "Add catalog record upstream")
        _run(root, "pull", "--ff-only")
        assert expect("PulledTitle", 1)[0]["matched_version"] == new_path
        before = annex_lib_search(manager, collection="Misc")["works"]
        manager.refresh_now(force=True)
        assert annex_lib_search(manager, collection="Misc")["works"] == before
    finally:
        manager.stop()
