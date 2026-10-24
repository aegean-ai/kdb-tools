# kdb-tools

Tools that store and check a kDB (Knowledge DB): a continuously updated, grounded information
source kept as an [Open Knowledge Format (OKF) v0.2](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
bundle of markdown pages in git. Every claim on a page cites a sentence or a table cell in a PDF,
by page and bounding box, and is highlighted in an annotated copy of that PDF.

| Command | What it does |
|---|---|
| `okf-validate` | checks that the bundle is valid OKF v0.2 and follows the kDB rules: sources, citations, index, log, one page per entity, and the ontology |
| `okf-ground` | checks that every quoted sentence is on its cited page and inside its bbox, that every table value is inside its bbox, and that every bbox matches a highlight in the annotated PDF |
| `okf-diff` | lists the pages that changed between two versions of a bundle, and checks that unchanged pages stayed byte-identical and kept their `generated` stamps |
| `okf-seed` | writes a seed ontology into the bundle, with each term defined by a quote it finds and highlights in the bundle's own PDFs |
| `okf-mcp` | serves the tools below over MCP (needs the `mcp` extra) |

None of them calls a model. The same checks are the Python package `kdb_tools`, and
`kdb_tools.locate.find_quote` and `kdb_tools.annotate.highlight` let an agent place and highlight
evidence exactly where the checks will look for it.

## Checking only some pages

`okf-validate` and `okf-ground` take `--pages` (and `validate()` and `ground()` take `pages=`)
to report only the findings on the pages you name. An ingest uses it to check the pages it just
wrote, so an error left by an earlier ingest does not fail every later one.

## Tools for agents

The same checks are available as tools an agent calls while it works: `find_quote` and
`find_value` say where a sentence or a table value is on a page, the way `okf-ground` looks for
it, and when it is not there, which page it is on. `check_pages` runs `okf-validate` and
`okf-ground` on the pages you name and returns their errors and warnings. A call the agent should
correct, such as an unknown paper, comes back as an error that says how to fix it.

| Install | Use |
|---|---|
| `kdb-tools[agent]` | `kdb_tools.agent.toolset(Path("kdb"))` is a Pydantic AI toolset: `Agent(model, toolsets=[toolset(root)])` |
| `kdb-tools[mcp]` | `okf-mcp kdb` serves the same tools over MCP on stdio, for an agent in any framework |

Both wrap the functions in `kdb_tools.tools`, which need neither extra.

## Profiles

Each course project that builds a kDB ships a profile: the rules for its own page types and its
seed specs. Installing the project's repository installs this package and the profile, so the
commands check that project's rules. With no profile installed, the commands check the core
rules only. [`docs/design.md`](docs/design.md) describes the rules, the bundle layout and the
profile interface, and [`docs/split.md`](docs/split.md) records why this package was split out
of the Physical AI kDB project.

| Project | Profile |
|---|---|
| Physical AI kDB | [pantelis/physical-ai-kdb](https://github.com/pantelis/physical-ai-kdb) (`vla`) |

## Develop

```bash
uv sync
uv run pytest
```

The fixtures in `kdb_tools.testing` build small bundles and PDFs; a profile's tests import them
with `from kdb_tools.testing import *` in their `conftest.py`.

## License

MIT
