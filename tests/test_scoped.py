"""validate and ground can check only the pages an ingest wrote."""

from conftest import GOOD_SOURCE, concept, highlight, make_pdf

from kdb_tools import ground as G
from kdb_tools import validate as V

QUOTE = GOOD_SOURCE["quote"]


def bad_and_good(bundle):
    bundle.add("models/good.md", concept(title="Good"))
    bundle.add("models/bad.md", concept(title="Bad", generated={"by": "x"}))


def test_validate_reports_only_findings_on_the_given_pages(bundle):
    bad_and_good(bundle)
    assert [f.path for f in V.validate(bundle.root, profiles=[]).findings] == ["models/bad.md"]
    assert V.validate(bundle.root, profiles=[], pages=["models/good.md"]).findings == []
    assert [f.path for f in V.validate(bundle.root, profiles=[], pages=["models/bad.md"]).findings] == ["models/bad.md"]


def test_validate_main_takes_pages(bundle, capsys):
    bad_and_good(bundle)
    assert V.main([str(bundle.root), "--pages", "models/good.md"]) == 0
    assert V.main([str(bundle.root), "--pages", "models/good.md", "models/bad.md"]) == 1


def test_ground_checks_only_the_sources_on_the_given_pages(bundle):
    pdf = bundle.root / "papers" / "kim2024.pdf"
    boxes = make_pdf(pdf, [[(72, 700, QUOTE)]])
    highlight(pdf, bundle.root / "annotated" / pdf.name, [(1, boxes[QUOTE])])
    bundle.add("models/good.md", concept(title="Good", sources=[dict(GOOD_SOURCE, bbox=boxes[QUOTE])]))
    bundle.add("models/bad.md", concept(title="Bad", sources=[dict(GOOD_SOURCE, quote="Not in the paper at all.")]))
    full = G.ground(bundle.root)
    assert {f.path for f in full.findings} == {"models/bad.md"} and full.extra["checked"] == 2
    scoped = G.ground(bundle.root, pages=["models/good.md"])
    assert scoped.findings == [] and scoped.extra["checked"] == 1


def test_ground_main_takes_pages(bundle):
    bundle.add("models/bad.md", concept(title="Bad", sources=[dict(GOOD_SOURCE, quote="Not in the paper at all.")]))
    assert G.main([str(bundle.root), "--pages", "models/other.md"]) == 0
    assert G.main([str(bundle.root), "--pages", "models/bad.md"]) == 1
