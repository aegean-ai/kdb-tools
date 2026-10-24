# Tool design

This repository ships four command-line tools and the same checks as a Python package
(`kdb_tools`). Each course project that builds a kDB installs them through its profile, which
adds the project's own page rules and seed ontology (see [Profiles](#profiles)). Students run
the tools themselves, and their agent calls them as part of every build and ingest. The course
runs the same tools at grading time, so a clean run means the mechanical checks will pass.

A kDB (Knowledge DB) is a continuously updated, grounded information source. It is stored as an
OKF v0.2 bundle: markdown pages with YAML frontmatter, kept in git.

## Roles

The student's agent, which they write themselves, reads the project's documents, writes kDB
pages and annotates PDFs. `okf-validate`, `okf-ground` and `okf-diff` do not call a model and do
not write to the bundle; they only check it and report what is wrong. `okf-seed` writes the seed
ontology pages, their highlights and one log entry.

Every tool prints a human-readable report by default and a JSON report with `--json`. It
exits with status 0 when there are no errors, 1 when there are errors, and 2 when it could
not run (for example, a missing directory). Warnings never change the exit status.

## Bundle layout

```text
kdb/
  index.md          # lists every concept page
  log.md            # one entry per ingest, newest first
  <concept>.md      # one concept per page, in any subdirectory
  papers/           # the source documents, unmodified: PDFs, and .xlsx or .csv spreadsheets
  documents/        # the same, for a project whose sources are not papers
  annotated/        # the annotated copies of the PDFs, same file names
```

Paths follow OKF section 6.2. A path that starts with `/` is relative to the bundle root, and
any other path is relative to the page that contains it. Use the bundle-relative form, for
example `resource: /papers/kim2024.pdf`, so a page can move without breaking its sources. The
annotated copy of a PDF is the file with the same name in `annotated/`. That directory can
be changed with `--annotated`. The agent tools name a document by its file name without the
suffix, such as `2406.09246` or `ar4-dh`, and look for it in `papers/` and `documents/`.

## Coordinates

A `bbox` is `[x0, y0, x1, y1]` in PDF points, in PDF user space: the origin is the bottom-left
corner of the page and y grows upward. This is the space that PDF annotation rectangles use,
so a highlight written by PdfCraft or pypdf can be compared with a bbox directly. Pages are
numbered from 1.

pdfplumber, PyMuPDF and most text extractors report `top` and `bottom` measured down from the
top of the page. Convert them with `y0 = height - bottom` and `y1 = height - top`. When a bbox
misses its text but the same box flipped top to bottom contains it, `okf-ground` says so and
prints the corrected bbox.

## Sources

A source is a sentence or a table cell in a PDF, or a cell in a spreadsheet.

| Kind | Required keys |
|---|---|
| sentence | `id`, `resource`, `page`, `quote`, `bbox` |
| table cell | `id`, `resource`, `page`, `table`, `row`, `column`, `value`, `bbox` |
| spreadsheet cell | `id`, `resource`, `cell`, `value`, and `sheet` for an `.xlsx` file |

A spreadsheet cell source cites an `.xlsx` or `.csv` file. `cell` is one A1 reference in
capitals, such as `B4`. A `.csv` file has a single unnamed sheet, so its sources have no
`sheet`. A key named `cell` is what makes a source a spreadsheet cell, so it is checked before
`quote` and `value`.

## Findings

Each finding has a rule id, a level (`error` or `warning`), the page path, and a message. Rule
ids that start with `okf.` come from the OKF v0.2 conformance section. Rule ids that start with
`project.` are course rules, stricter than OKF, and listed on the project page.

## okf-validate

Checks the bundle's structure without opening any PDF.

