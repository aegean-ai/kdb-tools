"""Reading an OKF bundle: concept pages, reserved files, frontmatter and footnotes."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

RESERVED = {"index.md", "log.md"}
SKIP_DIRS = {".git", "papers", "annotated", "node_modules", ".venv"}

CITATION = re.compile(r"\[\^([^\]\s]+)\](?!:)")
DEFINITION = re.compile(r"^\[\^([^\]\s]+)\]:", re.MULTILINE)


class FrontmatterError(ValueError):
    pass


def split_frontmatter(text: str) -> tuple[dict | None, str]:
    """Return (frontmatter, body). Frontmatter is None when the file has no block."""
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 3)
    if end == -1:
        if text.endswith("\n---"):
            end = len(text) - 4
        else:
            raise FrontmatterError("the frontmatter block has no closing ---")
    raw = text[4:end]
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"the frontmatter is not valid YAML: {exc.problem or exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise FrontmatterError("the frontmatter is not a mapping")
    return data, text[end + 5 :]


@dataclass
class Page:
    rel: str  # path relative to the bundle root, with forward slashes
    path: Path
    text: str
    frontmatter: dict | None = None
    body: str = ""
    error: str | None = None  # why the frontmatter could not be read

    @property
    def sources(self) -> list:
        value = (self.frontmatter or {}).get("sources")
        return value if isinstance(value, list) else []

    @property
    def source_ids(self) -> set[str]:
        return {s["id"] for s in self.sources if isinstance(s, dict) and isinstance(s.get("id"), str)}

    def citations(self) -> list[str]:
        return CITATION.findall(strip_code(self.body))

    def definitions(self) -> set[str]:
        return set(DEFINITION.findall(strip_code(self.body)))


@dataclass
class Bundle:
    root: Path
    concepts: list[Page] = field(default_factory=list)
    indexes: list[Page] = field(default_factory=list)
    logs: list[Page] = field(default_factory=list)


def strip_code(body: str) -> str:
    """Blank out fenced code blocks so their contents are never read as prose or citations."""
    return re.sub(r"^(```|~~~).*?^\1[^\n]*$", "", body, flags=re.MULTILINE | re.DOTALL)


def resolve(root: Path, page_rel: str, value: str) -> Path | None:
    """Resolve a path-valued field (OKF section 6.2). URLs give None."""
    if re.match(r"^[a-z][a-z0-9+.-]*://", value):
        return None
    if value.startswith("/"):
        return (root / value.lstrip("/")).resolve()
    return (root / page_rel).parent.joinpath(value).resolve()


def read_page(root: Path, path: Path) -> Page:
    rel = path.relative_to(root).as_posix()
    text = path.read_text(encoding="utf-8")
    page = Page(rel=rel, path=path, text=text)
    try:
        page.frontmatter, page.body = split_frontmatter(text)
    except FrontmatterError as exc:
        page.error = str(exc)
        page.body = text
    return page


def load(root: Path) -> Bundle:
    bundle = Bundle(root=root)
    for path in sorted(root.rglob("*.md")):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        page = read_page(root, path)
        if path.name == "index.md":
            bundle.indexes.append(page)
        elif path.name == "log.md":
            bundle.logs.append(page)
        else:
            bundle.concepts.append(page)
    return bundle
