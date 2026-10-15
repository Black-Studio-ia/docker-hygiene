# Changelog

## 0.1.0 — 2026-10-15

Initial release.

- Pre-build context audit: missing-ignore, secret-in-context, heavy-dir,
  heavy-context, broad-copy, no-dockerfile.
- Ignore matcher (`*`, `?`, `**`, `!`, dir-only, last-match-wins) plus
  BuildKit `<Dockerfile>.dockerignore` convention.
- `--strict`, `--max-mb`, `--format json`; exit codes 0/1/2.
- 21-test stdlib `unittest` suite; CI workflow.
