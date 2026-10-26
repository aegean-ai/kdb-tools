"""okf-ground on lake rows: the evidence a row cites is checked against the registered bytes."""

import json

import pytest
from conftest import highlight, make_xlsx
from test_objects import QUOTE, RESULT, T1, register, setup_kdb, sha
from test_objects import pdfs  # noqa: F401,F811  the paper fixture

from kdb_tools.actions.objects import UpsertObjects
from kdb_tools.ground import ground, main
from kdb_tools.lake import object_key

IRI = T1 + "results/openvla-libero"
PATH = f"results/{IRI}"


@pytest.fixture
def ctx(bundle, tmp_path):
    c = setup_kdb(bundle, tmp_path)
    yield c
    c.lake.close()


def upsert(ctx, *refs):
    UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [{"iri": IRI, "title": "OpenVLA on LIBERO",
                                                                 "evidence": list(refs)}]})


def on_rows(findings):
    """The findings on rows. The type pages cite a stub PDF, whose findings are not this file's subject."""
    return [f for f in findings if f.path.startswith("results/")]


def found(report):
    return sorted((f.rule, f.path, f.message) for f in on_rows(report.findings))


def grounded(ctx):
    return ground(ctx.root, lake=ctx.lake, store=ctx.store)


@pytest.fixture
def cited(ctx, pdfs):
    """The paper and its highlighted copy registered; returns (sha of the paper, boxes)."""
    pdf, marked, boxes = pdfs
    register(ctx, pdf)
    register(ctx, marked, annotates=sha(pdf))
    return sha(pdf), boxes


def test_a_row_with_a_correct_quote_bbox_and_highlight_grounds_clean(ctx, cited):
    h, boxes = cited
    upsert(ctx, {"sha256": h, "page": 1, "bbox": boxes[QUOTE], "quote": QUOTE})
    report = grounded(ctx)
    assert found(report) == []
    assert report.extra["checked"] == 1


def test_a_wrong_quote_gives_ground_quote_on_the_row(ctx, cited):
    h, boxes = cited
    upsert(ctx, {"sha256": h, "page": 1, "bbox": boxes[QUOTE], "quote": "OpenVLA reaches 99 percent."})
    assert found(grounded(ctx)) == [("ground.quote", PATH, "the quote is not on page 1")]
    assert on_rows(grounded(ctx).findings)[0].source_id == "evidence[0]"


def test_a_quote_outside_its_bbox_gives_ground_bbox(ctx, cited):
    h, boxes = cited
    upsert(ctx, {"sha256": h, "page": 1, "bbox": boxes["Another sentence entirely."], "quote": QUOTE})
    rules = [f[0] for f in found(grounded(ctx))]
    assert rules == ["ground.bbox", "ground.highlight"]


def test_a_page_past_the_end_gives_ground_page(ctx, cited):
    h, boxes = cited
    upsert(ctx, {"sha256": h, "page": 9, "bbox": boxes[QUOTE], "quote": QUOTE})
    assert found(grounded(ctx)) == [("ground.page", PATH, "page 9 does not exist; the paper has 2 pages")]


def test_the_highlight_is_looked_for_in_the_media_row_that_annotates_the_paper(ctx, pdfs):
    pdf, _, boxes = pdfs
    register(ctx, pdf)
    upsert(ctx, {"sha256": sha(pdf), "page": 1, "bbox": boxes[QUOTE], "quote": QUOTE})
    assert found(grounded(ctx)) == [("ground.highlight", PATH, f"no media row annotates {sha(pdf)}")]


def test_a_sha256_that_is_not_registered_gives_ground_media(ctx, cited):
    h, boxes = cited
    upsert(ctx, {"sha256": h, "page": 1, "bbox": boxes[QUOTE], "quote": QUOTE})
    ctx.lake.con.execute("DELETE FROM kdb.media WHERE sha256 = ?", [h])  # edited outside an action
    assert found(grounded(ctx)) == [("ground.media", PATH, f"{h} is not registered in kdb.media")]


def test_bytes_that_no_longer_hash_to_their_sha256_give_ground_media(ctx, cited):
    h, boxes = cited
    upsert(ctx, {"sha256": h, "page": 1, "bbox": boxes[QUOTE], "quote": QUOTE})
    (ctx.store.root / object_key(h)).write_bytes(b"%PDF-1.4 something else")
    assert found(grounded(ctx)) == [("ground.media", PATH, f"the stored bytes of {h} no longer hash to it")]


