# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |

## Design guarantees

- **Read-only.** docker-hygiene walks the context directory and parses
  Dockerfiles/ignore files. It never builds, pushes, writes, or transmits
  anything, and never requires a Docker daemon.
- **Paths only, never contents.** Findings contain paths, rule ids, and
  fixed messages.
- **No network access and no dependencies.**

## What this tool is not

- Not a secret-content scanner and not a Dockerfile linter (use hadolint).
- Ignore evaluation is a conservative approximation; `docker build`
  remains the ground truth. If it was already shipped in an image, rotate
  the secret — ignoring it afterwards does not remove it from layers.

## Reporting a vulnerability

Open a GitHub issue with the `security` label. Never include real secrets.
