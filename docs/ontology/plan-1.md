# kDB ontology layer, actions and the AI class migration: implementation plan 1

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** kdb-tools gains the type layer (classes, interfaces, properties, constraints with IRIs and
table bindings), a DuckLake lakehouse with a document-evidence `media` table, and typed logged
actions as the only write path; the AI class kDB (physical-ai-kdb profile, starter and project
page) moves onto it.

**Architecture:** Class, Interface and Property pages are ordinary OKF pages with new frontmatter,
checked by new core rules in `okf-validate`. A `kdb_tools.lake` module opens a DuckLake catalog
(local directory, or a RustFS endpoint) and a `kdb_tools.actions` package implements typed actions
that edit pages and rows in one transaction and write an `action_log` row. Validation rejects any
page whose bytes match no logged action. The VLA profile turns its Paper, Model, Dataset, Result
and Analysis pages into classes bound to lakehouse tables, and the starter writes rows through
actions.

**Tech Stack:** Python 3.11+, uv, DuckDB 2.x with the `ducklake` extension, Pydantic 2,
pdfplumber, Pydantic AI (agent extra), MCP (mcp extra), RustFS in Docker (compose file only),
boto3 (new `s3` extra).

**Spec:** `docs/ontology/design.md` (approved 2026-10-25), with `docs/ontology/foundry.md`.
Jira: AURA-877.

## Scope of this plan

The design's section 10 lists four pieces. This plan covers piece 1 and pulls forward the part of
piece 2 the AI class cannot migrate without: the `media` table and citations of PDF passages,
table values and spreadsheet cells (`MediaRef` with page, bbox, quote, value, sheet, cell).
Ruling: the AI class is due in early December and its evidence is all documents, so document
evidence moves into plan 1; images, video, figure rendering and the AR4 migration move to plan 2;
sync and flags are plan 3; the vision check is plan 4. Plan 1 defines `xywh` and `t` in the
`MediaRef` model but no check reads them yet.

Out of scope here: the AR4 profile and kDB, the physical-ai-kdb-solution reference kDB (it is
rebuilt on the new model in its own short plan once Task 12 lands), flags, and any web hosting of
IRIs.

## Global Constraints

- Repositories and branches: kdb-tools on a new branch `ontology` created with
  `git switch -c ontology` from `xlsm` (this carries the uncommitted 0.4.1 change set along);
  physical-ai-kdb on a new branch `ontology`; the eaia project page in the existing worktree
  `eaia/.worktrees/aiml-common-physical-ai-kdb` (branch `physical-ai-wiki`).
- No commits and no pushes. The owner commits the whole set when asked. Each task ends by
  recording a tree snapshot in the ledger instead of a commit.
- `uv` for everything; `uv run pytest` in each repository; commit-ready `uv.lock` after any
  `pyproject.toml` change. Never `pip install`.
- New dependencies, exactly: `duckdb>=2.0,<3` and `pytz>=2024.1` (DuckDB needs pytz for
  timestamps with time zones) in kdb-tools core; `boto3>=1.35` in a new `s3` extra. The DuckLake
  extension is installed at runtime with `INSTALL ducklake`; tests may need network once to fetch
  it, after which DuckDB caches it.
- Version: kdb-tools becomes 0.5.0. Tagging and pushing are owner gates.
- The kDB holds types only. No task writes an instance (paper, model, dataset, result, analysis,
  run, arm, twin) as a page.
- Students never hold cloud credentials. Nothing in the starter reads an R2 key.
- Models: the starter and every agent path work with a local model or OpenRouter (free or capped)
  through `kdb.toml`, as today. No commercial agent is required anywhere.
- Prose (README, docs, docstrings meant for readers, the project page) follows the owner's rules:
  no em dashes or en dashes, plain language, short sentences, and the `technical-writing` and
  `humanizer:humanizer` skills on any authored prose before it is saved.
- Downloaded files are untrusted: read them with `python -I`.
- Existing behaviour that stays: OKF v0.2 bundle structure, `index.md` and `log.md` rules, Term
  pages and `okf-seed` grounding, quote and table-value grounding against PDFs and highlights,
  spreadsheet cell grounding, `generated` stamps, the guard, the config loader.

## Decisions this plan fixes (from the design, made concrete)

- Page types: `Class`, `Interface`, `Property`. They live under `ontology/<domain>/classes/`,
  `ontology/<domain>/interfaces/` and `ontology/<domain>/properties/`. An Ontology page
  (`ontology/<domain>/ontology.md`) gains `base_iri`, and every `iri` under that domain starts
  with it. Term pages gain an `iri` too, written by `okf-seed` as
  `<base_iri><kind>/<slug>`.
- The core module's domain is `core` with `base_iri: https://kdb.aegean.ai/core/`. The VLA
  domain is `vla` with `base_iri: https://kdb.aegean.ai/vla/`. A student kDB uses
  `https://kdb.aegean.ai/vla/<team>/` for any type it adds. Instance IRIs are minted under the
  lake's `iri_base`, per design section 4.1: `https://data.aegean.ai/vla/<team>/` for a student,
  `https://data.aegean.ai/vla-solution/` for the solution (Tasks 8 and 11 check this).
- Datatypes, exactly: `string`, `integer`, `double`, `boolean`, `date`, `timestamp`, `iri`,
  `iri_list`, `string_list`, `media_ref_list`, `json`. DuckDB column types:
  `VARCHAR`, `BIGINT`, `DOUBLE`, `BOOLEAN`, `DATE`, `TIMESTAMPTZ`, `VARCHAR`, `VARCHAR[]`,
  `VARCHAR[]`, `JSON`, `JSON`.
