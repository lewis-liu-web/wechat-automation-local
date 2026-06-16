"""Project-local skills for agent workers."""

from pathlib import Path
from typing import Dict


ROOT = Path(__file__).resolve().parent


def load_skill(skill_name: str) -> str:
    """Load skill markdown text by name.

    Skills are stored under ``<this_package>/<skill_name>/skill.md``.
    """
    path = ROOT / skill_name / "skill.md"
    if not path.exists():
        raise FileNotFoundError(f"Skill not found: {skill_name} ({path})")
    return path.read_text(encoding="utf-8")


def list_skills() -> Dict[str, Path]:
    """Return a mapping of skill name -> skill.md path."""
    out: Dict[str, Path] = {}
    for subdir in ROOT.iterdir():
        if subdir.is_dir() and (subdir / "skill.md").exists():
            out[subdir.name] = subdir / "skill.md"
    return out
