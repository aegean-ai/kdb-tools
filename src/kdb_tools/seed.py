"""okf-seed: write a project's seed ontology into a kDB bundle, grounded in the bundle's own PDFs.

Each term in the seed spec becomes a Term page under ontology/<domain>/<kind>/<slug>.md. Every
scope sentence cites a quote from a library paper. A spec may also hold properties, interfaces and
classes, which become Property, Interface and Class pages under ontology/<domain>/properties,
interfaces and classes. Their scope may be plain text with no quote, as in the core module, which
okf-seed --core writes from the package data. When the spec gives a base_iri, the Ontology page
carries it and every page gets an iri of <base_iri><kind>/<slug>. The quote is found in papers/<arxiv-id>.pdf of
the bundle, so the page and bbox match the copy of the paper you have, and it is highlighted in
annotated/<arxiv-id>.pdf. Running it again leaves unchanged pages byte-identical, and keeps what
actions own on a Class page (its status, binding, constraints and deprecation).

The okf-seed command runs each spec as the SeedOntology action, so the pages and the log row land
together. It needs a [lake] in kdb.toml and refuses without one. A spec with any error changes
nothing. The library function seed() writes directly: a term whose quote cannot be found is
reported and not written, and the rest is.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber
import yaml

from . import bundle as B
from . import profile as P
from .annotate import highlight
from .ground import Paper, iou
from .locate import find_quote
from .report import Report, fail

TOOL = "okf-seed"
ACTOR = "okf-seed/1"
CORE_SEEDS = Path(__file__).parent / "data" / "core"
TYPE_FOLDERS = (("properties", "Property", "a property"), ("interfaces", "Interface", "an interface"),
                ("classes", "Class", "a class"))


def term_path(domain: str, kind: str, slug: str) -> str:
    return f"ontology/{domain}/{kind}/{slug}.md"


def locate(pdf: Path, page_hint: int, text: str) -> tuple[int, list[float]] | None:
    """The page (from 1) and bbox of text, looking at the hinted page first."""
    with pdfplumber.open(pdf) as doc:
        order = [page_hint] + [n for n in range(1, len(doc.pages) + 1) if n != page_hint]
        for n in order:
            if 1 <= n <= len(doc.pages):
                spans = find_quote(doc.pages[n - 1], text)
                if spans:
                    return n, spans[0]
    return None


def render(fm: dict, body: str) -> str:
    return "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n" + body


def old_page(path: Path) -> tuple[dict | None, str | None]:
    """The frontmatter and body of the page at path, or (None, None) when it is absent or unreadable."""
    if not path.exists():
        return None, None
    try:
        return B.split_frontmatter(path.read_text(encoding="utf-8"))
    except B.FrontmatterError:
        return None, None


def new_text(path: Path, fm: dict, body: str) -> str | None:
    """The page's new text, or None when only its generated stamp would change."""
    old_fm, old_body = old_page(path)
    if old_fm is not None:
        if {k: v for k, v in old_fm.items() if k != "generated"} == {k: v for k, v in fm.items() if k != "generated"} \
                and old_body.strip() == body.strip():
            return None
    return render(fm, body)


INDEX_ENTRY = re.compile(r"^\* \[.*\]\(([^)\s]+)\) - ")
ABOUT = "* [About this ontology](ontology.md) - what it covers and what it extends"


def _target(line: str) -> str | None:
    m = INDEX_ENTRY.match(line)
    return m.group(1) if m else None


def index_text(title: str, old: str | None, entries: list[str]) -> str:
    """A domain index.md: the heading, the About line, then the entries sorted by link target.

    entries are full lines. An entry of the old index whose target is not among them is kept, so a
    page an action added stays listed when the seed runs again."""
    mine = {_target(e) for e in entries}
    kept = [ln for ln in (old or "").splitlines() if _target(ln) not in (None, "ontology.md") and _target(ln) not in mine]
    return "\n".join([f"# {title} ontology", "", ABOUT] + sorted(entries + kept, key=_target)) + "\n"


