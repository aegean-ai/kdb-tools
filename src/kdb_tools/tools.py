"""The checks as tool functions an agent can call, with results it can act on.

They depend on no agent framework. kdb_tools.agent wraps them as a Pydantic AI toolset and
kdb_tools.mcp serves them over MCP, so every agent sees the same tools and the same answers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

from . import ground as G
from . import validate as V
from .locate import find_quote


class ToolInputError(ValueError):
    """A call the agent should correct; the message says how."""


@dataclass
class Match:
    found: bool
    boxes: list[list[float]] = field(default_factory=list)  # [x0, y0, x1, y1] in PDF points from the bottom left
    note: str = ""


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

    def papers(self) -> list[str]:
        return sorted(p.stem for p in (self.root / "papers").glob("*.pdf"))

    def _locate(self, paper: str, page: int, text: str, what: str) -> Match:
        pdf = self.root / "papers" / f"{paper}.pdf"
        if not pdf.is_file():
            raise ToolInputError(f"there is no paper {paper!r}; the papers are: {', '.join(self.papers())}")
        with pdfplumber.open(pdf) as doc:
            if not 1 <= page <= len(doc.pages):
                raise ToolInputError(f"{paper} has pages 1 to {len(doc.pages)}; there is no page {page}")
            boxes = find_quote(doc.pages[page - 1], text)
            if boxes:
                return Match(True, boxes)
            elsewhere = [n for n in range(1, len(doc.pages) + 1) if n != page and find_quote(doc.pages[n - 1], text)]
        if elsewhere:
            return Match(False, note=f"the {what} is not on page {page}; it is on page {', '.join(map(str, elsewhere))}")
        return Match(False, note=f"the {what} is not in {paper}; copy it word for word from the page")

    def find_quote(self, paper: str, page: int, text: str) -> Match:
        """Where a sentence is on a page of a paper, as the checks will look for it."""
        return self._locate(paper, page, text, "sentence")

    def find_value(self, paper: str, page: int, value: str) -> Match:
        """Where a table value, such as 76.5, is on a page of a paper."""
        return self._locate(paper, page, value, "value")

    def check_pages(self, paths: list[str]) -> Checked:
        """okf-validate and okf-ground on the given pages only."""
        out = Checked()
        for report in (V.validate(self.root, self.profiles, pages=paths), G.ground(self.root, pages=paths)):
            for f in report.findings:
                (out.errors if f.level == "error" else out.warnings).append(Problem(f.rule, f.level, f.path, f.message, f.source_id))
        return out
