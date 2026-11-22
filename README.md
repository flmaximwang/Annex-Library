# Annex Library

Annex Library is a read-only Model Context Protocol (MCP) server for searching a local git-annex library and locating its tracked attachments. It builds a SQLite index from catalog Markdown and tracked files. The index is stored outside the library checkout and can be rebuilt from it.

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

For example, call `annex-lib-search` with `{"title": "conductive protein heme", "include_local_status": true}` to find matching works and include indexed attachment availability.

## Tests

```sh
.venv/bin/python -m pip install '.[dev]'
.venv/bin/python -m pytest
```
