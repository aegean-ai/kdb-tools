"""AdoptBundle: bring an existing bundle into the action model, once, at cutover.

It logs every current page as an edit from nothing to its present text and writes nothing new, so
okf-validate then finds every page matched by a logged action. The files stay byte-identical.
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from .. import bundle as B
from .base import ActionType, Failed, PageEdit, register


class NoParams(BaseModel):
    pass


@register
class AdoptBundle(ActionType[NoParams]):
    name = "AdoptBundle"
    version = 1
    params = NoParams
    adopts_existing = True

    def preconditions(self, ctx, p):
        rows = ctx.lake.con.execute("SELECT page_edits FROM kdb.action_log").fetchall()
        if any(json.loads(e) if isinstance(e, str) else e for (e,) in rows):
            return ["the bundle is already adopted: the action log holds page edits. Adopt once, at cutover"]
        return []

    def edits(self, ctx, p):
        pages = []
        for path in sorted(ctx.root.rglob("*.md")):
            rel = path.relative_to(ctx.root)
            if any(part in B.SKIP_DIRS for part in rel.parts[:-1]):
                continue
            try:
                text = ctx.read(rel.as_posix())
            except UnicodeDecodeError:
                raise Failed([f"{rel.as_posix()} is not valid UTF-8, so it cannot be adopted"])
            pages.append(PageEdit(rel.as_posix(), None, text))
        return pages, [], []
