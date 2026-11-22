"""Refresh the catalog when working files or the Git index change."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .catalog import catalog_scope, catalog_snapshot, head_revision
from .index import Index, IndexStats


@dataclass
class RefreshState:
    generation: int = 0
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
    check_interval: float = 0.5
    watch: bool = True
    state: RefreshState = field(default_factory=RefreshState)
    _thread: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _signatures: dict[str, tuple] = field(default_factory=dict)

    def start(self) -> None:
        self.refresh_now(force=True)
        if self.watch and self._thread is None:
            self._thread = threading.Thread(target=self._watch_loop, name="annex-library-refresh", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self.index.close()

    def _watch_loop(self) -> None:
        while not self._stop.wait(self.poll_interval):
            try:
                self.refresh_now()
            except Exception as exc:
                self.state.last_error = f"{type(exc).__name__}: {exc}"

    def refresh_now(self, *, force: bool = False) -> bool:
        """Refresh changed bundles, or rebuild the entire index when forced."""
        with self._lock:
            try:
                root = self.index.library_root
                revision = head_revision(root)
                files, signatures = catalog_snapshot(root)
                scopes = {
                    scope for path in self._signatures.keys() | signatures.keys()
                    if self._signatures.get(path) != signatures.get(path)
                    if (scope := catalog_scope(path)) is not None
                }
                existing = self.state.stats
                self.state.checked_at = time.monotonic()
                if not force and existing and not scopes and revision == existing.revision and self.index.exists():
                    return False
                self.state.rebuilding = True
                if force or not existing or not self.index.exists():
                    stats = self.index.build(files=files)
                else:
                    stats = self.index.refresh(files, scopes, revision)
                self._signatures = signatures
                self.state.built_at = time.time()
                self.state.generation += 1
                self.state.build_count += 1
                self.state.stats = stats
                self.state.last_error = None
                return True
            except Exception as exc:
                self.state.last_error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                self.state.rebuilding = False

    def ensure_fresh(self) -> bool:
        if self.state.stats is None or not self.index.exists():
            return self.refresh_now(force=True)
        # Serve the published snapshot while the watchdog scans for changes.
        if self.watch and self._thread is not None:
            return False
        if time.monotonic() - self.state.checked_at >= self.check_interval:
            return self.refresh_now()
        return False

    def is_stale(self) -> bool:
        try:
            _, signatures = catalog_snapshot(self.index.library_root)
            stats = self.state.stats
            return stats is None or signatures != self._signatures or stats.revision != head_revision(self.index.library_root)
        except Exception:
            return True

    def describe(self) -> dict:
        stats = self.state.stats
        return {
            "generation": self.state.generation,
            "index_path": str(self.index.path),
            "library_root": str(self.index.library_root),
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.state.built_at))
            if self.state.built_at else None,
            "entries": stats.entries if stats else None,
            "attachments": stats.attachments if stats else None,
            "diagnostics": stats.diagnostics if stats else None,
            "build_ms": round(stats.duration_ms, 1) if stats else None,
            "rebuilds": self.state.build_count,
            "stale": self.is_stale(),
            "last_error": self.state.last_error,
        }