- Units are QUDT unit IRIs in CURIE form (`unit:M`). The table is in Task 3.
- Every bound table has the columns `iri` (key, `VARCHAR`, deterministic, derived from columns
  by the caller, never a row number or random value), `type` (a class IRI) and `team`
  (`VARCHAR`). Lakehouse schema names: `kdb` for the system tables (`action_log`, `media`),
  `main` for bound tables.
- `MediaRef` (JSON in a `media_ref_list` column): `{sha256, page?, bbox?, quote?, value?, sheet?,
  cell?, xywh?, t?}`. `bbox` is `[x0, y0, x1, y1]` in PDF points from the bottom left, as today.
  `quote` is the W3C TextQuoteSelector `exact` text. Ruling: a JSON object with W3C-named fields
  instead of a fragment string, because PDF boxes already use bottom-left points and students
  already know that form; `MediaRef.fragment()` renders the W3C media-fragment string for
  display and export.
- Annotated (highlighted) PDF copies are `media` rows whose `annotates` column holds the sha256
  of the original.
- Action ids: `<UTC timestamp to microseconds>-<8 hex chars of sha256(params)>`, unique per log.

## Review Focus

1. A page edited by hand after an action, including whitespace only: `okf-validate` reports
   `action.unlogged` on that page and nowhere else (Task 6 test
   `test_hand_edit_whitespace_is_unlogged`).
2. Two actions prepared from the same state and submitted one after the other: the second fails
   with a conflict that names the page or row, and changes nothing (Task 5 test
   `test_second_submit_conflicts_and_changes_nothing`).
3. A row whose `iri` repeats an existing row of another team: rejected, because IRIs include the
   team and a foreign-team IRI is refused (Task 8 test `test_foreign_team_iri_refused`).
4. A bundle with no lake configured, such as a fresh clone: `okf-validate` still runs the page
   rules, and reports one `action.no-lake` warning instead of failing every page (Task 6 test
   `test_no_lake_is_one_warning`).
5. An export whose catalog or one object was altered after export: restore refuses before reading
   anything and names the file (Task 10 test `test_restore_refuses_tampered_object`).

---

### Task 1: The lakehouse module

**Files:**
- Create: `src/kdb_tools/lake.py`
- Create: `src/kdb_tools/data/compose.yml` (RustFS service, as in the eaia AI Lakehouse project)
- Modify: `pyproject.toml` (dependencies, `s3` extra, package data, version 0.5.0)
- Test: `tests/test_lake.py`

**Interfaces:**
- Produces:
  - `@dataclass LakeConfig(catalog: Path, data_path: str, team: str, iri_base: str, s3_endpoint: str | None = None, key_env: str | None = None, secret_env: str | None = None)`
    (`iri_base` must end with `/`)
  - `load_config(path: Path = Path("kdb.toml")) -> LakeConfig | None` reads the `[lake]` table;
    `None` when there is none. Secrets are named by environment variable, never stored (same
    rule and error as `config.load`).
  - `class Lake` with `open(cfg: LakeConfig) -> Lake` (classmethod), `.con` (the DuckDB
    connection with the catalog attached as `lake` and in use), `.snapshot() -> int`,
    `.transaction()` (context manager: `BEGIN` / `COMMIT`, `ROLLBACK` on exception),
    `.ensure_system_tables()`, `.close()`.
  - System tables created by `ensure_system_tables`, exact columns:
    - `kdb.action_log(action_id VARCHAR, action_type VARCHAR, action_version INTEGER, at TIMESTAMPTZ, actor VARCHAR, model VARCHAR, team VARCHAR, params JSON, page_edits JSON, row_edits JSON, evidence JSON, reverts VARCHAR, reverted_by VARCHAR)`
    - `kdb.media(sha256 VARCHAR, media_type VARCHAR, bytes BIGINT, object_key VARCHAR, name VARCHAR, origin VARCHAR, annotates VARCHAR, team VARCHAR, registered_by VARCHAR)`
  - `class Store` with `put(sha256: str, data: bytes) -> str` (returns the object key
    `media/sha256/<digest>`), `get(key: str) -> bytes`, `exists(key: str) -> bool`;
    `LocalStore(root: Path)` and `S3Store(endpoint, bucket, key, secret)` (boto3, `s3` extra);
    `store_for(cfg: LakeConfig) -> Store`.

- [ ] **Step 1: Write the failing tests**

```python
def test_local_lake_round_trip(tmp_path):
    cfg = LakeConfig(catalog=tmp_path / "meta.ducklake", data_path=str(tmp_path / "data") + "/", team="team-1",
                     iri_base="https://data.aegean.ai/vla/team-1/")
    lake = Lake.open(cfg); lake.ensure_system_tables()
    s0 = lake.snapshot()
    with lake.transaction():
        lake.con.execute("INSERT INTO kdb.media VALUES ('ab'*32, 'application/pdf', 9, 'media/sha256/x', 'a.pdf', 'upload', NULL, 'team-1', 'a1')")
    assert lake.snapshot() > s0
    assert lake.con.execute("SELECT count(*) FROM kdb.media").fetchone()[0] == 1

def test_transaction_rolls_back(tmp_path): ...  # raise inside the block; count stays 0 and snapshot unchanged

def test_config_refuses_inline_secret(tmp_path):  # [lake] with secret = "..." raises ConfigError naming secret_env

def test_local_store_put_get(tmp_path):
    st = LocalStore(tmp_path)
    key = st.put(hashlib.sha256(b"x").hexdigest(), b"x")
    assert key == "media/sha256/" + hashlib.sha256(b"x").hexdigest() and st.get(key) == b"x"
```

