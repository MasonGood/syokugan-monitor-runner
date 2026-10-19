"""Reject sensitive literals and files outside the public publishing allowlist."""

import re
import sys
from pathlib import Path


ALLOWLIST = {
    ".gitignore", "README.md", "monitor.py", "data/state.json",
    ".github/workflows/monitor.yml", ".github/workflows/verify.yml",
    "tests/test_monitor.py", "tests/test_tools.py",
    "tools/import_baseline.py", "tools/audit_public.py",
    "scheduler/worker.mjs", "scheduler/worker.test.mjs", "scheduler/schema.sql",
    "scheduler/wrangler.example.toml",
}
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
TOKEN = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
)


def audit(root):
    root = Path(root)
    findings = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in {".git", ".superpowers", "__pycache__"} for part in relative.parts):
            continue
        if not path.is_file():
            continue
        name = relative.as_posix()
        if name not in ALLOWLIST:
            findings.append(f"{name}: file is outside publishing allowlist")
            continue
        if path.is_symlink():
            findings.append(f"{name}: symlink is not allowed")
            continue
        try:
            text = path.read_text(encoding="utf8")
        except (UnicodeError, OSError):
            findings.append(f"{name}: non-text or unreadable file")
            continue
        if TOKEN.search(text):
            findings.append(f"{name}: credential-shaped literal")
        for match in EMAIL.finditer(text):
            address = match.group()
            if address.endswith("@syokugan-monitor"):
                continue
            if address == "41898282+github-actions[bot]@users.noreply.github.com":
                continue
            if name.startswith("tests/") and address.endswith("@example.invalid"):
                continue
            findings.append(f"{name}: personal-address-shaped literal")
            break
    return findings


if __name__ == "__main__":
    issues = audit(Path(__file__).resolve().parents[1])
    if issues:
        print("\n".join(issues))
        sys.exit(1)
    print("Public file allowlist and sensitive-literal audit passed")