def add_index_entry(old: str | None, title: str, entry: str) -> str:
    """The domain index with one more entry, placed in link-target order; a new index when there is none."""
    if old is None:
        return index_text(title, None, [entry])
    lines = old.rstrip("\n").split("\n")
    places = [i for i, ln in enumerate(lines) if _target(ln) not in (None, "ontology.md")]
    later = [i for i in places if _target(lines[i]) > _target(entry)]
    at = later[0] if later else (places[-1] + 1 if places else len(lines))
    return "\n".join(lines[:at] + [entry] + lines[at:]) + "\n"


def log_append(old: str | None, day: str, line: str) -> str:
    """log.md with one bullet line added under the '## <day>' heading, which is added when missing."""
    text = old.rstrip("\n") if old is not None else "# Log"
    heading = f"## {day}"
    if heading not in text:
        text += f"\n\n{heading}"
    return text + f"\n* {line}\n"


def already_highlighted(annotated: Path, page_no: int, bbox: list[float]) -> bool:
    if not annotated.is_file():
        return False
    paper = Paper(annotated)
    try:
        return page_no <= paper.pages and any(iou(bbox, h) >= 0.99 for h in paper.highlights(page_no))
    finally:
        paper.close()


def profile_specs(profiles: list[P.Profile] | None = None) -> list[tuple[str, dict]]:
    """The profiles' seed specs, ordered so that an ontology comes after the ones it extends.

    profiles default to the installed ones."""
    profiles = P.installed() if profiles is None else profiles
    specs = {f.name: yaml.safe_load(f.read_text()) for prof in profiles if prof.seeds is not None
             for f in prof.seeds.iterdir() if f.name.endswith(".yaml")}
    by_domain = {s["domain"]: (name, s) for name, s in specs.items()}
    ordered, done = [], set()

    def visit(domain: str) -> None:
        if domain in done or domain not in by_domain:
            return
        done.add(domain)
        for parent in by_domain[domain][1].get("extends", []):
            visit(parent)
        ordered.append(by_domain[domain])

    for domain in sorted(by_domain):
        visit(domain)
    return ordered


def seed(root: Path, spec_path: Path | None = None, profiles: list[P.Profile] | None = None) -> Report:
    """Write one seed spec, or every spec of the profiles when none is given."""
    report = Report(TOOL)
    if spec_path is None:
        specs = profile_specs(profiles)
    else:
        specs = [(str(spec_path), yaml.safe_load(Path(spec_path).read_text()))]
    report.extra.update({"written": [], "terms": 0, "types": 0})
    for name, spec in specs:
        seed_one(root, name, spec, report)
    return report


def broader_path(domain: str, spec: dict, value: str) -> str:
    """A broader entry is a slug in the same spec, or <ontology>/<kind>/<slug> in an ontology it extends."""
    if "/" in value:
        return f"/ontology/{value}.md"
    return "/" + term_path(domain, next(t["kind"] for t in spec.get("terms", []) if t["slug"] == value), value)


def scope_list(entry: dict) -> list[dict]:
    """An entry's scope as a list of {text, quote?}: a plain string is one sentence with no quote."""
    scope = entry.get("scope", [])
    return [{"text": scope}] if isinstance(scope, str) else scope


def cite(root: Path, rel: str, entry: dict, papers: dict, report: Report, marks: dict) -> tuple[list, list] | None:
    """The sources and sentences of one entry's scope, or None after reporting why it cannot be written."""
    sources, sentences = [], []
    for n, scope in enumerate(scope_list(entry), 1):
        q = scope.get("quote")
        if q is None:
            sentences.append(scope["text"].rstrip())
            continue
        arxiv = papers[q["paper"]]
        pdf = root / "papers" / f"{arxiv}.pdf"
        if not pdf.is_file():
            report.error("seed.paper", rel, f"papers/{arxiv}.pdf is missing; download arXiv {arxiv} and save it under that name")
            return None
        found = locate(pdf, q.get("page", 1), q["text"])
        if found is None:
            report.error("seed.quote", rel, f"term {entry['slug']}: the quote is not in papers/{arxiv}.pdf: {q['text'][:80]}")
            return None
        page_no, bbox = found
        sid = f"seed-{entry['slug']}-{n}"
        sources.append({"id": sid, "resource": f"/papers/{arxiv}.pdf", "page": page_no, "quote": q["text"], "bbox": bbox})
        sentences.append(f"{scope['text'].rstrip()}[^{sid}]")
        marks.setdefault(arxiv, []).append((page_no, q["text"], sid, bbox))
    return sources, sentences