- [ ] **Step 2: Run them and see them fail.** Run `uv run pytest tests/test_lake.py -v`, expected
  `ModuleNotFoundError: kdb_tools.lake`.
- [ ] **Step 3: Implement `kdb_tools/lake.py`.** `Lake.open` runs `INSTALL ducklake; LOAD
  ducklake;`, and for an `s3://` data path also `httpfs` and a `CREATE SECRET` with
  `ENDPOINT`, `URL_STYLE 'path'`, `USE_SSL false` for an `http://` endpoint, then
  `ATTACH 'ducklake:<catalog>' AS lake (DATA_PATH '<data_path>')` and `USE lake`.
  `snapshot()` is `max(snapshot_id)` from `ducklake_snapshots('lake')`. `S3Store.put` writes
  only when the key is absent.
- [ ] **Step 4: Add `src/kdb_tools/data/compose.yml`**: one `rustfs` service, image
  `rustfs/rustfs` pinned to a tag at least two weeks old, ports 9000 and 9001, a `./lake/rustfs`
  data volume, credentials from `RUSTFS_ACCESS_KEY` and `RUSTFS_SECRET_KEY` in the environment.
  Ship it as package data.
- [ ] **Step 5: Run the tests.** All of `tests/test_lake.py` pass, and the full suite still
  passes.
- [ ] **Step 6: Record the tree snapshot in the ledger.**

### Task 2: IRIs and the Class, Interface and Property pages

**Files:**
- Create: `src/kdb_tools/onto.py`
- Modify: `src/kdb_tools/validate.py` (call the new checks; `check_entities` skips the new types)
- Modify: `src/kdb_tools/testing.py` (helpers `klass`, `prop`, `interface`, `domain`)
- Test: `tests/test_onto.py`

**Interfaces:**
- Consumes: `bundle.Bundle`, `validate.term_domain`, `validate.ontology_graph`.
- Produces:
  - `@dataclass Prop(iri, datatype, unit: str | None, required: bool, rel)`,
    `@dataclass Binding(table: str, key: str, title: str, columns: dict[str, str])`,
    `@dataclass Klass(iri, rel, status, subclass_of: list[str], implements: list[str], binding: Binding | None, constraints: list[dict])`,
    `@dataclass Iface(iri, rel, properties: list[tuple[str, bool]], links: list[dict])`
  - `@dataclass Ontology(classes: dict[str, Klass], interfaces: dict[str, Iface], props: dict[str, Prop], terms: dict[str, str])`
    (`terms` maps a Term IRI to its page rel)
  - `load_ontology(b: Bundle, report: Report | None = None) -> Ontology` (cached on the bundle
    like `validate.ancestors`)
  - `check_ontology(b: Bundle, report: Report) -> None`, called from `validate.validate`
  - `property_columns(o: Ontology, k: Klass) -> dict[str, Prop]` (a class's columns with
    inherited properties from `subclass_of` and `implements`)

Frontmatter, exactly:

```yaml
# ontology/vla/classes/result.md
type: Class
title: Result
iri: https://kdb.aegean.ai/vla/classes/result
status: experimental        # experimental | active | deprecated
subclass_of: []             # class IRIs
implements: []              # interface IRIs
binding:
  table: results
  key: iri
  title: title
  columns: {title: https://kdb.aegean.ai/core/properties/title, ...}
constraints: []             # Task 3
deprecated: {reason: ..., deadline: 2027-01-31, replacement: <iri>}   # only when deprecated
# ontology/core/properties/length.md
type: Property
iri: https://kdb.aegean.ai/core/properties/length
datatype: double
unit: unit:M                # required for double and integer quantities with a dimension; absent otherwise
required: false
# ontology/core/interfaces/joint.md
type: Interface
iri: https://kdb.aegean.ai/core/interfaces/joint
properties: [{property: <iri>, required: true}]
links: [{name: parent, target: <class or interface iri>, cardinality: many-to-one, required: true}]
```

Rules (rule ids are exact): `onto.iri` (missing, does not start with its domain's `base_iri`,
or used twice in the bundle), `onto.class` (unknown `subclass_of` or `implements` IRI, a
subclass cycle, a bad `status`, a `deprecated` block on a non-deprecated class or missing on a
deprecated one), `onto.property` (unknown datatype, a unit on a non-numeric datatype, an unknown
unit), `onto.binding` (a column mapped to an unknown property, the key or title column not in
`columns`, the columns `iri`, `type` or `team` mapped at all (they are implicit), two classes
bound to the same table, a required interface property not mapped by an implementing class),
`onto.link` (a link target that is not a class or interface).

- [ ] **Step 1: Write the failing tests**, one per rule plus a clean bundle, for example:

