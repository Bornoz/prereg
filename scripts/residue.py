#!/usr/bin/env python3
"""Scan a repository for traces left by assistant tooling.

Written after checking three of my own repositories and finding that 515 of 651
commits in one of them carried a `Co-Authored-By:` trailer I had never looked at.
The traces are rarely in the code. They are in the commit trailers, the tracked
config directories, and a handful of words that show up in generated prose far
more often than in anybody's own.

    python scripts/residue.py [path]

Exit status is 1 if anything was found, so it works as a pre-push hook.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

TRAILERS = re.compile(
    r"co-authored-by:\s*(claude|gpt|copilot|cursor|codex|gemini)"
    r"|generated with .{0,24}(claude|copilot|cursor|codex)"
    r"|(claude|chatgpt)\.(ai|com)/",
    re.IGNORECASE,
)

TRACKED_PATHS = re.compile(
    r"(^|/)(CLAUDE\.md|AGENTS\.md|GEMINI\.md|\.claude/|\.cursor/|\.aider|"
    r"\.github/copilot-instructions\.md)",
)

# Pictographs and dingbats only. Arrows are deliberately not in here: a subject
# like "timeout 2500->800" written with a real arrow is ordinary punctuation, and
# a scanner that calls it residue stops being worth running.
EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿️⭐⭕]")

# Words that are unremarkable once and a tell in quantity.
TELLS = (
    "comprehensive", "robust", "seamless", "leverage", "delve", "elevate",
    "cutting-edge", "state-of-the-art", "best-in-class", "unlock the power",
    "in today's fast-paced", "it's not just", "game-changer", "supercharge",
)

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}


def git(repo: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def check_commits(repo: Path) -> list[str]:
    log = git(repo, "log", "--format=%H%x1f%s%x1f%b%x1e")
    if not log:
        return []
    found = []
    total = 0
    trailer_hits = []
    emoji_hits = []
    for entry in log.split("\x1e"):
        entry = entry.strip("\n")
        if not entry:
            continue
        total += 1
        sha, _, rest = entry.partition("\x1f")
        subject, _, body = rest.partition("\x1f")
        if TRAILERS.search(subject + "\n" + body):
            trailer_hits.append(sha[:8])
        if EMOJI.search(subject):
            emoji_hits.append(f"{sha[:8]} {subject[:60]}")
    if trailer_hits:
        found.append(
            f"{len(trailer_hits)} of {total} commits carry an assistant trailer "
            f"(e.g. {', '.join(trailer_hits[:4])})"
        )
    for hit in emoji_hits[:5]:
        found.append(f"emoji in commit subject: {hit}")
    if total and total < 3:
        found.append(
            f"only {total} commit(s); a single squashed history reads as generated"
        )
    return found


def check_tracked(repo: Path) -> list[str]:
    listing = git(repo, "ls-files")
    hits = [p for p in listing.splitlines() if TRACKED_PATHS.search(p)]
    return [f"tracked assistant config: {p}" for p in hits[:10]]


def check_text(root: Path) -> list[str]:
    found = []
    here = Path(__file__).resolve()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in {".md", ".py", ".txt", ".toml"}:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        # This file has to contain every word it looks for.
        if path.resolve() == here:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(root)
        lower = text.lower()
        for word in TELLS:
            count = lower.count(word)
            if count:
                found.append(f"{rel}: {count}x {word!r}")
        for line_no, line in enumerate(text.splitlines(), 1):
            # The trailer pattern belongs here too, not only in commit messages.
            # A pasted transcript or a copied changelog carries it into a file,
            # and the first version of this scanner walked straight past that.
            if TRAILERS.search(line):
                found.append(f"{rel}:{line_no}: assistant trailer in file content")
            if EMOJI.search(line):
                found.append(f"{rel}:{line_no}: emoji in text")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", default=".", type=Path)
    args = parser.parse_args()
    root = args.path.resolve()

    groups = {
        "commits": check_commits(root),
        "tracked files": check_tracked(root),
        "prose": check_text(root),
    }
    total = sum(len(v) for v in groups.values())

    for name, findings in groups.items():
        if findings:
            print(f"{name}:")
            for item in findings:
                print(f"  {item}")

    if total:
        print(f"\n{total} finding(s). Commit trailers cannot be edited without "
              f"rewriting history, so the cheapest time to fix this is now.")
        return 1
    print("clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
