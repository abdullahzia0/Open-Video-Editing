"""Inspect imports, local Markdown links, JSON, YAML, and skill metadata."""

import importlib
import json
import pkgutil
import re
from pathlib import Path

import yaml

import ove

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".venv", ".git", ".tools", ".ove", "dist", "__pycache__"}


def main() -> None:
    problems = []
    modules = list(pkgutil.walk_packages(ove.__path__, ove.__name__ + "."))
    for module in modules:
        importlib.import_module(module.name)
    checked = 0
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(
            part in SKIP or part.endswith("_cache") for part in path.parts
        ):
            continue
        checked += 1
        if path.suffix == ".json":
            json.loads(path.read_text())
        if path.suffix in {".yaml", ".yml"}:
            yaml.safe_load(path.read_text())
        if path.suffix == ".md":
            text = path.read_text()
            # Only inspect actual link syntax, not the proposal's illustrative directory tree.
            for link in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
                target = link.split("#", 1)[0]
                if not target or "://" in target or target.startswith("mailto:"):
                    continue
                if not (path.parent / target).exists():
                    problems.append(f"{path.relative_to(ROOT)}: missing link {target}")
            if path.name == "SKILL.md":
                parts = text.split("---", 2)
                metadata = yaml.safe_load(parts[1]) if len(parts) == 3 else {}
                if metadata.get("name") != path.parent.name or not metadata.get("description"):
                    problems.append(f"Invalid skill metadata: {path}")
    if problems:
        raise SystemExit("\n".join(problems))
    print(f"Checked {checked} files and imported {len(modules)} modules.")


if __name__ == "__main__":
    main()
