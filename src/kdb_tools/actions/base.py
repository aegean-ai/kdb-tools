"""The action machinery: every write to a kDB is a typed action, applied as one unit and logged.

An action type validates its parameters, checks its preconditions and computes an edit set: page
files to write or delete and lake rows to insert, update or delete by IRI. `dry_run` returns that
set as a Plan and changes nothing. `submit` checks that nothing the plan read has changed since
(the lake snapshot and the hash of every page it edits), then runs the schema statements, applies
the row edits, writes the action_log row and replaces the pages inside one lake transaction. If
anything fails after a page is replaced, the pages are put back and the transaction rolls back.
after_commit runs once the transaction has committed, for writes that must not happen unless the
action succeeds, such as media bytes in the object store (design 6.1).
okf-validate trusts a page only when its sha256 matches the after_sha256 a logged action recorded
for it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import ClassVar, Generic, Literal, TypeVar, final

from ..lake import Lake, Store

P = TypeVar("P")


@dataclass
class Context:
    """Who is acting on which kDB. model is None for a person, else the agent's model id."""

    root: Path
    lake: Lake
    store: Store
    actor: str
    model: str | None
    team: str
    iri_base: str
    now: datetime | None = None  # the action's time in UTC; dry_run and submit set it, edits read it

    def path(self, rel: str) -> Path:
        """The file a bundle-relative path names, refusing one that leaves the bundle."""
        target = self.root / _checked(rel)
        if not target.resolve().is_relative_to(self.root.resolve()):  # a symlink that points out
            raise Failed([f"{rel!r} resolves to a file outside the bundle"])
        return target

    def read(self, rel: str) -> str | None:
        """A page's current text, or None when it does not exist."""
        data = _read_bytes(self.path(rel))
        try:
            return None if data is None else data.decode("utf-8")
        except UnicodeDecodeError:
            raise Failed([f"{rel} is not UTF-8 text; save it as UTF-8"])


@dataclass
class PageEdit:
    path: str  # bundle-relative, forward slashes
    before: str | None  # None: the page does not exist yet
    after: str | None  # None: the edit deletes the page


@dataclass
class RowEdit:
    table: str
    iri: str
    op: Literal["insert", "update", "delete"]
    before: dict | None
    after: dict | None


@dataclass
class Plan:
    action_id: str
    page_edits: list[PageEdit]
    row_edits: list[RowEdit]
    evidence: list[str]
    snapshot: int
    page_hashes: dict[str, str | None] = field(default_factory=dict)  # None: the page was absent
    now: datetime = field(default_factory=lambda: _clock())  # the time edits saw; submit reuses it
    statements: list[str] = field(default_factory=list)  # schema SQL run before the row edits (BindTable)
    reverts: str | None = None  # the action id this action reverts (RevertAction)


class Failed(Exception):
    def __init__(self, messages: list[str]):
        super().__init__("\n".join(messages))
        self.messages = messages


class Conflict(Failed):
    """Something the plan read changed before it was submitted."""


REGISTRY: dict[str, type[ActionType]] = {}


def register(cls: type[ActionType]) -> type[ActionType]:
    other = REGISTRY.get(cls.name)
    if other is not None and other.__qualname__ != cls.__qualname__:
        raise ValueError(f"two action types are named {cls.name}: {other.__qualname__} and {cls.__qualname__}")
    REGISTRY[cls.name] = cls
    return cls


