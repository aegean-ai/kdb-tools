"""okf-ground: check every source in a kDB bundle against its document and its annotated copy.

A sentence source must appear on its page and inside its bbox. A table-cell source's value must
appear inside its bbox. Every bbox must match a highlight in the annotated copy of the PDF. A
spreadsheet cell source's cell must hold its value; a spreadsheet has no highlights.

With a lake, the evidence in rows is checked the same way: each MediaRef in a bound table's
media_ref_list columns, against the bytes registered for its sha256. The annotated copies are the
media rows that annotate the cited PDF; a highlight in any of them passes. A finding on a row has
the path <table>/<iri>.
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path

import duckdb
import pdfplumber

from . import bundle as B
from . import lake as L
from . import media as M
from . import onto as O
from . import sheet as S
from .locate import find_quote, inside
from .report import Report, fail

TOOL = "okf-ground"
MIN_IOU = 0.5
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SUFFIX = re.compile(r"\.[a-z0-9]{1,5}")


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
        self._books: dict[Path, S.Workbook | str] = {}

    def workbook(self, path: Path) -> S.Workbook | str:
        """The workbook, or why it could not be read."""
        if path not in self._books:
            try:
                self._books[path] = S.Workbook(path)
            except Exception as exc:  # not a readable spreadsheet
                self._books[path] = f"'{path.name}' could not be read as a spreadsheet: {exc}"
        return self._books[path]

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


def check_cell(b: B.Bundle, page: B.Page, source: dict, lib: Library, report: Report) -> bool:
    sid, resource, ref = source.get("id"), source.get("resource"), source.get("cell")
    if not isinstance(resource, str) or not S.is_a1(ref) or "value" not in source:
        return False  # malformed sources are okf-validate's job
    path = B.resolve(b.root, page.rel, resource)
    if path is None or path.suffix.lower() not in S.SUFFIXES or not path.is_file():
        report.warning("ground.resource", page.rel, f"'{resource}' is not a spreadsheet in the bundle, so it was not checked", sid)
        return False
    book = lib.workbook(path)
    if isinstance(book, str):
        report.error("ground.resource", page.rel, book, sid)
        return False
    check_book(book, source, page.rel, report)
    return True


def check_book(book: S.Workbook, source: dict, rel: str, report: Report) -> None:
    """The cited cell holds the cited value. Findings go on rel: a page, or a row's <table>/<iri>."""
    sid, ref = source.get("id"), source["cell"]
    try:
        held = book.read(source.get("sheet"), ref)
    except S.SheetError as exc:
        rule = "ground.sheet" if str(exc).startswith("there is no sheet") else "ground.cell"
        report.error(rule, rel, str(exc), sid)
        return
    if held is None or not S.same_value(source["value"], held):
        where = f"{source['sheet']}!{ref}" if source.get("sheet") else ref
        holds = "is empty" if held is None else f"holds {S.show(held)!r}"
        found = book.find(source["value"])
        hint = f"; '{source['value']}' is in {', '.join(found[:5])}" if found else ""
        report.error("ground.cell", rel, f"cell {where} {holds}, not '{source['value']}'{hint}", sid)


def check(b: B.Bundle, page: B.Page, source: dict, lib: Library, annotated_dir: Path, report: Report) -> bool:
    if "cell" in source:
        return check_cell(b, page, source, lib, report)
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
    copy = annotated_dir / path.name
    no_copy = f"no annotated copy at {copy.relative_to(b.root) if copy.is_relative_to(b.root) else copy}"
    check_paper(paper, lambda: [c for c in [lib.get(copy)] if c is not None], no_copy, source, page.rel, report)
    return True


