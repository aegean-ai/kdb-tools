import json

import pytest
from conftest import GOOD_CELL, GOOD_SOURCE, concept, highlight, make_pdf

from kdb_tools.ground import ground, main

QUOTE = GOOD_SOURCE["quote"]
CELL = "76.5"


@pytest.fixture
def paper(bundle):
    """A two-page paper: the quote on page 1, a table row on page 1, a heading on page 2."""
    pdf = bundle.root / "papers" / "kim2024.pdf"
    boxes = make_pdf(pdf, [
        [(72, 700, QUOTE), (72, 600, "OpenVLA"), (300, 600, CELL), (72, 500, "Another sentence entirely.")],
        [(72, 700, "Appendix")],
    ])
    return pdf, boxes


def annotate(bundle, pdf, marks):
    highlight(pdf, bundle.root / "annotated" / pdf.name, marks)


def found(report):
    return sorted({f.rule for f in report.findings})


def test_a_quote_on_its_page_inside_a_highlighted_bbox_is_grounded(bundle, paper):
    pdf, boxes = paper
    annotate(bundle, pdf, [(1, boxes[QUOTE])])
    bundle.add("models/openvla.md", concept(sources=[dict(GOOD_SOURCE, bbox=boxes[QUOTE])]))
    report = ground(bundle.root)
    assert found(report) == []
    assert report.extra["checked"] == 1


def test_a_quote_that_is_not_on_its_page_fails(bundle, paper):
    pdf, boxes = paper
    annotate(bundle, pdf, [(1, boxes[QUOTE])])
    src = dict(GOOD_SOURCE, quote="OpenVLA is trained on one million episodes.", bbox=boxes[QUOTE])
    bundle.add("models/openvla.md", concept(sources=[src]))
    assert "ground.quote" in found(ground(bundle.root))


def test_a_quote_on_the_page_but_outside_its_bbox_fails_only_the_bbox_check(bundle, paper):
    pdf, boxes = paper
    wrong = boxes["Another sentence entirely."]
    annotate(bundle, pdf, [(1, wrong)])
    bundle.add("models/openvla.md", concept(sources=[dict(GOOD_SOURCE, bbox=wrong)]))
    assert found(ground(bundle.root)) == ["ground.bbox"]


def test_a_bbox_measured_from_the_top_says_so(bundle, paper):
    pdf, boxes = paper
    x0, y0, x1, y1 = boxes[QUOTE]
    flipped = [x0, 792 - y1, x1, 792 - y0]
    annotate(bundle, pdf, [(1, flipped)])
    bundle.add("models/openvla.md", concept(sources=[dict(GOOD_SOURCE, bbox=flipped)]))
    [msg] = [f.message for f in ground(bundle.root).findings if f.rule == "ground.bbox"]
    assert "from the top" in msg
    assert str([round(v, 1) for v in boxes[QUOTE]]) in msg


def test_a_quote_in_one_column_grounds_when_page_text_interleaves_columns(bundle):
    pdf = bundle.root / "papers" / "kim2024.pdf"
    left = [(72, 700, "OpenVLA is trained on"), (72, 686, "970k robot episodes.")]
    right = [(320, 700, "Unrelated right column"), (320, 686, "text at the same height")]
    boxes = make_pdf(pdf, [left + right])
    bbox = [72, boxes["970k robot episodes."][1], 300, boxes["OpenVLA is trained on"][3]]
    annotate(bundle, pdf, [(1, bbox)])
    bundle.add("models/openvla.md", concept(sources=[dict(GOOD_SOURCE, bbox=bbox)]))
    assert found(ground(bundle.root)) == []


def test_a_bbox_elsewhere_on_the_page_is_told_where_the_quote_is(bundle, paper):
    pdf, boxes = paper
    wrong = boxes["Another sentence entirely."]
    annotate(bundle, pdf, [(1, wrong)])
    bundle.add("models/openvla.md", concept(sources=[dict(GOOD_SOURCE, bbox=wrong)]))
    [msg] = [f.message for f in ground(bundle.root).findings if f.rule == "ground.bbox"]
    assert "the quote is at [72.0, " in msg


def test_a_page_beyond_the_end_of_the_paper_fails(bundle, paper):
    pdf, boxes = paper
    bundle.add("models/openvla.md", concept(sources=[dict(GOOD_SOURCE, page=9, bbox=boxes[QUOTE])]))
    assert found(ground(bundle.root)) == ["ground.page"]


def test_a_table_cell_holds_its_value_inside_the_bbox(bundle, paper):
    pdf, boxes = paper
    annotate(bundle, pdf, [(1, boxes[CELL])])
    body = "OpenVLA scores 76.5%.[^kim2024-t1-r2-c3]\n"
    bundle.add("models/openvla.md", concept(sources=[dict(GOOD_CELL, bbox=boxes[CELL])]), body)
    assert found(ground(bundle.root)) == []
    bundle.add("models/openvla.md", concept(sources=[dict(GOOD_CELL, value="79.2", bbox=boxes[CELL])]), body)
    assert found(ground(bundle.root)) == ["ground.bbox"]


def test_a_missing_annotated_copy_fails_the_highlight_check(bundle, paper):
    pdf, boxes = paper
    bundle.add("models/openvla.md", concept(sources=[dict(GOOD_SOURCE, bbox=boxes[QUOTE])]))
    assert found(ground(bundle.root)) == ["ground.highlight"]


def test_a_highlight_elsewhere_on_the_page_does_not_count(bundle, paper):
    pdf, boxes = paper
    annotate(bundle, pdf, [(1, boxes["Another sentence entirely."])])
    bundle.add("models/openvla.md", concept(sources=[dict(GOOD_SOURCE, bbox=boxes[QUOTE])]))
    assert found(ground(bundle.root)) == ["ground.highlight"]


def test_a_highlight_on_another_page_does_not_count(bundle, paper):
    pdf, boxes = paper
    annotate(bundle, pdf, [(2, boxes[QUOTE])])
    bundle.add("models/openvla.md", concept(sources=[dict(GOOD_SOURCE, bbox=boxes[QUOTE])]))
    assert found(ground(bundle.root)) == ["ground.highlight"]


def test_a_highlight_that_roughly_covers_the_bbox_counts(bundle, paper):
    pdf, boxes = paper
    x0, y0, x1, y1 = boxes[QUOTE]
    annotate(bundle, pdf, [(1, [x0 - 2, y0 - 1, x1 + 3, y1 + 1])])
    bundle.add("models/openvla.md", concept(sources=[dict(GOOD_SOURCE, bbox=boxes[QUOTE])]))
    assert found(ground(bundle.root)) == []


def test_sources_that_are_not_pdfs_in_the_bundle_are_skipped_with_a_warning(bundle, paper):
    src = dict(GOOD_SOURCE, resource="https://arxiv.org/abs/2406.09246")
    bundle.add("models/openvla.md", concept(sources=[src]))
    report = ground(bundle.root)
    assert [(f.rule, f.level) for f in report.findings] == [("ground.resource", "warning")]


def test_main_prints_json_and_exits_1_when_grounding_fails(bundle, paper, capsys):
    pdf, boxes = paper
    bundle.add("models/openvla.md", concept(sources=[dict(GOOD_SOURCE, bbox=boxes[QUOTE])]))
    assert main([str(bundle.root), "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["tool"] == "okf-ground" and out["checked"] == 1
    assert out["findings"][0]["source_id"] == GOOD_SOURCE["id"]