def page_body(sources: list, sentences: list) -> str:
    body = " ".join(sentences) + "\n"
    if sources:
        notes = "\n".join(f"[^{s['id']}]: arXiv {Path(s['resource']).stem}, page {s['page']}" for s in sources)
        body += "\n" + notes + "\n"
    return body


def type_frontmatter(kind: str, entry: dict, iri: str, base: str, folders: dict) -> dict:
    """The frontmatter of a Property, Interface or Class page, before generated and sources."""
    fm = {"type": kind, "title": entry["title"], "iri": iri}

    def expand(folder: str, ref: str) -> str:
        return ref if "://" in ref else f"{base}{folder}/{ref}"

    if kind == "Property":
        fm["datatype"] = entry["datatype"]
        fm["required"] = bool(entry.get("required", False))
        if entry.get("unit"):
            fm["unit"] = entry["unit"]
    elif kind == "Interface":
        fm["properties"] = [{"property": expand("properties", p["property"]), "required": bool(p.get("required", False))}
                            for p in entry.get("properties", [])]
        fm["links"] = [{**link, "target": expand(folders.get(link["target"], "interfaces"), link["target"])}
                       for link in entry.get("links", [])]
    else:
        fm["status"] = entry.get("status", "experimental")
        fm["subclass_of"] = [expand("classes", r) for r in entry.get("subclass_of", [])]
        fm["implements"] = [expand("interfaces", r) for r in entry.get("implements", [])]
    for key in ("close_match", "related_match", "exact_match"):
        if entry.get(key):
            fm[key] = entry[key]
    return fm


ACTION_KEYS = ("binding", "constraints", "superseded", "deprecated")  # Class keys that only actions write


def carry_over(fm: dict, old: dict | None) -> dict:
    """A Class page's seeded frontmatter plus what actions own on an existing page: the keys in
    ACTION_KEYS and the status, and the subclass_of and implements entries actions added. The spec's
    status applies only when the page is created; after that, PromoteClass and DeprecateClass change
    it, and a second seed keeps what they did."""
    if old is None:
        return fm
    out = dict(fm)
    if isinstance(old.get("status"), str):
        out["status"] = old["status"]
    for key in ACTION_KEYS:
        if key in old:
            out[key] = old[key]
    for key in ("subclass_of", "implements"):  # the spec's entries, then what actions added, each once
        added = old.get(key)
        if isinstance(added, list):
            out[key] = list(dict.fromkeys(list(out.get(key, [])) + [x for x in added if isinstance(x, str)]))
    return out


@dataclass
class Seeded:
    """What one spec would change: the new text of each page that changes, and the highlights it needs."""

    domain: str
    pages: dict[str, str] = field(default_factory=dict)  # bundle-relative path -> new text
    marks: dict[str, list] = field(default_factory=dict)  # arXiv id -> (page, quote, id, bbox) to highlight
    written: list[str] = field(default_factory=list)  # the Term, type and Ontology pages among pages
    terms: int = 0
    types: int = 0


def seed_one(root: Path, name: str, spec: dict, report: Report) -> None:
    """Compute one spec's changes, then write them, the highlights and a log line."""
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    s = compute(root, name, spec, report, now)
    if s is None:
        return
    apply_marks(root, s.marks)
    for rel, text in s.pages.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    if s.written:
        log = root / "log.md"
        log.write_text(log_append(log.read_text() if log.exists() else None, dt.date.today().isoformat(),
                                  f"okf-seed wrote {len(s.written)} {s.domain} {'pages' if s.types else 'terms'}."))
    report.extra["written"] += s.written
    report.extra["terms"] += s.terms
    report.extra["types"] = report.extra.get("types", 0) + s.types


def apply_marks(root: Path, marks: dict[str, list]) -> None:
    """Highlight each quote in annotated/<arxiv-id>.pdf unless it is highlighted there already."""
    for arxiv, items in marks.items():
        annotated = root / "annotated" / f"{arxiv}.pdf"
        todo = [m for m in items if not already_highlighted(annotated, m[0], m[3])]
        if todo:
            highlight(root / "papers" / f"{arxiv}.pdf", annotated, todo)