def check_paper(paper: Paper, marked, no_copy: str, source: dict, rel: str, report: Report) -> None:
    """The page exists, the quote or value is inside the bbox, and a highlight in an annotated copy
    matches the bbox. marked() opens the copies, in a fixed order, or gives none; no_copy says where
    they were looked for. Any copy with a matching highlight passes. Findings go on rel: a page, or a
    row's <table>/<iri>."""
    sid, page_no, bbox = source.get("id"), source["page"], source["bbox"]
    if not 1 <= page_no <= paper.pages:
        report.error("ground.page", rel, f"page {page_no} does not exist; the paper has {paper.pages} pages", sid)
        return

    if "quote" in source:
        spans = find_quote(paper.pdf.pages[page_no - 1], str(source["quote"]))
        if not spans:
            report.error("ground.quote", rel, f"the quote is not on page {page_no}", sid)
        elif not any(inside(s, bbox) for s in spans):
            msg = f"the quote is on page {page_no} but not inside its bbox"
            report.error("ground.bbox", rel, msg + where_hint(paper, page_no, bbox, spans), sid)
    elif "value" in source:
        spans = find_quote(paper.pdf.pages[page_no - 1], str(source["value"]))
        if not any(inside(s, bbox) for s in spans):
            msg = f"the value '{source['value']}' is not inside its bbox on page {page_no}"
            report.error("ground.bbox", rel, msg + (where_hint(paper, page_no, bbox, spans) if spans else ""), sid)

    copies = marked()
    if not copies:
        report.error("ground.highlight", rel, no_copy, sid)
    elif not any(page_no <= c.pages and any(iou(bbox, h) >= MIN_IOU for h in c.highlights(page_no)) for c in copies):
        report.error("ground.highlight", rel, f"no highlight on page {page_no} of the annotated copy matches the bbox", sid)


class Media:
    """Registered files as local copies, so Library opens them as it opens bundle files.

    Each file is fetched from the store once and checked against its sha256 before it is used."""

    def __init__(self, lake: L.Lake, store: L.Store, tmp: Path):
        self.lake, self.store, self.tmp = lake, store, tmp
        self._files: dict[str, Path | str] = {}

    def file(self, sha: str) -> Path | str:
        """The local copy of a registered file, or the ground.media message that says why there is none."""
        if sha not in self._files:
            row = M.lookup(self.lake, sha)
            if row is None:
                self._files[sha] = f"{sha} is not registered in kdb.media"
            else:
                data = M.fetch(self.store, row)
                if isinstance(data, str):
                    self._files[sha] = data
                else:
                    suffix = Path(row["name"] or "").suffix.lower()
                    if not SUFFIX.fullmatch(suffix):
                        suffix = ""  # the name is the registrant's; only a plain suffix is used
                    path = self.tmp / f"{sha}{suffix}"
                    path.write_bytes(data)
                    self._files[sha] = path
        return self._files[sha]


def evidence_columns(o: O.Ontology) -> list[tuple[str, list[str]]]:
    """Each bound table with its media_ref_list columns, in table order."""
    out = {}
    for k in o.classes.values():
        if k.binding and NAME.fullmatch(k.binding.table):
            cols = [c for c, p in k.binding.columns.items()
                    if o.props.get(p) and o.props[p].datatype == "media_ref_list" and NAME.fullmatch(c)]
            if cols:
                out.setdefault(k.binding.table, cols)
    return sorted(out.items())


def lake_columns(lake: L.Lake, table: str) -> set[str]:
    rows = lake.con.execute("SELECT column_name FROM information_schema.columns WHERE table_catalog = 'lake' "
                            "AND table_schema = 'main' AND table_name = ?", [table]).fetchall()
    return {c for (c,) in rows}


def check_ref(media: Media, lib: Library, ref: M.MediaRef, sid: str, rel: str, report: Report) -> bool:
    """One MediaRef against its registered file, with today's PDF and spreadsheet checks."""
    found = media.file(ref.sha256)
    if isinstance(found, str):
        report.error("ground.media", rel, found, sid)
        return True
    source = {**ref.model_dump(exclude_none=True), "id": sid}
    kind = M.kind(M.lookup(media.lake, ref.sha256))
    if kind == "sheet" or ref.cell is not None:
        if kind != "sheet" or not S.is_a1(ref.cell) or ref.value is None:
            return False  # nothing a spreadsheet check can use; UpsertObjects refuses it
        book = lib.workbook(found)
        if isinstance(book, str):
            report.error("ground.resource", rel, book, sid)
        else:
            check_book(book, source, rel, report)
        return True
    if kind != "pdf" or ref.page is None or ref.bbox is None:
        return False  # not a PDF passage; other selectors are not checked yet
    paper = lib.get(found)
    if paper is None:
        report.error("ground.resource", rel, f"{ref.sha256} could not be opened as a PDF", sid)
        return False

    def marked():
        out = []
        for copy in M.annotated_copies(media.lake, ref.sha256):
            path = media.file(copy["sha256"])
            paper = lib.get(path) if isinstance(path, Path) else None
            if paper is not None:
                out.append(paper)
        return out

    check_paper(paper, marked, f"no media row annotates {ref.sha256}", source, rel, report)
    return True