class ActionType(Generic[P]):
    """Subclass with name, version, params (a Pydantic model), preconditions and edits."""

    name: ClassVar[str]
    version: ClassVar[int]
    params: ClassVar[type]
    adopts_existing: ClassVar[bool] = False  # only AdoptBundle: its edits start from None on pages that exist
    logs_no_op: ClassVar[bool] = True  # False: a submit that changes nothing writes no log row (instance actions)

    def preconditions(self, ctx: Context, p: P) -> list[str]:
        """Failure messages; an empty list means the action may run."""
        return []

    def edits(self, ctx: Context, p: P) -> tuple[list[PageEdit], list[RowEdit], list[str]]:
        """The page edits, the row edits and the evidence cited.

        It must be a pure function of the params, ctx.now and the bundle and lake state: read the
        time only from ctx.now, never from a clock. submit recomputes the edits with the plan's time
        and refuses with a Conflict when they differ from the plan's.
        """
        raise NotImplementedError

    def statements(self, ctx: Context, p: P) -> list[str]:
        """SQL that changes the lake schema (CREATE or ALTER TABLE, and the UPDATE a cast needs).

        submit runs it inside the action's transaction, before the row edits. It must be pure like
        edits(). The log row does not hold it: the params say what was asked. Default: none."""
        return []

    def reverts(self, ctx: Context, p: P) -> str | None:
        """The id of the logged action this one undoes, or None. submit writes it to the log row's
        reverts and sets that row's reverted_by in the same transaction."""
        return None

    def after_commit(self, ctx: Context, p: P) -> None:
        """Work done after the action's transaction commits, outside it. It must be safe to repeat:
        when it fails, the action stays committed, and submitting again finishes the work. It also
        runs when a submit changes nothing and logs_no_op is False. Default: nothing."""

    @final
    def dry_run(self, ctx: Context, raw: dict) -> Plan:
        p = self._validate(raw)
        ctx.now = _clock()
        return self._plan(ctx, p)

    @final
    def submit(self, ctx: Context, raw: dict, plan: Plan | None = None) -> str | None:
        """Apply the action and return its logged id. An action with logs_no_op False that has nothing
        to change logs nothing and returns None; a stale plan still gets the Conflict."""
        p = self._validate(raw)
        if plan is not None and plan.action_id.rsplit("-", 1)[-1] != _digest(_params_json(p)):
            raise Failed([f"the plan {plan.action_id} was made for other parameters"])
        ctx.now = plan.now if plan is not None else _clock()
        fresh = self._plan(ctx, p)
        supplied, plan = plan, plan or fresh
        if not self.logs_no_op and _empty(plan) and _empty(fresh) and \
                (supplied is None or ctx.lake.snapshot() == supplied.snapshot):
            self.after_commit(ctx, p)  # nothing to change or log; finish any work a failed run left
            return None
        touched: list[tuple[PageEdit, int | None]] = []
        try:
            with ctx.lake.transaction():
                _check(ctx, plan, fresh)
                for sql in plan.statements:
                    ctx.lake.con.execute(sql)
                _apply_rows(ctx.lake, plan.row_edits)
                _log(ctx, self, plan, _params_json(p))
                _write_pages(ctx, plan.page_edits, touched)  # last: the transaction commits right after
        except BaseException:
            _restore(ctx, touched)  # the transaction has rolled back; put the pages back too
            raise
        self.after_commit(ctx, p)
        return plan.action_id

    def _validate(self, raw: dict) -> P:
        try:
            return self.params.model_validate(raw)
        except ValueError as e:  # pydantic's ValidationError is a ValueError
            errors = e.errors() if hasattr(e, "errors") else [{"loc": (), "msg": str(e)}]
            raise Failed([f"{self.name}: {'.'.join(map(str, x['loc'])) or 'params'}: {x['msg']}" for x in errors])

    def _plan(self, ctx: Context, p: P) -> Plan:
        snapshot = ctx.lake.snapshot()  # first, so a change while the edits are computed shows up later
        if messages := self.preconditions(ctx, p):
            raise Failed(messages)
        pages, rows, evidence = self.edits(ctx, p)
        for e in pages:
            ctx.path(e.path)
        if len({e.path for e in pages}) != len(pages):
            raise Failed([f"{self.name}: edits the same page twice"])
        for r in rows:
            _ident(r.table)
            if r.op not in ("insert", "update", "delete"):
                raise Failed([f"{self.name}: row edit of {r.iri} has unknown op {r.op!r}"])
        pages = [e for e in pages if e.before != e.after]  # an unchanged page is not an edit
        disk = {e.path: _file_sha(ctx.path(e.path)) for e in pages}
        if not self.adopts_existing:
            wrong = [e.path for e in pages if disk[e.path] != _sha(e.before)]
            if wrong:
                raise Conflict([f"{self.name}: the before text of {', '.join(wrong)} is not what is on disk"])
        hashes = disk if self.adopts_existing else {e.path: _sha(e.before) for e in pages}
        return Plan(_new_id(ctx.now, _params_json(p)), pages, rows, evidence, snapshot, hashes, ctx.now,
                    self.statements(ctx, p), self.reverts(ctx, p))