```python
def test_clean_core_and_class(bundle):
    domain(bundle, "core", "https://kdb.aegean.ai/core/")
    prop(bundle, "core", "title", "string"); prop(bundle, "core", "length", "double", unit="unit:M")
    klass(bundle, "core", "rod", table="rods", columns={"title": "title", "length_m": "length"})
    assert errors(validate(bundle.root, [])) == []

def test_iri_outside_base(bundle): ...      # klass with iri https://example.org/x -> onto.iri
def test_subclass_cycle(bundle): ...        # a subclass_of b, b subclass_of a -> onto.class on both
def test_unit_on_string(bundle): ...        # -> onto.property
def test_two_classes_one_table(bundle): ... # -> onto.binding on the second
def test_interface_property_unmapped(bundle): ...  # implements joint, does not map its required axis -> onto.binding
```

- [ ] **Step 2: Run and see them fail.**
- [ ] **Step 3: Implement `onto.py`** and wire `check_ontology` into `validate.validate` after
  `check_terms`. Class, Interface and Property pages are exempt from `project.entity` and from
  `project.uncited` (their body is a scope note that may cite but need not).
- [ ] **Step 4: Run** `uv run pytest -v`: new tests pass, every existing test still passes.
- [ ] **Step 5: Record the tree snapshot.**

### Task 3: Units and constraints

**Files:**
- Create: `src/kdb_tools/units.py`
- Modify: `src/kdb_tools/onto.py` (constraint rules)
- Test: `tests/test_constraints.py`

**Interfaces:**
- Produces: `UNITS: dict[str, tuple[str, float]]` keyed by CURIE; `kind(unit) -> str | None`;
  `to_si(value, unit) -> float`; `check_value(constraint: dict, value: float, unit: str) -> bool`.
- The unit table, exactly (CURIE: kind, factor to SI): `unit:RAD` angle 1; `unit:DEG` angle
  pi/180; `unit:RAD-PER-SEC` angular speed 1; `unit:DEG-PER-SEC` angular speed pi/180;
  `unit:REV-PER-MIN` angular speed 2pi/60; `unit:N-M` torque 1; `unit:N-CentiM` torque 0.01;
  `unit:KiloGM_F-CentiM` torque 0.0980665; `unit:N` force 1; `unit:KiloGM` mass 1; `unit:GM`
  mass 1e-3; `unit:M` length 1; `unit:MilliM` length 1e-3; `unit:M-PER-SEC` speed 1;
  `unit:MilliM-PER-SEC` speed 1e-3; `unit:MilliM3` volume 1e-9; `unit:CentiM3` volume 1e-6;
  `unit:M3` volume 1; `unit:KiloGM-PER-M3` density 1; `unit:GM-PER-CentiM3` density 1000;
  `unit:KiloGM-M2` inertia 1; `unit:GM-CentiM2` inertia 1e-7; `unit:A` current 1; `unit:V`
  voltage 1; `unit:W` power 1; `unit:SEC` time 1; `unit:MicroSEC` time 1e-6; `unit:PERCENT`
  ratio 0.01; `unit:UNITLESS` ratio 1. This replaces `twin_tools.units` in plan 2.

A constraint, exactly:

```yaml
constraints:
  - property: https://kdb.aegean.ai/core/properties/length
    nominal: 0.305
    tolerance: 0.0005          # or bounds: [lower, upper]; exactly one of the two
    unit: unit:M
    evidence: [<media sha256 or lakehouse row IRI>, ...]   # at least one
    tolerance_evidence: [...]  # at least one; may be an action id of a logged owner decision
    version: 1
```

A constraint may give `formula` instead of `nominal`, in the ar4 profile's formula language
(`/path.md#name` references, numbers, `+ - * / **`, parentheses), moved into core as
`kdb_tools.formula.evaluate(b, formula, lookup)` with the same `FormulaError`.

Rules: `onto.constraint` (property not in the class's columns or inherited, unit of the wrong
kind for the property's unit, both or neither of `tolerance` and `bounds`, negative tolerance,
bounds not increasing, empty `evidence` or `tolerance_evidence`, a `version` that is not a
positive integer, a formula that does not evaluate).

- [ ] **Step 1: Failing tests**: `to_si(305, "unit:MilliM") == 0.305`; `check_value({"nominal":
  0.305, "tolerance": 0.0005, "unit": "unit:M"}, 305.4, "unit:MilliM") is True` and `305.6` is
  False; one test per rule above; a formula constraint that evaluates within tolerance is clean.
- [ ] **Step 2: Run, see them fail.**
- [ ] **Step 3: Implement.** Move the ar4 profile's formula evaluator into
  `src/kdb_tools/formula.py` unchanged in behaviour (copy, do not import from twin_tools).
- [ ] **Step 4: Run the suite.**
- [ ] **Step 5: Record the tree snapshot.**

### Task 4: The core ontology seed

**Files:**
- Create: `src/kdb_tools/data/core/` seed files: `ontology.yaml` (domain `core`, `base_iri`),
  `properties.yaml`, `interfaces.yaml`, `classes.yaml`
- Modify: `src/kdb_tools/seed.py` (a spec may hold `properties`, `interfaces` and `classes` as
  well as `terms`; `okf-seed --core` writes the core module; Term pages get `iri`)
- Test: `tests/test_seed.py` (extend)

**Interfaces:**
- Consumes: Tasks 2 and 3 page shapes.
- Produces: the core IRIs every later task names. Exactly these, all under
  `https://kdb.aegean.ai/core/`:
  - properties: `title` (string, required), `description` (string), `aliases` (string_list),
    `evidence` (media_ref_list), `mass` (double, unit:KiloGM), `length` (double, unit:M),
    `angle` (double, unit:RAD), `media_type` (string), `sha256` (string)
  - interfaces: `link`, `joint` (links `parent` and `child` to `link`, many-to-one, required),
    `actuator`, `gripper`, `robot`, `twin`, `observation`, `media-artifact`, `document`
  - classes: `revolute-joint`, `continuous-joint`, `prismatic-joint`, `fixed-joint`,
    `floating-joint`, `planar-joint` (each implements `joint`); no table binding (the design
    layers 1 and 2 are abstract here; bindings arrive with the projects)
