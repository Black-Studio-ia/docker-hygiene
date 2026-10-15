#!/usr/bin/env python3
"""docker-hygiene: audit a Docker build context before it costs you.

hadolint lints the Dockerfile; docker-hygiene audits everything around
it — the build context that gets sent to the daemon:

* missing ``.dockerignore`` (default is "send everything"),
* secret-bearing files that would be sent (``.env``, ``*.pem`` …),
* heavy directories shipped by accident (``node_modules``, ``.git`` …),
* oversized contexts with the top space offenders,
* broad ``COPY . .`` with no ignore file to restrain it.

Zero dependencies, offline, read-only. Never reads file *contents* except
to parse Dockerfiles and ignore files — never secret values.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys

__version__ = "0.1.0"

DEFAULT_MAX_MB = 100

SECRET_BASENAMES = {
    ".env", "credentials.json", ".npmrc", ".pypirc",
}
SAFE_ENV_TEMPLATES = {
    ".env.example", ".env.sample", ".env.template", ".env.dist",
}
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
SECRET_PREFIXES = ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")

# Directories that are pure build-context poison when sent: huge, and
# (for node_modules) potentially platform-wrong inside the image.
HEAVY_DIRS = {"node_modules", ".git", "vendor", "target", "__pycache__",
              ".venv", "venv", ".tox", ".next", "dist", "build", "coverage",
              ".cache"}

COPY_RE = re.compile(r"^\s*COPY\s+(?P<args>.+?)\s*$", re.IGNORECASE)
ADD_RE = re.compile(r"^\s*ADD\s+(?P<args>.+?)\s*$", re.IGNORECASE)


def parse_ignore_file(path: str) -> list[str]:
    """Read an ignore file into cleaned patterns (order preserved)."""
    patterns: list[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                patterns.append(line)
    except OSError:
        pass
    return patterns


def _pattern_to_regex(pattern: str) -> tuple[re.Pattern[str], bool]:
    """Compile one dockerignore-style pattern.

    Returns ``(regex, dir_only)``. Semantics (documented approximation of
    Docker, erring toward matching for safety): a pattern without a slash
    matches the basename at any depth; otherwise it is anchored at the
    context root. ``**`` crosses separators, ``!`` and dir-only ``/`` are
    handled by the caller.
    """
    negated = pattern.startswith("!")
    if negated:
        pattern = pattern[1:]
    dir_only = pattern.endswith("/")
    # A leading slash only anchors (match from the context root);
    # otherwise a slash anywhere else in the pattern anchors it too.
    anchored = pattern.startswith("/") or "/" in pattern.strip("/")
    core = pattern.strip("/")
    out = []
    i = 0
    while i < len(core):
        char = core[i]
        if char == "*":
            if core[i:i + 2] == "**":
                out.append(".*")
                i += 2
                # "**/" == "*/" or nothing; "/**" == "/*" or nothing.
                if i < len(core) and core[i] == "/":
                    out.append("(?:/)?")
                    i += 1
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char in ".+()|^$\\{}[]":
            out.append("\\" + char)
        else:
            out.append(char)
        i += 1
    body = "".join(out)
    # Every pattern also covers everything beneath a matched directory
    # (excluding `node_modules` excludes its whole subtree).
    if anchored:
        regex = f"^{body}(?:/.*)?$"
    else:
        regex = f"^(?:.*/)?{body}(?:/.*)?$"
    return re.compile(regex), negated


def is_ignored(rel_path: str, patterns: list[str], is_dir: bool = False) -> bool:
    """Last-match-wins ignore evaluation for a context-relative path."""
    rel = rel_path.replace(os.sep, "/")
    ignored = False
    for pattern in patterns:
        negated = pattern.startswith("!")
        core = pattern[1:] if negated else pattern
        regex, _ = _pattern_to_regex(core)
        # A directory pattern also ignores everything beneath it.
        if regex.match(rel) or (is_dir is False and regex.match(rel + "/")
                                and core.endswith("/")):
            ignored = not negated
    return ignored


def find_dockerfiles(root: str) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in {".git", "node_modules"})
        for name in sorted(filenames):
            lowered = name.lower()
            if lowered == "dockerfile" or lowered.startswith("dockerfile.") \
                    or lowered.endswith(".dockerfile"):
                found.append(os.path.join(dirpath, name))
    return found


def dockerfile_copy_sources(path: str) -> list[str]:
    """Return COPY/ADD source operands from a Dockerfile (best effort)."""
    sources: list[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return sources
    for raw in lines:
        line = raw.split("#", 1)[0].rstrip()
        if not line:
            continue
        match = COPY_RE.match(line) or ADD_RE.match(line)
        if not match:
            continue
        args = match.group("args").strip()
        if args.startswith("["):  # JSON array form: ["a", "b", "dest"]
            try:
                items = json.loads(args)
                if isinstance(items, list) and len(items) >= 2:
                    sources.extend(str(i) for i in items[:-1])
                continue
            except (ValueError, TypeError):
                pass
        parts = args.split()
        parts = [p for p in parts if not p.startswith("--")]
        if len(parts) >= 2:
            sources.extend(parts[:-1])  # last operand is the destination
    return sources


def check_secret_name(rel: str) -> str | None:
    base = rel.replace("\\", "/").rsplit("/", 1)[-1]
    if base in SAFE_ENV_TEMPLATES:
        return None
    if base in SECRET_BASENAMES or (
            base.startswith(".env.") and base not in SAFE_ENV_TEMPLATES):
        return f"{rel} would be sent to the daemon"
    lowered = base.lower()
    if lowered.endswith(SECRET_SUFFIXES):
        return f"{rel} would be sent to the daemon"
    _, dot, ext = lowered.rpartition(".")
    if lowered.startswith(SECRET_PREFIXES) and (
            not dot or f".{ext}" in {".pem", ".key", ".ppk"}):
        return f"{rel} looks like a private key that would be sent"
    return None


def walk_context(root: str) -> tuple[list[tuple[str, int, bool]], int]:
    """Return ``[(rel_path, size, is_dir)]`` plus total bytes."""
    entries: list[tuple[str, int, bool]] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames):
            full = os.path.join(dirpath, name)
            entries.append((os.path.relpath(full, root), 0, True))
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            total += size
            entries.append((os.path.relpath(full, root), size, False))
    return entries, total


def top_dirs(entries: list[tuple[str, int, bool]]) -> list[tuple[str, int]]:
    """Aggregate file sizes by top-level directory."""
    totals: dict[str, int] = {}
    for rel, size, is_dir in entries:
        if is_dir:
            continue
        top = rel.split(os.sep, 1)[0] if os.sep in rel else "(root files)"
        totals[top] = totals.get(top, 0) + size
    return sorted(totals.items(), key=lambda kv: kv[1], reverse=True)


def audit(root: str, max_mb: int) -> tuple[list[dict], dict]:
    findings: list[dict] = []
    entries, total = walk_context(root)
    files = {rel for rel, _, is_dir in entries if not is_dir}
    stats: dict = {"files": len(files), "context_bytes": total,
                   "top_dirs": [{"path": p, "bytes": b}
                                for p, b in top_dirs(entries)[:5]]}

    dockerfiles = find_dockerfiles(root)
    stats["dockerfiles"] = [os.path.relpath(p, root) for p in dockerfiles]
    if not dockerfiles:
        findings.append({"check": "no-dockerfile", "severity": "warning",
                         "path": ".", "message": "no Dockerfile found — "
                         "nothing to audit the context against",
                         "fix": "point --root at a build context"})
        return findings, stats

    # Resolve the applicable ignore file (BuildKit convention first).
    ignore_path: str | None = None
    first = dockerfiles[0]
    per_docker = first + ".dockerignore"
    root_ignore = os.path.join(root, ".dockerignore")
    if os.path.isfile(per_docker):
        ignore_path = per_docker
    elif os.path.isfile(root_ignore):
        ignore_path = root_ignore
    patterns = parse_ignore_file(ignore_path) if ignore_path else []
    stats["ignore_file"] = (os.path.relpath(ignore_path, root)
                            if ignore_path else None)

    if ignore_path is None:
        findings.append({
            "check": "missing-ignore", "severity": "error", "path": ".",
            "message": "no .dockerignore: the default is to send the "
                       "entire context (including .env, .git, node_modules) "
                       "to the daemon",
            "fix": "create .dockerignore (at least: .git, node_modules, "
                   ".env, *.pem, Dockerfile, .dockerignore)",
        })

    sent = [rel for rel in sorted(files)
            if not is_ignored(rel, patterns)]
    stats["sent_files"] = len(sent)
    for rel in sent:
        msg = check_secret_name(rel)
        if msg:
            findings.append({"check": "secret-in-context", "severity": "error",
                             "path": rel, "message": msg,
                             "fix": "add it to .dockerignore; if it was "
                                    "already shipped in an image, rotate it"})
    sent_top = [d for d in top_dirs(
        [(r, s, False) for r, s, _ in entries if r in set(sent)])]
    for dirname, _ in sent_top:
        if dirname in HEAVY_DIRS:
            findings.append({
                "check": "heavy-dir", "severity": "error", "path": dirname,
                "message": f"{dirname}/ is sent to the daemon (slow "
                           "builds, bloated cache; node_modules can even "
                           "break the image)",
                "fix": f"add {dirname}/ to .dockerignore",
            })
    if total > max_mb * 1024 * 1024:
        mb = total // (1024 * 1024)
        findings.append({
            "check": "heavy-context", "severity": "warning", "path": ".",
            "message": f"context is ~{mb} MB (over {max_mb} MB)",
            "fix": "extend .dockerignore; see top_dirs in JSON output",
        })
    for dockerfile in dockerfiles:
        rel = os.path.relpath(dockerfile, root)
        for src in dockerfile_copy_sources(dockerfile):
            if src in (".", "./"):
                if ignore_path is None:
                    findings.append({
                        "check": "broad-copy", "severity": "error",
                        "path": rel,
                        "message": f"{src} is copied with no .dockerignore "
                                   "to restrain it",
                        "fix": "create .dockerignore or copy only what "
                               "the build needs",
                    })
                break
    return findings, stats


def evaluate(findings: list[dict], strict: bool) -> tuple[bool, str]:
    errors = sum(1 for f in findings if f["severity"] == "error")
    warnings = sum(1 for f in findings if f["severity"] == "warning")
    if errors == 0 and (not strict or warnings == 0):
        return True, ""
    parts = [f"{errors} error(s)"]
    if warnings:
        parts.append(f"{warnings} warning(s)"
                     + (" (failing under --strict)" if strict
                        else " (advisory)"))
    return False, "; ".join(parts)


def format_text(findings: list[dict], stats: dict, reason: str) -> str:
    mb = stats.get("context_bytes", 0) // (1024 * 1024)
    sent = stats.get("sent_files")
    sent_bit = f" ({sent} sent)" if sent is not None else ""
    out = [f"docker-hygiene: {len(findings)} finding(s), "
           f"context ~{mb} MB across {stats.get('files', 0)} files"
           f"{sent_bit}"]
    if stats.get("ignore_file"):
        out.append(f"ignore file: {stats['ignore_file']}")
    for finding in findings:
        out.append(f"  [{finding['severity']}] {finding['path']} "
                   f"({finding['check']}) {finding['message']}")
        if finding.get("fix"):
            out.append(f"      fix: {finding['fix']}")
    out.append("FAIL: " + reason if reason else "PASS: context looks clean")
    return "\n".join(out) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docker-hygiene",
        description="Audit a Docker build context: missing .dockerignore, "
                    "secrets that would be sent, heavy directories, "
                    "oversized contexts, unrestrained COPY. Read-only.",
    )
    parser.add_argument("--root", default=".",
                        help="build context directory (default: .)")
    parser.add_argument("--max-mb", type=int, default=DEFAULT_MAX_MB,
                        help=f"context size warning threshold in MB "
                             f"(default: {DEFAULT_MAX_MB})")
    parser.add_argument("--strict", action="store_true",
                        help="also fail on warnings")
    parser.add_argument("--format", choices=("text", "json"), default="text",
                        help="output format (default: text)")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_mb < 1:
        parser.error("--max-mb must be at least 1")
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print("docker-hygiene: error: --root is not a directory: "
              f"{args.root}", file=sys.stderr)
        return 2
    findings, stats = audit(root, args.max_mb)
    ok, reason = evaluate(findings, args.strict)
    if args.format == "json":
        print(json.dumps({"ok": ok, "reason": reason, "stats": stats,
                          "findings": findings}, indent=2))
    else:
        print(format_text(findings, stats, reason), end="")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
