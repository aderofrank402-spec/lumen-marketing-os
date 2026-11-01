from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES = (
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "CHANGELOG.md",
    ".env.example",
    ".github/workflows/ci.yml",
    "docs/PORTFOLIO_ARCHITECTURE.md",
    "docs/CONTROL_MODEL.md",
    "docs/OPERATOR_WALKTHROUGH.md",
    "docs/ENGINEERING_DECISIONS.md",
)

FORBIDDEN_TRACKED_NAMES = {".env", "lmos.db", "lmos.sqlite", "lmos.sqlite3"}
SECRET_PATTERNS = {
    "OpenAI-style API key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "GitHub token": re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}

MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def fail(message: str, failures: list[str]) -> None:
    failures.append(message)


def iter_public_text_files() -> list[Path]:
    suffixes = {".md", ".py", ".toml", ".json", ".yml", ".yaml", ".sql", ".ps1", ".example"}
    return [
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and path.suffix.lower() in suffixes
    ]


def validate_required_files(failures: list[str]) -> None:
    for relative in REQUIRED_FILES:
        if not (ROOT / relative).is_file():
            fail(f"missing required file: {relative}", failures)


def validate_tracked_artifacts(failures: list[str]) -> None:
    for path in ROOT.rglob("*"):
        if ".git" in path.parts or not path.is_file():
            continue
        if path.name in FORBIDDEN_TRACKED_NAMES:
            fail(f"forbidden runtime or secret file: {path.relative_to(ROOT)}", failures)
        if "runtime" in path.relative_to(ROOT).parts:
            fail(f"runtime artifact present: {path.relative_to(ROOT)}", failures)


def validate_secrets(files: list[Path], failures: list[str]) -> None:
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                fail(f"possible {label} in {path.relative_to(ROOT)}", failures)


def validate_markdown_links(failures: list[str]) -> None:
    for document in ROOT.rglob("*.md"):
        if ".git" in document.parts:
            continue
        text = document.read_text(encoding="utf-8")
        for raw_target in MARKDOWN_LINK.findall(text):
            target = raw_target.strip().split("#", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            target = unquote(target.strip("<>"))
            resolved = (document.parent / target).resolve()
            if not resolved.exists():
                fail(
                    f"broken relative link in {document.relative_to(ROOT)}: {raw_target}",
                    failures,
                )


def validate_claim_boundaries(failures: list[str]) -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8").lower()
    for phrase in ("dry-run by default", "exact content hash", "scope and limitations"):
        if phrase not in readme:
            fail(f"README is missing operating boundary: {phrase}", failures)

    development = (ROOT / "config/development.json").read_text(encoding="utf-8")
    if '"dry_run": true' not in development:
        fail("development configuration must remain dry-run", failures)


def main() -> int:
    failures: list[str] = []
    files = iter_public_text_files()
    validate_required_files(failures)
    validate_tracked_artifacts(failures)
    validate_secrets(files, failures)
    validate_markdown_links(failures)
    validate_claim_boundaries(failures)

    if failures:
        print("Public repository validation failed:")
        for item in failures:
            print(f"- {item}")
        return 1

    print(f"Public repository validation passed ({len(files)} text files inspected).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