- Alignment fields (`close_match`) are written only where verified in the source vocabulary
  (CORA, SOSA, QUDT, PROV-O); otherwise left out, as the existing seed rule says.

- [ ] **Step 1: Failing tests**: `okf-seed --core` on an empty bundle writes the pages above and
  `okf-validate` on the result has no errors; a rerun leaves every page byte-identical; every
  seeded Term page has `iri` equal to `<base_iri><kind>/<slug>`.
- [ ] **Step 2: Run, see them fail.**
- [ ] **Step 3: Implement** the seed files and the spec reader. Class, Interface and Property
  entries carry a `scope` like terms do, but a scope may be plain text with no quote (the core
  module has no library papers); such a page's body is the scope text with no citation.
- [ ] **Step 4: Run the suite.**
- [ ] **Step 5: Record the tree snapshot.**

### Task 5: The action machinery

**Files:**
- Create: `src/kdb_tools/actions/__init__.py`, `src/kdb_tools/actions/base.py`
- Test: `tests/test_actions_base.py`

**Interfaces:**
- Consumes: `Lake`, `LakeConfig` (Task 1).
- Produces:
  - `class Context(root: Path, lake: Lake, store: Store, actor: str, model: str | None, team: str)`
  - `@dataclass PageEdit(path: str, before: str | None, after: str | None)` (None means absent)
  - `@dataclass RowEdit(table: str, iri: str, op: Literal["insert", "update", "delete"], before: dict | None, after: dict | None)`
  - `@dataclass Plan(action_id: str, page_edits: list[PageEdit], row_edits: list[RowEdit], evidence: list[str], snapshot: int, page_hashes: dict[str, str | None])`
  - `class Failed(Exception)` carrying `messages: list[str]`; `class Conflict(Failed)`
  - `class ActionType(Generic[P])` with class attributes `name: str`, `version: int`,
    `params: type[P]` (a Pydantic model), and methods
    `preconditions(ctx, p) -> list[str]` (failure messages; empty means pass),
    `edits(ctx, p) -> tuple[list[PageEdit], list[RowEdit], list[str]]` (the last list is the
    evidence cited), and the final methods
    `dry_run(ctx, raw: dict) -> Plan` and `submit(ctx, raw: dict, plan: Plan | None = None) -> str` (returns the action id)
  - `REGISTRY: dict[str, type[ActionType]]` and `register(cls)`
- Submit, in this order: validate params; run preconditions (raise `Failed` with all messages);
  compute edits; if a `plan` was passed, compare its `snapshot` and `page_hashes` with the
  current state and raise `Conflict` naming each changed page or the snapshot; open a lake
  transaction; apply row edits; insert the `action_log` row with `page_edits` holding
  `{path, before_sha256, after_sha256, before_text}` for each page; write each page to a
  temporary file and `os.replace` it into place; commit. On any exception after the first page
  is replaced, restore the replaced pages from `before` and let the transaction roll back.
  When no `plan` is passed, `submit` makes one internally, so the check still runs against the
  state read at the start of the call.

- [ ] **Step 1: Failing tests** with a toy action registered in the test module
  (`WriteNote(path, text)` that writes one page and inserts one row into a test table):

```python
def test_dry_run_changes_nothing(ctx): ...                # files and snapshot unchanged, plan lists the edit
def test_submit_logs_hashes(ctx): ...                      # action_log row has after_sha256 == sha256(file bytes)
def test_failed_precondition_message(ctx): ...             # Failed.messages == ["text is empty"]
def test_second_submit_conflicts_and_changes_nothing(ctx):
    a = WriteNote().dry_run(ctx, {"path": "n.md", "text": "one"})
    b = WriteNote().dry_run(ctx, {"path": "n.md", "text": "two"})
    WriteNote().submit(ctx, {"path": "n.md", "text": "one"}, a)
    with pytest.raises(Conflict) as e:
        WriteNote().submit(ctx, {"path": "n.md", "text": "two"}, b)
    assert "n.md" in str(e.value)
    assert (ctx.root / "n.md").read_text().endswith("one\n")
def test_failure_after_write_restores_pages(ctx, monkeypatch): ...  # make the commit raise; page content is the old content
```

- [ ] **Step 2: Run, see them fail.**
- [ ] **Step 3: Implement `actions/base.py`.**
- [ ] **Step 4: Run the suite.**
- [ ] **Step 5: Record the tree snapshot.**

### Task 6: Page-hash enforcement in okf-validate

**Files:**
- Modify: `src/kdb_tools/validate.py` (`validate(root, profiles, pages, lake: Lake | None = None)`;
  `main` gains `--lake-config PATH`, default `kdb.toml` beside or above the bundle, and reads
  `[lake]`)
- Create: `src/kdb_tools/actions/adopt.py` (`AdoptBundle`)
- Test: `tests/test_unlogged.py`

