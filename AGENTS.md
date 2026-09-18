# AGENTS.md

Instructions for coding agents working in this repository. `CLAUDE.md` is a
symlink to this file, so Claude Code, GitHub Copilot, Cursor and Codex all
read the same rules.

## What this repo does

It deploys a Kentik Universal Agent (`kagent`) onto a Talos Linux Kubernetes
cluster running on Proxmox VE. There are two entry points:

| Command | Does |
| --- | --- |
| `deploy-talos` | Creates the Proxmox VMs and bootstraps the Talos cluster |
| `deploy-kagent` | Installs the `kagent` Helm release and applies the post-install patches |

Both accept `--config <file>` and `--dry-run`. A dry run must never contact
Proxmox or Kentik, and must never require `talosctl`, `helm` or `kubectl` to
be installed.

## Commands

Always use `uv`. Never call `pip`, `python -m venv` or a global interpreter.

| Command | Does |
| --- | --- |
| `make validate` | Everything CI runs: lint, tests, markdown lint, dry runs |
| `make lint` | `ruff check src/ tests/` |
| `make test` | `pytest tests/ -q` |
| `make docs` | `markdownlint-cli2` over every Markdown file |
| `make plan` | Both scripts must produce a plan from the `*.example` configs |
| `make secrets` | `gitleaks` scan of the working tree and history |
| `make install-hooks` | Installs a pre-push hook that runs `make validate` |

Run `make validate` before you commit. The GitHub Actions workflow in
[.github/workflows/validate.yml](.github/workflows/validate.yml) calls the
same targets, so a green local run means a green CI run.

`make format` exists but is not enforced: `src/` predates `ruff format`, and
reformatting it wholesale would bury real changes in noise. Do not run it as
part of an unrelated change.

## Layout

| Path | Contents |
| --- | --- |
| `src/deploy_talos_proxmox/config.py` | Loader and dataclasses for `talos.yaml` |
| `src/deploy_talos_proxmox/deploy_talos.py` | VM creation, `talosctl` config generation, bootstrap |
| `src/deploy_talos_proxmox/deploy_kagent.py` | Helm install/upgrade plus the post-install `kubectl patch` steps |
| `tests/test_deploy_contract.py` | Contract tests. Each one maps to a defect that reached production |
| `docs/kagent-synthetics-values.md` | Customer-facing guide to the Helm values |
| `utils/` | Standalone shell helpers for Kentik provisioning tokens |

### Files you must not commit

`.gitignore` already covers these. Check before adding anything near them.

- `.env` holds Proxmox and Kentik credentials. `.env.example` is the
  template and is the only one that belongs in git.
- `talos.yaml` and `kagent.yaml` are the live configs. `talos.yaml.example`
  and `kagent.yaml.example` are the tracked versions.
- `config-talos/` and `config-kagent/` are generated. They contain machine
  configs, the Talos PKI and the agent keypairs.

When you change a real config, mirror the structural change into its
`.example` twin with placeholder values. The tests load the examples, so an
example that drifts will fail `make test` rather than fail silently in
production.

Never put a real hostname, IP, token, company ID or agent ID in a tracked
file. Talos node names are randomly suffixed and are easy to paste in by
accident.

## Invariants the tests protect

Do not weaken these without reading the test that covers them.

- **`linux_capabilities` must include `SYS_CHROOT`, `SETUID` and `SETGID`.**
  `scamper`, the synthetics prober, is privilege-separated: it chroots to
  `/var/empty`, then drops to `nobody` for the probing half. Without those
  three, every ping fails with `could not open icmp4 socket: Operation not
  permitted`, which points at `NET_RAW` and sends you down the wrong path.
  `NET_RAW` alone is not enough.
- **`hostNetwork: true` is applied by a patch, not by the chart.** The
  upstream chart has no such key, so setting it in values is silently
  discarded. The pod needs it to preserve the real source IP in flow
  records, otherwise the CNI masquerades the source and Kentik drops the
  flow. The patch also sets `dnsPolicy: ClusterFirstWithHostNet` and feeds
  `HOSTNAME` from the downward API, because the keypair init container
  reads `$HOSTNAME` to pick its StatefulSet ordinal.
- **Only patch fields the chart does not render.** Helm uses server-side
  apply and owns every field it renders. Patching one of those fields makes
  every later `helm upgrade` fail with an ownership conflict.
- **Use `kubectl patch --type=strategic`, never `--type=merge`.** A merge
  patch replaces whole arrays; a strategic patch merges list entries by
  `name`. A merge patch on `containers` will delete the rest of the pod
  spec.

## Python conventions

- Python 3.14 or newer. Use modern language features where they make the
  code clearer, not to show them off.
- `uv` for dependencies, `ruff` for lint and format.
- Prefer plain functions and dataclasses. Add an abstraction only when a
  second caller actually exists.

<!-- shared-rules:start -->
<!-- Synced from ~/.claude/CLAUDE.md; regenerate by re-running the
     agents-md skill instead of hand-editing this block. -->
## Writing style

- Never use em dashes: neither the `U+2014` character itself nor a `--`
  stand-in used as punctuation. This applies everywhere: code, docs,
  docstrings, commit messages, agent instruction files, chat replies.
  Rewrite with a comma, a colon, parentheses, or a second sentence
  instead. `--` is fine as a literal CLI flag or argument separator, for
  example `git commit -- file`.
- Keep comments and docstrings concise. State the WHY, not the WHAT, and
  skip restating what already readable code shows.

## Git branching

Never commit directly to `main`. Every unit of work gets its own branch,
created from an up-to-date `main` and named
`<category>/<short-kebab-description>`.

| Prefix | Use for |
| --- | --- |
| `feat/` | New capability or user-visible functionality |
| `fix/` | Correcting broken behavior |
| `refactor/` | Restructuring with no behavior change |
| `perf/` | Optimization where the observable behavior is unchanged |
| `docs/` | Documentation, comments, README or design docs only |
| `test/` | Adding or fixing tests only |
| `chore/` | Deps, tooling, CI, config, `.gitignore`, release prep |
| `security/` | Fixing a vulnerability or hardening a boundary |

- One concern per branch. If describing the change needs an "and", it is
  probably two branches.
- Name the change, not the component. `fix/env-parse-order` beats
  `fix/cli`. Keep it under about 40 characters.
- Push the branch and open a pull request. Let the platform merge it
  instead of running `git merge` locally. Squash and merge is the default.
- Ask before force-pushing, rebasing shared history, or deleting a branch
  that was pushed to a remote.
<!-- shared-rules:end -->
