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


def make_xlsx(path: Path, sheets: dict[str, list[list]]) -> None:
    """Write an .xlsx file with one sheet per entry, rows from A1. A string starting with = is a formula."""
    from openpyxl import Workbook

    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    wb.save(path)


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


def domain(bundle, name, base_iri, extends=()):
    """Add the Ontology page of one domain with its base_iri."""
    fm = concept(type="Ontology", title=f"{name} ontology", base_iri=base_iri)
    if extends:
        fm["extends"] = [f"/ontology/{e}/ontology.md" for e in extends]
    bundle.add(f"ontology/{name}/ontology.md", fm)
    bundle.__dict__.setdefault("bases", {})[name] = base_iri


def _type_page(bundle, domain_name, folder, slug, fm, iri=None):
    """Write a type page under ontology/<domain>/<folder>/; the iri defaults to <base_iri><folder>/<slug>."""
    base = bundle.__dict__.get("bases", {}).get(domain_name, f"https://example.org/{domain_name}/")
    fm = {"title": slug.replace("-", " ").title(), "iri": iri or f"{base}{folder}/{slug}",
          "generated": {"by": "test", "at": "2026-11-02T18:00:00Z"}, **fm}
    bundle.add(f"ontology/{domain_name}/{folder}/{slug}.md", fm, body="A scope note.\n")


def _expand(bundle, domain_name, folder, ref):
    """A slug becomes <base_iri><folder>/<slug>; a full IRI stays as it is."""
    if "://" in ref:
        return ref
    return f"{bundle.__dict__.get('bases', {})[domain_name]}{folder}/{ref}"


def prop(bundle, domain_name, slug, datatype, unit=None, required=False, iri=None, **extra):
    """A Property page."""
    fm = {"type": "Property", "datatype": datatype, "required": required, **extra}
    if unit is not None:
        fm["unit"] = unit
    _type_page(bundle, domain_name, "properties", slug, fm, iri)


def klass(bundle, domain_name, slug, table=None, columns=None, key="iri", title="title", subclass_of=(),
          implements=(), status="experimental", iri=None, **extra):
    """A Class page. Column values, subclass_of and implements may be slugs of this domain."""
    fm = {"type": "Class", "status": status,
          "subclass_of": [_expand(bundle, domain_name, "classes", r) for r in subclass_of],
          "implements": [_expand(bundle, domain_name, "interfaces", r) for r in implements], **extra}
    if table is not None:
        cols = {c: _expand(bundle, domain_name, "properties", p) for c, p in (columns or {}).items()}
        fm["binding"] = {"table": table, "key": key, "title": title, "columns": cols}
    _type_page(bundle, domain_name, "classes", slug, fm, iri)


def interface(bundle, domain_name, slug, properties=(), links=(), iri=None):
    """An Interface page; properties are (slug or IRI, required) pairs."""
    fm = {"type": "Interface",
          "properties": [{"property": _expand(bundle, domain_name, "properties", p), "required": r} for p, r in properties],
          "links": [dict(link) for link in links]}
    _type_page(bundle, domain_name, "interfaces", slug, fm, iri)


def rules(report):
    return sorted({(f.rule, f.level) for f in report.findings})


def errors(report):
    return sorted({f.rule for f in report.findings if f.level == "error"})
