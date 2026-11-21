"""Disposable Library checkout fixtures for the Annex Library test suite.

Each fixture is a real ``git`` + ``git-annex`` repository built from scratch so
that catalog rules, annex placeholder handling and index rebuilds are exercised
against actual Git/annex behaviour instead of mocks.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

GIT_ANEX_ATTRS = """\
* annex.largefiles=nothing
*.pdf annex.largefiles=anything
*.png annex.largefiles=anything
"""

WILEY_GLANCE = """\
---
type: paper
source: Crossref / publisher (Wiley)
url: https://doi.org/10.1002/smll.202311661
doi: 10.1002/smll.202311661
journal: Small
published: 2024-04-10
captured: 2026-11-09
stage: library
work_id: travaglini-conductive-heme-nanowires
version_type: journal-article
tags: [gamma-prefoldin, protein-heme, nanowire, conductive-AFM, bioelectronics]
---

# Fabrication of Electronically Conductive Protein-Heme Nanowires for Power Harvesting

**Authors:** Lorenzo Travaglini, Nga T. Lam, Artur Sawicki, Hee-Jeong Cha, Dominic J. Glover
**Journal:** Small 2024, 20, 2311661
**DOI:** 10.1002/smll.202311661

## One-liner

Heme arranged along gamma-prefoldin filaments conducts and harvests power from humidity.
"""

ARXIV_GLANCE = """\
---
type: paper
source: arXiv
url: https://doi.org/10.48550/arXiv.2310.10042
doi: 10.48550/arXiv.2310.10042
journal: arXiv preprint
published: 2023-10-15
captured: 2026-11-09
stage: library
work_id: travaglini-conductive-heme-nanowires
version_type: preprint
tags: [gamma-prefoldin, protein-heme, nanowire, preprint]
---

# Conductive protein-heme nanowires from gamma-prefoldin

**Authors:** Lorenzo Travaglini, Artur Sawicki, Dominic J. Glover

