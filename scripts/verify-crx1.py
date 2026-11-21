"""End-to-end acceptance verification for ticket annex-library-crx.1.

Runs the real MCP server over stdio (the same transport a host would use) and
asserts the acceptance criteria of the ticket against the configured checkout.

    .venv/bin/python scripts/verify-crx1.py

`ANNEX_LIBRARY_ROOT` selects the checkout (default: the production Library) and
`ANNEX_LIBRARY_INDEX` the disposable index (default: /tmp/anxidx/prod.sqlite3).
The checkout is only read.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO = Path(__file__).resolve().parents[1]
LIB = os.environ.get("ANNEX_LIBRARY_ROOT", "/Users/maxim/digitalmind/Library")
IDX = os.environ.get("ANNEX_LIBRARY_INDEX", "/tmp/anxidx/prod.sqlite3")

WILEY = (
    "DOI/DOI-10.1002_smll.202311661/SMALL-Travaglini-2024-04-10-"
    "Conductive-Protein-Heme-Nanowires/wiley-glance.md"
)
# a bundle whose annex content is a placeholder in the production checkout
PLACEHOLDER = "DOI/DOI-10.7554_eLife.64891"


def check(label: str, condition: object, detail: str = "") -> bool:
    passed = bool(condition)
    print(("PASS " if passed else "FAIL ") + label + (f" | {detail}" if detail else ""))
    return passed


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m", "annex_library.server",
            "--library", LIB,
            "--index", IDX,
            "--no-watch",
        ],
        cwd=str(REPO),
    )
    results: list[bool] = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("server", init.server_info.name, init.server_info.version)
            tools = sorted(tool.name for tool in (await session.list_tools()).tools)
            print("tools:", tools)

            async def call(name: str, **arguments) -> tuple[bool, dict]:
                res = await session.call_tool(name, arguments)
                data = res.structured_content or json.loads(res.content[0].text)
                return res.is_error, data

            # 1. exact DOI: grouping, matched version, locator, revision, annex status
            _, data = await call(
                "annex-lib-search",
                doi="https://doi.org/10.1002/SMLL.202311661",
                include_local_status=True,
            )
            results.append(
                check("DOI search finds one work", data["total_works"] == 1, f"works={data['total_works']}")
            )
            work = data["works"][0]
            version = work["versions"][0]
            results.append(
                check("matched version named", work["matched_version"] == WILEY, work["matched_version"].split("/")[-1])
            )
            results.append(check("record path relative", not version["record_path"].startswith("/")))
            results.append(check("revision is 40-hex", len(version["revision"]) == 40))
            results.append(
                check(
                    "locator = bundle + record",
                    version["record_path"] == WILEY
                    and version["bundle"] == "DOI/DOI-10.1002_smll.202311661",
                )
            )
            pdfs = [a for a in version["attachments"] if a["path"].endswith(".pdf")]
            results.append(check("tracked attachment listed", len(pdfs) >= 1, f"{len(pdfs)} pdf"))
            results.append(
                check("annex availability batched", all("local_content" in a for a in version["attachments"]))
            )
            results.append(
                check(
                    "index tells revision and build time",
                    len(data["index"]["library_revision"]) == 40 and bool(data["index"]["built_at"]),
                )
            )

            # 2. coauthor surname
            _, data = await call("annex-lib-search", author="Travaglini", limit=20)
            results.append(check("coauthor search returns the work", data["total_works"] >= 1, f"{data['total_works']} works"))

            # 3. title keywords
            _, data = await call("annex-lib-search", title="conductive protein heme nanowires")
            results.append(check("title keyword search", data["total_works"] >= 1, f"{data['total_works']} works"))

            # 4. exact tags combined with AND
            _, data = await call("annex-lib-search", tags=["gamma-prefoldin", "nanowire"])
            results.append(check("tags AND", data["total_works"] >= 1))
            _, data_miss = await call("annex-lib-search", tags=["gamma-prefoldin", "a-tag-that-does-not-exist"])
            results.append(check("tags AND excludes", data_miss["total_works"] == 0))

            # 5. inclusive captured range
            _, wide = await call(
                "annex-lib-search", captured_from="2026-11-01", captured_to="2026-11-30",
                collection="Misc", limit=100,
            )
            _, narrow = await call(
                "annex-lib-search", captured_from="2026-11-20", captured_to="2026-11-30",
                collection="Misc", limit=100,
            )
            results.append(
                check(
                    "captured range narrows",
                    wide["total_works"] >= narrow["total_works"] > 0,
                    f"{wide['total_works']} -> {narrow['total_works']}",
                )
            )

            # 6. ambiguous identifiers stay searchable and carry warnings
            _, data = await call("annex-lib-search", text="herdr 设计哲学", limit=20)
            warned = [v for w in data["works"] for v in w["versions"] if v.get("warnings")]
            results.append(check("ambiguous entries searchable", data["total_works"] >= 1, f"{data['total_works']} works"))
            results.append(
                check(
                    "warnings surface on ambiguous",
                    bool(warned),
                    warned[0]["warnings"][0][:60] if warned else "",
                )
            )

            # 7. placeholders are reported, not hidden
            _, data = await call("annex-lib-attachments", path=PLACEHOLDER, refresh_local=True)
            results.append(
                check("attachment tool reports placeholders", len(data.get("missing", [])) > 0,
                      f"{len(data.get('missing', []))} missing")
            )

            # 8. structured errors
            _, data = await call("annex-lib-search")
            results.append(check("no-criterion error structured", bool(data.get("error"))))
            _, data = await call("annex-lib-search", captured_from="2026-11-20", captured_to="2026-11-01")
            results.append(check("reversed range rejected", data["error"]["code"] == "invalid_argument"))
            _, data = await call("annex-lib-search", doi="10.1002/smll.202311661", limit=101)
            results.append(check("limit cap enforced", data["error"]["code"] == "invalid_argument"))
            _, data = await call("annex-lib-check", path=WILEY)
            results.append(
                check("follow-up tool reports not_implemented", data["error"]["code"] == "not_implemented")
            )

    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
