"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from annex_library.index import Index
from annex_library.refresh import IndexManager
from tests.fixtures import build_fixture


@pytest.fixture(scope="session")
def fixture_library(tmp_path_factory) -> Path:
    return build_fixture(tmp_path_factory.mktemp("library") / "Library")


@pytest.fixture()
def manager(fixture_library, tmp_path) -> IndexManager:
    manager = IndexManager(
        index=Index(tmp_path / "index.sqlite3", fixture_library), poll_interval=0.2, watch=False
    )
    manager.start()
    yield manager
    manager.stop()
