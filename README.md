# Annex Library

Annex Library is a Model Context Protocol (MCP) server for managing a local git-annex library. The server searches the catalog, locates attachments, validates archive records, saves prepared DOI and Misc entries, and edits catalog metadata.

The server builds a SQLite index from catalog Markdown and tracked files. It stores the index outside the library checkout and can rebuild it from the checkout.

## Installation

Requires Python 3.11 or later, Git, git-annex, and an existing library checkout that follows the [catalog layout](docs/CATALOG.md).

```sh
git clone https://github.com/flmaximwang/Annex-Library.git
cd Annex-Library
python3 -m venv .venv
.venv/bin/python -m pip install .
```

## MCP setup

Add the following entry to the MCP client's configuration. Replace both paths with absolute paths on your machine.

```json
{
  "mcpServers": {
    "annex-library": {
      "command": "/path/to/Annex-Library/.venv/bin/annex-library-mcp",
      "args": ["--library", "/path/to/Library"]
    }
  }
}
```

The server uses stdio. Pass `--index /path/to/index.sqlite3` to use a separate cache for the checkout. To build the index and inspect its status, run `.venv/bin/annex-library-mcp --library /path/to/Library --status`.

## Tools

| Tool | Function |
| --- | --- |
| `annex-lib-search` | Search by DOI, title, author, tags, capture dates, collection, or free text. Results group linked versions by work and identify the version that matched. |
| `annex-lib-attachments` | List tracked attachments by `path` or `entry_id`, including git-annex placeholders. Set `refresh_local` to `true` to check current local availability. |
| `annex-lib-check` | Report archive structure and metadata issues for a Library-relative `path` or the full checkout. |
| `annex-lib-save` | Archive `metadata` (including the Markdown `body`) and optional absolute `staged_files` with a stable `request_id`. |
| `annex-lib-edit` | Apply `field_updates` to the YAML of the record at `path` with the expected `revision` and a stable `request_id`. |

Save and edit return the record locator and local commit SHA on a committed change. Retry `committed_index_pending` with the same request ID and payload to recover search visibility.

For example, call `annex-lib-search` with `{"title": "conductive protein heme", "include_local_status": true}` to find matching works and include current attachment availability.

## Tests

```sh
.venv/bin/python -m pip install '.[dev]'
.venv/bin/python -m pytest
```