def check_rows(root: Path, lake: L.Lake, store: L.Store, lib: Library, wanted: set[str] | None, report: Report) -> int:
    """Check the evidence in every bound table's rows; the number of MediaRefs checked."""
    o = O.load_ontology(B.load(root))
    checked = 0
    with tempfile.TemporaryDirectory(prefix="okf-ground-") as tmp:
        media = Media(lake, store, Path(tmp))
        try:
            for table, cols in evidence_columns(o):
                have = lake_columns(lake, table)
                cols = [c for c in cols if c in have]
                if not cols or "iri" not in have:
                    continue  # not bound in the lake yet
                select = ", ".join(f'"{c}"' for c in cols)
                for iri, *values in lake.con.execute(f'SELECT iri, {select} FROM "{table}" ORDER BY iri').fetchall():
                    rel = f"{table}/{iri}"
                    if wanted is not None and rel not in wanted:
                        continue
                    for col, value in zip(cols, values):
                        checked += check_value(media, lib, col, value, rel, report)
        finally:
            lib.close()  # the copies live in tmp, which goes next
    return checked


def check_value(media: Media, lib: Library, col: str, value, rel: str, report: Report) -> int:
    """The MediaRefs in one media_ref_list cell; a malformed one is a ground.media finding."""
    if value is None:
        return 0
    try:
        refs = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        refs = None
    if not isinstance(refs, list):
        report.error("ground.media", rel, f"{col} is not a list of MediaRefs", col)
        return 0
    checked = 0
    for i, raw in enumerate(refs):
        sid = f"{col}[{i}]"
        try:
            ref = M.MediaRef.model_validate(raw)
        except ValueError as e:
            report.error("ground.media", rel, f"{sid} is not a MediaRef: {M.first_error(e)}", sid)
            continue
        checked += check_ref(media, lib, ref, sid, rel, report)
    return checked


def ground(root: Path, annotated: str = "annotated", pages: list[str] | None = None,
           lake: L.Lake | None = None, store: L.Store | None = None) -> Report:
    """Check every source against its PDF; with pages, only the sources on those pages.

    With a lake (and its object store), the MediaRefs in bound tables' rows are checked too; pages
    may then name rows as <table>/<iri>."""
    if lake is not None and store is None:
        raise ValueError("ground needs the object store of the lake to read the cited files")
    report = Report(TOOL)
    wanted = {p.lstrip("/") for p in pages} if pages is not None else None
    b = B.load(root)
    annotated_dir = (root / annotated).resolve()
    lib = Library()
    checked = 0
    try:
        for page in b.concepts:
            if wanted is not None and page.rel not in wanted:
                continue
            for source in page.sources:
                if isinstance(source, dict) and check(b, page, source, lib, annotated_dir, report):
                    checked += 1
    finally:
        lib.close()
    if lake is not None:
        checked += check_rows(root, lake, store, Library(), wanted, report)
    report.extra["checked"] = checked
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=TOOL, description=__doc__.splitlines()[0])
    parser.add_argument("bundle", type=Path, help="the bundle root directory")
    parser.add_argument("--annotated", default="annotated", help="directory of annotated copies, relative to the bundle root")
    parser.add_argument("--json", action="store_true", help="print a JSON report")
    parser.add_argument("--pages", nargs="+", metavar="PAGE", help="check only the sources on these pages")
    parser.add_argument("--lake-config", type=Path, metavar="PATH",
                        help="the kdb.toml that holds [lake]; default: the nearest kdb.toml in or above the bundle")
    args = parser.parse_args(argv)
    if not args.bundle.is_dir():
        return fail(TOOL, f"{args.bundle} is not a directory")
    from .validate import find_config

    config = args.lake_config or find_config(args.bundle)
    try:
        cfg = L.load_config(config) if config else None
    except (OSError, ValueError, KeyError) as e:  # a bad or unreadable kdb.toml (ConfigError is a ValueError)
        return fail(TOOL, f"cannot read the lake config {config}: {e}")
    lake = store = None
    if cfg is not None:
        try:
            lake, store = L.Lake.open(cfg), L.store_for(cfg)
        except Exception as e:
            if lake is not None:
                lake.close()
            return fail(TOOL, f"cannot open the lake at {cfg.catalog}: {e}")
    try:
        report = ground(args.bundle, args.annotated, args.pages, lake, store)
    except duckdb.Error as e:
        return fail(TOOL, f"cannot read the lake: {e}")
    finally:
        if lake is not None:
            lake.close()
    report.print(args.json)
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
