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

def render_skill(skill_name: str, **variables) -> str:
    """Load a skill template and render it with the given variables.

    Uses Jinja2 when available; otherwise falls back to simple string
    replacement for ``{{variable}}`` style placeholders. Missing variables
    are replaced with an empty string.
    """
    template_text = load_skill(skill_name)
    try:
        from jinja2 import Template
        return Template(template_text).render(**variables)
    except ImportError:  # pragma: no cover - jinja2 is optional
        rendered = template_text
        for key, value in variables.items():
            rendered = rendered.replace(f"{{{{{key}}}}}", str(value or ""))
        # Strip any leftover placeholders so missing variables become empty.
        import re
        rendered = re.sub(r"\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}", "", rendered)
        return rendered


def list_skills() -> Dict[str, Path]:
    """Return a mapping of skill name -> skill.md path."""
    out: Dict[str, Path] = {}
    for subdir in ROOT.iterdir():
        if subdir.is_dir() and (subdir / "skill.md").exists():
            out[subdir.name] = subdir / "skill.md"
    return out
