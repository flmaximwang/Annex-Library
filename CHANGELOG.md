# Changelog

All notable changes to Annex Library are recorded here. Versions follow
[Semantic Versioning](https://semver.org/): the public surface is the MCP tool
set, its arguments and its result shapes.

## [0.2.0] - 2026-11-26

The catalog can now be written to, not just read: `annex-lib-save` covers all
seven collections, `annex-lib-edit` patches existing records, and ISBN becomes a
first-class search criterion.

### Added

- **Saving for every collection.** `annex-lib-save` writes `DOI`, `ISBN`,
  `Misc`, `SelfHosted`, `Bilibili`, `YouTube` and `Vimeo`, each with the layout
  the catalog already documents: a `<source>-glance.md` at the DOI, ISBN or
  SelfHosted bundle root, a `summary.md` in a video bundle, a `<title>.md` in a
  Misc timestamp bundle, and attachments under the bundle (`assets/` for Misc,
  video and SelfHosted).
- **Markdown attachments are Git text.** A staged `.md` file is committed as an
  ordinary Git blob instead of going through git-annex; every other attachment
  keeps `git annex add --force-large`. Commit replay compares Markdown by
  content hash, so re-staging the same file reuses it and a different file at the
  same path is a `conflict`.
- **ISBN metadata.** `isbn` is validated for its 10- or 13-digit check digit and
  normalised without losing the caller's hyphens; an existing canonical
  `ISBN-<isbn>` directory is reused for the same digits. Books accept the Zotero
  field set (`publisher`, `edition`, `language`, `series`, `series_number`,
  `volume`, `number_of_volumes`, `place`, `pages`, `short_title`, `doi`,
  `work_id`, `version_type`), an empty `url`, and `published` as `YYYY` or
  `YYYY-MM` in addition to a full date.
- **ISBN and SelfHosted glance records.** A `<source>-glance.md` in those bundles
  is now the catalog record; Markdown beside it (chapter distillation) stays an
  attachment. A bundle without a glance is still indexed from its archived
  filenames and reports a warning.
- **`annex-lib-search(isbn=…)`.** ISBN matching ignores hyphens and spaces in
  both directions, and free text also matches stored identifiers. Legacy books
  with no glance expose the ISBN derived from their directory name, which is what
  makes the 197 production ISBN bundles searchable at all.
- **`annex-lib-edit`.** `field_updates` patches the YAML of the record at `path`
  against the expected content `revision`, with `request_id` replay.
- **`annex-lib-check` with a documented severity rule.** A structurally invalid
  ISBN directory is an error only when the bundle carries a record; existing
  books without one degrade to a warning, so a legacy checkout still passes.
- **Batched annex availability.** `include_local_status` now asks git-annex once
  for the whole result page (`git annex find --batch`) instead of per attachment,
  through the new `annex_present_paths` helper.
- `scripts/verify-crx1.py` and `scripts/verify-v1.py`: acceptance runs of the real
  stdio MCP server against the production checkout and against disposable
  fixtures.

### Changed

- The reported `version` in MCP `serverInfo` is now the package version
  (`0.1.0` was hard-coded as an empty string, so clients never saw one).
- Incremental refresh follows the checkout's changed bundles instead of
  rebuilding the whole index, and `search` reads under one index snapshot.
- `annex-lib-save` requires `metadata.collection` for the video collections and
  SelfHosted, and infers it from `doi`, `isbn` or `body` otherwise. `metadata.body`
  is always required; the server allocates `Misc` and `SelfHosted` ids itself.

### Fixed

- An attachment named like a record (`<source>-glance.md` beside the record in an
  ISBN or SelfHosted bundle) is rejected as a `conflict` instead of creating a
  second, competing record.
- A video save without `metadata.collection` reports `invalid_argument` instead
  of silently becoming a Misc note.
- A stale ISBN directory with an unverified suffix (for example
  `ISBN-978-0-306-40615-7-ABC123`) blocks the save with `conflict` rather than
  being ignored.
- `edit_record` protects `isbn`, like the other identifiers, from being changed.

### Removed

- The internal `skip_markdown` switch on catalog attachment scanning. Markdown is
  excluded only when it is the record itself.

## [0.1.0] - 2026-11-22

First release: `annex-lib-search`, `annex-lib-attachments` and `annex-lib-check`
over a disposable SQLite index built from the checkout. Saving and editing were
planned but not implemented.