def _empty(plan: Plan) -> bool:
    return not (plan.page_edits or plan.row_edits or plan.statements or plan.reverts)


def _checked(rel: str) -> PurePosixPath:
    path = PurePosixPath(rel)
    if not rel or "\\" in rel or path.is_absolute() or ".." in path.parts:
        raise Failed([f"{rel!r} is not a path inside the bundle; use a relative path with forward slashes"])
    return path


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _sha(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha(path: Path) -> str | None:
    data = _read_bytes(path)
    return None if data is None else hashlib.sha256(data).hexdigest()


def _params_json(p) -> str:
    return json.dumps(p.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _digest(params: str) -> str:
    return hashlib.sha256(params.encode("utf-8")).hexdigest()[:8]


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(now: datetime, params: str) -> str:
    """<UTC timestamp to microseconds>-<8 hex chars of sha256(params)>."""
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + _digest(params)


def _check(ctx: Context, plan: Plan, fresh: Plan) -> None:
    """The optimistic check: the lake and every edited page are as the plan saw them."""
    messages = []
    now = ctx.lake.snapshot()
    if now != plan.snapshot:
        messages.append(f"the lake moved from snapshot {plan.snapshot} to {now} since the plan was made")
    for path, expected in plan.page_hashes.items():
        if _file_sha(ctx.path(path)) != expected:
            messages.append(f"{path} changed since the plan was made")
    if not messages and (fresh.page_edits, fresh.row_edits, fresh.statements, fresh.reverts) != \
            (plan.page_edits, plan.row_edits, plan.statements, plan.reverts):
        messages.append("the edits computed now differ from the plan's")
    if messages:
        raise Conflict(messages + ["make a new plan with a dry run and submit that"])


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _ident(name: str) -> str:
    """A table or column name, quoted. Names cannot be bound as parameters, so they are checked."""
    parts = name.split(".")
    if not 1 <= len(parts) <= 2 or not all(_IDENT.fullmatch(x) for x in parts):
        raise Failed([f"{name!r} is not a valid table or column name"])
    return ".".join(f'"{x}"' for x in parts)


KEYS = {"kdb.media": "sha256"}  # a table keyed by something other than iri; RowEdit.iri holds that key


def _apply_rows(lake: Lake, rows: list[RowEdit]) -> None:
    for r in rows:
        table, key = _ident(r.table), KEYS.get(r.table, "iri")
        if r.op == "insert":
            values = {key: r.iri, **(r.after or {})}
            cols = ", ".join(_ident(c) for c in values)
            lake.con.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(values))})",
                             list(values.values()))
            continue
        if r.op == "update":
            values = {c: v for c, v in (r.after or {}).items() if c != key}
            if not values:
                raise Failed([f"update of {r.iri} in {r.table} sets no column"])
            sets = ", ".join(f"{_ident(c)} = ?" for c in values)
            n = lake.con.execute(f"UPDATE {table} SET {sets} WHERE {_ident(key)} = ?",
                                 [*values.values(), r.iri]).fetchone()[0]
        else:
            n = lake.con.execute(f"DELETE FROM {table} WHERE {_ident(key)} = ?", [r.iri]).fetchone()[0]
        if n != 1:
            raise Failed([f"{r.op} of {r.iri} in {r.table} matched {n} rows, not 1"])


