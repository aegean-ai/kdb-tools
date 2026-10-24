"""Write highlights into a copy of a PDF, one quad per line of the highlighted text."""

from __future__ import annotations

from pathlib import Path

import pdfplumber
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Highlight
from pypdf.generic import ArrayObject, FloatObject, NameObject, TextStringObject

from .locate import find_quote_lines, inside


def highlight(src: Path, dst: Path, marks: list[tuple], color: str = "ffe066") -> list[bool]:
    """Highlight each mark in a copy of src written to dst, and report which were found.

    A mark is (page, text, comment) or (page, text, comment, bbox), with page counted from 1.
    When text occurs more than once on the page, give the bbox of the occurrence you mean;
    otherwise the first one is used. The comment is shown when a reader hovers over the
    highlight; the source id is a good choice. If dst exists, its highlights are kept.
    """
    src, dst = Path(src), Path(dst)
    writer = PdfWriter(clone_from=PdfReader(dst if dst.exists() else src))
    found = []
    with pdfplumber.open(src) as pdf:
        for mark in marks:
            page_no, text, comment = mark[:3]
            want = mark[3] if len(mark) > 3 else None
            occurrences = find_quote_lines(pdf.pages[page_no - 1], text) if 1 <= page_no <= len(pdf.pages) else []
            if want is not None:
                occurrences = [o for o in occurrences if inside(_union(o), want)]
            if not occurrences:
                found.append(False)
                continue
            lines = occurrences[0]
            quads = ArrayObject(FloatObject(v) for x0, y0, x1, y1 in lines for v in (x0, y1, x1, y1, x0, y0, x1, y0))
            annot = Highlight(rect=tuple(_union(lines)), quad_points=quads, highlight_color=color)
            annot[NameObject("/Contents")] = TextStringObject(comment)
            writer.add_annotation(page_number=page_no - 1, annotation=annot)
            found.append(True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "wb") as fh:
        writer.write(fh)
    return found


def _union(lines: list[list[float]]) -> list[float]:
    return [min(b[0] for b in lines), min(b[1] for b in lines), max(b[2] for b in lines), max(b[3] for b in lines)]
