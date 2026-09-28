"""Project roots touched by this process; dependency-free so storage/leases can record them."""
from __future__ import annotations

from pathlib import Path

_projects: set[Path] = set()


def remember_project(path):
    """Only remember explicit project roots touched by this process."""
    try:
        path = Path(path).absolute()
        if path.resolve() == path and ((path / '.cowmata-farm.json').is_file()
                                      or (path / '资源索引.json').is_file()):
            _projects.add(path)
    except OSError:
        pass  # Optional housekeeping must never prevent opening/saving data.
