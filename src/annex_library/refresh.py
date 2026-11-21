"""Index lifecycle and freshness for the Annex Library service.

Freshness model
---------------
The checkout is the source of truth. The index is refreshed when the checkout
*moved*: the fingerprint is ``(HEAD, git status --porcelain)``, which covers
commits, pulls, manual edits, deletions and renames including untracked files.

A background watchdog polls the fingerprint every ``poll_interval`` seconds
(default 2 s, so a change is visible well inside the five second budget) and
rebuilds the index in a worker thread. Searches always read the current index,
so a rebuild never blocks a query; ``index_generation`` in the response tells
the caller whether it is reading a rebuilt index.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import run_git
from .index import Index, IndexStats


def checkout_fingerprint(root: Path) -> str:
    """Cheap fingerprint of every catalog-relevant change in the checkout."""
    parts: list[bytes] = []
    try:
        parts.append(run_git(root, "rev-parse", "HEAD", check=False))
    except RuntimeError:
        parts.append(b"")
    parts.append(run_git(root, "status", "--porcelain", "-z", "-uall", check=False))
    digest = hashlib.sha256(b"\0".join(parts)).hexdigest()
    return digest


@dataclass
class RefreshState:
    generation: int = 0
    fingerprint: str = ""
    built_at: float = 0.0
    checked_at: float = 0.0
    rebuilding: bool = False
    last_error: str | None = None
    build_count: int = 0
    stats: IndexStats | None = None


@dataclass
class IndexManager:
    index: Index
    poll_interval: float = 2.0
    # Search never pays for more than one fingerprint per window. The window is
    # far below the five second freshness budget, so a change made between two
    # queries is still visible well within it.
    check_interval: float = 0.5
    watch: bool = True
    state: RefreshState = field(default_factory=RefreshState)
    _thread: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Build if needed, then start the watchdog."""
        self.refresh_now(force=True)
        if self.watch and self._thread is None:
            thread = threading.Thread(target=self._watch_loop, name="annex-library-refresh", daemon=True)
            self._thread = thread
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self.index.close()

    def _watch_loop(self) -> None:
        while not self._stop.wait(self.poll_interval):
            try:
                if checkout_fingerprint(self.index.library_root) != self.state.fingerprint:
                    self.refresh_now(force=False)
            except Exception as exc:  # never let the watchdog die
                self.state.last_error = f"{type(exc).__name__}: {exc}"

    # -- rebuilding -------------------------------------------------------

    def refresh_now(self, *, force: bool = False) -> bool:
        """Rebuild when the checkout fingerprint changed (or when forced)."""
        with self._lock:
            if self.state.rebuilding and not force:
                return False
            fingerprint = checkout_fingerprint(self.index.library_root)
            if not force and fingerprint == self.state.fingerprint:
                return False
            self.state.rebuilding = True
            try:
                stats = self.index.build()
            except Exception as exc:
                self.state.last_error = f"{type(exc).__name__}: {exc}"
                self.state.rebuilding = False
                raise
            self.state.fingerprint = fingerprint
            self.state.checked_at = time.monotonic()
            self.state.built_at = time.time()
            self.state.generation += 1
            self.state.build_count += 1
            self.state.stats = stats
            self.state.last_error = None
            self.state.rebuilding = False
            return True

    def ensure_fresh(self) -> bool:
        """Rebuild before answering a query when the checkout moved.

        The fingerprint costs one ``git status`` on the checkout (~100 ms in the
        production Library), which would dominate a 2 ms query, so it is
        recomputed at most once per ``check_interval`` window.
        """
        if not self.state.fingerprint:
            return self.refresh_now(force=True)
        now = time.monotonic()
        if now - self.state.checked_at < self.check_interval:
            return False
        self.state.checked_at = now
        if self.is_stale():
            return self.refresh_now(force=False)
        return False

    def is_stale(self) -> bool:
        try:
            return checkout_fingerprint(self.index.library_root) != self.state.fingerprint
        except Exception:
            return True

    def describe(self) -> dict:
        stats = self.state.stats
        return {
            "generation": self.state.generation,
            "index_path": str(self.index.path),
            "library_root": str(self.index.library_root),
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.state.built_at))
            if self.state.built_at
            else None,
            "entries": stats.entries if stats else None,
            "attachments": stats.attachments if stats else None,
            "diagnostics": stats.diagnostics if stats else None,
            "build_ms": round(stats.duration_ms, 1) if stats else None,
            "rebuilds": self.state.build_count,
            "stale": self.state.checked_at and (
                time.monotonic() - self.state.checked_at >= self.check_interval
            ) and self.is_stale(),
            "last_error": self.state.last_error,
        }
