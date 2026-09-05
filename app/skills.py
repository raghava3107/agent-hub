from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import yaml

from .settings import SKILL_SOURCES


@dataclass
class Skill:
    name: str
    description: str
    kind: str
    source: str
    path: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "source": self.source,
            "path": self.path,
        }


FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _parse(md_path: Path) -> Optional[dict]:
    try:
        text = md_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    m = FM_RE.match(text)
    if not m:
        return {"name": md_path.stem, "description": ""}
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {"name": md_path.stem, "description": ""}
    if not isinstance(fm, dict):
        return {"name": md_path.stem, "description": ""}
    return fm


def discover_skills() -> List[Skill]:
    """Scan every SKILL_SOURCES dir. Dedupe by resolved absolute path so
    symlinks (per-file OR whole-dir) don't produce duplicates."""
    skills: List[Skill] = []
    seen_paths: set[str] = set()
    seen_pairs: set[str] = set()
    for kind, directory in SKILL_SOURCES:
        if not directory.exists():
            continue
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            md_files: List[Path] = []
            if entry.is_file() and entry.suffix == ".md":
                md_files = [entry]
            elif entry.is_dir():
                skill_md = entry / "SKILL.md"
                if skill_md.exists():
                    md_files = [skill_md]
            for md in md_files:
                try:
                    resolved = str(md.resolve())
                except OSError:
                    continue
                if resolved in seen_paths:
                    continue
                fm = _parse(md)
                if fm is None:
                    continue
                name = str(fm.get("name") or md.stem)
                pair_key = f"{kind}:{name}"
                if pair_key in seen_pairs:
                    continue
                seen_paths.add(resolved)
                seen_pairs.add(pair_key)
                skills.append(
                    Skill(
                        name=name,
                        description=str(fm.get("description") or "").strip(),
                        kind=kind,
                        source=str(directory),
                        path=str(md),
                    )
                )
    return skills


def find_skill(name: str, kind: Optional[str] = None) -> Optional[Skill]:
    for s in discover_skills():
        if s.name == name and (kind is None or s.kind == kind):
            return s
    return None
