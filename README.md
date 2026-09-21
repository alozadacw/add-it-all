# Add-IT-ALL

`add-it-all` — unified, plugin-based CLI for looking up a person's identity/asset footprint
across Okta, Jira, Jamf, CAIRO, and allwhere -- with
room to add more services without touching core code.

```
add-it-all lookup jdoe               # aggregate across all installed plugins
add-it-all okta jdoe                 # one service: status by default
add-it-all okta jdoe -d              # ...and its devices
add-it-all jira jdoe -t
add-it-all plugins list              # see what's installed
```


> **Renamed 2026-09-21** from `lookup-cli`. Upgrading an existing checkout:
> the command is now `add-it-all`, the env prefix is `ADD_IT_ALL_` (so
> `LOOKUP_CLI_MOCK_OKTA` becomes `ADD_IT_ALL_MOCK_OKTA`), and the cache moved
> to `~/.add-it-all/`. Run `pip uninstall lookup-cli lookup-cli-okta-plugin
> lookup-cli-jira-plugin lookup-cli-cairo-plugin lookup-cli-echo-plugin`
> before `./scripts/bootstrap.sh`, or the old and new distributions coexist.
> Service credential names (`OKTA_API_TOKEN`, `JIRA_API_TOKEN`,
> `CAIRO_API_KEY`) are unchanged.

## Status

Actively built stage-by-stage, TDD-first. See [`docs/STAGES.md`](docs/STAGES.md)
for the full project plan, current stage, and task breakdown. Currently:
**Stage 0 (plugin framework) and Stage 1 (cache/data model) implemented and
verified green** (15 tests, 87% coverage). Stages 2-8 not started.

## Quick start

**First thing to run in a fresh clone — this is the whole setup:**

```bash
./scripts/bootstrap.sh
source .venv/bin/activate
```

That creates `.venv`, installs the core package plus every plugin package
under `plugins/`, copies `.env.example` to `.env` if you don't have one, and
then verifies the install actually works: the CLI loads, entry-point plugin
discovery finds the plugins, and the full test suite passes. It exits
non-zero if any check fails, and it's safe to re-run — it reuses an existing
`.venv` and never overwrites an existing `.env`.

Requires Python >= 3.11 (CI pins 3.11; the script warns if your local minor
differs). Useful flags:

```bash
./scripts/bootstrap.sh --skip-tests        # install only
./scripts/bootstrap.sh --recreate          # rebuild .venv from scratch
PYTHON=python3.11 ./scripts/bootstrap.sh   # pin the interpreter
```

If a check fails, fix it rather than working around it — a failure there
means the scaffold itself is broken, not your machine.

Then fill in real credentials in `.env` (Okta and Jira have credentials
available today; Jamf and allwhere stay on their `ADD_IT_ALL_MOCK_*=1`
flags until creds are provisioned) and pick up the next unchecked task in
[`docs/STAGES.md`](docs/STAGES.md).

### Running things by hand

`bootstrap.sh` is just a wrapper over these, if you'd rather drive them
individually:

```bash
pip install -e ".[dev]"
pip install -e plugins/echo_plugin        # and any other plugin packages

pytest -m plugin_framework                # Stage 0
pytest -m cache                           # Stage 1
pytest --cov=src/add_it_all --cov-report=term-missing
pytest plugins/echo_plugin/tests          # plugin packages' own tests
add-it-all plugins list                   # expect: echo, echo_standalone
```

Note that `pytest` from the repo root does **not** pick up the per-plugin
test suites under `plugins/*/tests` — `testpaths` is scoped to `tests/`, so
those need running explicitly (`bootstrap.sh` does this for you). See Stage 0
in [`docs/STAGES.md`](docs/STAGES.md).

## Using Claude Code in this repo

[`CLAUDE.md`](CLAUDE.md) loads automatically and tells Claude the ground rules
(TDD-first, the core/plugin boundary, the task board). Four things the *human*
needs to know:

1. **Don't rely on an activated venv.** Every tool call starts a fresh shell,
   so your `source .venv/bin/activate` doesn't carry into Claude's shell. The
   convention here is explicit paths — `.venv/bin/pytest`, `.venv/bin/add-it-all`.
   A bare `pytest` can silently run a global install against the wrong
   interpreter and report a green suite that means nothing.
2. **Point it at the board.** *"Read `docs/STAGES.md` and start the next
   unchecked Stage 2 task"* is a good opener. Stage 2 (Okta) is next and has
   real credentials.
3. **Watch the core/plugin boundary.** A connector task should not edit
   `src/add_it_all/`. That boundary is the architecture; `CLAUDE.md` tells
   Claude to flag rather than cross it, but you're the backstop.
4. **Tests-first isn't enforced by CI.** The suite runs on every PR, but
   nothing blocks implementation that showed up without tests.

`.claude/settings.json` is committed and pre-approves the read-only and
`.venv/bin/*` commands, so you shouldn't have to click through permission
prompts for routine test runs. It also denies reading `.env`, which holds real
API tokens. Personal tweaks belong in `.claude/settings.local.json`
(gitignored). Committed settings apply to newly started sessions.

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) -- how the plugin system, cache, and CLI fit together
- [`docs/CONNECTOR_GUIDE.md`](docs/CONNECTOR_GUIDE.md) -- step-by-step guide to adding a new service connector
- [`docs/STAGES.md`](docs/STAGES.md) -- staged project plan, one epic per stage, task-board-ready
- [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md) -- dev workflow, TDD conventions, branching/PR conventions
- [`CLAUDE.md`](CLAUDE.md) -- context file for Claude Code / Claude Cowork
