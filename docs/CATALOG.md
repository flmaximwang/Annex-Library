# Catalog selection rules and the evidence for them

Every rule below was derived by inspecting the production checkout at
`/Users/maxim/digitalmind/Library` (revision `47b589b96cc4b1fe0548dfd10403c233ea554ced`,
branch `adjusted/main(unlocked)`, 3960 tracked files) and by reading the
Library's own `README.md`. The production Library was only ever read; nothing in
it was modified.

The numbers in the right-hand column are from that revision. They are the
reason the rule exists, and they are what the fixture in `tests/fixtures.py`
reproduces in miniature.

## What counts as a catalog record

| Collection | Record | Everything else |
| --- | --- | --- |
| `DOI/` | `DOI-<doi>/<JOURNAL>-<Author>-<YYYY-MM-DD>-<Title>/<source>-glance.md` | `*-notes.md` (23), `skill/**` (24), `assets/**`, the PDF itself |
| `ISBN/` | the bundle itself, with its book PDF | chapter distillation (`ch0N/verified.md`, `PIPELINE_STATE.md`, `BOOK_OVERVIEW.md`, `GLOSSARY.md`, `SKILL.md`, `.cangjie/**`) — 703 markdown files, none of them a record |
| `Misc/` | `Misc/<YYYYMMDDTHHMMSS.ffffffZ>/<topic>.md` | `assets/**` annex attachments |
| `Bilibili/` `YouTube/` `Vimeo/` | `<id>/<作者或作者名>_<YYYY-MM-DD>_<短标题>/summary.md` (82 + 16 + 1) | `transcript.md` (25), `.seg/wNNNN-NNNN.md` (9), `00-要点速览.md` (1) |
| `SelfHosted/` | document bundle, metadata from the archived filename | — (no markdown at all in 515 bundles) |

Measured at that revision:

* 3960 tracked files: 2124 PDF, 1024 Markdown, 260 JPG, 170 PNG, 138 JSON,
  36 PPTX, 33 MP4.
* 1024 Markdown files: 76 `-glance.md`, 23 `-notes.md`, 61 Misc records, 1
  legacy flat Misc file, 99 summaries (82 Bilibili, 16 YouTube, 1 Vimeo), 35
  Bilibili transcript/segment files (25 `transcript.md`, 9 `.seg` windows, 1
  `00-要点速览.md`), 24 `DOI/**/skill/**`, and 703 ISBN distillation documents.
  The remaining two are `README.md` and `.beads/README.md`, which belong to the
  checkout rather than the catalog.
* 2692 annex-tracked paths; 2544 present locally, 148 absent (placeholders).
* The index built from this revision holds 2260 entries and 2937 attachments:
  1312 DOI paper archives, 515 SelfHosted documents, 196 ISBN books, 99 video
  summaries, 76 DOI glances, 61 Misc records and 1 Misc legacy record.

## Rules and the evidence behind them

### 1. A glance is the DOI catalog record; notes never are

`README.md` §DOI: "one concise `<source>-glance.md` per version", and "notes are
added later as `<source>-notes.md` and never overwrite a glance". 76 glances live
in 75 bundles: `DOI/DOI-10.1110_ps.062599907` has both `crossref-glance.md` and
`pubmed-glance.md`. The scanner therefore emits one version per glance and adds a
`warning` diagnostic, instead of picking one arbitrarily — searching either
source's metadata still finds the work.

### 2. Notes are excluded even when they are the only markdown in a bundle

`DOI/DOI-10.1126_science.aah6219` contains only `pdf-notes.md`. It is not a
catalog record; it produces an `info` diagnostic and no entry, so a search for
notes-only bundles cannot return an entry with no metadata.

### 3. A bundle without a glance is still searchable

1388 DOI bundle directories are tracked and together yield 1388 entries over 1387
distinct bundles: 76 glances cover 75 bundles (one bundle has two glances) and
1312 bundles have no glance at all, so they are indexed as
`kind="paper-archive"` with metadata derived from the archived filename plus a
`no glance: metadata derived from archived filename` warning. The remaining
bundle (`DOI/DOI-10.1126_science.aah6219`) holds only a notes file and is
skipped with an `info` diagnostic. The same derivation applies to 515
`SelfHosted` bundles and 196 ISBN books.

### 4. `work_id` groups versions; a record without it is its own work

`grep work_id` over the whole catalog returns zero matches: the field is new in
the MCP schema. The ticket requires that "when the same work has multiple
versions, search results group them by work and explicitly name the version that
matched". So `work_id` is the grouping key when present, and otherwise the record
path is — a legacy record stays independently searchable and never silently
merges with a namesake.

`version_type` is reported as-is and never inferred; `preprint` in the fixture is
what the arXiv glance declares.

### 5. The archive locator is the archive-relative record path