**Interfaces:**
- Consumes: Task 5.
- Produces: rule `action.unlogged` (error) on every `.md` file in the bundle, reserved files
  included, whose sha256 differs from the `after_sha256` of the latest logged edit of that path,
  or that no logged edit names; rule `action.no-lake` (one warning, path `.`) when no lake is
  configured; action `AdoptBundle` (params: none) that logs every current page as an edit with
  `before: null`. It is how an existing bundle enters the new model, once, at cutover.

- [ ] **Step 1: Failing tests**: after `AdoptBundle` a bundle validates clean;
  `test_hand_edit_whitespace_is_unlogged` (append one space to a page, exactly that page gets
  `action.unlogged`); a page created by hand gets it; a page deleted by hand gives
  `action.unlogged` naming the missing path; `test_no_lake_is_one_warning` (no lake: page rules
  run, exactly one `action.no-lake`).
- [ ] **Step 2 to 4:** run, implement, run the suite.
- [ ] **Step 5: Record the tree snapshot.**

### Task 7: The type actions

**Files:**
- Create: `src/kdb_tools/actions/types.py`
- Modify: `src/kdb_tools/seed.py` (`okf-seed` runs as the action `SeedOntology` when a lake is
  configured; without one it refuses and says to configure `[lake]`)
- Test: `tests/test_actions_types.py`

