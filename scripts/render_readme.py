#!/usr/bin/env python3
"""Render README.md from docs/README.template.md.

README.md is a generated file.  Edit the template, then run this script (or let the
"README" GitHub workflow do it when ``__version__`` changes on main).

The values come from the files that own them, so nothing is typed twice:

    {{VERSION}}        __version__ in src/imgstrip/__init__.py            (1.2.2)
    {{VERSION_BADGE}}  the same, escaped for a shields.io static badge   (1.2.2)
    {{PYTHON_MIN}}     requires-python in pyproject.toml, lower bound    (3.11)

Standard library only, and it never imports imgstrip, so it runs before anything is installed.

    python scripts/render_readme.py            write README.md
    python scripts/render_readme.py --check    exit 1 if README.md is out of date (for CI)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
TEMPLATE_NOTES = re.compile(r"\A\s*<!--\s*template-only.*?-->\s*", re.DOTALL)
GENERATED_NOTICE = (
    "<!-- GENERATED FILE. Edit docs/README.template.md and run `python scripts/render_readme.py`; "
    "changes made here are overwritten. -->\n"
)


def read_version(root: Path) -> str:
    text = (root / "src" / "imgstrip" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r"""^__version__\s*=\s*["']([^"']+)["']\s*$""", text, re.MULTILINE)
    if not match:
        raise SystemExit("render_readme: no __version__ = \"...\" line in src/imgstrip/__init__.py")
    return match.group(1)


def read_python_min(root: Path) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"""^requires-python\s*=\s*["']\s*>=\s*([0-9][0-9.]*)""", text, re.MULTILINE)
    if not match:
        raise SystemExit("render_readme: no requires-python = \">=X.Y\" line in pyproject.toml")
    return match.group(1)


def badge_escape(value: str) -> str:
    """shields.io static badges read '-' and '_' as separators; a literal one is written doubled."""
    return value.replace("-", "--").replace("_", "__")


def values(root: Path) -> dict[str, str]:
    version = read_version(root)
    return {
        "VERSION": version,
        "VERSION_BADGE": badge_escape(version),
        "PYTHON_MIN": read_python_min(root),
    }


def render(template: str, data: dict[str, str]) -> str:
    template = template.replace("\r\n", "\n")
    # A leading "<!-- template-only ... -->" comment is notes for template editors; it is dropped, and the
    # generated file gets its own notice instead (the notes say "this file", which would be wrong there).
    template = TEMPLATE_NOTES.sub("", template, count=1)
    unknown = sorted({m.group(1) for m in PLACEHOLDER.finditer(template)} - data.keys())
    if unknown:
        raise SystemExit(
            "render_readme: unknown placeholder(s) in the template: "
            + ", ".join("{{" + name + "}}" for name in unknown)
            + f" (known: {', '.join('{{' + k + '}}' for k in data)})"
        )
    body = PLACEHOLDER.sub(lambda m: data[m.group(1)], template).strip("\n")
    return GENERATED_NOTICE + body + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render README.md from docs/README.template.md.")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 if README.md is out of date")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this script's repository)")
    args = parser.parse_args(argv)

    root: Path = args.root.resolve()
    template_path = root / "docs" / "README.template.md"
    readme_path = root / "README.md"
    data = values(root)
    rendered = render(template_path.read_text(encoding="utf-8"), data)
    current = readme_path.read_text(encoding="utf-8").replace("\r\n", "\n") if readme_path.exists() else None

    if args.check:
        if current == rendered:
            print(f"README.md is up to date (version {data['VERSION']}).")
            return 0
        print(
            "README.md is out of date: run `python scripts/render_readme.py` and commit the result "
            "(edit docs/README.template.md, never README.md).",
            file=sys.stderr,
        )
        return 1

    if current == rendered:
        print(f"README.md already up to date (version {data['VERSION']}).")
        return 0
    readme_path.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"README.md written (version {data['VERSION']}, Python >= {data['PYTHON_MIN']}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