def _log(ctx: Context, action: ActionType, plan: Plan, params: str) -> None:
    if ctx.lake.con.execute("SELECT count(*) FROM kdb.action_log WHERE action_id = ?", [plan.action_id]).fetchone()[0]:
        raise Conflict([f"the action {plan.action_id} is already logged"])
    pages = [{"path": e.path, "before_sha256": _sha(e.before), "after_sha256": _sha(e.after), "before_text": e.before}
             for e in plan.page_edits]
    rows = [{"table": r.table, "iri": r.iri, "op": r.op, "before": r.before, "after": r.after} for r in plan.row_edits]
    ctx.lake.con.execute(
        'INSERT INTO kdb.action_log (action_id, action_type, action_version, "at", actor, model, team, params, '
        "page_edits, row_edits, evidence, reverts, reverted_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        [plan.action_id, action.name, action.version, _clock(), ctx.actor, ctx.model, ctx.team,
         params, _json(pages), _json(rows), _json(plan.evidence), plan.reverts])
    if plan.reverts is not None:
        n = ctx.lake.con.execute("UPDATE kdb.action_log SET reverted_by = ? WHERE action_id = ? AND reverted_by IS NULL",
                                 [plan.action_id, plan.reverts]).fetchone()[0]
        if n != 1:
            raise Conflict([f"the action {plan.reverts} is not logged or is already reverted"])


def _json(value) -> str:
    return json.dumps(value, default=_tagged)


def _tagged(value):
    """JSON has no date or time, so a row value of either is logged as {"$date": ...} or
    {"$timestamp": ...}; load_row_edits turns it back. Row values are never other objects: JSON
    columns hold their text."""
    if isinstance(value, datetime):
        return {"$timestamp": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    return str(value)


def _untagged(obj: dict):
    if len(obj) == 1 and "$timestamp" in obj and isinstance(obj["$timestamp"], str):
        return datetime.fromisoformat(obj["$timestamp"])
    if len(obj) == 1 and "$date" in obj and isinstance(obj["$date"], str):
        return date.fromisoformat(obj["$date"])
    return obj


def load_row_edits(value) -> list[dict]:
    """A log row's row_edits with each value back in the Python type it was logged with."""
    if not isinstance(value, str):
        return value or []
    return json.loads(value, object_hook=_untagged)


def _write_pages(ctx: Context, pages: list[PageEdit], touched: list[tuple[PageEdit, int | None]]) -> None:
    """Replace each page atomically, noting it and its mode in touched first so a failure can put it back."""
    for e in pages:
        target = ctx.path(e.path)
        if e.after is not None and _read_bytes(target) == e.after.encode("utf-8"):
            continue  # already as written (an adoption): leave the file and its mode alone
        touched.append((e, _file_mode(target)))
        if e.after is None:
            target.unlink(missing_ok=True)
        else:
            _replace(target, e.after.encode("utf-8"))


def _file_mode(path: Path) -> int | None:
    try:
        return path.stat().st_mode & 0o7777
    except FileNotFoundError:
        return None


def _new_file_mode() -> int:
    """The mode a plain open() would give a new file: 0o666 less the umask."""
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def _replace(target: Path, data: bytes, mode: int | None = None) -> None:
    """Write through a temp file and os.replace. mkstemp makes 0600 files, so set the page's own mode:
    the given one, else the existing target's, else a new file's."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        mode = _file_mode(target)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, _new_file_mode() if mode is None else mode)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _restore(ctx: Context, touched: list[tuple[PageEdit, int | None]]) -> None:
    for e, mode in reversed(touched):
        target = ctx.path(e.path)
        if e.before is None:
            target.unlink(missing_ok=True)
        else:
            _replace(target, e.before.encode("utf-8"), mode)
