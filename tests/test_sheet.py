"""Spreadsheet cell sources: a value cited by file, sheet and cell, grounded by reading that cell."""

import pytest
from conftest import concept, make_pdf, make_xlsx

from kdb_tools.ground import ground
from kdb_tools.tools import KdbTools, ToolInputError
from kdb_tools.validate import validate

CELL = {"id": "annin-dh-b4", "resource": "/documents/ar4-dh.xlsx", "sheet": "DH", "cell": "B4", "value": "169.77"}
BODY = "Link 2 is 169.77 mm long.[^annin-dh-b4]\n\n[^annin-dh-b4]: Annin, AR4 DH parameters, sheet DH, cell B4\n"


def errors(report):
    return sorted({f.rule for f in report.findings if f.level == "error"})


@pytest.fixture
def sheet(bundle):
    make_xlsx(bundle.root / "documents" / "ar4-dh.xlsx", {
        "DH": [["joint", "a (mm)", "d (mm)"], ["J1", 64.2, 169.77], ["J2", 305, 0], ["J3", 0, 169.77]],
        "Motors": [["joint", "gear ratio"], ["J1", "=10*4"]],  # a formula with no saved value
    })
    return bundle


def add(bundle, **cell):
    bundle.add("parts/link-2.md", concept(type="Link", title="Link 2", sources=[dict(CELL, **cell)]), BODY)


def test_a_spreadsheet_cell_source_is_valid(sheet):
    add(sheet)
    assert errors(validate(sheet.root)) == []


def test_an_xlsx_cell_source_needs_its_sheet(sheet):
    cell = dict(CELL)
    del cell["sheet"]
    sheet.add("parts/link-2.md", concept(type="Link", title="Link 2", sources=[cell]), BODY)
    assert errors(validate(sheet.root)) == ["project.source"]


@pytest.mark.parametrize("ref", ["B0", "4B", "b4", "B4:C5", 4])
def test_a_cell_is_an_a1_reference(sheet, ref):
    add(sheet, cell=ref)
    assert errors(validate(sheet.root)) == ["project.source"]


def test_a_cell_source_must_cite_a_spreadsheet(sheet):
    make_pdf(sheet.root / "documents" / "manual.pdf", [[(72, 700, "Manual")]])
    add(sheet, resource="/documents/manual.pdf")
    assert errors(validate(sheet.root)) == ["project.source"]


def test_a_cell_holding_the_value_is_grounded_without_a_highlight(sheet):
    add(sheet, cell="C2")
    report = ground(sheet.root)
    assert errors(report) == [] and report.extra["checked"] == 1


@pytest.mark.parametrize("value", ["169.77", "169.770", 169.77])
def test_numbers_match_by_value_not_by_spelling(sheet, value):
    add(sheet, cell="C2", value=value)
    assert errors(ground(sheet.root)) == []


def test_a_wrong_value_names_what_the_cell_holds(sheet):
    add(sheet, cell="B2")
    report = ground(sheet.root)
    assert errors(report) == ["ground.cell"]
    assert "64.2" in report.findings[0].message


def test_a_missing_sheet_lists_the_sheets_there_are(sheet):
    add(sheet, sheet="Joints")
    report = ground(sheet.root)
    assert errors(report) == ["ground.sheet"]
    assert "DH" in report.findings[0].message and "Motors" in report.findings[0].message


def test_a_formula_with_no_saved_value_says_so(sheet):
    add(sheet, sheet="Motors", cell="B2", value="40")
    report = ground(sheet.root)
    assert errors(report) == ["ground.cell"] and "formula" in report.findings[0].message


def test_a_csv_cell_needs_no_sheet(bundle):
    path = bundle.root / "documents" / "motors.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("joint,rated torque (Nm)\nJ1,1.26\n")
    cell = {"id": "annin-motors-b2", "resource": "/documents/motors.csv", "cell": "B2", "value": "1.26"}
    bundle.add("parts/j1.md", concept(type="Motor", title="J1 motor", sources=[cell]),
               "J1 is rated at 1.26 Nm.[^annin-motors-b2]\n\n[^annin-motors-b2]: Annin motors, cell B2\n")
    assert errors(validate(bundle.root)) == []
    assert errors(ground(bundle.root)) == []


def test_find_cell_gives_every_cell_holding_a_value(sheet):
    hit = KdbTools(sheet.root, profiles=[]).find_cell("ar4-dh", "169.77")
    assert hit.found and hit.cells == ["DH!C2", "DH!C4"]
    assert KdbTools(sheet.root, profiles=[]).find_cell("ar4-dh", "169.77", sheet="DH").cells == ["DH!C2", "DH!C4"]
    assert not KdbTools(sheet.root, profiles=[]).find_cell("ar4-dh", "1.5").found


def test_find_cell_sends_back_an_unknown_spreadsheet(sheet):
    with pytest.raises(ToolInputError, match="ar4-dh"):
        KdbTools(sheet.root, profiles=[]).find_cell("dh", "169.77")


def test_documents_can_live_under_documents_as_well_as_papers(bundle):
    boxes = make_pdf(bundle.root / "documents" / "ar4-manual.pdf", [[(72, 700, "The J1 axis turns 340 degrees.")]])
    hit = KdbTools(bundle.root, profiles=[]).find_quote("ar4-manual", 1, "The J1 axis turns 340 degrees.")
    assert hit.found and hit.boxes == [pytest.approx(boxes["The J1 axis turns 340 degrees."], abs=1)]
