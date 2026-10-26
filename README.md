# kdb-tools

Tools that store and check a kDB (Knowledge DB): a continuously updated, grounded information
source. A kDB has two parts. The kDB itself holds types: ontology terms, and the classes,
interfaces and properties that say what a thing is. It is an
[Open Knowledge Format (OKF) v0.2](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
bundle of markdown pages in git. A lakehouse holds the instances: every paper, part, run or
result is a row in a table that a class binds, and every document is a file in its media table.
Every change to either goes through a typed action that is logged in the lakehouse.

Evidence is checkable. A row cites a sentence or a table cell in a registered PDF, by its sha256,
page and bounding box, and the same spot is highlighted in an annotated copy of the PDF. A row can
also cite a cell in a spreadsheet (`.xlsx`, `.xlsm` or `.csv`) by sheet and cell reference. Term
pages cite their sources the same way, as page frontmatter.

| Command | What it does |
|---|---|
| `okf-validate` | checks that the bundle is valid OKF v0.2 and follows the kDB rules: sources, citations, index, log, the ontology, and the classes and their bindings. With a lake, it runs each profile's row rules and reports `action.unlogged` for any page that no logged action wrote; without one, it warns `action.no-lake` |
| `okf-ground` | checks that every quoted sentence is on its cited page and inside its bbox, that every table value is inside its bbox, that every bbox matches a highlight in the annotated PDF, and that every spreadsheet cell holds its value. With a lake, it checks the evidence of every row too, and that each cited sha256 is registered and its stored bytes still match it |
| `okf-diff` | lists the pages that changed between two versions of a bundle, and checks that unchanged pages stayed byte-identical and kept their `generated` stamps. With `--lake-config` and `--snapshots A B`, it lists the rows added, changed and removed between two snapshots of the lake, with the actions that made each change, and reports `diff.unlogged` for a change no action made |
| `okf-seed` | writes a seed ontology into the bundle through the `SeedOntology` action, with each term defined by a quote it finds and highlights in the bundle's own PDFs. It needs a `[lake]` in `kdb.toml` and refuses without one |
| `okf-export` | writes the lake at its latest snapshot into a folder: the catalog file, its data files, every media file, and a manifest of their sha256 |
| `okf-restore` | checks every hash in an export's manifest, then restores it into an empty folder and prints the local lake config |
| `okf-mcp` | serves the tools below over MCP (needs the `mcp` extra) |

None of them calls a model. The same checks are the Python package `kdb_tools`, and
`kdb_tools.locate.find_quote` and `kdb_tools.annotate.highlight` let an agent place and highlight
evidence exactly where the checks will look for it. `okf-validate`, `okf-ground` and `okf-seed`
find `kdb.toml` in the bundle's folder or the nearest folder above it; `--lake-config` names
another one.

## The lakehouse

`kdb_tools.lake` opens a [DuckLake](https://ducklake.select/) catalog in DuckDB. The `ducklake`
extension is installed at run time with `INSTALL ducklake`, so the first run needs the network
once. Each commit is a snapshot. The `kdb` schema holds two system tables: `action_log`, one row
per action, and `media`, one row per registered file. Bound tables live in `main`, and every one
has the columns `iri`, `type` and `team`. Media bytes are stored under a key derived from their
sha256, so a file is stored once.

A `[lake]` table in `kdb.toml` names the lake. Paths are relative to the folder that holds
`kdb.toml`. The local form needs nothing else, and the tests use it:

```toml
[lake]
catalog = "lake/catalog.ducklake"
data_path = "lake/data/"
team = "team-7"
iri_base = "https://data.aegean.ai/vla/team-7/"
```

For S3 storage, set `data_path` to an `s3://` bucket path and add `s3_endpoint`, `key_env` and
`secret_env`. The last two name environment variables; a key or secret written in `kdb.toml` is
refused. The S3 store needs the `s3` extra. For a team's own machine, the package ships a compose
file for [RustFS](https://rustfs.com/) (`kdb_tools.lake.COMPOSE`) that opens its ports on
127.0.0.1 only and keeps the data in a Docker volume. RustFS starts with no buckets, so create
the one `data_path` names before the first write.

`iri_base` is where the lake mints instance IRIs. Every row IRI must start with it, so one team
never writes another team's rows.

## Actions

Every write is an action: a typed operation with Pydantic parameters, preconditions with a
readable message each, and an edit set of pages and rows. `dry_run` returns the plan and changes
nothing. `submit` checks that nothing the plan read has changed (the lake snapshot and the hash of
every page it edits), then applies the schema statements, the row edits, the log row and the page
writes as one transaction. A failure raises `Failed` with every message, or `Conflict` when the
state moved. Each log row records the actor, the model for an agent, the parameters, and every
page and row edit with its before and after.

| Action | What it does |
|---|---|
| `RegisterMedia` | adds a file to `kdb.media` and stores its bytes after the commit; `annotates` names the original of an annotated copy; the same bytes again change nothing |
| `UpsertObjects` | inserts and updates rows of a class's table, as a patch: a column left out keeps its value, null clears it; checks datatypes, required properties, Term IRIs, the `iri_base` and that every `MediaRef` is registered |
| `DeleteObjects` | removes rows of a class's table, with a reason |
| `SeedOntology` | writes a seed spec's pages; `okf-seed` runs it |
| `ProposeClass`, `AddProperty`, `ImplementInterface`, `AddConstraint`, `ReviseConstraint`, `BindTable`, `PromoteClass`, `DeprecateClass` | change the type layer; `BindTable` creates or alters the bound table |
| `RevertAction` | undoes an earlier action by the same actor while nothing later has touched what it edited |
| `AdoptBundle` | logs the pages of a bundle written before it had a lake, once |

A page edited by hand, even by whitespace, no longer matches the hash its last action recorded,
and `okf-validate` reports it as `action.unlogged`. `index.md` and `log.md` count too: the type
actions add their own lines.

## Checking only some pages

`okf-validate` and `okf-ground` take `--pages` (and `validate()` and `ground()` take `pages=`)
to report only the findings on the pages you name. A row is named `<table>/<iri>`, the path of
every finding on it. An ingest uses it to check the pages and rows it just wrote, so an error left
by an earlier ingest does not fail every later one.

## Tools for agents

The same checks are available as tools an agent calls while it works. `find_quote` and
`find_value` say where a sentence or a table value is on a page of a PDF, the way `okf-ground`
looks for it, and when it is not there, which page it is on. `find_cell` lists the spreadsheet
cells that hold a value. `check_pages` runs `okf-validate` and `okf-ground` on the pages and rows
you name and returns their errors and warnings. A call the agent should correct, such as an unknown
document, comes back as an error that says how to fix it.

Given a `Context` (a lake, a team and who is acting), the tools also read the type layer and the
lake and run actions. `list_classes` and `get_class` read classes. `query_objects` returns rows of
a class's table that equal the values given, and `get_object` returns one row by IRI. `actions`
lists every action with the JSON schema of its parameters. `dry_run` returns an action's plan and
`submit` applies it. No tool writes a page or a row except through `submit`. An agent's submit
that edits an active Class page is refused, because a person submits those; an agent may write
rows of any class.

| Install | Use |
|---|---|
| `kdb-tools[agent]` | `kdb_tools.agent.toolset(Path("kdb"), ctx=ctx)` is a Pydantic AI toolset: `Agent(model, toolsets=[toolset(root, ctx=ctx)])` |
| `kdb-tools[mcp]` | `okf-mcp kdb` serves the same tools over MCP on stdio, for an agent in any framework. With a `[lake]` it adds the lake and action tools. It treats every caller as an agent and logs `--model`, or `mcp-client` when you give none. `--person` turns that off, for a server you run for yourself |

Both wrap the functions in `kdb_tools.tools`, which need neither extra.

## The common agent

The `agent` extra also holds the parts of a kDB agent that every project shares. `kdb_tools.config`
reads the model server from `kdb.toml`, which can name a local server or OpenRouter, and
`kdb_tools.guard` refuses a prompt the server would cut. `kdb_tools.agent.extraction_agent`
reads a few pages of a PDF or one sheet of a spreadsheet and returns your own output type. Every
quote or cell value in that output is checked against the document, and the model is sent back
to fix the ones that are not there. `kdb_tools.agent.ask_agent` answers a question from the kDB
alone and sends back any claim that cites a source its page does not have. Writing the rows is
left to each project; the Physical AI kDB starter also has an ask agent that reads rows. [`docs/design.md`](docs/design.md) has the details.

## Profiles

Each course project that builds a kDB ships a profile: rules for its own pages, rules for the
rows of its lake (`check_rows`, run by `okf-validate` when it has a lake), and its seed specs. Installing the project's repository installs this package and the profile, so the
commands check that project's rules. With no profile installed, the commands check the core
rules only. [`docs/design.md`](docs/design.md) describes the rules, the bundle layout and the
profile interface, and [`docs/split.md`](docs/split.md) records why this package was split out
of the Physical AI kDB project.

| Project | Profile |
|---|---|
| Physical AI kDB | [pantelis/physical-ai-kdb](https://github.com/pantelis/physical-ai-kdb) (`vla`) |
| AR4 digital twin | [pantelis/ar4-digital-twin](https://github.com/pantelis/ar4-digital-twin) (`ar4`) |

## Develop

```bash
uv sync
uv run pytest
```

The fixtures in `kdb_tools.testing` build small bundles and PDFs; a profile's tests import them
with `from kdb_tools.testing import *` in their `conftest.py`.

## License

MIT