| Rule | Level | Check |
|---|---|---|
| `okf.frontmatter` | error | every non-reserved `.md` file has a parseable YAML frontmatter block |
| `okf.type` | error | every frontmatter block has a non-empty `type` |
| `okf.index` | error | an `index.md` has no frontmatter, except `okf_version` at the bundle root |
| `okf.log` | error | every `##` heading in a `log.md` is an ISO date, `YYYY-MM-DD` |
| `project.index` | error | the bundle root has `index.md`, and every concept page is linked from an `index.md` |
| `project.log` | error | the bundle root has `log.md`, and it has at least one `## YYYY-MM-DD` heading |
| `project.generated` | error | every concept has `generated.by` and an ISO 8601 `generated.at` |
| `project.source` | error | every source is a well-formed sentence or table cell, with a unique `id` on its page and a `resource` that exists. The error names the path it looked for, and when a path without a leading `/` would exist from the bundle root it suggests the `/` form |
| `project.citation` | error | every footnote label in the body is the `id` of a source on the same page |
| `project.footnote` | warning | every cited footnote label has a footnote definition |
| `project.unused-source` | warning | every source is cited at least once |
| `project.uncited` | warning | every sentence of prose carries at least one citation |
| `project.aliases` | error | `aliases` is a list of strings |
| `project.entity` | error | no two pages share a title or alias after normalization; Term pages are checked by `project.term-label` instead |
| `project.contested` | error | `contested_by` maps a source id on the page to a list of source ids that exist in the bundle |
| `project.ontology` | error | an `Ontology` page is `ontology/<domain>/ontology.md`, its `extends` lists `Ontology` pages, and no ontologies extend each other in a cycle |
| `project.term` | error | a `Term` page lives under `ontology/<domain>/`, has `pref_label`, lists `alt_labels` as strings, and its `broader` entries are Term paths in the same ontology or one it extends, with no cycle |
| `project.term-label` | error | no two Terms share a preferred or alternative label after normalization within one ontology and the ontologies it extends or is extended by; unrelated ontologies may share a label |
| `project.computation` | error | an `Attested Computation` page has `runtime: python` and either a `computation` file that exists or a `# Computation` python block |

## Profiles

A profile is what a course project adds to these checks: rules for its own page types, and the
directory of its seed specs. A project registers one as a Python entry point in the group
`kdb_tools.profiles`. `okf-validate` runs the rules above on every page, then each installed
profile's rules; `okf-seed` writes the seeds of the installed profiles. Both take `--profile` to
use one profile only. The rules of a profile are documented in its project repository; for
example, `pantelis/physical-ai-kdb` adds `project.result`, `project.condition`, `project.dataset`
and `project.trained-on`. [`split.md`](split.md) records why the checks were split out of that
project.

## The ontology

Profiles make page fields name ontology terms, not words, because the same words name
different things: LIBERO-90 is an evaluation suite of the LIBERO benchmark and also the set of
90 tasks the LIBERO paper uses as pretraining data. Each
term is one page under `ontology/<domain>/`, with fields modelled on W3C SKOS: `pref_label`,
`alt_labels`, `broader`, and optional `exact_match` links to established vocabularies. Its body
is a scope note, cited like any other prose. A term's domain is the directory after `ontology/`,
and each domain is one ontology.

Ontologies are layered. A page `ontology/<domain>/ontology.md` of type `Ontology` declares the
ontology and lists in `extends` the ontologies it specializes. A term's `broader` may then point
into an ontology its own ontology extends, directly or through others, and a label may not be
reused along that chain, because a specialized ontology refines the terms it inherits and never
redefines them. Unrelated ontologies may share a label. A query that follows `broader` therefore
crosses layers: a question about pretraining also finds data used for cross-embodiment
pretraining, a term of the specialized ontology.

A project's seed ontology ships as YAML specs in its profile. `okf-seed` writes the specs in
dependency order, an extended ontology before the
ones that extend it. Each term lists its labels,
its `broader` terms, any verified `close_match` or `related_match` identifiers, and one or more
scope sentences, each with the exact quote from a library paper that supports it. `okf-seed`
finds every quote in the bundle's own `papers/<arxiv-id>.pdf`, starting with the page the spec
names, so the bbox matches the copy of the paper the student has. A term whose quote is not
found is reported as `seed.quote` and not written. Running `okf-seed` again rewrites only pages
whose content changed, so it never breaks the byte-identical rule. Write arXiv ids in the spec
in quotes: YAML reads `2306.03310` as a number and drops the trailing zero, which `okf-seed`
reports as `seed.spec`.

Name normalization for `project.entity` lowercases the name, writes π as `pi`, and drops every
character that is not a letter or digit. So `OpenVLA-OFT` and `openvla oft` collide, while
`π0` and `pi-zero` do not, which is why `aliases` exists.

## okf-ground

Opens the PDFs and checks every source against them.

| Rule | Level | Check |
|---|---|---|
| `ground.page` | error | the page exists in the PDF |
| `ground.quote` | error | the quote appears in the page's characters, after normalizing whitespace, ligatures, quotes, dashes and line-break hyphens. The characters are read in content-stream order, then in reading order, then column by column, for a two-column page drawn line by line across both columns. A match that jumps a gap wider than two characters within a line is text spliced from two columns and does not count |
| `ground.bbox` | error | at least one occurrence of the quote, or of the table-cell value, lies inside the bbox, with 2 points of slack on each side. The error gives the position of the text, and says so when the bbox looks measured from the top of the page |
| `ground.highlight` | error | the annotated copy has a highlight on that page whose rectangle overlaps the bbox with intersection over union of at least 0.5 |
| `ground.sheet` | error | the sheet a spreadsheet cell source names exists. The error lists the sheets there are |
| `ground.cell` | error | the cell holds the value. Numbers are compared as numbers, so `169.77`, `169.770` and the float 169.77 match; anything else is compared as text with outer spaces removed. The error gives what the cell holds and the cells that do hold the value. A formula whose value was never saved, as in a file written by a script, is reported as such |

