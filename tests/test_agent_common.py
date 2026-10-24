"""The common agent: model config, the prompt guard, and extraction and ask agents that send back bad evidence.

A scripted model stands in for the server, so these run offline.
"""

import json

import pytest
from conftest import make_pdf, make_xlsx

pytest.importorskip("pydantic_ai")
from pydantic import BaseModel  # noqa: E402
from pydantic_ai.messages import ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402

from kdb_tools import agent as A  # noqa: E402
from kdb_tools import config, guard  # noqa: E402

QUOTE = "The J1 axis turns 340 degrees."


def scripted(*outputs):
    """A model that returns each output in turn as its final answer, and records the retry prompts it saw."""
    seen, calls = [], [0]

    def respond(messages, info: AgentInfo) -> ModelResponse:
        # a retry comes back as a RetryPromptPart, or for an output tool in later releases as a ToolReturnPart
        seen.extend(p.content for m in messages for p in m.parts if isinstance(p, (RetryPromptPart, ToolReturnPart)))
        out = outputs[min(calls[0], len(outputs) - 1)]
        calls[0] += 1
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, out)])

    return FunctionModel(respond), seen


class Spec(A.Quote):
    joint: str
    limit_deg: float


class Ratio(A.CellValue):
    joint: str


class Specs(BaseModel):
    specs: list[Spec] = []
    ratios: list[Ratio] = []


def test_config_names_any_openai_compatible_server(tmp_path):
    (tmp_path / "kdb.toml").write_text('base_url = "http://localhost:8000/v1"\nmodel = "qwen3-32b"\ncontext = 32768\n'
                                       'reasoning_effort = "none"\n')
    cfg = config.load(tmp_path / "kdb.toml")
    assert (cfg.base_url, cfg.model, cfg.context) == ("http://localhost:8000/v1", "qwen3-32b", 32768)
    assert config.model_from(cfg).model_name == "qwen3-32b"
    assert config.settings_from(cfg) == {"openai_reasoning_effort": "none"}


def test_config_refuses_a_key_written_in_the_file_and_reads_one_from_the_environment(tmp_path, monkeypatch):
    path = tmp_path / "kdb.toml"
    path.write_text('base_url = "x"\nmodel = "m"\napi_key = "sk-secret"\n')
    with pytest.raises(config.ConfigError, match="api_key_env"):
        config.load(path)
    path.write_text('base_url = "x"\nmodel = "m"\napi_key_env = "MY_KEY"\n')
    with pytest.raises(config.ConfigError, match="MY_KEY"):
        config.load(path)
    monkeypatch.setenv("MY_KEY", "sk-from-env")
    assert config.load(path).api_key == "sk-from-env"


def test_the_guard_refuses_a_prompt_the_server_would_cut():
    guard.check("short", context=32768)
    with pytest.raises(guard.PromptTooLong):
        guard.check("word " * 100_000, context=32768)


@pytest.fixture
def manual(tmp_path):
    path = tmp_path / "documents" / "ar4-manual.pdf"
    make_pdf(path, [[(72, 700, "Introduction")], [(72, 700, QUOTE)]])
    return path


def test_pdf_chunks_cover_every_page_with_page_markers(manual):
    chunks = list(A.pdf_chunks(manual, pages_per_chunk=1))
    assert [c.pages for c, _ in chunks] == [[1], [2]]
    assert "=== page 2 ===" in chunks[1][1] and QUOTE in chunks[1][1]


def test_the_extraction_agent_sends_back_a_quote_that_is_not_on_its_pages(manual):
    good = {"joint": "J1", "limit_deg": 340, "quote": QUOTE, "page": 2}
    bad = dict(good, quote="The J1 axis turns 360 degrees.")
    wrong_page = dict(good, page=1)
    model, seen = scripted({"specs": [good, bad, wrong_page]}, {"specs": [good]})
    chunk, prompt = list(A.pdf_chunks(manual, pages_per_chunk=2))[0]
    out = A.extraction_agent(model, Specs, "Extract joint limits.").run_sync(prompt, deps=chunk).output
    assert [s.quote for s in out.specs] == [QUOTE]
    assert any("360 degrees" in str(s) for s in seen) and any("page 1" in str(s) for s in seen)


@pytest.fixture
def dh(tmp_path):
    path = tmp_path / "documents" / "ar4-dh.xlsx"
    make_xlsx(path, {"Gears": [["joint", "ratio"], ["J1", 40], ["J2", 50]], "Notes": [["x"]]})
    return path


def test_sheet_chunks_show_each_sheet_with_cell_references(dh):
    chunks = list(A.sheet_chunks(dh))
    assert [c.sheet for c, _ in chunks] == ["Gears", "Notes"]
    assert "B2: 40" in chunks[0][1] and "A3: J2" in chunks[0][1]


def test_the_extraction_agent_sends_back_a_cell_that_does_not_hold_its_value(dh):
    good = {"joint": "J1", "value": "40", "cell": "B2", "sheet": "Gears"}
    bad = dict(good, joint="J2", cell="B3", value="40")
    model, seen = scripted({"ratios": [good, bad]}, {"ratios": [good]})
    chunk, prompt = list(A.sheet_chunks(dh))[0]
    out = A.extraction_agent(model, Specs, "Extract gear ratios.").run_sync(prompt, deps=chunk).output
    assert [r.cell for r in out.ratios] == ["B2"]
    assert any("B3" in str(s) and "50" in str(s) for s in seen)


def test_the_ask_agent_sends_back_a_source_id_that_is_not_on_the_page(tmp_path):
    root = tmp_path / "kdb"
    (root / "joints").mkdir(parents=True)
    (root / "joints" / "j1.md").write_text(
        "---\ntype: Joint\ntitle: J1\nsources:\n- id: manual-p2-1\n  resource: /documents/ar4-manual.pdf\n  page: 2\n"
        f"  quote: {json.dumps(QUOTE)}\n---\n\nJ1 turns 340 degrees.[^manual-p2-1]\n")
    claim = {"text": "J1 turns 340 degrees.", "page": "joints/j1.md", "source_ids": ["manual-p2-1"]}
    model, seen = scripted({"answerable": True, "claims": [dict(claim, source_ids=["made-up"])]},
                           {"answerable": True, "claims": [claim]})
    out = A.ask(A.ask_agent(model), root, "How far does J1 turn?")
    assert out.claims[0].source_ids == ["manual-p2-1"]
    assert any("made-up" in str(s) for s in seen)