def compute(root: Path, name: str, spec: dict, report: Report, now: str) -> Seeded | None:
    """What seeding spec would change, without writing anything; None after reporting a bad spec.

    now is the generated stamp of the pages it writes. log.md is left out: the caller logs."""
    domain, papers = spec["domain"], spec.get("papers", {})
    base = spec.get("base_iri")
    numeric = sorted(k for k, v in papers.items() if not isinstance(v, str))
    if numeric:
        report.error("seed.spec", name,
                     f"arXiv ids for {', '.join(numeric)} were read as numbers; quote them, as in '2306.03310'")
        return None
    if base is not None and not base.endswith("/"):
        report.error("seed.spec", name, f"base_iri '{base}' must end with /")
        return None
    if any(spec.get(folder) for folder, _, _ in TYPE_FOLDERS) and base is None:
        report.error("seed.spec", name, "a spec with properties, interfaces or classes needs a base_iri")
        return None
    out = Seeded(domain)
    written, listed, marks = out.written, [], out.marks

    def put(rel: str, text: str | None) -> bool:
        if text is not None:
            out.pages[rel] = text
        return text is not None

    terms = spec.get("terms", [])
    for term in terms:
        rel = term_path(domain, term["kind"], term["slug"])
        cited = cite(root, rel, term, papers, report, marks)
        if cited is None:
            continue
        sources, sentences = cited
        fm = {"type": "Term", "title": term["pref_label"], "pref_label": term["pref_label"],
              "alt_labels": term.get("alt_labels", [])}
        if base is not None:
            fm["iri"] = f"{base}{term['kind']}/{term['slug']}"
        if term.get("broader"):
            fm["broader"] = [broader_path(domain, spec, b) for b in term["broader"]]
        for key in ("close_match", "related_match", "exact_match"):
            if term.get(key):
                fm[key] = term[key]
        fm["generated"] = {"by": ACTOR, "at": now}
        fm["sources"] = sources
        if put(rel, new_text(root / rel, fm, page_body(sources, sentences))):
            written.append(rel)
        listed.append((rel, term["pref_label"], "a term"))

    kinds = {entry["slug"]: folder for folder, _, _ in TYPE_FOLDERS for entry in spec.get(folder, [])}  # slug -> folder, for link targets
    types = 0
    for folder, kind, what in TYPE_FOLDERS:
        for entry in spec.get(folder, []):
            rel = term_path(domain, folder, entry["slug"])
            cited = cite(root, rel, entry, papers, report, marks)
            if cited is None:
                continue
            sources, sentences = cited
            fm = type_frontmatter(kind, entry, f"{base}{folder}/{entry['slug']}", base, kinds)
            if kind == "Class":
                fm = carry_over(fm, old_page(root / rel)[0])
            fm["generated"] = {"by": ACTOR, "at": now}
            if sources:
                fm["sources"] = sources
            if put(rel, new_text(root / rel, fm, page_body(sources, sentences))):
                written.append(rel)
            listed.append((rel, entry["title"], what))
            types += 1

    title = spec.get("title", domain.capitalize())
    onto_fm = {"type": "Ontology", "title": f"{title} ontology",
               "description": spec.get("description", f"The {domain} ontology seeded by okf-seed.")}
    if base is not None:
        onto_fm["base_iri"] = base
    if spec.get("extends"):
        onto_fm["extends"] = [f"/ontology/{e}/ontology.md" for e in spec["extends"]]
    onto_fm["generated"] = {"by": ACTOR, "at": now}
    body = "* Terms: [index](index.md)\n" + "".join(f"* Extends: [{e}](/ontology/{e}/ontology.md)\n" for e in spec.get("extends", []))
    onto_rel = f"ontology/{domain}/ontology.md"
    if put(onto_rel, new_text(root / onto_rel, onto_fm, body)):
        written.append(onto_rel)

    index = root / "ontology" / domain / "index.md"
    old = index.read_text() if index.exists() else None
    text = index_text(title, old, [f"* [{label}]({Path(rel).relative_to(f'ontology/{domain}').as_posix()}) - {what}"
                                   for rel, label, what in sorted(listed)])
    if text != old:
        put(f"ontology/{domain}/index.md", text)
    root_index = root / "index.md"
    link = f"(ontology/{domain}/index.md)"
    if root_index.exists() and link not in root_index.read_text():
        put("index.md", root_index.read_text().rstrip("\n") + f"\n* [{title} ontology]{link} - seed terms\n")
    out.terms, out.types = len(listed) - types, types
    return out


