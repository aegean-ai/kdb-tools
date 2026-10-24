"""The checks as tool functions an agent can call, with results it can act on.

They depend on no agent framework. kdb_tools.agent wraps them as a Pydantic AI toolset and
kdb_tools.mcp serves them over MCP, so every agent sees the same tools and the same answers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

from . import ground as G
from . import sheet as S
from . import validate as V
from .locate import find_quote

DOCUMENT_DIRS = ("papers", "documents")  # where a bundle keeps its source files


class ToolInputError(ValueError):
    """A call the agent should correct; the message says how."""


@dataclass
class Match:
    found: bool
    boxes: list[list[float]] = field(default_factory=list)  # [x0, y0, x1, y1] in PDF points from the bottom left
    note: str = ""
    cells: list[str] = field(default_factory=list)  # spreadsheet cells, such as "DH!B4"


@dataclass
class Problem:
    rule: str
    level: str
    path: str
    message: str
    source_id: str | None = None


@dataclass
class Checked:
    errors: list[Problem] = field(default_factory=list)
    warnings: list[Problem] = field(default_factory=list)


class KdbTools:
    """The tools for one kDB bundle."""

    def __init__(self, root: Path, profiles=None):
        self.root = Path(root)
        self.profiles = profiles

    def documents(self, suffixes: tuple[str, ...] = (".pdf",)) -> dict[str, Path]:
        """The bundle's source files with these suffixes, by id (the file name without its suffix)."""
        return {p.stem: p for d in DOCUMENT_DIRS for p in sorted((self.root / d).glob("*"))
                if p.is_file() and p.suffix.lower() in suffixes}

    def papers(self) -> list[str]:
        return sorted(self.documents())

    def _document(self, document: str, suffixes: tuple[str, ...], kind: str) -> Path:
        found = self.documents(suffixes)
        if document not in found:
            raise ToolInputError(f"there is no {kind} {document!r}; the {kind}s are: {', '.join(sorted(found)) or 'none'}")
        return found[document]

    def _locate(self, document: str, page: int, text: str, what: str) -> Match:
        pdf = self._document(document, (".pdf",), "PDF")
        with pdfplumber.open(pdf) as doc:
            if not 1 <= page <= len(doc.pages):
                raise ToolInputError(f"{document} has pages 1 to {len(doc.pages)}; there is no page {page}")
            boxes = find_quote(doc.pages[page - 1], text)
            if boxes:
                return Match(True, boxes)
            elsewhere = [n for n in range(1, len(doc.pages) + 1) if n != page and find_quote(doc.pages[n - 1], text)]
        if elsewhere:
            return Match(False, note=f"the {what} is not on page {page}; it is on page {', '.join(map(str, elsewhere))}")
        return Match(False, note=f"the {what} is not in {document}; copy it word for word from the page")

    def find_quote(self, document: str, page: int, text: str) -> Match:
        """Where a sentence is on a page of a PDF, as the checks will look for it."""
        return self._locate(document, page, text, "sentence")

    def find_value(self, document: str, page: int, value: str) -> Match:
        """Where a table value, such as 76.5, is on a page of a PDF."""
        return self._locate(document, page, value, "value")

    def find_cell(self, document: str, value: str, sheet: str | None = None) -> Match:
        """Every cell of a spreadsheet that holds the value, as the checks will read it."""
        path = self._document(document, S.SUFFIXES, "spreadsheet")
        try:
            cells = S.Workbook(path).find(value, sheet)
        except S.SheetError as exc:
            raise ToolInputError(str(exc)) from exc
        if cells:
            return Match(True, cells=cells)
        return Match(False, note=f"no cell in {document}{' sheet ' + sheet if sheet else ''} holds {value!r}")

    def check_pages(self, paths: list[str]) -> Checked:
        """okf-validate and okf-ground on the given pages only."""
        out = Checked()
        for report in (V.validate(self.root, self.profiles, pages=paths), G.ground(self.root, pages=paths)):
            for f in report.findings:
                (out.errors if f.level == "error" else out.warnings).append(Problem(f.rule, f.level, f.path, f.message, f.source_id))
        return out
