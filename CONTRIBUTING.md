# Contributing

Thank you for helping. This page covers how to set up, what a change must pass, and how a pull request gets merged.

## Set up

You need Python 3.12 or newer and Docker.

```bash
git clone https://github.com/sept-usc/muse-mcp-adapter && cd muse-mcp-adapter
python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'
git config core.hooksPath .githooks     # run the full gate before every commit
```

## The gate

Every change must pass the same checks locally that CI runs, so nothing that fails ever reaches a pull request.

```bash
scripts/verify.sh           # the whole gate: what the commit hook runs
scripts/verify.sh --quick   # lint, types and tests only, for iterating (not the gate)
```

It checks, in order:

1. no credentials are tracked;
2. the GitHub workflows are valid and every action is pinned to a commit that exists;
3. lint (`ruff`), types (`pyright`, strict) and the full test suite, on Linux with Python 3.12 and again in your own virtualenv;
4. both container images build, start as the unprivileged user, and have no known high or critical vulnerabilities;
5. every link in the documentation resolves, and every Mermaid diagram renders.

If Docker is not running the gate fails; it never skips a step. On GitHub the same checks run for every pull request as the `gate` check, and a pull request can be merged only when `gate` passes.

## Making a change

1. Branch from `main` and keep the change focused: one concern per pull request.
2. Add or update tests with the change. A bug fix starts with a test that fails without it.
3. Update the documentation a user would read (the README, `docs/`) in the same pull request.
4. Draw diagrams in [Mermaid](https://mermaid.js.org/) (a fenced `mermaid` block), not as images.
5. Write commit messages as `type(scope): what and why`, for example `fix(vm): retry dropped ssh connections`.
6. Never put real credentials, account names, hostnames or addresses in code, tests, docs, commit messages or pull request text. Use obvious placeholders such as `203.0.113.7` and `example.com`.

## What a good change looks like

- It works on the supported paths: a Google Cloud VM that `muse-vm` creates, and any Linux machine reached over SSH.
- It keeps the security properties in [docs/security.md](docs/security.md): least privilege, no secret in the database, logs or images, no new inbound port.
- It states what it changed for users, and what it did not.

## Reporting a vulnerability

Do not open a public issue. See [SECURITY.md](SECURITY.md).