`entry_id`, `record_path` and `bundle` are all checkout-relative (no absolute
paths leak into results), and `revision` is the worktree blob SHA of the record
file, which matches `git hash-object` for the same content.

### 6. Attachments are reported even when they are placeholders

`.gitattributes` sets `* annex.largefiles=nothing`, then overrides media, office
formats, `Zotero/**` and `*.pdf` to `anything`. In the production checkout
(unlocked adjusted branch) a present annex file is a real regular file, and an
absent one is a ~100 byte pointer file whose content starts with
`/annex/objects/…`, mode 100644 — not a dangling symlink. Both shapes are probed
(see `probe_local_present`), and `git annex find --in=here` is the oracle the
probe is tested against.

Attachments are listed regardless of availability — a placeholder is the normal
end state of an archive, and hiding it would make the attachment list lie.

### 7. Misc identity is the UTC timestamp id; ambiguity warns but does not crash

61 bundles follow `Misc/<YYYYMMDDTHHMMSS.ffffffZ>/<topic>.md`, and the frontmatter
`id` embeds the same timestamp. One stray tracked flat file,
`Misc/2026-11-21_orca-vs-herdr-设计哲学.md` (commit `168e82086`), duplicates
`Misc/20261121T125651.861690Z/orca-vs-herdr-设计哲学.md` — the same `id` on two
records. Both stay searchable and both carry
`ambiguous identifier …: 2 catalog records share it`. This is the "malformed or
ambiguous entries stay searchable with warnings" requirement, exercised by a real
record, not a synthetic one.

### 8. Dates are tolerated, not trusted

`captured` shapes in production: `YYYY-MM-DD` (238), `YYYY-MM-DD N` (17), ISO
datetime (4), empty (7). `published`: `YYYY-MM-DD` (156), ISO with timezone (85),
empty (9), `unknown` (3), `YYYY-MM` (2), `YYYY` (2), ranges (`YYYY-MM ~ YYYY-MM`,
`YYYY–YYYY`, 3). `parse_iso_date` therefore accepts the leading `YYYY-MM-DD` of a
datetime or of the `2026-11-07 1` form, rejects `unknown`/partial dates, and a
record whose date cannot be parsed is indexed with a NULL `captured_date` — it is
excluded from date-filtered searches but still searchable by everything else.

The captured filter is inclusive at both ends: `2026-11-11..2026-11-14` returns
both Misc bundles in the fixture, `2026-11-11..2026-11-13` returns only the
earlier one.

### 9. Tags and authors are normalised, but the originals are returned

Tags are stored as written (for display) and in `normalize_tag` form (casefold +
width folding only) for matching.

Author names are **not** a frontmatter field in production. They appear in the
glance body in three shapes, all of which feed the same `authors` list:

* `**Authors:** Lorenzo Travaglini, Nga T. Lam, …` — 32 of 76 glances;
* a bold citation opener, `**Popp D, Iwasa M, Narita A** (Biopolymers. 2009;…)`;
* a bare citation opener, `Kollipara L, Zahedi RP. *Protein carbamylation* …`,
  sometimes after a `#` title line.

Extraction is deliberately conservative: a candidate is accepted only when it
parses as `Surname Initials` pairs, so prose, journal names and `**Journal:**`
lines are rejected rather than turned into fake coauthors. That yields authors
for 64 of 76 glances; the remaining 12 are protocol/prose notes that never state
an author line, and they stay searchable by everything else. Coauthor search
matches any surname component of any author, with diacritics folded
(`Maéda` matches `Maeda`).

### 10. The index is disposable

The SQLite file lives outside the checkout (default
`~/Library/Caches/annex-library/index-v1.sqlite3`; `Config.check()` refuses an
index path inside the checkout). Deleting it and rebuilding must produce
equivalent search results — `test_delete_and_rebuild_is_equivalent` compares seven
query shapes before and after a rebuild.

## Known production anomalies the scanner reports

| Path | Treat |
| --- | --- |
| `DOI/DOI-10.1110_ps.062599907` | two glances → two versions + warning |
| `DOI/DOI-10.1126_science.aah6219` | notes only → info diagnostic, no entry |
| `Misc/2026-11-21_orca-vs-herdr-设计哲学.md` | duplicate `id` with a timestamp bundle → both searchable, both warned |
| `ISBN/ISBN-978-1-119-87842-1 ` (trailing space) | name is kept verbatim |
| `ISBN-96017519`, `ISBN-0792365909`, `ISBN-7040418304`, `ISBN-978-7-202-12132` | non-13-digit / truncated ISBN forms are kept verbatim, never rewritten |
| `ISBN/ISBN-978-0-12-227410-7-C5XKA6VZ`, `…-YI73UQM9` | suffixed bundle names are kept verbatim |
