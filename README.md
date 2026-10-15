# docker-hygiene

One sentence: **docker-hygiene audits a Docker build context before you build — missing `.dockerignore`, secrets that would be sent to the daemon, heavy directories shipped by accident, and unrestrained `COPY . .` — with zero dependencies and no Docker daemon required.**

## The problem

`docker build` sends your *entire* context directory to the daemon unless a `.dockerignore` says otherwise. No ignore file means `.env` files, `node_modules`, and `.git` travel with every build: slow uploads, bloated layer caches, broken native modules — and secrets baked into image layers where anyone who can pull the image can read them. A 71k-view Stack Overflow thread and fresh 2025–2026 guides show developers keep discovering this the hard way.

## Why docker-hygiene instead of …

| Alternative | What docker-hygiene does differently |
|---|---|
| `hadolint` | Excellent Dockerfile linter (pinning, `COPY` vs `ADD`, …) — but it lints *instructions only* and has no build-context/`.dockerignore` rule (verified against its rule list). docker-hygiene audits the *context*; use both. |
| `dive` / image scanners | Analyze an already-built image and need a daemon. docker-hygiene fails *before* the build, statically, with no daemon. |
| `docker build` itself | The ground truth for what gets sent — but slow and daemon-dependent. docker-hygiene approximates the ignore evaluation offline in milliseconds. |

> **Honest positioning.** For Dockerfile instruction quality, use hadolint. For image contents after the fact, use dive. docker-hygiene owns the gap between them: the context, checked pre-build.

## Features (all implemented and tested)

- **missing-ignore** (error): Dockerfile present but no `.dockerignore` (or `<Dockerfile>.dockerignore` per BuildKit convention)
- **secret-in-context** (error): `.env*`, `*.pem`, private keys … that ignore rules would still send
- **heavy-dir** (error): `node_modules/`, `.git/`, `vendor/` … sent to the daemon
- **heavy-context** (warning): context over `--max-mb` (default 100) with top space offenders in JSON
- **broad-copy** (error): `COPY . .` / `ADD . .` with no ignore file to restrain it
- **no-dockerfile** (warning): nothing to audit the context against
- Ignore evaluation: `*`, `?`, `**`, `!` negation, dir-only `/`, last-match-wins — tested
- `--strict`, `--format json`, exit codes 0/1/2; read-only; paths only, never contents

## Requirements

- Python 3.9+ (standard library only), Windows/macOS/Linux. No Docker needed.

## Installation

```sh
curl -O https://raw.githubusercontent.com/Black-Studio-ia/docker-hygiene/main/dockerhygiene.py
```

Verify: `python dockerhygiene.py --version`

## Quick start

```sh
cd your-docker-project
python /path/to/dockerhygiene.py --root .
```

Example:

```
docker-hygiene: 2 finding(s), context ~612 MB across 1403 files (1399 sent)
  [error] . (missing-ignore) no .dockerignore: the default is to send the entire context ...
      fix: create .dockerignore (at least: .git, node_modules, .env, *.pem, Dockerfile, .dockerignore)
  [error] node_modules (heavy-dir) node_modules/ is sent to the daemon ...
      fix: add node_modules/ to .dockerignore
FAIL: 2 error(s)
```

### GitHub Actions

See [`.github/workflows/ci.yml`](.github/workflows/ci.yml). To gate your own repo:

```yaml
- name: Audit Docker build context
  run: python dockerhygiene.py --root . --strict
```

## Usage

```
usage: docker-hygiene [--root DIR] [--max-mb N] [--strict] [--format text|json]

  --root     build context directory (default: .)
  --max-mb   context size warning threshold in MB (default: 100)
  --strict   also fail on warnings
```

## Security and privacy

- **Read-only.** Walks the context directory; never writes, builds, pushes, or transmits anything. No network access, no dependencies.
- **Paths only.** Findings never include file contents. Dockerfiles and ignore files are parsed for structure only.
- Not a secret-content scanner and not a substitute for `docker build` itself, which remains the ground truth for what gets sent. See [SECURITY.md](SECURITY.md).

## Known limitations

- Single-context model: `--root` is one build context. Monorepos with per-service contexts (and compose `build.context` pointers) need one run per directory.
- Ignore matching is a documented conservative approximation of Docker's Go matcher (slashless patterns match at any depth); edge cases may differ from the daemon.
- `latest`-tag / instruction linting is hadolint's job and intentionally out of scope.
- Compose files are not parsed.

## Running the tests

```sh
python -m unittest discover -s tests -v
```

21 tests, stdlib only: matcher semantics (negation, `**`, anchoring, dir-only), COPY parsing (shell + JSON forms), clean/drift fixtures per rule, CLI exit codes, JSON validity.

## License

MIT — see [LICENSE](LICENSE).

## Support this project

If this tool saves you a leaked secret or a gigabyte upload, you may optionally support its continued development with USDT.

Network: TRC20 (Tron)

USDT address: TXQCrX61CceFxX5gnFg9N3ssZEC7MavwBQ

Please verify the network and address carefully before sending — USDT on the wrong network can be lost permanently. Contributions are voluntary and do not guarantee specific features, services, or financial returns. The software remains fully usable under its MIT license regardless.

Contact: ia.creative.tn@gmail.com
