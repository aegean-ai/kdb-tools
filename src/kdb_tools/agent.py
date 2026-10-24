"""The kDB tools as a Pydantic AI toolset. Needs the agent extra: kdb-tools[agent].

    from kdb_tools.agent import toolset
    agent = Agent(model, toolsets=[toolset(Path("kdb"))])
"""

from __future__ import annotations

import functools
from pathlib import Path

from pydantic_ai import ModelRetry, Tool
from pydantic_ai.toolsets import FunctionToolset

from .tools import Checked, KdbTools, Match, ToolInputError


def toolset(root: Path, profiles=None) -> FunctionToolset:
    kt = KdbTools(root, profiles)

    def retry_on_bad_input(fn):
        @functools.wraps(fn)  # keeps the name, signature and docstring the model sees
        def wrapped(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ToolInputError as e:
                raise ModelRetry(str(e)) from e
        return wrapped

    @retry_on_bad_input
    def find_quote(paper: str, page: int, text: str) -> Match:
        """Find a sentence on a page of a paper. paper is its arXiv id, such as 2406.09246, and page counts
        from 1. Returns the bounding boxes where the sentence is, or a note saying which page it is on."""
        return kt.find_quote(paper, page, text)

    @retry_on_bad_input
    def find_value(paper: str, page: int, value: str) -> Match:
        """Find a table value, such as 76.5, on a page of a paper. Returns its bounding boxes, or a note."""
        return kt.find_value(paper, page, value)

    def check_pages(paths: list[str]) -> Checked:
        """Run okf-validate and okf-ground on kDB pages, such as models/openvla.md, and return their errors
        and warnings. Use it after writing pages to see what to fix."""
        return kt.check_pages(paths)

    return FunctionToolset([Tool(f, takes_ctx=False) for f in (find_quote, find_value, check_pages)])
