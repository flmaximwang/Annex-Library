"""MCP server exposing the Annex Library catalog.

One MCP service (``annex-library``) serves exactly one Library checkout. The
catalog's source of truth is the Markdown YAML in that checkout, Git holds
history, git-annex owns attachments and the SQLite index lives outside the
checkout at ``ANNEX_LIBRARY_INDEX``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from mcp.server.mcpserver import MCPServer

from .config import Config
from .index import Index
from .refresh import IndexManager
from .tools import (
    ToolError,
    annex_lib_check,
    annex_lib_edit,
    annex_lib_locate_attachments,
    annex_lib_save,
    annex_lib_search,
)

SERVICE_NAME = "annex-library"

_INSTRUCTIONS = """\
Annex Library catalog service. It serves one configured Library checkout.

Catalog rules (derived from the checkout's own README and history):
* DOI papers: one bundle `DOI/DOI-<doi with / -> _>`, one concise
  `*-glance.md` per version. Notes (`*-notes.md`) are outside the catalog.
* Preprints and journal articles with distinct identifiers get their own
  bundles; a shared `work_id` groups explicitly verified versions.
* Videos: `Bilibili|YouTube|Vimeo/<id>/<author>_<date>_<title>/summary.md`.
* Misc: `Misc/<YYYYMMDDTHHMMSS.ffffffZ>/<topic>.md` with a UTC timestamp id.
* Books: `ISBN/<bundle>` with a book PDF; chapter distillation is not cataloged.

Search uses YAML metadata. Results are grouped by work, name the version that
matched, and carry the archive locator, relative record path, content revision
and tracked attachments (including annex placeholders). Pass
`include_local_status=true` for batched git-annex content availability.
"""


def build_server(manager: IndexManager) -> MCPServer:
    server = MCPServer(SERVICE_NAME, instructions=_INSTRUCTIONS)

    @server.tool(
        name="annex-lib-search",
        title="Search the Annex Library",
        description=(
            "Search catalog records by exact DOI, title keywords, coauthor surname, "
            "inclusive captured date range, exact tags combined with AND, collection "
            "or free text. Returns works grouped by work_id with the directly matched "
            "version, each version's archive locator (relative glance path), content "
            "revision, and tracked attachments. Set include_local_status=true to add "
            "batched git-annex local content availability for the returned page."
        ),
    )
    def search_tool(
        doi: str | None = None,
        title: str | None = None,
        author: str | None = None,
        tags: list[str] | None = None,
        captured_from: str | None = None,
        captured_to: str | None = None,
        collection: str | None = None,
        text: str | None = None,
        limit: int = 20,
        offset: int = 0,
        include_local_status: bool = False,
        include_attachments: bool = True,
    ) -> dict:
        try:
            return annex_lib_search(
                manager,
                doi=doi,
                title=title,
                author=author,
                tags=tags,
                captured_from=captured_from,
                captured_to=captured_to,
                collection=collection,
                text=text,
                limit=limit,
                offset=offset,
                include_local_status=include_local_status,
                include_attachments=include_attachments,
            )
        except ToolError as exc:
            return {"error": {"code": exc.code, "message": str(exc), **exc.detail}}

    @server.tool(
        name="annex-lib-attachments",
        title="Locate archived attachments for one record",
        description=(
            "Return the tracked attachments of one bundle by record path (or entry id), "
            "with per-file role and, when refresh_local=true, a fresh filesystem probe "
            "of git-annex local content so placeholders are reported as missing."
        ),
    )
    def attachments_tool(
        path: str | None = None,
        entry_id: str | None = None,
        refresh_local: bool = False,
    ) -> dict:
        try:
            return annex_lib_locate_attachments(
                manager, path=path, entry_id=entry_id, refresh_local=refresh_local
            )
        except ToolError as exc:
            return {"error": {"code": exc.code, "message": str(exc), **exc.detail}}

    @server.tool(
        name="annex-lib-check",
        title="Check archive structure",
        description="Read-only structural validation of one path or the whole checkout.",
    )
    def check_tool(path: str | None = None) -> dict:
        try:
            return annex_lib_check(manager, path=path)
        except ToolError as exc:
            return {"error": {"code": exc.code, "message": str(exc), **exc.detail}}

    @server.tool(
        name="annex-lib-save",
        title="Archive a prepared entry",
        description="Archive prepared metadata and optional server-readable staged attachments.",
    )
    def save_tool(metadata: dict, staged_files: list[str] | None = None, request_id: str | None = None) -> dict:
        try:
            return annex_lib_save(manager, metadata=metadata, staged_files=staged_files, request_id=request_id)
        except ToolError as exc:
            return {"error": {"code": exc.code, "message": str(exc), **exc.detail}}

    @server.tool(
        name="annex-lib-edit",
        title="Patch one catalog record",
        description="Patch one catalog YAML record using a search result's path and revision.",
    )
    def edit_tool(
        path: str,
        revision: str,
        field_updates: dict,
        request_id: str | None = None,
    ) -> dict:
        try:
            return annex_lib_edit(
                manager, path=path, revision=revision, field_updates=field_updates, request_id=request_id
            )
        except ToolError as exc:
            return {"error": {"code": exc.code, "message": str(exc), **exc.detail}}

    return server


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=SERVICE_NAME, description="Annex Library MCP service")
    parser.add_argument("--library", default=None, help="Library checkout path")
    parser.add_argument("--index", default=None, help="SQLite index path (outside the checkout)")
    parser.add_argument("--no-watch", action="store_true", help="disable the refresh watchdog")
    parser.add_argument("--rebuild", action="store_true", help="rebuild the index and exit")
    parser.add_argument("--status", action="store_true", help="print index status and exit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = Config.from_env(library_root=args.library, index_path=args.index)
    config.check()
    manager = IndexManager(
        index=Index(config.index_path, config.library_root),
        watch=False if args.no_watch else config.watch,
    )
    if args.rebuild:
        manager.refresh_now(force=True)
        print(json_status(manager))
        manager.stop()
        return 0
    if args.status:
        manager.ensure_fresh()
        print(json_status(manager))
        manager.stop()
        return 0
    manager.start()
    server = build_server(manager)
    try:
        asyncio.run(server.run_stdio_async())
    finally:
        manager.stop()
    return 0


def json_status(manager: IndexManager) -> str:
    return json.dumps(manager.describe(), ensure_ascii=False, indent=2)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