def stamp(now: dt.datetime) -> str:
    """A generated stamp: the time in UTC to the second, with a Z."""
    return now.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def seed_action(ctx, name: str, spec: dict, report: Report) -> None:
    """Seed one spec as the SeedOntology action, then highlight its quotes.

    The seeder's findings come first: a spec with an error changes nothing. The highlights in
    annotated/*.pdf are not pages, so they are written directly, after the commit."""
    from .actions import Failed
    from .actions.types import SeedOntology

    found = Report(TOOL)
    s = compute(ctx.root, name, spec, found, stamp(dt.datetime.now(dt.timezone.utc)))
    report.findings += found.findings
    if s is None or found.errors:
        return
    try:
        SeedOntology().submit(ctx, {"spec": spec})
    except Failed as e:
        for message in e.messages:
            report.error("seed.action", name, message)
        return
    apply_marks(ctx.root, s.marks)
    report.extra["written"] += s.written
    report.extra["terms"] += s.terms
    report.extra["types"] += s.types


def open_context(bundle: Path, config: Path | None, actor: str):
    """The action context for bundle from the [lake] of kdb.toml, or an error message."""
    from . import lake as L
    from .actions import Context
    from .validate import find_config

    config = config or find_config(bundle)
    try:
        cfg = L.load_config(config) if config else None
    except (OSError, ValueError, KeyError) as e:  # ConfigError is a ValueError
        return None, f"cannot read the lake config {config}: {e}"
    if cfg is None:
        return None, ("okf-seed writes pages only through the SeedOntology action, which needs a lake; "
                      "configure [lake] in kdb.toml")
    try:
        lake = L.Lake.open(cfg)
        lake.ensure_system_tables()
        store = L.store_for(cfg)
    except Exception as e:
        return None, f"cannot open the lake at {cfg.catalog}: {e}"
    return Context(root=bundle, lake=lake, store=store, actor=actor, model=None, team=cfg.team,
                   iri_base=cfg.iri_base), None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=TOOL, description=__doc__.splitlines()[0])
    parser.add_argument("bundle", type=Path, help="the bundle root directory")
    parser.add_argument("--spec", type=Path, help="one seed spec; default: the seeds of the installed profiles")
    parser.add_argument("--core", action="store_true", help="seed the core ontology (properties, interfaces and joint classes)")
    parser.add_argument("--profile", help="seed only this installed profile's specs")
    parser.add_argument("--json", action="store_true", help="print a JSON report")
    parser.add_argument("--lake-config", type=Path, metavar="PATH",
                        help="the kdb.toml that holds [lake]; default: the nearest kdb.toml in or above the bundle")
    parser.add_argument("--actor", help="who runs the seed, for the action log; default: your user name")
    args = parser.parse_args(argv)
    if not args.bundle.is_dir():
        return fail(TOOL, f"{args.bundle} is not a directory")
    profiles = P.installed(args.profile)
    if args.profile and not profiles:
        return fail(TOOL, f"no installed profile is called {args.profile}")
    spec_path = CORE_SEEDS / "core.yaml" if args.core else args.spec
    try:
        specs = profile_specs(profiles) if spec_path is None else [(str(spec_path), yaml.safe_load(spec_path.read_text()))]
    except (OSError, yaml.YAMLError) as e:
        return fail(TOOL, f"cannot read the seed spec: {e}")
    ctx, problem = open_context(args.bundle, args.lake_config, args.actor or getpass.getuser())
    if ctx is None:
        return fail(TOOL, problem)
    report = Report(TOOL, extra={"written": [], "terms": 0, "types": 0})
    try:
        for name, spec in specs:
            seed_action(ctx, name, spec, report)
    finally:
        ctx.lake.close()
    if not args.json:
        print(f"{report.extra['terms']} terms and {report.extra['types']} type pages in place, {len(report.extra['written'])} written")
    report.print(args.json)
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
