"""Media citations: the MediaRef a row cites as evidence, and reading registered bytes back.

A MediaRef names a file by its sha256 and can point into it: a page and a bbox in PDF points from
the bottom left, the quoted text (the W3C TextQuoteSelector exact), a table value, a spreadsheet
sheet and cell, an image region (xywh) or a time range (t). It is stored as a JSON object with
W3C-named fields; fragment() renders the W3C media-fragment string for display and export.
"""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import PurePath

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import sheet as S
from .lake import Lake, Store

SHA256 = r"^[0-9a-f]{64}$"
MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
}


class MediaRef(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    sha256: str = Field(pattern=SHA256)
    page: int | None = Field(default=None, ge=1)
    bbox: list[float] | None = None  # [x0, y0, x1, y1] in PDF points from the bottom left
    quote: str | None = None
    value: str | None = None
    sheet: str | None = None
    cell: str | None = None
    xywh: list[float] | None = None
    t: list[float] | None = None  # [start] or [start, end] in seconds

    @field_validator("bbox", "xywh")
    @classmethod
    def _four(cls, v):
        if v is not None and len(v) != 4:
            raise ValueError("needs four numbers")
        return v

    @field_validator("t")
    @classmethod
    def _range(cls, v):
        if v is not None and len(v) not in (1, 2):
            raise ValueError("needs a start, or a start and an end")
        return v

    def fragment(self) -> str:
        """The W3C media fragment, such as #page=3&xywh=120,40,400,140. A bbox becomes xywh as x0, y0,
        width and height, still in PDF points from the bottom left."""
        parts = []
        if self.page is not None:
            parts.append(f"page={self.page}")
        box = self.xywh or (self.bbox and [self.bbox[0], self.bbox[1], self.bbox[2] - self.bbox[0],
                                           self.bbox[3] - self.bbox[1]])
        if box:
            parts.append("xywh=" + ",".join(_num(v) for v in box))
        if self.t:
            parts.append("t=" + ",".join(_num(v) for v in self.t))
        if self.sheet is not None:
            parts.append(f"sheet={self.sheet}")
        if self.cell is not None:
            parts.append(f"cell={self.cell}")
        return "#" + "&".join(parts) if parts else ""


def kind(row: dict) -> str | None:
    """What okf-ground can check in a registered file: "pdf", "sheet" (by its name's suffix), or None."""
    if PurePath(row.get("name") or "").suffix.lower() in S.SUFFIXES:
        return "sheet"
    return "pdf" if row.get("media_type") == "application/pdf" else None


def unchecked(ref: MediaRef, row: dict) -> str | None:
    """Why okf-ground could not check what the MediaRef cites in this file, or None. A reference that
    cites no quote, value or cell has nothing to check and is fine."""
    if ref.quote is None and ref.value is None and ref.cell is None:
        return None
    k = kind(row)
    if k == "pdf":
        if ref.cell is not None:
            return "a cell needs a spreadsheet, and this file is a PDF"
        if ref.page is None or ref.bbox is None:
            return "a quote or value in a PDF needs a page and a bbox"
        return None
    if k == "sheet":
        if ref.quote is not None:
            return "a spreadsheet citation gives a cell and a value, not a quote"
        if ref.cell is None or ref.value is None:
            return "a spreadsheet citation needs both a cell and a value"
        if not S.is_a1(ref.cell):
            return f"cell {ref.cell!r} is not an A1 cell reference such as B4"
        return None
    return f"a quote, value or cell needs a PDF or a spreadsheet, and this file is {row.get('media_type')}"


def first_error(e: ValueError) -> str:
    """The first problem a pydantic ValidationError names, as "field: message"."""
    errors = e.errors() if hasattr(e, "errors") else []
    if not errors:
        return str(e)
    x = errors[0]
    return f"{'.'.join(map(str, x['loc'])) or 'value'}: {x['msg']}"


def _num(v: float) -> str:
    return f"{v:g}"


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def media_type(name: str) -> str:
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return MEDIA_TYPES.get(suffix) or mimetypes.guess_type(name)[0] or "application/octet-stream"


def lookup(lake: Lake, sha256: str) -> dict | None:
    """The kdb.media row of a sha256, or None when it is not registered."""
    cur = lake.con.execute("SELECT * FROM kdb.media WHERE sha256 = ?", [sha256])
    names = [d[0] for d in cur.description]
    found = cur.fetchone()
    return None if found is None else dict(zip(names, found))


def annotated_copies(lake: Lake, sha256: str) -> list[dict]:
    """The media rows that annotate the file, in sha256 order; empty when none does."""
    cur = lake.con.execute("SELECT * FROM kdb.media WHERE annotates = ? ORDER BY sha256", [sha256])
    names = [d[0] for d in cur.description]
    return [dict(zip(names, found)) for found in cur.fetchall()]


def fetch(store: Store, row: dict) -> bytes | str:
    """A registered file's bytes, or why they cannot be trusted."""
    sha = row["sha256"]
    try:
        if not store.exists(row["object_key"]):
            return f"the bytes of {sha} are not in the object store"
        data = store.get(row["object_key"])
    except Exception as exc:  # an unreachable store is a finding, not a crash
        return f"the bytes of {sha} could not be read: {exc}"
    return data if sha256_of(data) == sha else f"the stored bytes of {sha} no longer hash to it"
