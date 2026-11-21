"""Text normalisation helpers shared by indexing and querying.

Matching rules (documented because they define search behaviour):

* All comparisons are NFKC-normalised and case-insensitive.
* Diacritics are stripped, so ``Höllrigl`` matches ``Hollrigl``.
* Punctuation, hyphens and underscores become spaces for *title* and *name*
   matching, so ``Protein-Heme`` matches ``protein heme``.
* CJK text is left intact: Chinese has no word separators, so title matching
  is substring based rather than token based.
"""

from __future__ import annotations

import datetime as _dt
import re
import unicodedata

_WS_RE = re.compile(r"\s+")
_CJK_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
)
_DATE_IN_TEXT_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi.org/",
    "doi:",
)


def strip_diacritics(text: str) -> str:
    """Remove combining marks while preserving the base character."""
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_text(text: str, *, keep_cjk: bool = True) -> str:
    """Return the normalised form used for title / name matching."""
    if text is None:
        return ""
    value = unicodedata.normalize("NFKC", str(text)).casefold()
    value = strip_diacritics(value)
    value = re.sub(r"[^\w\s]+", " ", value, flags=re.UNICODE)
    value = value.replace("_", " ")
    value = _WS_RE.sub(" ", value).strip()
    if keep_cjk:
        return value
    return value


def normalize_tag(tag: str) -> str:
    """Tags compare exactly, ignoring case and width only."""
    if tag is None:
        return ""
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", str(tag)).casefold()).strip()


def normalize_doi(value: str | None) -> str:
    """Normalise a DOI for exact comparison (DOIs are case-insensitive)."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip()
    lowered = text.casefold()
    for prefix in DOI_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix) :]
            lowered = text.casefold()
            break
    text = text.strip().strip(".,;，。；")
    text = _WS_RE.sub("", text)
    return text.casefold()


def parse_iso_date(value: object) -> _dt.date | None:
    """Parse the several date shapes found in Library frontmatter.

    Accepts ``date``/``datetime`` objects produced by YAML for unquoted values,
    ISO strings with or without time and timezone, and takes the leading
    ``YYYY-MM-DD`` of looser values such as ``2026-11-07 1``.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    text = str(value).strip()
    if not text or text.lower() in {"unknown", "n/a", "none"}:
        return None
    match = _DATE_IN_TEXT_RE.match(text)
    if not match:
        return None
    try:
        return _dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def date_to_iso(value: _dt.date | None) -> str | None:
    return value.isoformat() if value else None


def contains_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def split_query_terms(query: str) -> list[str]:
    """Split a keyword query into AND-ed terms after normalisation.

    CJK runs stay together (they have no separators); Latin text splits on
    whitespace and punctuation.
    """
    normalised = normalize_text(query)
    if not normalised:
        return []
    if contains_cjk(normalised):
        # Keep CJK runs whole, split Latin words, and let each CJK run be a term.
        parts = [
            part
            for part in re.split(r"\s+", normalised)
            if part
        ]
        return parts
    return [part for part in normalised.split(" ") if part]
