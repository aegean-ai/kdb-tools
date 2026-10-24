import pdfplumber
from conftest import GOOD_SOURCE, concept, make_pdf
from pypdf import PdfReader

from kdb_tools.annotate import highlight
from kdb_tools.ground import ground

QUOTE = "OpenVLA is trained on 970k robot episodes."


def test_highlight_writes_one_quad_per_line_and_a_comment(tmp_path):
    src = tmp_path / "p.pdf"
    make_pdf(src, [[(72, 700, "Some words first. OpenVLA is trained"), (72, 686, "on 970k robot episodes. More words.")]])
    dst = tmp_path / "annotated" / "p.pdf"
    found = highlight(src, dst, [(1, QUOTE, "kim2024-p3-s2")])
    assert found == [True]
    [annot] = [a.get_object() for a in PdfReader(dst).pages[0]["/Annots"]]
    assert annot["/Subtype"] == "/Highlight"
    assert len(annot["/QuadPoints"]) == 16  # two lines, eight numbers each
    assert annot["/Contents"] == "kim2024-p3-s2"


def test_text_that_is_not_on_the_page_is_reported_and_not_highlighted(tmp_path):
    src = tmp_path / "p.pdf"
    make_pdf(src, [[(72, 700, QUOTE)]])
    dst = tmp_path / "annotated" / "p.pdf"
    assert highlight(src, dst, [(1, "Not in this paper.", "x")]) == [False]
    assert "/Annots" not in PdfReader(dst).pages[0]


def test_a_highlight_written_this_way_passes_okf_ground(bundle):
    pdf = bundle.root / "papers" / "kim2024.pdf"
    make_pdf(pdf, [[(72, 700, "Some words first. OpenVLA is trained"), (72, 686, "on 970k robot episodes. More words.")]])
    from kdb_tools.locate import find_quote
    with pdfplumber.open(pdf) as doc:
        [bbox] = find_quote(doc.pages[0], QUOTE)
    highlight(pdf, bundle.root / "annotated" / "kim2024.pdf", [(1, QUOTE, GOOD_SOURCE["id"])])
    bundle.add("models/openvla.md", concept(sources=[dict(GOOD_SOURCE, bbox=bbox)]))
    assert [f.rule for f in ground(bundle.root).findings] == []


def test_the_occurrence_inside_a_given_bbox_is_the_one_highlighted(tmp_path):
    src = tmp_path / "p.pdf"
    boxes = make_pdf(src, [[(72, 700, "Score 76.5 here."), (72, 400, "And 76.5 again.")]])
    dst = tmp_path / "annotated" / "p.pdf"
    lower = boxes["And 76.5 again."]
    assert highlight(src, dst, [(1, "76.5", "cell", lower)]) == [True]
    [annot] = [a.get_object() for a in PdfReader(dst).pages[0]["/Annots"]]
    assert float(annot["/Rect"][1]) < 500
