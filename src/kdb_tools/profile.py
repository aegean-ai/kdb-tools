"""Profiles: what a project adds to the core checks.

A profile names a project's own page rules and the directory of its seed specs. A project
registers one as a Python entry point in the group "kdb_tools.profiles", for example

    [project.entry-points."kdb_tools.profiles"]
    vla = "physical_ai_kdb:PROFILE"

okf-validate runs the core rules on every page and then each installed profile's check_page;
okf-seed writes the seeds of the installed profiles. With no profile installed, only the core
rules run.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata
from importlib.resources.abc import Traversable
from pathlib import Path

GROUP = "kdb_tools.profiles"


@dataclass
class Profile:
    name: str
    check_page: Callable | None = None  # (bundle, page, report) -> None, for each concept page
    seeds: Traversable | Path | None = None  # a directory of seed spec YAML files


def installed(name: str | None = None) -> list[Profile]:
    """The installed profiles, or only the one called name."""
    return [ep.load() for ep in metadata.entry_points(group=GROUP) if name is None or ep.name == name]
