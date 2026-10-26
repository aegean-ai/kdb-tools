"""The common agent parts for a kDB project, on Pydantic AI. Needs the agent extra: kdb-tools[agent].

toolset(root) gives an agent the kDB checks as tools:

    agent = Agent(model, toolsets=[toolset(Path("kdb"))])

extraction_agent() reads one chunk of a document, a few PDF pages or one sheet, and returns the
project's own output type. Any Quote or CellValue inside that output is checked against the
document, and the model is sent back to fix the ones that are not there. ask_agent() answers a
question from the kDB alone and sends back a claim that cites a source its page does not have.
A project supplies the output type and the instructions; writing the kDB pages stays its own.
"""

from __future__ import annotations

import functools
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber
from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext, Tool
from pydantic_ai.toolsets import FunctionToolset

from . import bundle as B
from . import sheet as S
from .locate import find_quote as locate_quote
from .actions import Context, Failed
from .tools import Checked, KdbTools, Match, ToolInputError


def toolset(root: Path, profiles=None, ctx: Context | None = None) -> FunctionToolset:
    """The kDB tools. Given a Context (a lake), the agent also reads classes and objects and runs actions."""
    kt = KdbTools(root, profiles, ctx)

    def retry_on_bad_input(fn):
        @functools.wraps(fn)  # keeps the name, signature and docstring the model sees
        def wrapped(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ToolInputError as e:
                raise ModelRetry(str(e)) from e
            except Failed as e:  # an action that failed or conflicted: the model gets every message
                raise ModelRetry("\n".join(e.messages)) from e
        return wrapped

    @retry_on_bad_input
    def find_quote(document: str, page: int, text: str) -> Match:
        """Find a sentence on a page of a PDF. document is the file name without .pdf, such as 2406.09246 or
        ar4-manual, and page counts from 1. Returns the bounding boxes where the sentence is, or a note saying
        which page it is on."""
        return kt.find_quote(document, page, text)

    @retry_on_bad_input
    def find_value(document: str, page: int, value: str) -> Match:
        """Find a table value, such as 76.5, on a page of a PDF. Returns its bounding boxes, or a note."""
        return kt.find_value(document, page, value)

    @retry_on_bad_input
    def find_cell(document: str, value: str, sheet: str | None = None) -> Match:
        """Find the cells of a spreadsheet (.xlsx, .xlsm or .csv) that hold a value, such as 169.77. document is the
        file name without its suffix, and sheet limits the search to one sheet. Returns cells such as DH!B4."""
        return kt.find_cell(document, value, sheet)

    def check_pages(paths: list[str]) -> Checked:
        """Run okf-validate and okf-ground on kDB pages, such as models/openvla.md, and return their errors
        and warnings. Use it after writing pages to see what to fix."""
        return kt.check_pages(paths)

    tools = [find_quote, find_value, find_cell, check_pages]
    if ctx is not None:
        tools += _lake_tools(kt, retry_on_bad_input)
    return FunctionToolset([Tool(f, takes_ctx=False) for f in tools])


def _lake_tools(kt: KdbTools, guarded) -> list:
    @guarded
    def list_classes() -> list[dict]:
        """Every class in the kDB with its status (experimental, active or deprecated) and bound table."""
        return kt.list_classes()

    @guarded
    def get_class(iri: str) -> dict:
        """One class: status, supertypes, interfaces, binding, constraints and its properties by column."""
        return kt.get_class(iri)

    @guarded
    def query_objects(class_iri: str, where: dict[str, str] | None = None, limit: int = 50) -> list[dict]:
        """Rows of a class's table. where maps column names to values the column must equal, compared as text."""
        return kt.query_objects(class_iri, where, limit)

    @guarded
    def get_object(iri: str) -> dict | None:
        """The row with this iri, from whichever class's table holds it, or null."""
        return kt.get_object(iri)

    @guarded
    def actions() -> list[dict]:
        """Every action you can run, with its version and the JSON schema of its params."""
        return kt.actions()

    @guarded
    def dry_run(action: str, params: dict) -> dict:
        """The plan an action would carry out (pages and rows it would edit). It changes nothing."""
        return kt.dry_run(action, params)

    @guarded
    def submit(action: str, params: dict) -> dict:
        """Run an action. It is the only way to change a page or a row. An agent's action that edits an active Class
        page is refused: a person must submit it. Returns the action_id."""
        return kt.submit(action, params)

    return [list_classes, get_class, query_objects, get_object, actions, dry_run, submit]


class Quote(BaseModel):
    """Evidence from a PDF. Subclass it in your output type; the extraction agent checks every one."""

    quote: str = Field(description="one sentence copied word for word from the page")
    page: int = Field(description="the page the quote is on, counted from 1")


class CellValue(BaseModel):
    """Evidence from a spreadsheet. Subclass it in your output type; the extraction agent checks every one."""

    value: str = Field(description="the value exactly as the cell holds it")
    cell: str = Field(description="the cell that holds it, such as B4")
    sheet: str | None = Field(default=None, description="the sheet the cell is on; leave it out for a .csv file")


@dataclass
class Chunk:
    """The part of a document one extraction call reads: some pages of a PDF, or one sheet of a spreadsheet."""

    document: Path
    pages: list[int] = field(default_factory=list)  # PDF pages, counted from 1
    sheet: str | None = None


def pdf_chunks(pdf: Path, pages_per_chunk: int = 3) -> Iterator[tuple[Chunk, str]]:
    """Each run of pages_per_chunk pages, with the prompt text that marks where each page starts."""
    with pdfplumber.open(pdf) as doc:
        texts = [p.extract_text() or "" for p in doc.pages]
    for start in range(0, len(texts), pages_per_chunk):
        pages = list(range(start + 1, min(start + pages_per_chunk, len(texts)) + 1))
        yield Chunk(pdf, pages), "\n".join(f"=== page {n} ===\n{texts[n - 1]}" for n in pages)


def sheet_chunks(path: Path) -> Iterator[tuple[Chunk, str]]:
    """Each sheet of a spreadsheet, one row per line, every value next to its cell reference."""
    book = S.Workbook(path)
    for name, cells in book.sheets.items():
        rows: dict[int, list[str]] = {}
        for (r, c), v in sorted(cells.items()):
            rows.setdefault(r, []).append(f"{S.column_letters(c)}{r}: {S.show(v)}")
        title = f"sheet {name}" if name else path.name
        yield Chunk(path, sheet=name or None), f"=== {title} ===\n" + "\n".join(" | ".join(v) for v in rows.values())


def evidence(obj) -> Iterator[Quote | CellValue]:
    """Every Quote and CellValue inside an output, however deeply nested."""
    if isinstance(obj, (Quote, CellValue)):
        yield obj
    if isinstance(obj, BaseModel):
        for name in type(obj).model_fields:
            yield from evidence(getattr(obj, name))
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from evidence(item)
    elif isinstance(obj, dict):
        for item in obj.values():
            yield from evidence(item)


def problems_with(chunk: Chunk, items: list[Quote | CellValue]) -> list[str]:
    out = []
    quotes = [e for e in items if isinstance(e, Quote)]
    cells = [e for e in items if isinstance(e, CellValue)]
    is_sheet = chunk.document.suffix.lower() in S.SUFFIXES
    if quotes:
        if is_sheet:
            out += [f"{q.quote!r} is a quote, but this chunk is a spreadsheet: cite its cells instead" for q in quotes]
        else:
            with pdfplumber.open(chunk.document) as doc:
                for q in quotes:
                    if q.page not in chunk.pages:
                        out.append(f"page {q.page} is not one of the pages you were given ({chunk.pages}): {q.quote!r}")
                    elif not locate_quote(doc.pages[q.page - 1], q.quote):
                        out.append(f"page {q.page}: {q.quote!r} is not on that page")
    if cells:
        if not is_sheet:
            return out + [f"cell {c.cell} is cited, but this chunk is a PDF: quote its pages instead" for c in cells]
        book = S.Workbook(chunk.document)
        for c in cells:
            sheet = c.sheet or chunk.sheet
            where = f"{sheet}!{c.cell}" if sheet else c.cell
            try:
                held = book.read(sheet, c.cell)
            except S.SheetError as exc:
                out.append(f"{where}: {exc}")
                continue
            if held is None or not S.same_value(c.value, held):
                out.append(f"{where} {'is empty' if held is None else f'holds {S.show(held)!r}'}, not {c.value!r}")
    return out


def extraction_agent(model, output_type: type[BaseModel], instructions: str, root: Path | None = None,
                     settings: dict | None = None, retries: int = 2) -> Agent[Chunk, BaseModel]:
    """An agent that reads one Chunk and returns output_type. Given the kDB root, it can also call the kDB tools,
    so it can look evidence up before it answers instead of being sent back."""
    agent = Agent(model, deps_type=Chunk, output_type=output_type, instructions=instructions, retries=retries,
                  toolsets=[toolset(root)] if root is not None else [], model_settings=settings or None)

    @agent.output_validator
    def evidence_is_in_the_document(ctx: RunContext[Chunk], out):
        problems = problems_with(ctx.deps, list(evidence(out)))
        if problems:
            raise ModelRetry("This evidence is not in the document where you said it is. Fix or drop it and return "
                             "everything again:\n" + "\n".join(problems))
        return out

    return agent


class Claim(BaseModel):
    text: str = Field(description="one sentence of the answer, in your own words")
    page: str = Field(description="the kDB page the evidence is on, such as joints/j1.md")
    source_ids: list[str] = Field(min_length=1, description="ids of sources on that page that support the sentence")


class Answer(BaseModel):
    answerable: bool = Field(description="false when the kDB does not contain the answer")
    claims: list[Claim] = Field(default_factory=list)
    note: str = Field(default="", description="when not answerable, what is missing")


@dataclass
class KDB:
    root: Path


ASK_INSTRUCTIONS = (
    "You answer questions using only a knowledge base (kDB) of pages. Find the pages you need, read them, and write "
    "each sentence of the answer as a claim that names its page and the ids of the sources on that page that support "
    "it. If the kDB does not contain the answer, set answerable to false."
)


def page_sources(root: Path, rel: str) -> dict[str, dict] | None:
    """The sources of a kDB page by id, or None when there is no such page inside the kDB."""
    path = (root / rel).resolve()
    if not path.is_file() or root.resolve() not in path.parents:
        return None
    page = B.read_page(root.resolve(), path)
    return {s["id"]: s for s in page.sources if isinstance(s, dict) and "id" in s}


def ask_agent(model, instructions: str = ASK_INSTRUCTIONS, settings: dict | None = None,
              retries: int = 2) -> Agent[KDB, Answer]:
    """An agent with two tools that read the kDB, whose every claim must cite sources that exist on its page.
    list_pages does not scale to a large kDB: add tools that search, with agent.tool."""
    agent = Agent(model, deps_type=KDB, output_type=Answer, instructions=instructions, retries=retries,
                  model_settings=settings or None)

    @agent.tool
    def list_pages(ctx: RunContext[KDB]) -> list[dict]:
        """Every page with its type and title."""
        return [{"path": p.rel, "type": (p.frontmatter or {}).get("type"), "title": (p.frontmatter or {}).get("title")}
                for p in B.load(ctx.deps.root).concepts]

    @agent.tool
    def read_page(ctx: RunContext[KDB], path: str) -> dict:
        """One page's text and the sources you may cite from it."""
        found = page_sources(ctx.deps.root, path)
        if found is None:
            raise ModelRetry(f"there is no page {path}; call list_pages")
        page = B.read_page(ctx.deps.root.resolve(), (ctx.deps.root / path).resolve())
        return {"body": page.body, "sources": list(found.values())}

    @agent.output_validator
    def claims_cite_real_sources(ctx: RunContext[KDB], answer: Answer) -> Answer:
        for c in answer.claims:
            found = page_sources(ctx.deps.root, c.page)
            if found is None:
                raise ModelRetry(f"claim cites page {c.page}, which does not exist")
            unknown = [s for s in c.source_ids if s not in found]
            if unknown:
                raise ModelRetry(f"{c.page} has no sources {unknown}; it has {sorted(found)}")
        return answer

    return agent


def ask(agent: Agent[KDB, Answer], root: Path, question: str) -> Answer:
    return agent.run_sync(question, deps=KDB(root)).output
