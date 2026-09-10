"""Versioned prompt templates — PRD sections 25 and 21.

The folder name for each version *is* the `prompt_version` value stored on
every `extraction_runs` row. That is what lets a production correction rate
(section 16) be attributed back to a specific prompt, so never edit a prompt
file in place after it has run against real documents — add a new version
folder instead.
"""

from __future__ import annotations

from pathlib import Path

#: The version new extraction runs use. Bump when adding a version folder.
CURRENT_PROMPT_VERSION = "v1"

_PROMPTS_ROOT = Path(__file__).parent


def load_prompt(name: str, version: str = CURRENT_PROMPT_VERSION) -> str:
    """Load a prompt template by name and version.

    Raises FileNotFoundError rather than falling back to another version: a
    silently-substituted prompt would make `prompt_version` a lie, and that
    column is the join key for the entire eval story.
    """
    path = _PROMPTS_ROOT / version / f"{name}.txt"
    if not path.is_file():
        raise FileNotFoundError(f"No prompt {name!r} in version {version!r} (looked at {path})")
    return path.read_text(encoding="utf-8")


def available_versions() -> list[str]:
    return sorted(p.name for p in _PROMPTS_ROOT.iterdir() if p.is_dir() and p.name != "__pycache__")
