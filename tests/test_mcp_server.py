"""okf-mcp serves the same tools over MCP; a bad call comes back flagged as an error."""

import json
import sys

import anyio
import pytest
from conftest import GOOD_SOURCE, concept, highlight, make_pdf

pytest.importorskip("mcp")
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

QUOTE = GOOD_SOURCE["quote"]


@pytest.fixture
def kdb(bundle):
    pdf = bundle.root / "papers" / "kim2024.pdf"
    boxes = make_pdf(pdf, [[(72, 700, "Introduction")], [(72, 700, QUOTE)]])
    highlight(pdf, bundle.root / "annotated" / pdf.name, [(2, boxes[QUOTE])])
    bundle.add("models/good.md", concept(title="Good", sources=[dict(GOOD_SOURCE, page=2, bbox=boxes[QUOTE])]))
    return bundle.root


async def session_calls(root, calls):
    params = StdioServerParameters(command=sys.executable, args=["-m", "kdb_tools.mcp", str(root)])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        names = sorted(t.name for t in (await session.list_tools()).tools)
        return names, [await session.call_tool(name, args) for name, args in calls]


def text(result):
    return json.loads(result.content[0].text)


def test_okf_mcp_serves_the_four_tools_over_stdio(kdb):
    names, (hit, miss, checked, bad) = anyio.run(session_calls, kdb, [
        ("find_quote", {"document": "kim2024", "page": 2, "text": QUOTE}),
        ("find_quote", {"document": "kim2024", "page": 1, "text": QUOTE}),
        ("check_pages", {"paths": ["models/good.md"]}),
        ("find_quote", {"document": "nope", "page": 1, "text": QUOTE}),
    ])
    assert names == ["check_pages", "find_cell", "find_quote", "find_value"]
    assert text(hit)["found"] is True and not hit.is_error
    assert text(miss)["found"] is False and "page 2" in text(miss)["note"]
    assert text(checked) == {"errors": [], "warnings": []}
    assert bad.is_error and "kim2024" in bad.content[0].text