def test_missing_bytes_give_ground_media(ctx, cited):
    h, boxes = cited
    upsert(ctx, {"sha256": h, "page": 1, "bbox": boxes[QUOTE], "quote": QUOTE})
    (ctx.store.root / object_key(h)).unlink()
    assert found(grounded(ctx)) == [("ground.media", PATH, f"the bytes of {h} are not in the object store")]


def test_a_malformed_evidence_value_is_a_finding(ctx, cited):
    h, boxes = cited
    upsert(ctx, {"sha256": h, "page": 1, "bbox": boxes[QUOTE], "quote": QUOTE})
    ctx.lake.con.execute("UPDATE results SET evidence = ? WHERE iri = ?", [json.dumps([{"sha256": "nope"}, 3]), IRI])
    report = grounded(ctx)
    assert [(f.rule, f.path, f.source_id) for f in on_rows(report.findings)] == [
        ("ground.media", PATH, "evidence[0]"), ("ground.media", PATH, "evidence[1]")]
    ctx.lake.con.execute("UPDATE results SET evidence = ? WHERE iri = ?", ['{"not": "a list"}', IRI])
    assert [(f.rule, f.path) for f in on_rows(grounded(ctx).findings)] == [("ground.media", PATH)]


def test_a_spreadsheet_cell_is_checked_from_its_bytes(ctx, tmp_path):
    book = tmp_path / "src" / "results.xlsx"
    make_xlsx(book, {"LIBERO": [["model", "success"], ["OpenVLA", 76.5]]})
    register(ctx, book)
    upsert(ctx, {"sha256": sha(book), "sheet": "LIBERO", "cell": "B2", "value": "76.5"})
    assert found(grounded(ctx)) == []
    upsert(ctx, {"sha256": sha(book), "sheet": "LIBERO", "cell": "B2", "value": "80.1"})
    assert found(grounded(ctx)) == [("ground.cell", PATH, "cell LIBERO!B2 holds '76.5', not '80.1'")]


def test_without_a_lake_rows_are_not_checked(ctx, cited):
    h, boxes = cited
    upsert(ctx, {"sha256": h, "page": 1, "bbox": boxes[QUOTE], "quote": "OpenVLA reaches 99 percent."})
    report = ground(ctx.root)
    assert found(report) == [] and report.extra["checked"] == 0


def test_pages_limits_the_rows_checked_too(ctx, cited):
    h, boxes = cited
    upsert(ctx, {"sha256": h, "page": 1, "bbox": boxes[QUOTE], "quote": "OpenVLA reaches 99 percent."})
    assert on_rows(ground(ctx.root, pages=["models/x.md"], lake=ctx.lake, store=ctx.store).findings) == []
    assert [f.rule for f in ground(ctx.root, pages=[PATH], lake=ctx.lake, store=ctx.store).findings] == ["ground.quote"]


def test_the_cli_finds_the_lake_in_kdb_toml(ctx, cited, tmp_path, capsys):
    h, boxes = cited
    upsert(ctx, {"sha256": h, "page": 1, "bbox": boxes[QUOTE], "quote": "OpenVLA reaches 99 percent."})
    ctx.lake.close()
    (tmp_path / "kdb.toml").write_text(f'[lake]\ncatalog = "{tmp_path / "meta.ducklake"}"\n'
                                       f'data_path = "{tmp_path / "data"}/"\nteam = "team-1"\n'
                                       f'iri_base = "{T1}"\n')
    assert main([str(ctx.root), "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert [(f["rule"], f["path"]) for f in out["findings"] if f["path"].startswith("results/")] == [("ground.quote", PATH)]


def test_a_highlight_in_any_annotated_copy_of_the_paper_passes(ctx, pdfs, tmp_path):
    """Two registered copies annotate the paper; only the one that sorts second by sha256 marks the quote."""
    pdf, good, boxes = pdfs
    x0, y0, x1, y1 = boxes["Another sentence entirely."]
    for k in range(50):  # vary the wrong highlight until its copy sorts first
        bad = tmp_path / "src" / f"kim2024-other-{k}.pdf"
        highlight(pdf, bad, [(1, [x0, y0, x1 + k, y1])])
        if sha(bad) < sha(good):
            break
    assert sha(bad) < sha(good)
    register(ctx, pdf)
    register(ctx, bad, annotates=sha(pdf))
    register(ctx, good, annotates=sha(pdf))
    upsert(ctx, {"sha256": sha(pdf), "page": 1, "bbox": boxes[QUOTE], "quote": QUOTE})
    assert found(grounded(ctx)) == []
