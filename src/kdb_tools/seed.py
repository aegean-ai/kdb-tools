"""okf-seed: write a project's seed ontology into a kDB bundle, grounded in the bundle's own PDFs.

Each term in the seed spec becomes a Term page under ontology/<domain>/<kind>/<slug>.md. Every
scope sentence cites a quote from a library paper. The quote is found in papers/<arxiv-id>.pdf of
the bundle, so the page and bbox match the copy of the paper you have, and it is highlighted in
annotated/<arxiv-id>.pdf. A term whose quote cannot be found is reported and not written. Running
the command again leaves unchanged pages byte-identical.
"""

from __future__ import annotations

import argparse
import datetime as dt
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


def write_if_changed(path: Path, fm: dict, body: str) -> bool:
    """Write the page unless only its generated stamp would change."""
    if path.exists():
        try:
            old_fm, old_body = B.split_frontmatter(path.read_text(encoding="utf-8"))
        except B.FrontmatterError:
            old_fm, old_body = None, None
        if old_fm is not None:
            if {k: v for k, v in old_fm.items() if k != "generated"} == {k: v for k, v in fm.items() if k != "generated"} \
                    and old_body.strip() == body.strip():
                return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(fm, body), encoding="utf-8")
    return True


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
    report.extra.update({"written": [], "terms": 0})
    for name, spec in specs:
        seed_one(root, name, spec, report)
    return report


def broader_path(domain: str, spec: dict, value: str) -> str:
    """A broader entry is a slug in the same spec, or <ontology>/<kind>/<slug> in an ontology it extends."""
    if "/" in value:
        return f"/ontology/{value}.md"
    return "/" + term_path(domain, next(t["kind"] for t in spec["terms"] if t["slug"] == value), value)


def seed_one(root: Path, name: str, spec: dict, report: Report) -> None:
    domain, papers = spec["domain"], spec["papers"]
    numeric = sorted(k for k, v in papers.items() if not isinstance(v, str))
    if numeric:
        report.error("seed.spec", name,
                     f"arXiv ids for {', '.join(numeric)} were read as numbers; quote them, as in '2306.03310'")
        return
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    written, listed, marks = [], [], {}
    for term in spec["terms"]:
        rel = term_path(domain, term["kind"], term["slug"])
        sources, sentences, ok = [], [], True
        for n, scope in enumerate(term["scope"], 1):
            q = scope["quote"]
            arxiv = papers[q["paper"]]
            pdf = root / "papers" / f"{arxiv}.pdf"
            if not pdf.is_file():
                report.error("seed.paper", rel, f"papers/{arxiv}.pdf is missing; download arXiv {arxiv} and save it under that name")
                ok = False
                break
            found = locate(pdf, q.get("page", 1), q["text"])
            if found is None:
                report.error("seed.quote", rel, f"term {term['slug']}: the quote is not in papers/{arxiv}.pdf: {q['text'][:80]}")
                ok = False
                break
            page_no, bbox = found
            sid = f"seed-{term['slug']}-{n}"
            sources.append({"id": sid, "resource": f"/papers/{arxiv}.pdf", "page": page_no, "quote": q["text"], "bbox": bbox})
            sentences.append(f"{scope['text'].rstrip()}[^{sid}]")
            marks.setdefault(arxiv, []).append((page_no, q["text"], sid, bbox))
        if not ok:
            continue
        fm = {"type": "Term", "title": term["pref_label"], "pref_label": term["pref_label"],
              "alt_labels": term.get("alt_labels", [])}
        if term.get("broader"):
            fm["broader"] = [broader_path(domain, spec, b) for b in term["broader"]]
        for key in ("close_match", "related_match", "exact_match"):
            if term.get(key):
                fm[key] = term[key]
        fm["generated"] = {"by": ACTOR, "at": now}
        fm["sources"] = sources
        notes = "\n".join(f"[^{s['id']}]: arXiv {Path(s['resource']).stem}, page {s['page']}" for s in sources)
        if write_if_changed(root / rel, fm, " ".join(sentences) + "\n\n" + notes + "\n"):
            written.append(rel)
        listed.append((rel, term["pref_label"]))

    for arxiv, items in marks.items():
        annotated = root / "annotated" / f"{arxiv}.pdf"
        todo = [m for m in items if not already_highlighted(annotated, m[0], m[3])]
        if todo:
            highlight(root / "papers" / f"{arxiv}.pdf", annotated, todo)

    title = spec.get("title", domain.capitalize())
    onto_fm = {"type": "Ontology", "title": f"{title} ontology",
               "description": spec.get("description", f"The {domain} ontology seeded by okf-seed.")}
    if spec.get("extends"):
        onto_fm["extends"] = [f"/ontology/{e}/ontology.md" for e in spec["extends"]]
    onto_fm["generated"] = {"by": ACTOR, "at": now}
    body = "* Terms: [index](index.md)\n" + "".join(f"* Extends: [{e}](/ontology/{e}/ontology.md)\n" for e in spec.get("extends", []))
    if write_if_changed(root / "ontology" / domain / "ontology.md", onto_fm, body):
        written.append(f"ontology/{domain}/ontology.md")

    index = root / "ontology" / domain / "index.md"
    lines = [f"# {title} ontology", "", "* [About this ontology](ontology.md) - what it covers and what it extends"] + [
        f"* [{label}]({Path(rel).relative_to(f'ontology/{domain}').as_posix()}) - a term" for rel, label in sorted(listed)]
    text = "\n".join(lines) + "\n"
    if not index.exists() or index.read_text() != text:
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(text)
    root_index = root / "index.md"
    link = f"(ontology/{domain}/index.md)"
    if root_index.exists() and link not in root_index.read_text():
        root_index.write_text(root_index.read_text().rstrip("\n") + f"\n* [{title} ontology]{link} - seed terms\n")
    if written:
        log = root / "log.md"
        heading = f"## {dt.date.today().isoformat()}"
        text = log.read_text().rstrip("\n") if log.exists() else "# Log"
        if heading not in text:
            text += f"\n\n{heading}"
        log.write_text(text + f"\n* okf-seed wrote {len(written)} {domain} terms.\n")
    report.extra["written"] += written
    report.extra["terms"] += len(listed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=TOOL, description=__doc__.splitlines()[0])
    parser.add_argument("bundle", type=Path, help="the bundle root directory")
    parser.add_argument("--spec", type=Path, help="one seed spec; default: the seeds of the installed profiles")
    parser.add_argument("--profile", help="seed only this installed profile's specs")
    parser.add_argument("--json", action="store_true", help="print a JSON report")
    args = parser.parse_args(argv)
    if not args.bundle.is_dir():
        return fail(TOOL, f"{args.bundle} is not a directory")
    profiles = P.installed(args.profile)
    if args.profile and not profiles:
        return fail(TOOL, f"no installed profile is called {args.profile}")
    report = seed(args.bundle, args.spec, profiles)
    if not args.json:
        print(f"{report.extra['terms']} terms in place, {len(report.extra['written'])} written")
    report.print(args.json)
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