An `.xlsx` file is read with openpyxl, using the value each formula last computed. A
spreadsheet has no annotated copy and no highlight: the cell reference is already exact.

Quotes are matched against the page's characters in the order the PDF draws them, not against
extracted page text. Extractors reorder lines: two columns at the same height come out
interleaved, so a sentence can be on the page and still be missing from its text. The same
matcher is available to agents as `kdb_tools.locate.find_quote(page, text)`, which returns the
bbox of every occurrence on a pdfplumber page, so an agent and the checker agree on where a
quote is.

`kdb_tools.locate.find_quote_lines(page, text)` gives the same matches as one box per line, and
`kdb_tools.annotate.highlight` writes them as a single highlight with one quad per line, so the
highlight covers only the quoted words. Its rectangle is the union of the lines, which is what
`ground.highlight` compares with the bbox.

A quote that appears on the page but outside its bbox fails `ground.bbox`, not `ground.quote`,
so the report says whether the text or the location is wrong.

## okf-diff

Compares two versions of a bundle, given as two directories or as two git revisions of the
repository the bundle lives in (`--git <old> <new>`). It lists every concept page that was
added, removed or changed, and for a changed page whether the body, the frontmatter or both
changed.

| Rule | Level | Check |
|---|---|---|
| `diff.rewrite` | error | a page whose content did not change is byte-identical (no reordered keys or reflowed text) |
| `diff.stamp` | error | a page whose content did not change kept its `generated` stamp |
| `diff.stale-stamp` | warning | a page whose content changed has a new `generated.at` |

A page's content is its body and every frontmatter key except `generated` and `verified`. OKF section 5.2 defines
`generated.at` as the time of the content's last meaningful change, so a page that gains a source, an alias or
a `contested_by` entry needs a new stamp even when its body reads the same.

It also lists changes to `index.md`, `log.md` and the files under `papers/` and `annotated/`, so
the findings report can be checked against them.

## Scoped checks and agent tools

`validate(root, pages=...)` and `ground(root, pages=...)`, and `--pages` on both commands, report
only findings on the named pages; `ground` then opens only the PDFs those pages cite. The checks
still read the whole bundle, because some rules span pages (one page per entity, ontology
layers), and a finding is kept when its path is one of the named pages.

`kdb_tools.tools.KdbTools` exposes `find_quote`, `find_value`, `find_cell` and `check_pages` with dataclass
results and a `ToolInputError` for a call the agent should correct. `kdb_tools.agent` wraps them
as a Pydantic AI `FunctionToolset` and turns `ToolInputError` into `ModelRetry`. `kdb_tools.mcp`
serves them with the MCP SDK 2.x `MCPServer` and turns `ToolInputError` into `ToolError`, the
only exception whose message that SDK passes to the model; any other exception is reported as
a crash with its message hidden. Neither extra is needed by the core.

## The common agent

Every project that builds a kDB writes an agent that reads documents and one that answers
questions. The parts that do not depend on the project live in `kdb_tools.agent`, with the
model configuration in `kdb_tools.config` and the prompt guard in `kdb_tools.guard`.

`config.load` reads `kdb.toml`: an OpenAI-compatible `base_url`, a `model`, the server's
`context` in tokens and an optional `reasoning_effort`. It refuses an API key written in the
file and reads it from the environment variable that `api_key_env` names. `guard.check`
refuses a prompt longer than three quarters of the context, because Ollama cuts an overlong
prompt without an error.

`extraction_agent(model, output_type, instructions)` reads one `Chunk`: a few pages of a PDF from
`pdf_chunks`, or one sheet from `sheet_chunks`, which prints every value next to its cell
reference. The output type is the project's own. Every `Quote` or `CellValue` inside it, at any
depth, is checked against the document, and the model is sent back with the list of evidence
that is not where it says. `ask_agent(model)` answers from the kDB with `list_pages` and
`read_page`, and sends back a claim that cites a source id its page does not have.

Writing kDB pages is not here. It is the work each project grades.

## Not checked here

Whether a cited sentence actually supports the claim attached to it is judged by a TA, not by
these tools. The tools do not run Attested Computations yet.
