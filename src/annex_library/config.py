"""Configuration for the Annex Library MCP service.

The service always works against exactly one configured Library checkout.
The SQLite index deliberately lives *outside* that checkout so that rebuilding
or deleting it never shows up as a catalog change.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# Environment variable names are part of the public interface of the service.
ENV_LIBRARY_ROOT = "ANNEX_LIBRARY_ROOT"
ENV_INDEX_PATH = "ANNEX_LIBRARY_INDEX"
ENV_WATCH = "ANNEX_LIBRARY_WATCH"

DEFAULT_LIBRARY_ROOT = Path.home() / "digitalmind" / "Library"
INDEX_FILENAME = "index-v1.sqlite3"


def default_index_path() -> Path:
    """Return the default index path, which is never inside the checkout."""
    override = os.environ.get(ENV_INDEX_PATH)
    if override:
        return Path(override).expanduser()
    cache_home = os.environ.get("XDG_CACHE_HOME")
    if cache_home:
        base = Path(cache_home).expanduser() / "annex-library"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "annex-library"
    else:
        base = Path.home() / ".cache" / "annex-library"
    return base / INDEX_FILENAME


def default_library_root() -> Path:
    override = os.environ.get(ENV_LIBRARY_ROOT)
    if override:
        return Path(override).expanduser()
    return DEFAULT_LIBRARY_ROOT


@dataclass(frozen=True)
class Config:
    library_root: Path
    index_path: Path
    watch: bool = True

    @classmethod
    def from_env(
        cls,
        library_root: str | os.PathLike[str] | None = None,
        index_path: str | os.PathLike[str] | None = None,
    ) -> "Config":
        root = Path(library_root).expanduser() if library_root else default_library_root()
        idx = Path(index_path).expanduser() if index_path else default_index_path()
        watch_env = os.environ.get(ENV_WATCH, "1").strip().lower()
        watch = watch_env not in {"0", "false", "no", "off"}
        return cls(library_root=root.resolve(), index_path=idx.resolve(), watch=watch)

    def check(self) -> None:
        """Validate the configuration before any work is attempted."""
        if not self.library_root.is_dir():
            raise FileNotFoundError(
                f"Library checkout not found: {self.library_root} "
                f"(set {ENV_LIBRARY_ROOT} or pass --library)"
            )
        if self.index_path.is_relative_to(self.library_root):
            raise ValueError(
                f"Index path {self.index_path} must live outside the Library checkout "
                f"{self.library_root}; set {ENV_INDEX_PATH} elsewhere."
            )
