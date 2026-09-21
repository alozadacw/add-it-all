# Contributing

## Dev setup

```bash
./scripts/bootstrap.sh && source .venv/bin/activate
```

Creates `.venv`, installs core + every `plugins/*` package, seeds `.env`, and
verifies the CLI and test suite actually work. Re-runnable; exits non-zero if
any check fails. `README.md` is the single source of truth for setup details
and flags -- don't duplicate the install steps here, so they can't drift.

## TDD workflow (required)

For every task in `docs/STAGES.md`:

1. Write the test(s) first, under the correct `pytest` marker for that
   stage (see `pyproject.toml` `[tool.pytest.ini_options]`).
2. Run it, confirm it fails for the right reason (not a typo/import error).
3. Implement the minimum code to pass.
4. Run the full stage's marker set, then the full suite, before opening a PR.

```bash
pytest -m okta                # just this stage/plugin
pytest                        # everything
pytest -m okta --cov=src/add_it_all
```

Do not merge a PR that adds implementation code without a preceding
test commit (or at minimum, tests and implementation in the same PR
with tests visibly written to fail first in the PR description).

## Branching & commits

- Branch per task: `stage-2/okta-status-lookup`, `stage-4/jamf-mock-fixtures`.
- Commit messages: `[stage-N] short description` (e.g. `[stage-2] add Okta status connector with mocked tests`).
- One task from `docs/STAGES.md` per PR where reasonably possible --
  keeps review scoped and keeps the task board accurate.

## PR checklist

- [ ] Tests written before/alongside implementation, and pass locally
- [ ] New/changed env vars added to `.env.example` with a comment
- [ ] If a new plugin: `docs/CONNECTOR_GUIDE.md` followed, no edits to
      `src/add_it_all/` core (registry/base/cache/models) unless the
      task is explicitly about the framework itself
- [ ] `docs/STAGES.md` task checkbox updated

## Code conventions

- Type hints everywhere; `from __future__ import annotations` at the
  top of new modules.
- Plugins never let `fetch()` raise for ordinary failures -- return
  `ConnectorResult(error=...)`.
- Keep the real HTTP/SDK call in its own method (`_call_backend` or
  similar) so it's mockable without a full HTTP-mocking library when a
  test doesn't need that fidelity.
- No secrets in code, tests, or fixtures -- fixtures use obviously fake
  values (`jdoe`, `not-a-real-token`, etc).
