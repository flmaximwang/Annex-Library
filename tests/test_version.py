"""The package version is one value, wired into every surface that reports it."""

from __future__ import annotations

import tomllib
from pathlib import Path

from annex_library import __version__
from annex_library.server import build_server

REPO = Path(__file__).resolve().parents[1]


def test_metadata_and_runtime_versions_agree():
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == __version__


def test_server_info_reports_the_package_version(manager):
    server = build_server(manager)
    assert server.version == __version__
    assert __version__ != ""
