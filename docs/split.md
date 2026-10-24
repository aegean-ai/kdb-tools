# Splitting kdb-tools out of the Physical AI kDB project

Two course projects build a kDB: Physical AI kDB (papers on vision-language-action models) and
the AR4 digital twin (the AR4 manual and datasheets). Both need the same machinery to store and
check one: the OKF bundle, sources that point at a page and a `bbox`, grounding against the PDF,
highlights, `okf-diff` and the layered ontology. Until now that machinery lived in the Physical AI
project repository, with the project's own rules built into it. This document records how it is
split so that a fix lands once and both projects grade with the same code.

## Repositories

| Repository | Visibility | Holds |
|---|---|---|
| `aegean-ai/kdb-tools` | public | the package `kdb-tools`, module `kdb_tools`: bundle I/O, `locate`, `annotate`, `okf-validate`, `okf-ground`, `okf-diff`, `okf-seed`, the ontology machinery, and the rules every kDB follows |
| `pantelis/physical-ai-kdb` | course | the package `physical-ai-kdb`, module `physical_ai_kdb`: the VLA profile (its rules and three seed specs) and the student README |
| `pantelis/ar4-digital-twin` | course | the `ar4` profile (quantities with units, cited or recomputed by formula), next to `twin-check` and `twin-mcp-log` |

The command names and the module name stay the same, so nothing on the project page changes:
students still run `okf-validate kdb`, and code that imports `kdb_tools.locate.find_quote` still
works. A project pins the core by tag (`kdb-tools @ git+https://github.com/aegean-ai/kdb-tools@v0.2.0`),
so a core fix reaches students only when the project moves its tag.

## Profiles

A profile is what a project adds to the core: rules for its own page types, and its seed specs.

```python
@dataclass
class Profile:
    name: str                                   # "vla"
    check_page: Callable[[Bundle, Page, Report], None] | None = None
    seeds: Traversable | None = None            # a directory of seed spec YAML files
```

A project registers its profile as a Python entry point in the group `kdb_tools.profiles`.
`okf-validate` runs the core rules on every page, then each installed profile's `check_page`.
`okf-seed` with no `--spec` writes the seeds of the installed profiles. With no profile installed
the tools still run, with the core rules only; a profile can also be chosen with `--profile`.

Rule ids do not change. The core keeps `okf.*` and the `project.*` rules every kDB follows:
`index`, `log`, `generated`, `source`, `citation`, `footnote`, `unused-source`, `uncited`,
`aliases`, `entity`, `contested`, `ontology`, `term`, `term-label` and `computation`. The VLA
profile takes `project.result`, `project.condition`, `project.dataset` and `project.trained-on`.

Reference-agent code (chunked ingest, the table-row reader, the `ask` agent's search tools)
stays in the private solution repositories. In both projects students write their own agent. From
0.4.0 the parts of an agent that do not depend on the project are in the core's `agent` extra:
the `kdb.toml` model config, the prompt guard, and extraction and ask agents that send back
evidence that is not where the model says. Each project's starter supplies its own output types
and instructions.

## Order

1. Build `kdb-tools` from `pantelis/physical-ai-kdb@317ca68`, with the profile hook and the core
   tests, and tag `v0.2.0`.
2. Turn `pantelis/physical-ai-kdb` into the VLA profile package that depends on that tag, with the
   VLA tests. Its 91 tests, split between the two repositories, still pass.
3. Point `pantelis/physical-ai-kdb-solution` at the profile package and rerun its tests.
4. Release the AI project only after steps 1 to 3, so students install the split layout from the
   start.
5. Add the AR4 profile when the AR4 kDB is designed. Done in 0.4.0, which also added spreadsheet
   cell sources for Annin's spreadsheets and moved the common agent parts into the core.