Preprint of the Small paper.
"""

ARXIV_NOTES_MALFORMED = """\
---
source: arxiv
tags: [gamma-prefoldin, nanowire
captured: 2026-11-09
---

Notes with malformed YAML: this must stay outside the catalog.
"""

MISC_NOTE = """\
---
source: internal-synthesis
url: multiple - see references
published: 2026-11-11
captured: {captured}
stage: library
tags: [proximity-labeling, virtual-cell, dataset-design]
id: {misc_id}
legacy_path: Topics/2026-11-11_proximity-labeling.md
title: 邻近标记能为虚拟细胞提供什么数据集
---

# 邻近标记能为虚拟细胞提供什么数据集
"""

BILIBILI_SUMMARY = """\
---
source: 长赢之道研究院（B站，mid 388067280）
url: https://www.bilibili.com/video/BV1YZ4b6gEjv
published: 2026-11-05T18:25+08:00
captured: 2026-11-07
stage: library
tags: [A股, 上证指数, 视频摘要]
---

# 长赢之道研究院｜《连续两天超 2 万亿！这波反弹还能走多久？》
"""


def _run(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "fixture")
    env.setdefault("GIT_AUTHOR_EMAIL", "fixture@example.test")
    env.setdefault("GIT_COMMITTER_NAME", "fixture")
    env.setdefault("GIT_COMMITTER_EMAIL", "fixture@example.test")
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}\n{proc.stdout}")
    return proc


def _write(root: Path, rel_path: str, content: str) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _pdf_bytes(marker: str) -> bytes:
    return b"%PDF-1.4\n% fixture " + marker.encode() + b"\n% dummy payload\nendobj\n"


def build_fixture(root: Path, *, with_annex: bool = True) -> Path:
    """Create a disposable Library checkout with the documented catalog cases."""
    root.mkdir(parents=True, exist_ok=True)
    _run(root, "init", "-q", ".")
    _write(root, ".gitattributes", GIT_ANEX_ATTRS)
    _write(root, "README.md", "# Fixture Library\n\nDisposable checkout for tests.\n")

    # --- DOI: Wiley journal version and the arXiv preprint as separate bundles
    wiley_dir = "DOI/DOI-10.1002_smll.202311661/SMALL-Travaglini-2024-04-10-Conductive-Protein-Heme-Nanowires"
    _write(root, f"{wiley_dir}/wiley-glance.md", WILEY_GLANCE)
    wiley_pdf = "DOI/DOI-10.1002_smll.202311661/Small - 2024 - Travaglini - Fabrication of Electronically Conductive Protein%E2%80%90Heme Nanowires.pdf"
    wiley_pdf_path = root / wiley_pdf
    wiley_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    wiley_pdf_path.write_bytes(_pdf_bytes("wiley"))

    arxiv_dir = "DOI/DOI-10.48550_arXiv.2310.10042/ARXIV-Travaglini-2023-10-15-Conductive-Protein-Heme-Nanowires"
    _write(root, f"{arxiv_dir}/arxiv-glance.md", ARXIV_GLANCE)
    _write(root, f"{arxiv_dir}/arxiv-notes.md", ARXIV_NOTES_MALFORMED)
    arxiv_pdf = f"{arxiv_dir}/travaglini-2023-arxiv-2310.10042.pdf"
    _write_bytes(root, arxiv_pdf, _pdf_bytes("arxiv"))

    # --- DOI: bundle with a convenience copy of a figure inside assets/
    asset_png = f"{wiley_dir}/assets/fig3a_nyquist.png"
    _write_bytes(root, asset_png, b"\x89PNG\r\n\x1a\nfixture")

    # --- DOI: bundle with only notes (outside the catalog)
    _write(
        root,
        "DOI/DOI-10.1126_science.aah6219/SCIENCE-Notes-Only/pdf-notes.md",
        "---\ntype: paper-note\nsource: pubmed\n---\n\n# Notes only bundle\n",
    )

    # --- Misc: two bundles, adjacent captured dates to prove inclusive ranges
    _write(
        root,
        "Misc/20261121T125651.861607Z/proximity-labeling-datasets-for-virtual-cells.md",
        MISC_NOTE.format(captured="2026-11-11", misc_id="20261121T125651.861607Z"),
    )
    _write(
        root,
        "Misc/20261121T125651.861611Z/deepseek-model-selection.md",
        MISC_NOTE.format(captured="2026-11-14", misc_id="20261121T125651.861611Z")
        .replace("邻近标记能为虚拟细胞提供什么数据集", "模型选型备忘")
        .replace("tags: [proximity-labeling, virtual-cell, dataset-design]",
                 "tags: [llm, model-selection, pricing]"),
    )

    # --- Misc: legacy flat note sharing the id of a bundle (ambiguous id case)
    legacy_flat = "Misc/2026-11-21_legacy-flat-note.md"
    _write(root, legacy_flat, MISC_NOTE.format(captured="2026-11-21", misc_id="20261121T125651.861690Z"))
    _write(
        root,
        "Misc/20261121T125651.861690Z/legacy-flat-note.md",
        MISC_NOTE.format(captured="2026-11-21", misc_id="20261121T125651.861690Z"),
    )

    # --- videos
    _write(
        root,
        "Bilibili/BV1YZ4b6gEjv/长赢之道研究院_2026-11-05_连续两天超2万亿/summary.md",
        BILIBILI_SUMMARY,
    )
    _write_bytes(
        root,
        "Bilibili/BV1YZ4b6gEjv/长赢之道研究院_2026-11-05_连续两天超2万亿/assets/frame01.png",
        b"\x89PNG\r\n\x1a\nfixture",
    )
    _write(root, "YouTube/abc123/Park Systems_2022-05-12_Conductive-AFM-Webinar/summary.md",
           BILIBILI_SUMMARY.replace("长赢之道研究院", "Park Systems"))

    # --- ISBN: book bundle plus chapter distillation docs that are not records
    _write_bytes(
        root,
        "ISBN/ISBN-978-0-12-457551-X/Lowe, Peterson. (2006). Quantum chemistry. 3rd ed..pdf",
        _pdf_bytes("isbn-book"),
    )
    _write(root, "ISBN/ISBN-978-0-12-457551-X/ch01/verified.md", "# chapter distillation\n")
    _write(root, "ISBN/ISBN-978-0-12-457551-X/ch01/skills/skill-x/SKILL.md", "# skill\n")

    _run(root, "annex", "init", "-q", check=False) if with_annex else None
    _run(root, "add", ".gitattributes", "README.md") if with_annex else _run(root, "add", "-A")
    _run(root, "commit", "-qm", "fixture: initial library")

    if with_annex:
        _run(root, "annex", "add", wiley_pdf, arxiv_pdf, asset_png)
        _write(root, "README.md", "# Fixture Library\n\nDisposable checkout for tests.\n")
        _run(root, "add", "-A")
        _run(root, "commit", "-qm", "fixture: remaining catalog files")
    else:
        _run(root, "add", "-A")
        _run(root, "commit", "-qm", "fixture: remaining catalog files")
    return root


def _write_bytes(root: Path, rel_path: str, data: bytes) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def drop_annex_content(root: Path, rel_path: str) -> None:
    """Turn a present annex file into a placeholder (dangling by design)."""
    proc = subprocess.run(
        ["git", "annex", "drop", "--force", rel_path],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git annex drop failed: {proc.stderr}")
    _run(root, "commit", "-qm", f"fixture: drop {rel_path}", check=False)


def clone_fixture(source: Path, target: Path) -> Path:
    """Copy a fixture checkout (including .git) for a destructive test."""
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, symlinks=True)
    return target
