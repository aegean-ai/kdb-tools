"""kdb_tools.agent: the checks as Pydantic AI tools, with results a model can act on."""

import pytest
from conftest import GOOD_SOURCE, concept, highlight, make_pdf

pytest.importorskip("pydantic_ai")
from pydantic_ai import ModelRetry  # noqa: E402

from kdb_tools import agent as A  # noqa: E402

QUOTE = GOOD_SOURCE["quote"]


@pytest.fixture
def tools(bundle):
    pdf = bundle.root / "papers" / "kim2024.pdf"
    boxes = make_pdf(pdf, [[(72, 700, "Introduction")], [(72, 700, QUOTE), (300, 600, "76.5")]])
    highlight(pdf, bundle.root / "annotated" / pdf.name, [(2, boxes[QUOTE])])
    bundle.add("models/good.md", concept(title="Good", sources=[dict(GOOD_SOURCE, page=2, bbox=boxes[QUOTE])]))
    bundle.add("models/bad.md", concept(title="Bad", generated={"by": "x"}, sources=[dict(GOOD_SOURCE, page=2, bbox=boxes[QUOTE])]))
    ts = A.toolset(bundle.root, profiles=[])
    return {name: t.function for name, t in ts.tools.items()}, boxes


def test_the_toolset_has_the_four_tools(tools):
    assert sorted(tools[0]) == ["check_pages", "find_cell", "find_quote", "find_value"]


def test_find_quote_gives_the_bbox_or_says_where_the_text_is(tools):
    fns, boxes = tools
    hit = fns["find_quote"](document="kim2024", page=2, text=QUOTE)
    assert hit.found and hit.boxes == [pytest.approx(boxes[QUOTE], abs=1)]
    miss = fns["find_quote"](document="kim2024", page=1, text=QUOTE)
    assert not miss.found and "page 2" in miss.note


def test_find_value_finds_a_table_number(tools):
    fns, _ = tools
    assert fns["find_value"](document="kim2024", page=2, value="76.5").found


def test_an_unknown_document_is_sent_back_with_the_documents_there_are(tools):
    fns, _ = tools
    with pytest.raises(ModelRetry, match="kim2024"):
        fns["find_quote"](document="nope", page=1, text=QUOTE)


def test_check_pages_reports_only_the_pages_asked_about(tools):
    fns, _ = tools
    assert fns["check_pages"](paths=["models/good.md"]).errors == []
    bad = fns["check_pages"](paths=["models/bad.md"])
    assert [e.rule for e in bad.errors] == ["project.generated"] and bad.errors[0].path == "models/bad.md"


def test_find_cell_reads_a_spreadsheet(bundle):
    from conftest import make_xlsx

    make_xlsx(bundle.root / "documents" / "ar4-dh.xlsx", {"DH": [["joint", "d (mm)"], ["J1", 169.77]]})
    fns = {name: t.function for name, t in A.toolset(bundle.root, profiles=[]).tools.items()}
    assert fns["find_cell"](document="ar4-dh", value="169.77").cells == ["DH!B2"]
    with pytest.raises(ModelRetry, match="DH"):
        fns["find_cell"](document="ar4-dh", value="169.77", sheet="Joints")