**Interfaces:**
- Consumes: Tasks 2, 3, 5.
- Produces these registered actions (params are Pydantic models with exactly these fields):
  - `ProposeClass(domain, slug, title, description, subclass_of: list[str] = [], implements: list[str] = [], binding: dict | None = None)` creates the page with `status: experimental` and adds it to the domain's `index.md`
  - `AddProperty(domain, slug, title, datatype, unit: str | None = None, required: bool = False, description)`
  - `ImplementInterface(class_iri, interface_iri, column_map: dict[str, str])`
  - `AddConstraint(class_iri, constraint: dict)` (version set to 1)
  - `ReviseConstraint(class_iri, property, constraint: dict, reason: str)` (version + 1; the old
    version is kept in the page's `superseded` list)
  - `BindTable(class_iri, binding: dict, migration: Literal["none", "drop", "move", "cast"] = "none", move: dict[str, str] = {})`
    creates or alters the bound table (`CREATE TABLE IF NOT EXISTS` with `iri`, `type`, `team`
    and one column per mapped property with the Task 2 DuckDB type); a change that drops or
    retypes a column of a table with rows needs a migration other than `none`
  - `PromoteClass(class_iri)` (experimental to active)
  - `DeprecateClass(class_iri, reason, deadline: date, replacement: str)`
  - `SeedOntology(spec)` wrapping the existing seeder
  - `RevertAction(action_id)`: allowed only when no later log row edits a page or row it edited,
    and only by the same actor; restores `before_text` and reverses row edits; sets
    `reverted_by` on the original
- Preconditions every type action shares: the domain's `ontology.md` exists; IRIs it creates
  are new; an `active` class's IRI, key or table cannot change and the class cannot be deleted
  (message: `<iri> is active; deprecate it and propose a replacement`); every action also
  appends one line to `log.md` under today's date heading, as part of its page edits.

- [ ] **Step 1: Failing tests**: each action's happy path leaves `okf-validate` (with the lake)
  clean; `ProposeClass` twice with the same slug fails; renaming an active class's table fails
  with the message above; `BindTable` that retypes a column with rows and `migration: none`
  fails, and with `cast` succeeds and keeps the rows; `RevertAction` by another actor fails, by
  the same actor restores the page byte for byte; `SeedOntology` rerun logs no page edits when
  nothing changed.
- [ ] **Step 2 to 4:** run, implement, run the suite.
- [ ] **Step 5: Record the tree snapshot.**

### Task 8: Instance actions and document evidence

**Files:**
- Create: `src/kdb_tools/actions/objects.py`, `src/kdb_tools/media.py`
- Modify: `src/kdb_tools/ground.py` (`ground(root, ..., lake: Lake | None = None)` also checks
  the evidence in rows)
- Test: `tests/test_objects.py`, `tests/test_media_ground.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 5, 7.
- Produces:
  - `class MediaRef(BaseModel)`: `sha256: str` (64 hex), `page: int | None`, `bbox: list[float] | None`,
    `quote: str | None`, `value: str | None`, `sheet: str | None`, `cell: str | None`,
    `xywh: list[float] | None`, `t: list[float] | None`; `fragment() -> str`
  - `RegisterMedia(path: str, origin: str, annotates: str | None = None)`: hashes the file,
    stores it with the `Store`, inserts the `kdb.media` row; registering the same bytes twice
    is a no-op that logs nothing
  - `UpsertObjects(class_iri, rows: list[dict])`: each row gets `type = class_iri` and
    `team = ctx.team`; checks required properties and datatypes, that every `iri` starts with
    the lake's `iri_base`, that `iri_list` and `iri` columns that point at Term IRIs name
    existing terms, and that every `MediaRef` names a registered sha256; inserts new rows and
    updates changed ones; logs before and after for each
  - `DeleteObjects(class_iri, iris: list[str], reason)`
  - ground rules on rows, reusing today's rule ids and messages: `ground.quote`,
    `ground.bbox`, `ground.highlight` (the highlight is looked for in the media row that
    `annotates` the cited PDF), `ground.cell`, `ground.page`; rule `ground.media` when a
    `MediaRef` sha256 is not registered or its bytes no longer hash to it
- A finding on a row uses `path = "<table>/<iri>"` so reports stay one list.

- [ ] **Step 1: Failing tests**: a row with a correct quote, bbox and highlight grounds clean;
  a wrong quote gives `ground.quote` with path `results/<iri>`; a sha256 that is not registered
  gives `ground.media`; `test_foreign_team_iri_refused` (team-2's context upserting an IRI
  under team-1's base fails with a message naming both); registering the same PDF twice
  leaves one `media` row and one log row; a required property missing fails with the property
  IRI in the message.
- [ ] **Step 2 to 4:** run, implement, run the suite.
- [ ] **Step 5: Record the tree snapshot.**

### Task 9: The agent and MCP tool surface

**Files:**
- Modify: `src/kdb_tools/tools.py`, `src/kdb_tools/agent.py`, `src/kdb_tools/mcp.py`
- Test: `tests/test_agent_toolset.py`, `tests/test_mcp_server.py` (extend)

**Interfaces:**
- Consumes: Tasks 2, 5, 7, 8.
- Produces, on `KdbTools(root, profiles, ctx: Context | None = None)` and in both wrappers:
  - reads: `get_class(iri) -> dict`, `list_classes() -> list[dict]`,
    `query_objects(class_iri, where: dict[str, str] = {}, limit: int = 50) -> list[dict]`
    (equality filters only; values compared as strings), `get_object(iri) -> dict | None`
  - `actions() -> list[dict]` (name, version, JSON schema of params)
  - `dry_run(action: str, params: dict) -> dict` (the plan as a dict)
  - `submit(action: str, params: dict) -> dict` (`{"action_id": ...}`), which waits for a
    person when the action's policy says so: `ResolveFlag` does not exist yet; in plan 1 the
    policy is `submit` refuses any action touching an `active` class unless the context's
    actor is a person (`ctx.model is None`), with the message
    `this action touches an active class; a person must submit it`
  - The existing `find_quote`, `find_value`, `find_cell` and `check_pages` stay.
  - No tool writes a page or a row except through `submit`.
- An action failure (`Failed`, `Conflict`) reaches the model as `ModelRetry` in the toolset
  and as a `ToolError` over MCP, with every message.

- [ ] **Step 1: Failing tests**: a scripted Pydantic AI `FunctionModel` calls `dry_run` then
  `submit` for `UpsertObjects` and the row exists; a failing precondition comes back as a
  retry with the message; an agent context submitting `PromoteClass` is refused with the
  message above; MCP lists the new tools and returns a `ToolError` on a conflict.
- [ ] **Step 2 to 4:** run, implement, run the suite.
- [ ] **Step 5: Record the tree snapshot.**

### Task 10: Snapshot diff, export and restore

**Files:**
- Modify: `src/kdb_tools/diff.py` (`okf-diff --lake-config PATH --snapshots A B`)
- Create: `src/kdb_tools/export.py` with the commands `okf-export` and `okf-restore`
- Modify: `pyproject.toml` (scripts)
- Test: `tests/test_export.py`, `tests/test_diff.py` (extend)

**Interfaces:**
- Consumes: Task 1, Task 5.
- Produces:
  - lake diff: per bound table, the IRIs added, removed and changed between two snapshots, and
    the action ids that made each change (rule ids `diff.row-added`, `diff.row-removed`,
    `diff.row-changed` as info findings with level `warning`); a row change with no action log
    row is `diff.unlogged` (error)
  - `export(cfg: LakeConfig, bundle: Path, out: Path, snapshot: int | None = None) -> Path`
    writes `out/<team>-<snapshot>/` with `catalog/` (a copy of the DuckLake catalog file),
    `data/` (every data file the catalog references at that snapshot), `media/` (every
    object in `kdb.media`), and `manifest.json` (`{team, snapshot, created_at, files: {relpath: sha256}}`).
    The bundle itself is not exported: it is graded from its git commit.
  - `restore(src: Path, into: Path) -> LakeConfig` checks every hash in the manifest first and
    raises `RestoreError` naming the first mismatching or missing file before opening anything;
    then rewrites the catalog's data path to the restored `data/` directory and returns a
    local `LakeConfig`.
- Students export into their shared Google Drive folder by pointing `out` at it (a synced
  folder); kdb-tools does not talk to Google Drive.

- [ ] **Step 1: Failing tests**: export then restore into another directory gives the same rows
  at the same snapshot; `test_restore_refuses_tampered_object` (change one byte of a media
  object: `RestoreError` names it, and nothing was opened); the lake diff between two
  snapshots of an `UpsertObjects` lists the changed IRI with its action id.
- [ ] **Step 2 to 4:** run, implement, run the suite.
- [ ] **Step 5: Record the tree snapshot.**

### Task 11: The VLA profile on the new model (physical-ai-kdb)

**Files:**
- Create: `src/physical_ai_kdb/seeds/vla.yaml` (domain `vla`, `base_iri`
  `https://kdb.aegean.ai/vla/`, extends `core`, the classes below)
- Modify: `src/physical_ai_kdb/rules.py` (row rules replace page rules), `__init__.py`
  (`Profile` gains `check_rows`)
- Modify: kdb-tools `src/kdb_tools/profile.py` (`Profile.check_rows: Callable | None`, called
  as `(ontology, lake, report)` by `okf-validate` when a lake is configured)
- Modify: physical-ai-kdb `pyproject.toml` (kdb-tools from the local path, editable, during
  development; the tag is an owner gate)
- Test: `tests/test_vla_profile.py` (rewritten)

**Interfaces:**
- Consumes: Tasks 2 to 8.
- Produces these classes and bindings (table: columns), with VLA properties under
  `https://kdb.aegean.ai/vla/properties/`:
  - `paper` → `papers`: `title`, `arxiv_id` (string, required), `version` (string), `media`
    (string: the PDF's sha256, required)
  - `model` → `models`: `title`, `aliases`, `description`, `model_class` (iri, a Term),
    `evidence`
  - `dataset` → `datasets`: `title`, `aliases`, `description`, `collection` (iri_list, Terms of
    `datasets` or an extending ontology), `embodiments` (iri_list), `tasks` (iri_list),
    `modalities` (iri_list), `evidence`
  - `trained-on` → `trained_on` (an object-backed link): `model` (iri), `dataset` (iri), `role`
    (iri, a Term), `evidence`
  - `result` → `results`: `title`, `model` (iri), `task`, `benchmark`, `suite`, `embodiment`,
    `perturbation` (iri, Terms), `trials` (integer), `trials_reported` (boolean), `value`
    (double), `metric` (string), `evidence`
  - `contested` → `contested`: `claim` (string: a row IRI plus evidence index), `disputed_by`
    (string_list), `evidence`
  - `computation` → `computations`: `title`, `code` (string), `inputs` (string_list), `value`
    (double), `evidence`
  - `analysis` → `analyses`: `title`, `question` (string), `answer` (string), `drew_on`
    (iri_list), `evidence`
- Row rules (keep today's rule ids and messages where the rule survives): `project.result`
  (task, benchmark, suite, embodiment set; trials set or `trials_reported` false),
  `project.condition` (each condition IRI is a Term), `project.dataset` (collection non-empty,
  in the `datasets` family), `project.trained-on` (model and dataset rows exist, role in the
  `datasets` family, evidence non-empty), `project.entity` (no two rows of models, datasets
  or papers share a normalized title or alias), `project.iri` (every row IRI starts with
  the lake's `iri_base`, and every type the team adds starts with `https://kdb.aegean.ai/vla/<team>/`).

- [ ] **Step 1: Failing tests**: port every test in today's `test_vla_profile.py` to rows (same
  cases, same rule ids), plus `project.iri` and a clean seeded bundle with one row per class.
- [ ] **Step 2 to 4:** run, implement, run both suites.
- [ ] **Step 5: Record the tree snapshot of both repositories.**

### Task 12: The AI class starter on the new model

**Files:**
- Modify: `starter/kdbagent/ingest.py`, `ask.py`, `cli.py`, `starter/kdb.toml.example`
  (`[lake]` table, local by default), `starter/README.md`, `starter/pyproject.toml`
- Create: `starter/compose.yml` (copied from kdb-tools package data by `kdb lake init`)
- Test: `starter/tests/test_starter.py` (rewritten)

**Interfaces:**
- Consumes: Tasks 8, 9, 10, 11.
- Produces: `kdb lake init --team <team>` (writes `compose.yml` and a local `[lake]` table with
  `iri_base = "https://data.aegean.ai/vla/<team>/"`), `kdb ingest
  <pdf>` (registers the PDF with `RegisterMedia`, extracts facts as today, and leaves turning
  facts into rows to the student: `write_rows(ctx, paper, facts)` raises `NotImplementedError`
  with a message naming `UpsertObjects` and `dry_run`), `kdb ask` (the ask agent reads with
  `query_objects` and `get_object` instead of `list_pages` and `read_page`), `kdb export <dir>`
  (calls `okf-export`), `kdb history` (unchanged: still the student's to write).
- The scripted-model test runs offline against a local lake: ingest registers the PDF, a
  student-style `write_rows` written in the test upserts one Result row, `okf-validate` and
  `okf-ground` with the lake are clean, and `kdb export` then `okf-restore` round-trips.

- [ ] **Step 1 to 4:** failing tests, run, implement, run.
- [ ] **Step 5: Record the tree snapshot.**

### Task 13: Documentation and the project page

**Files:**
- Modify: kdb-tools `README.md` (tools table: the new commands and rules; the lake; actions)
- Modify: physical-ai-kdb `README.md` and `docs/design.md`
- Modify: eaia worktree `projects/nlp/physical-ai-kdb/index.mdx` sections "What you are given",
  "The kDB format", "The ontology", "Incremental updates", "The command line", "Deliverables",
  "How you are graded"

**Interfaces:**
- Consumes: everything above. The page must describe exactly what Tasks 11 and 12 built: types
  in the kDB, every paper, model, dataset, result and analysis as a row, evidence as
  `MediaRef`s, every change as an action, `ingest` as one or more actions whose log names the
  rows it touched, the incremental-update check as `okf-diff --snapshots`, and submission as the
  kDB repository at a commit plus `kdb export` into the student's shared Google Drive folder.
- Keep the page rules from the eaia CLAUDE.md: second person, no dates or deadlines, no em
  dashes, sentence-case headings, language tags on code blocks. Run `technical-writing` to draft
  and `humanizer:humanizer` on the changed sections. Run the repository's Vale lint if one
  applies to `.mdx` pages.

- [ ] **Step 1:** rewrite the sections; every YAML and command example on the page is run once
  against a scratch bundle and lake, and the page states nothing the tools do not do.
- [ ] **Step 2:** `okf-validate` and `okf-ground` on the scratch bundle used for the examples are
  clean.
- [ ] **Step 3: Record the tree snapshot of all three repositories.** Do not push; the eaia PR
  flow (aiml-common, then eaia) is the owner's call.
