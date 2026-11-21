# annex-library

An MCP service over one `Annex Library` checkout.

The catalog's source of truth is the Markdown YAML in the checkout: Git holds
history, git-annex owns the attachments, and the SQLite index is a disposable
cache that lives **outside** the checkout.

## Tickets

| Ticket | Tool | State |
| --- | --- | --- |
| `annex-library-crx.1` | `annex-lib-search`, `annex-lib-attachments` | delivered |
| `annex-library-crx.2` | `annex-lib-check` | not started |
| `annex-library-crx.3` | `annex-lib-save` | not started |
| `annex-library-crx.4` | `annex-lib-edit` (versions and related versions) | not started |
| `annex-library-crx.5` | MCP registration and operating notes | not started |

## Install and run

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

The service is run as a stdio MCP server:

```sh
.venv/bin/python -m annex_library.server \
  --library /path/to/Library \
  --index ~/Library/Caches/annex-library/index-v1.sqlite3
```

| Option | Meaning |
| --- | --- |
| `--library` | The checkout to serve (env `ANNEX_LIBRARY_ROOT`) |
| `--index` | SQLite index path, must be outside the checkout (env `ANNEX_LIBRARY_INDEX`) |
| `--no-watch` | Do not poll the checkout for external changes |
| `--rebuild` | Build the index and exit |
| `--status` | Print index generation, entry count, build duration, staleness, and exit |

The default index path is `~/Library/Caches/annex-library/index-v1.sqlite3` on
macOS and `$XDG_CACHE_HOME/annex-library/index-v1.sqlite3` elsewhere. Deleting
the index is always safe: it is rebuilt from the checkout on the next start.

## Tools

### `annex-lib-search`

Searches the catalog. Every criterion is optional but at least one is required,
and all given criteria must match.

| Argument | Meaning |
| --- | --- |
| `doi` | Exact DOI; `https://doi.org/…` and `doi:` prefixes are accepted |
| `title` | Title keywords, all terms must occur, case and diacritics folded |
| `author` | Coauthor surname or name fragment |
| `tags` | Tag list, combined with AND |
| `captured_from`, `captured_to` | Inclusive `YYYY-MM-DD` bounds on the captured date |
| `collection` | `DOI`, `ISBN`, `Misc`, `Bilibili`, `YouTube`, `Vimeo`, `SelfHosted` |
| `text` | Free text over title, authors and tags |
| `limit`, `offset` | Paging; `limit` is capped at 100 |
| `include_local_status` | Add batched git-annex local content availability |
| `include_attachments` | Omit the attachment list (defaults to `true`) |

Results are grouped by work. Each work names the version that matched
(`matched_version`), lists every version of that work with its archive locator
(`bundle` + `record_path`, both relative to the checkout), its content revision
(the worktree blob SHA of the record), and its tracked attachments — including
git-annex placeholders, whose local availability is reported as `present` or
`missing` rather than hidden.

### `annex-lib-attachments`

Returns the tracked attachments of one bundle by `record_path` (or `entry_id`),
with a fresh filesystem probe when `refresh_local` is true.

### `annex-lib-check`, `annex-lib-save`, `annex-lib-edit`

Declared for a stable tool surface; they report `not_implemented` until tickets
`.2`, `.3` and `.4` land.

## Catalog rules

[`docs/CATALOG.md`](docs/CATALOG.md) records every selection rule together with
the production evidence it was derived from: which Markdown file is a catalog
record, why notes and chapter distillation are not, how dates and tags are
tolerated, and what happens to the known anomalies (two glances in one bundle,
a duplicate Misc id, a bundle with a trailing space).

## Verification

```sh
.venv/bin/python -m pytest tests -q        # 32 tests over a git+git-annex fixture
.venv/bin/python scripts/verify-crx1.py    # 20 end-to-end checks over real MCP stdio
```

`scripts/verify-crx1.py` starts the server over the same stdio transport a host
uses and asserts the acceptance criteria against the configured checkout. It
reads the checkout and writes only the index. Point it at another checkout with
`ANNEX_LIBRARY_ROOT` and `ANNEX_LIBRARY_INDEX`.

Measured on the production checkout (3960 tracked files, revision
`47b589b96cc4b1fe0548dfd10403c233ea554ced`; 2260 entries, 2937 attachments):
cold build 0.7 s, warm queries p95 0.3–0.8 ms, external change visible within
the watchdog interval (measured 0.36 s).
