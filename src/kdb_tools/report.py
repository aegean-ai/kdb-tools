"""Findings and the report every tool prints."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field


@dataclass
class Finding:
    rule: str
    level: str  # "error" or "warning"
    path: str
    message: str
    source_id: str | None = None


@dataclass
class Report:
    tool: str
    findings: list[Finding] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def error(self, rule: str, path: str, message: str, source_id: str | None = None) -> None:
        self.findings.append(Finding(rule, "error", path, message, source_id))

    def warning(self, rule: str, path: str, message: str, source_id: str | None = None) -> None:
        self.findings.append(Finding(rule, "warning", path, message, source_id))

    @property
    def errors(self) -> int:
        return sum(f.level == "error" for f in self.findings)

    @property
    def warnings(self) -> int:
        return sum(f.level == "warning" for f in self.findings)

    def as_dict(self) -> dict:
        return {
            "tool": self.tool,
            "ok": self.errors == 0,
            "errors": self.errors,
            "warnings": self.warnings,
            **self.extra,
            "findings": [{k: v for k, v in asdict(f).items() if v is not None} for f in self.findings],
        }

    def print(self, as_json: bool) -> None:
        if as_json:
            print(json.dumps(self.as_dict(), indent=2, ensure_ascii=False))
            return
        for f in sorted(self.findings, key=lambda f: (f.path, f.level, f.rule)):
            where = f"{f.path} [{f.source_id}]" if f.source_id else f.path
            print(f"{f.level.upper():7} {f.rule:22} {where}: {f.message}")
        print(f"{self.tool}: {self.errors} errors, {self.warnings} warnings")

    def exit_code(self) -> int:
        return 1 if self.errors else 0


def fail(tool: str, message: str) -> int:
    """The tool could not run at all."""
    print(f"{tool}: {message}", file=sys.stderr)
    return 2
