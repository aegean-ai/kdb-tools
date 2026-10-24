"""Fixtures for testing kdb_tools and the profiles built on it.

A project's conftest.py imports them with `from kdb_tools.testing import *`.
"""

from pathlib import Path

import pytest
import yaml


def page(frontmatter: dict, body: str) -> str:
    return "---\n" + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True) + "---\n\n" + body


GOOD_SOURCE = {
    "id": "kim2024-p3-s2",
    "resource": "/papers/kim2024.pdf",
    "page": 1,
    "quote": "OpenVLA is trained on 970k robot episodes.",
    "bbox": [72, 700, 400, 714],
}

GOOD_CELL = {
    "id": "kim2024-t1-r2-c3",
    "resource": "/papers/kim2024.pdf",
    "page": 1,
    "table": 1,
    "row": 2,
    "column": 3,
    "value": "76.5",
    "bbox": [300, 600, 330, 612],
}


def concept(**overrides) -> dict:
    fm = {
        "type": "Model",
        "title": "OpenVLA",
        "description": "An open vision-language-action model.",
        "generated": {"by": "team-1-agent/qwen3-32b", "at": "2026-11-02T18:00:00Z"},
        "sources": [dict(GOOD_SOURCE)],
    }
    fm.update(overrides)
    return fm


GOOD_BODY = "OpenVLA is trained on robot episodes.[^kim2024-p3-s2]\n\n[^kim2024-p3-s2]: Kim et al. 2024, p. 3\n"


class Bundle:
    def __init__(self, root: Path):
        self.root = root
        (root / "papers").mkdir(parents=True)
        (root / "annotated").mkdir()
        (root / "papers" / "kim2024.pdf").write_bytes(b"%PDF-1.4\n")
        self.write("log.md", "# Update log\n\n## 2026-11-02\n* **Creation**: Built the kDB.\n")
        self._pages: list[tuple[str, str]] = []
        self.reindex()

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def add(self, rel: str, frontmatter: dict, body: str = GOOD_BODY, index: bool = True) -> Path:
        path = self.write(rel, page(frontmatter, body))
        if index:
            self._pages.append((frontmatter.get("title", rel), rel))
            self.reindex()
        return path

    def reindex(self) -> None:
        lines = ["# Concepts", ""] + [f"* [{t}]({r}) - a concept" for t, r in self._pages]
        self.write("index.md", "\n".join(lines) + "\n")


@pytest.fixture
def bundle(tmp_path) -> Bundle:
    return Bundle(tmp_path / "kdb")


def make_pdf(path: Path, pages: list[list[tuple[float, float, str]]], size: int = 12) -> dict[str, list[float]]:
    """Write a PDF with text at (x, baseline y). Return the PDF-space bbox of each string."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=(612, 792))
    boxes = {}
    for items in pages:
        c.setFont("Helvetica", size)
        for x, y, text in items:
            c.drawString(x, y, text)
            boxes[text] = [x, y - 0.25 * size, x + stringWidth(text, "Helvetica", size), y + 0.85 * size]
        c.showPage()
    c.save()
    return boxes


def highlight(src: Path, dst: Path, marks: list[tuple[int, list[float]]]) -> None:
    """Copy src to dst and add one highlight per (page number from 1, bbox)."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.annotations import Highlight
    from pypdf.generic import ArrayObject, FloatObject

    writer = PdfWriter(clone_from=PdfReader(src))
    for page_no, (x0, y0, x1, y1) in marks:
        quads = ArrayObject([FloatObject(v) for v in (x0, y1, x1, y1, x0, y0, x1, y0)])
        writer.add_annotation(page_number=page_no - 1, annotation=Highlight(rect=(x0, y0, x1, y1), quad_points=quads))
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "wb") as fh:
        writer.write(fh)


def term(label, domain="robotics", kind="tasks", **extra):
    """A Term page (path, frontmatter) under ontology/<domain>/<kind>/."""
    fm = concept(type="Term", title=label, pref_label=label)
    fm.update(extra)
    return f"ontology/{domain}/{kind}/{label.lower().replace(' ', '-')}.md", fm


def ontology(bundle, name, extends=()):
    """Add the Ontology page of one domain, extending the given domains."""
    fm = concept(type="Ontology", title=f"{name} ontology")
    if extends:
        fm["extends"] = [f"/ontology/{e}/ontology.md" for e in extends]
    bundle.add(f"ontology/{name}/ontology.md", fm)


def rules(report):
    return sorted({(f.rule, f.level) for f in report.findings})


def errors(report):
    return sorted({f.rule for f in report.findings if f.level == "error"})
