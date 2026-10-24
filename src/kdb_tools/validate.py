"""okf-validate: check a kDB bundle's structure against OKF v0.2 and the project rules.

It reads only markdown and frontmatter. It never opens a PDF; okf-ground does that. The core
rules hold for every kDB; each installed profile (see kdb_tools.profile) adds its own.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

from . import bundle as B
from . import profile as P
from . import sheet as S
from .report import Report, fail

TOOL = "okf-validate"
SENTENCE_KEYS = ("id", "resource", "page", "quote", "bbox")
CELL_KEYS = ("id", "resource", "page", "table", "row", "column", "value", "bbox")
SHEET_KEYS = ("id", "resource", "cell", "value")  # plus sheet, for a workbook with named sheets
ABBREVIATIONS = ("et al.", "e.g.", "i.e.", "vs.", "Fig.", "Eq.", "Sec.", "Tab.", "approx.", "resp.")
LINK = re.compile(r"\]\(([^)\s]+)")
SENTENCE = re.compile(r".+?[.!?](?:\[\^[^\]\s]+\])*(?=\s|$)|.+$", re.DOTALL)


def normalize_name(name: str) -> str:
    name = unicodedata.normalize("NFKC", name).replace("π", "pi").replace("Π", "pi").lower()
    return re.sub(r"[^0-9a-z]", "", name)


def is_datetime(value) -> bool:
    if isinstance(value, dt.datetime):
        return True
    if not isinstance(value, str) or "T" not in value:
        return False
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def is_bbox(value) -> bool:
    if not isinstance(value, list) or len(value) != 4:
        return False
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        return False
    x0, y0, x1, y1 = value
    return x1 > x0 and y1 > y0


def prose_sentences(body: str):
    """Yield the sentences of prose paragraphs: not headings, lists, tables, quotes or code."""
    text = B.strip_code(body)
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.strip().splitlines() if ln.strip()]
        if not lines:
            continue
        first = lines[0].lstrip()
        if re.match(r"(#|[-*+] |\d+[.)] |\||>|<|\[\^[^\]]+\]:)", first):
            continue
        para = " ".join(ln.strip() for ln in lines)
        masked = para
        for abbr in ABBREVIATIONS:
            masked = masked.replace(abbr, abbr.replace(".", "\0"))
        for match in SENTENCE.finditer(masked):
            sentence = match.group(0).replace("\0", ".").strip()
            if sentence:
                yield sentence


def check_index_files(b: B.Bundle, report: Report) -> None:
    for page in b.indexes:
        is_root = page.rel == "index.md"
        if page.error:
            report.error("okf.index", page.rel, page.error)
        elif page.frontmatter is not None:
            extra = set(page.frontmatter) - ({"okf_version"} if is_root else set())
            if extra:
                where = "a bundle-root index.md may carry only okf_version" if is_root else "an index.md carries no frontmatter"
                report.error("okf.index", page.rel, f"{where}; found {', '.join(sorted(extra))}")
    for page in b.logs:
        for heading in re.findall(r"^##\s+(.+?)\s*$", page.text, re.MULTILINE):
            try:
                dt.date.fromisoformat(heading)
            except ValueError:
                report.error("okf.log", page.rel, f"date heading '{heading}' is not YYYY-MM-DD")
    if not (b.root / "index.md").is_file():
        report.error("project.index", "index.md", "the bundle root has no index.md")
    if not (b.root / "log.md").is_file():
        report.error("project.log", "log.md", "the bundle root has no log.md")
    for page in b.logs:
        if page.rel == "log.md" and not re.search(r"^##\s+\S", page.text, re.MULTILINE):
            report.error("project.log", page.rel, "log.md has no '## YYYY-MM-DD' date headings; put each entry under the date it was made")


def listed_pages(b: B.Bundle) -> set[Path]:
    listed = set()
    for page in b.indexes:
        for target in LINK.findall(page.text):
            target = target.split("#", 1)[0]
            if target:
                path = B.resolve(b.root, page.rel, target)
                if path is not None:
                    listed.add(path)
    return listed


def missing_resource(b: B.Bundle, page: B.Page, resource: str, path: Path) -> str:
    try:
        where = path.relative_to(b.root.resolve()).as_posix()
    except ValueError:
        where = str(path)
    msg = f"resource '{resource}' does not exist in the bundle (looked for {where})"
    if not resource.startswith("/") and (b.root / resource).is_file():
        msg += f"; a path without a leading / is relative to this page, so write '/{resource}'"
    return msg


def check_source(b: B.Bundle, page: B.Page, source, seen: set, report: Report) -> None:
    sid = source.get("id") if isinstance(source, dict) else None
    label = sid if isinstance(sid, str) else None
    if not isinstance(source, dict):
        report.error("project.source", page.rel, "a sources entry is not a mapping")
        return
    if "cell" in source:
        kind, keys = "spreadsheet cell", SHEET_KEYS
    elif "quote" in source:
        kind, keys = "sentence", SENTENCE_KEYS
    elif "value" in source or "table" in source:
        kind, keys = "table cell", CELL_KEYS
    else:
        report.error("project.source", page.rel, "a source needs a quote (sentence), a table, row, column and value "
                     "(table cell), or a cell and value (spreadsheet cell)", label)
        return
    missing = [k for k in keys if k not in source]
    if missing:
        report.error("project.source", page.rel, f"a {kind} source is missing {', '.join(missing)}", label)
    if label is None:
        return
    if label in seen:
        report.error("project.source", page.rel, f"source id '{label}' appears twice on this page", label)
    seen.add(label)
    if "page" in source and not (isinstance(source["page"], int) and source["page"] >= 1):
        report.error("project.source", page.rel, "page is a page number counted from 1", label)
    if "bbox" in source and not is_bbox(source["bbox"]):
        report.error("project.source", page.rel, "bbox is [x0, y0, x1, y1] in PDF points with x1 > x0 and y1 > y0", label)
    if kind == "table cell":
        for key in ("table", "row", "column"):
            if key in source and not (isinstance(source[key], int) and source[key] >= 1):
                report.error("project.source", page.rel, f"{key} is a number counted from 1", label)
    resource = source.get("resource")
    if isinstance(resource, str):
        path = B.resolve(b.root, page.rel, resource)
        if path is not None and not path.is_file():
            report.error("project.source", page.rel, missing_resource(b, page, resource, path), label)
        if kind == "spreadsheet cell" and path is not None:
            check_sheet_source(page, source, path, label, report)
    elif "resource" in source:
        report.error("project.source", page.rel, "resource is a path", label)


def check_sheet_source(page: B.Page, source: dict, path: Path, label: str, report: Report) -> None:
    if path.suffix.lower() not in S.SUFFIXES:
        report.error("project.source", page.rel, f"a spreadsheet cell source cites a {' or '.join(S.SUFFIXES)} file, "
                     f"not '{source['resource']}'", label)
        return
    if "cell" in source and not S.is_a1(source["cell"]):
        report.error("project.source", page.rel, "cell is one A1 reference in capitals, such as B4", label)
    if S.needs_sheet(path) and not isinstance(source.get("sheet"), str):
        report.error("project.source", page.rel, "a cell in an .xlsx file needs the name of its sheet", label)


def check_concept(b: B.Bundle, page: B.Page, listed: set[Path], all_ids: set[str], report: Report) -> None:
    fm = page.frontmatter
    if page.error or fm is None:
        report.error("okf.frontmatter", page.rel, page.error or "the file has no YAML frontmatter block")
        return
    if not isinstance(fm.get("type"), str) or not fm["type"].strip():
        report.error("okf.type", page.rel, "the frontmatter has no non-empty type")
    if page.path.resolve() not in listed:
        report.error("project.index", page.rel, "no index.md links to this page")

    gen = fm.get("generated")
    if not isinstance(gen, dict) or not gen.get("by") or not is_datetime(gen.get("at")):
        report.error("project.generated", page.rel, "generated needs by (an actor) and at (an ISO 8601 datetime)")

    raw_sources = fm.get("sources", [])
    if not isinstance(raw_sources, list):
        report.error("project.source", page.rel, "sources is a list")
        raw_sources = []
    seen: set[str] = set()
    for source in raw_sources:
        check_source(b, page, source, seen, report)

    cited = page.citations()
    defined = page.definitions()
    for label in dict.fromkeys(cited):
        if label not in seen:
            report.error("project.citation", page.rel, f"footnote [^{label}] does not match a source id on this page", label)
        elif label not in defined:
            report.warning("project.footnote", page.rel, f"footnote [^{label}] has no definition line", label)
    for label in sorted(seen - set(cited)):
        report.warning("project.unused-source", page.rel, f"source '{label}' is never cited", label)
    for sentence in prose_sentences(page.body):
        if "[^" not in sentence:
            report.warning("project.uncited", page.rel, f"sentence has no citation: {sentence[:80]}")

    aliases = fm.get("aliases", [])
    if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
        report.error("project.aliases", page.rel, "aliases is a list of strings")

    if "contested_by" in fm:
        contested = fm["contested_by"]
        if not isinstance(contested, dict):
            report.error("project.contested", page.rel, "contested_by maps a source id on this page to a list of source ids")
        else:
            for key, disputers in contested.items():
                if key not in seen:
                    report.error("project.contested", page.rel, f"contested_by key '{key}' is not a source id on this page", str(key))
                if not isinstance(disputers, list) or not disputers:
                    report.error("project.contested", page.rel, f"contested_by '{key}' needs a list of source ids", str(key))
                    continue
                for d in disputers:
                    if d not in all_ids:
                        report.error("project.contested", page.rel, f"'{d}' is not a source id anywhere in the bundle", str(key))

    if fm.get("type") == "Attested Computation":
        if fm.get("runtime") != "python":
            report.error("project.computation", page.rel, "an Attested Computation has runtime: python")
        comp = fm.get("computation")
        if comp is not None:
            path = B.resolve(b.root, page.rel, comp) if isinstance(comp, str) else None
            if path is None or not path.is_file():
                report.error("project.computation", page.rel, f"computation file '{comp}' does not exist")
        elif not re.search(r"^#\s+Computation\s*$.*?^```(?:python|py)\s*$", page.body, re.MULTILINE | re.DOTALL):
            report.error("project.computation", page.rel, "no computation file and no python block under # Computation")


def term_domain(rel: str) -> str | None:
    """ontology/<domain>/... gives the domain; any other path gives None."""
    parts = rel.split("/")
    return parts[1] if len(parts) >= 3 and parts[0] == "ontology" else None


def is_term(page: B.Page) -> bool:
    return (page.frontmatter or {}).get("type") == "Term"


def term_at(b: B.Bundle, page: B.Page, value) -> B.Page | None:
    """The Term page a path-valued field points at, or None."""
    if not isinstance(value, str):
        return None
    path = B.resolve(b.root, page.rel, value)
    target = next((p for p in b.concepts if path is not None and p.path.resolve() == path), None)
    return target if target is not None and is_term(target) and term_domain(target.rel) else None




def ontology_graph(b: B.Bundle, report: Report | None = None) -> dict[str, set[str]]:
    """Each domain's ancestors: the ontologies it extends, directly or through others.

    An ontology is declared by ontology/<domain>/ontology.md, a page of type Ontology whose
    extends field lists the ontology pages it specializes.
    """
    direct: dict[str, list[str]] = {}
    for page in b.concepts:
        fm = page.frontmatter or {}
        if fm.get("type") != "Ontology":
            continue
        domain = term_domain(page.rel)
        if domain is None or page.rel != f"ontology/{domain}/ontology.md":
            if report:
                report.error("project.ontology", page.rel, "an Ontology page is ontology/<domain>/ontology.md")
            continue
        direct[domain] = []
        extends = fm.get("extends", [])
        if not isinstance(extends, list):
            if report:
                report.error("project.ontology", page.rel, "extends is a list of Ontology page paths")
            continue
        for value in extends:
            path = B.resolve(b.root, page.rel, value) if isinstance(value, str) else None
            target = next((q for q in b.concepts if path is not None and q.path.resolve() == path), None)
            if target is None or (target.frontmatter or {}).get("type") != "Ontology":
                if report:
                    report.error("project.ontology", page.rel, f"extends '{value}' is not an Ontology page")
                continue
            direct[domain].append(term_domain(target.rel))
    ancestors: dict[str, set[str]] = {}
    for domain in direct:
        seen, stack = set(), list(direct[domain])
        while stack:
            d = stack.pop()
            if d == domain:
                if report:
                    report.error("project.ontology", f"ontology/{domain}/ontology.md", "ontologies extend each other in a cycle")
                break
            if d not in seen:
                seen.add(d)
                stack.extend(direct.get(d, []))
        ancestors[domain] = seen - {domain}
    return ancestors


def in_family(ancestors: dict[str, set[str]], domain: str | None, root: str) -> bool:
    """True when domain is root or an ontology that extends root."""
    return domain is not None and (domain == root or root in ancestors.get(domain, set()))


def check_terms(b: B.Bundle, report: Report) -> None:
    ancestors = ontology_graph(b, report)
    terms = [p for p in b.concepts if is_term(p)]
    broader: dict[str, list[str]] = {}
    labels: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for page in terms:
        fm = page.frontmatter or {}
        domain = term_domain(page.rel)
        if domain is None:
            report.error("project.term", page.rel, "a Term page lives under ontology/<domain>/, such as ontology/robotics/tasks/")
            continue
        if not isinstance(fm.get("pref_label"), str) or not fm["pref_label"].strip():
            report.error("project.term", page.rel, "a Term needs pref_label")
        alts = fm.get("alt_labels", [])
        if not isinstance(alts, list) or not all(isinstance(a, str) for a in alts):
            report.error("project.term", page.rel, "alt_labels is a list of strings")
            alts = []
        for name in [fm.get("pref_label")] + alts:
            if isinstance(name, str) and normalize_name(name):
                labels[(domain, normalize_name(name))].append((page.rel, name))
        parents = fm.get("broader", [])
        if not isinstance(parents, list):
            report.error("project.term", page.rel, "broader is a list of Term paths")
            parents = []
        broader[page.rel] = []
        for value in parents:
            target = term_at(b, page, value)
            if target is None:
                report.error("project.term", page.rel, f"broader '{value}' is not a Term page")
            elif term_domain(target.rel) != domain and term_domain(target.rel) not in ancestors.get(domain, set()):
                report.error("project.term", page.rel,
                             f"broader '{value}' is in the {term_domain(target.rel)} ontology, which the {domain} ontology does not extend")
            else:
                broader[page.rel].append(target.rel)
    for start in broader:
        seen, stack = set(), list(broader[start])
        while stack:
            node = stack.pop()
            if node == start:
                report.error("project.term", start, "broader terms form a cycle back to this term")
                break
            if node not in seen:
                seen.add(node)
                stack.extend(broader.get(node, []))
    by_label: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for (domain, key), entries in labels.items():
        for rel, name in entries:
            by_label[key].append((domain, rel, name))
    for key, entries in sorted(by_label.items()):
        for domain, rel, name in entries:
            clash = sorted({r for d, r, _ in entries if r != rel and
                            (d == domain or d in ancestors.get(domain, set()) or domain in ancestors.get(d, set()))})
            if clash:
                names = ", ".join(sorted({n for _, _, n in entries}))
                report.error("project.term-label", rel,
                             f"the label {names} also names {', '.join(clash)} in the {domain} ontology or one it extends or is extended by")


def check_entities(b: B.Bundle, report: Report) -> None:
    owners: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for page in b.concepts:
        fm = page.frontmatter or {}
        if is_term(page):
            continue  # terms are checked per domain by check_terms
        names = [fm.get("title")] + (fm.get("aliases") if isinstance(fm.get("aliases"), list) else [])
        for name in names:
            if isinstance(name, str) and normalize_name(name):
                owners[normalize_name(name)].append((page.rel, name))
    for key, entries in sorted(owners.items()):
        pages = sorted({rel for rel, _ in entries})
        if len(pages) > 1:
            names = ", ".join(sorted({name for _, name in entries}))
            for rel in pages:
                others = ", ".join(p for p in pages if p != rel)
                report.error("project.entity", rel, f"the name {names} also names {others}; one entity gets one page")


def ancestors(b: B.Bundle) -> dict[str, set[str]]:
    """Each ontology domain mapped to the domains it extends, directly or not; computed once per bundle."""
    found = getattr(b, "_ancestors", None)
    if found is None:
        found = b._ancestors = ontology_graph(b)
    return found


def validate(root: Path, profiles: list[P.Profile] | None = None, pages: list[str] | None = None) -> Report:
    """The core rules, then each profile's page rules; profiles default to the installed ones.

    With pages, only findings on those pages (paths from the bundle root) are reported, so an
    ingest answers for the pages it wrote and not for errors an earlier ingest left."""
    report = Report(TOOL)
    b = B.load(root)
    profiles = P.installed() if profiles is None else profiles
    check_index_files(b, report)
    listed = listed_pages(b)
    all_ids = set().union(*(p.source_ids for p in b.concepts)) if b.concepts else set()
    for page in b.concepts:
        check_concept(b, page, listed, all_ids, report)
        if page.frontmatter is not None and not page.error:
            for prof in profiles:
                if prof.check_page:
                    prof.check_page(b, page, report)
    check_entities(b, report)
    check_terms(b, report)
    report.extra["pages"] = len(b.concepts)
    if pages is not None:
        wanted = {p.lstrip("/") for p in pages}
        report.findings = [f for f in report.findings if f.path in wanted]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=TOOL, description=__doc__.splitlines()[0])
    parser.add_argument("bundle", type=Path, help="the bundle root directory")
    parser.add_argument("--json", action="store_true", help="print a JSON report")
    parser.add_argument("--profile", help="run only this installed profile's rules; default: every installed profile")
    parser.add_argument("--pages", nargs="+", metavar="PAGE", help="report only findings on these pages, such as models/openvla.md")
    args = parser.parse_args(argv)
    if not args.bundle.is_dir():
        return fail(TOOL, f"{args.bundle} is not a directory")
    profiles = P.installed(args.profile)
    if args.profile and not profiles:
        return fail(TOOL, f"no installed profile is called {args.profile}")
    report = validate(args.bundle, profiles, args.pages)
    report.print(args.json)
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
