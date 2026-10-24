"""okf-ground: check every source in a kDB bundle against its PDF and its annotated copy.

A sentence source must appear on its page and inside its bbox. A table-cell source's value must
appear inside its bbox. Every bbox must match a highlight in the annotated copy of the paper.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pdfplumber

from . import bundle as B
from .locate import find_quote, inside
from .report import Report, fail

TOOL = "okf-ground"
MIN_IOU = 0.5


def iou(a: list[float], b: list[float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


class Paper:
    """One PDF, opened once."""

    def __init__(self, path: Path):
        self.pdf = pdfplumber.open(path)

    @property
    def pages(self) -> int:
        return len(self.pdf.pages)

    def highlights(self, page_no: int) -> list[list[float]]:
        page = self.pdf.pages[page_no - 1]
        out = []
        for annot in page.annots:
            subtype = str((annot.get("data") or {}).get("Subtype", ""))
            if "Highlight" in subtype:
                out.append([annot["x0"], page.height - annot["bottom"], annot["x1"], page.height - annot["top"]])
        return out

    def close(self) -> None:
        self.pdf.close()


class Library:
    def __init__(self):
        self._open: dict[Path, Paper | None] = {}

    def get(self, path: Path) -> Paper | None:
        if path not in self._open:
            try:
                self._open[path] = Paper(path) if path.is_file() else None
            except Exception:  # not a readable PDF
                self._open[path] = None
        return self._open[path]

    def close(self) -> None:
        for paper in self._open.values():
            if paper is not None:
                paper.close()


def where_hint(paper: Paper, page_no: int, bbox: list[float], spans: list[list[float]]) -> str:
    """Say where the text actually is, and whether the bbox looks measured from the top of the page."""
    height = paper.pdf.pages[page_no - 1].height
    x0, y0, x1, y1 = bbox
    flipped = [x0, height - y1, x1, height - y0]
    for span in spans:
        if inside(span, flipped):
            return (f"; its y looks measured from the top of the page, but bbox y is measured from the bottom"
                    f" (y = page height - pdfplumber's top/bottom), which gives {[round(v, 1) for v in flipped]}")
    return f"; the quote is at {[round(v, 1) for v in spans[0]]}"


def check(b: B.Bundle, page: B.Page, source: dict, lib: Library, annotated_dir: Path, report: Report) -> bool:
    sid = source.get("id")
    resource, page_no, bbox = source.get("resource"), source.get("page"), source.get("bbox")
    if not isinstance(resource, str) or not isinstance(page_no, int) or not isinstance(bbox, list) or len(bbox) != 4:
        return False  # malformed sources are okf-validate's job
    path = B.resolve(b.root, page.rel, resource)
    if path is None or path.suffix.lower() != ".pdf" or not path.is_file():
        report.warning("ground.resource", page.rel, f"'{resource}' is not a PDF in the bundle, so it was not checked", sid)
        return False
    paper = lib.get(path)
    if paper is None:
        report.error("ground.resource", page.rel, f"'{resource}' could not be opened as a PDF", sid)
        return False
    if not 1 <= page_no <= paper.pages:
        report.error("ground.page", page.rel, f"page {page_no} does not exist; the paper has {paper.pages} pages", sid)
        return True

    if "quote" in source:
        spans = find_quote(paper.pdf.pages[page_no - 1], str(source["quote"]))
        if not spans:
            report.error("ground.quote", page.rel, f"the quote is not on page {page_no}", sid)
        elif not any(inside(s, bbox) for s in spans):
            msg = f"the quote is on page {page_no} but not inside its bbox"
            report.error("ground.bbox", page.rel, msg + where_hint(paper, page_no, bbox, spans), sid)
    elif "value" in source:
        spans = find_quote(paper.pdf.pages[page_no - 1], str(source["value"]))
        if not any(inside(s, bbox) for s in spans):
            msg = f"the value '{source['value']}' is not inside its bbox on page {page_no}"
            report.error("ground.bbox", page.rel, msg + (where_hint(paper, page_no, bbox, spans) if spans else ""), sid)

    copy = annotated_dir / path.name
    marked = lib.get(copy)
    if marked is None:
        report.error("ground.highlight", page.rel, f"no annotated copy at {copy.relative_to(b.root) if copy.is_relative_to(b.root) else copy}", sid)
    elif page_no > marked.pages or not any(iou(bbox, h) >= MIN_IOU for h in marked.highlights(page_no)):
        report.error("ground.highlight", page.rel, f"no highlight on page {page_no} of the annotated copy matches the bbox", sid)
    return True


def ground(root: Path, annotated: str = "annotated") -> Report:
    report = Report(TOOL)
    b = B.load(root)
    annotated_dir = (root / annotated).resolve()
    lib = Library()
    checked = 0
    try:
        for page in b.concepts:
            for source in page.sources:
                if isinstance(source, dict) and check(b, page, source, lib, annotated_dir, report):
                    checked += 1
    finally:
        lib.close()
    report.extra["checked"] = checked
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=TOOL, description=__doc__.splitlines()[0])
    parser.add_argument("bundle", type=Path, help="the bundle root directory")
    parser.add_argument("--annotated", default="annotated", help="directory of annotated copies, relative to the bundle root")
    parser.add_argument("--json", action="store_true", help="print a JSON report")
    args = parser.parse_args(argv)
    if not args.bundle.is_dir():
        return fail(TOOL, f"{args.bundle} is not a directory")
    report = ground(args.bundle, args.annotated)
    report.print(args.json)
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
