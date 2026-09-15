# CLAUDE.md

Context for Claude Code / Claude Cowork working in this repo. Read this
before making changes.

## First session in this repo — do this before anything else

Setup is scripted. Run it and confirm it's green before any feature work:

```bash
./scripts/bootstrap.sh
source .venv/bin/activate
```

That creates `.venv`, installs core + every `plugins/*` package, and
verifies the CLI loads, entry-point discovery works, and all tests pass.
It exits non-zero if any check fails, and it's re-runnable. If a check
fails, **stop and report it rather than patching around it** — that means
the scaffold has a real bug, and `docs/STAGES.md` checkboxes should not
be flipped until it's fixed.

Expected green state as of 2026-08-25 (verified on Python 3.13.15,
macOS/arm64): `lookup-cli plugins list` shows two rows — `echo` (built-in)
and `echo_standalone` (the template package) — and **52 tests pass in a
single `pytest` run at 96% coverage**. Plugin packages' own tests are now
collected by the root run (`testpaths = ["tests", "plugins"]` with
`--import-mode=importlib`), so there is no longer a separate count to
reconcile.

Python: install 3.13 via `brew install python@3.13`. macOS ships only
3.9, which is below `requires-python`, and bootstrap will refuse to run.

Then:

1. **Fill in `.env`** (bootstrap creates it from `.env.example` if absent)
   with real `OKTA_ORG_URL` / `OKTA_API_TOKEN` and `JIRA_BASE_URL` /
   `JIRA_EMAIL` / `JIRA_API_TOKEN` (both services already have
   credentials per `docs/STAGES.md`). Leave the Jamf/ABM/allwhere
   `LOOKUP_CLI_MOCK_*=1` flags as-is — no credentials for those yet.
2. **Resolve or triage the Open Decisions Log** at the bottom of
   `docs/STAGES.md` before starting Stage 2-3 implementation work —
   at minimum, flag which ones block starting vs. which can wait. Two
   are hard blockers for the first real connector, because both are
   breaking changes once five plugins exist:
   - **`fetch()` sync vs. async** — serial aggregation costs the sum of
     five round trips, and `async def` later breaks every plugin
   - **How plugins receive credentials** — `base.py` defines no
     `__init__`, so today each plugin calls `os.getenv` itself and core
     can't tell configured from unconfigured

   The rest can wait: per-plugin TTLs, Jira `reporter` vs. `assignee`
   (blocks Stage 3 only), ABM auth path (blocks the real-API follow-up,
   not Stage 5's mock work), and whether per-plugin CLI subcommands stay
   centralized in `cli.py`.
3. **Only after the above**, pick up the next unchecked task in
   `docs/STAGES.md` (Stage 2, Okta, is next — real credentials are
   already available for it) and follow the TDD loop in "When picking
   up a task" below.

## Running commands in this repo (read this before running anything)

**Always use explicit venv paths: `.venv/bin/pytest`, `.venv/bin/pip`,
`.venv/bin/lookup-cli`, `.venv/bin/python`.**

Each Bash tool call starts a fresh shell from the user's profile, so a venv
the developer activated in their own terminal is *not* active here —
`VIRTUAL_ENV` is unset and `.venv/bin` is not on `PATH`. A bare `pytest` will
either fail with "command not found" or, worse, silently run a
globally-installed pytest against an interpreter that has none of this
project's dependencies and report a misleading result. There is no PATH
override in `.claude/settings.json` to paper over this: Claude Code's `env`
block takes literal strings with no variable interpolation, so a committed
`PATH` would have to hardcode one machine's absolute paths and freeze them.
The explicit-path convention is the fix.

`.claude/settings.json` (committed) pre-approves the read-only and
`.venv/bin/*` commands so sessions don't stall on permission prompts, and
denies reading `.env` — it holds real Okta/Jira tokens, which should never
land in a transcript. Personal overrides go in `.claude/settings.local.json`,
which is gitignored.

Two more things worth knowing:

- **Read `docs/STAGES.md` before starting work.** It's the live task board.
  Stage 2 (Okta) is next and has real credentials. "Start the next unchecked
  Stage 2 task" is a well-formed request; the TDD loop below then applies.
- **Nothing mechanically enforces tests-first.** CI runs the suite but won't
  block implementation that arrived without tests. The discipline in "Ground
  rules" below is the only thing holding that line.

## What this project is

A Python CLI (`lookup-cli`) that gathers data about a subject across
several services and aggregates it into one record. Most connectors are
person-scoped — Okta, Jira, Jamf, ABM, allwhere all take a username and
return that person's status/assets. **Not all are:** CAIRO takes a vendor
or application name. Don't assume `identifier` means "a person" when
adding a connector (that assumption was baked into the old
`UnifiedUserRecord` name, renamed 2026-09-04). Built plugin-first: every service is a
`ConnectorPlugin` discovered via Python entry points, so adding a new
service never requires touching core code. Full rationale in
`docs/ARCHITECTURE.md`.

## Ground rules for working in this repo

1. **TDD is not optional here.** For any new behavior: write the failing
   test under the right `pytest` marker first, then implement. See
   `docs/CONTRIBUTING.md`. If asked to "add feature X," write
   `tests/.../test_x.py` (or the plugin's own `tests/test_plugin.py`)
   before touching implementation files, and say so explicitly.
2. **Core vs. plugin boundary is load-bearing.** Files under
   `src/lookup_cli/plugins/base.py`, `registry.py`, `cache.py`,
   `models.py`, and `cli.py` are core. A task about "add Jamf support"
   should almost never touch these -- if it seems to require touching
   them, stop and flag it rather than assuming it's fine (see
   `docs/STAGES.md` Stage 8, which exists specifically to catch this).
3. **New connector plugin -> follow `docs/CONNECTOR_GUIDE.md` exactly.**
   Copy `plugins/echo_plugin/` as the starting point, don't write one
   from scratch. Two contract points that are easy to miss: `fetch()` is
   `async def`, and credentials come from the injected `self.config`
   (`PluginConfig`) — never `os.getenv`. Declare what you need in
   `required_credentials` so `plugins list` can report it.
4. **`fetch()` must never raise for ordinary failures** (not found,
   auth error, timeout, 5xx). Catch and return `ConnectorResult(error=...)`.
   Only let genuine bugs propagate. **Build that error string with
   `safe_error(exc)` from `lookup_cli.redaction`, never `str(exc)`** —
   it gets persisted to the SQLite cache and printed, and raw client
   exceptions carry URLs and auth headers.
5. **Secrets stay in `.env` / env vars, never in code, tests, git
   history, or fixtures.** Fixtures use obviously-fake values. gitleaks
   runs on every PR, but it's a backstop, not permission to be casual.
6. **Check `docs/STAGES.md` before starting work.** It's the live task
   board -- update checkboxes as you complete tasks, and add newly
   discovered tasks/decisions to the "Open decisions log" at the bottom
   rather than silently deciding them yourself.

## Repo map

```
src/lookup_cli/            core (registry, cache, models, cli, base contract)
src/lookup_cli/redaction.py  secret scrubbing for connector error strings
src/lookup_cli/plugins/config.py  PluginConfig injected into every connector
plugins/echo_plugin/       template plugin package -- copy for new connectors
plugins/okta_plugin/       Stage 2 connector -- the reference *real* service
tests/unit/framework/      Stage 0 tests (registry, redaction)
tests/unit/cache/          Stage 1 tests (cache, retention)
tests/unit/config/         Stage 1 tests (Settings loader)
tests/cli/                 aggregation/CLI tests (Stage 7) + cache commands
plugins/*/tests/           each connector's own tests (collected by root pytest)
docs/ARCHITECTURE.md       why the system is shaped this way
docs/CONNECTOR_GUIDE.md    how to add a new service, step by step
docs/STAGES.md             the project plan / task board
docs/CONTRIBUTING.md       TDD workflow, branching, PR checklist
```

## Commands Claude should know

```bash
./scripts/bootstrap.sh                    # install + verify everything (start here)
pip install -e ".[dev]"
pip install -e plugins/echo_plugin        # and any other plugin packages
pytest -m plugin_framework                # Stage 0
pytest -m cache                           # Stage 1
pytest -m okta / jira / jamf / abm / allwhere / cli   # per-stage/plugin
pytest --cov=src/lookup_cli               # full suite with coverage
lookup-cli plugins list
lookup-cli cache path|clear|purge         # local PII cache: inspect / empty
lookup-cli okta <user>                    # status (default view)
lookup-cli okta <user> -d                 # devices only;  -sd for both
lookup-cli okta <user> -a                 # applications;  -apps / --apps
lookup-cli okta <user> -u                 # authenticators; -authenticators
lookup-cli okta <user> -g                 # Okta groups the user belongs to
lookup-cli okta <user> -sdaug             # all five sections
lookup-cli okta <user> --team             # compare group memberships across the team
lookup-cli okta <user> --team --html f.html  # ...and write it as a standalone page
lookup-cli okta --find <name>             # search by name -> usernames (not chainable)
lookup-cli okta --find "first last"       # multiple words narrow (AND)
lookup-cli okta --find <name> --all       # every match, not just the first 15
lookup-cli jira <user>                    # assigned issues;  -r reported;  -tr both
lookup-cli jira <user> --all              # page the cursor for a real count
lookup-cli jira ENG-123                   # one issue by key (auto-detected)
lookup-cli cairo <name>                   # CAIRO/TPRM vendor + its applications
lookup-cli lookup <identifier>            # once Stage 7 lands
```

Marker runs cover plugin packages too, so `pytest -m okta` will include
`plugins/okta_plugin/tests/` once it exists — provided that test file sets
`pytestmark = pytest.mark.okta`.

## Current state (update this section as stages complete)

- Stage 0 (plugin framework): **verified green** 2026-08-25 on Python
  3.13.15. 57 tests under `-m plugin_framework` (includes the plugin
  package's own tests now); `lookup-cli plugins list` shows `echo` and
  `echo_standalone` with a configured/mock/missing status column.
  `testpaths` gap closed; `safe_error()` scrubbing added to the contract;
  `fetch()` is now `async def`; credentials arrive via an injected
  `PluginConfig`.
- Stage 1 (cache/data model): **verified green** 2026-08-25 — 30 tests
  under `-m cache`. `cache.py` and `config.py` both 100%. Expiry now
  deletes rows; `lookup-cli cache clear|purge` added; DB is `0600` in a
  `0700` directory.
- Stage 2 (Okta): **mocks green** 2026-09-15 — `pytest -m okta` 333 passed.
  `plugins/okta_plugin/` implements the real client. Four sections select
  via flags (bare = status), and `LOOKUP_CLI_MOCK_OKTA=1` runs any of them
  with no credentials:
  - `-s`/`--status` — account status, `access_blocked`, deactivation
  - `-d`/`--devices` — Okta device registry (`--last-signin` adds per-device
    sign-in times from the System Log)
  - `-a`/`-apps`/`--apps` — assigned applications
  - `-u`/`-authenticators`/`--authenticators` — enrolled MFA factors
  - `-g`/`-groups`/`--groups` — Okta group memberships (bundles: `-sdaug`)
  - `--team` — **mode flag** (long-only, composes with nothing, like `--find`):
    compare group memberships across the queried person's team. The subject is
    **not** assumed to be a manager — the connector reads *their* manager from
    `profile.<OKTA_MANAGER_ATTRIBUTE>` (default `managerId`) and gathers
    everyone who reports to that same manager (the subject + their peers), so
    looking up an IC compares them against their teammates and looking up a
    manager compares them against their peer managers. A subject with no
    manager on file falls back to comparing their own direct reports (tagged
    `cohort="reports"`). `--html PATH` also writes the matrix as a
    self-contained web page: columns ordered oldest hire date first
    (`OKTA_HIRE_DATE_ATTRIBUTE`, default `hireDate`, falling back to account
    `created`), and group rows ordered shared-by-all first down to
    individual access, shaded as a prevalence heat map with a legend.
    Per-member group calls run concurrently; one
    member's failure degrades its column (`?`, excluded from drift maths)
    rather than sinking the run. ⚠️ Whether `managerId` holds a login/email/id
    is org-specific — flagged in the Open Decisions Log and unverified against
    a real org.
  - `--find` — resolve a partial name to a username. **Long-only and
    composes with nothing** (it is a mode, not a section); combining it with
    a section flag is a usage error. ⚠️ Okta's List Users endpoint excludes
    `DEPROVISIONED` users by default — no status clause is sent and a test
    pins that, but whether the API honours it is unverified against a real
    org. See the CLI shape note and Stage 2 notes in `docs/STAGES.md`.

  **CLI shape is `<service> <identifier> [flags]` for every connector** —
  no noun subcommands, and flags bundle (`-sdau`). Read the CLI shape note
  at the top of `docs/STAGES.md` before adding a stage's CLI: it covers the
  three-spellings-per-section rule and the two ways a flag name can silently
  misparse. **Outstanding: the manual smoke test against the real org**,
  blocked on a real `OKTA_API_TOKEN` in `.env` — it is now the only
  unchecked Stage 2 task.
  Note `-d` shows Okta's *device registry* (Okta Verify / device trust),
  not hardware inventory — Jamf/ABM are the authoritative sources and will
  legitimately disagree.
- CAIRO (TPRM vendor/application register): **verified against the live
  API** 2026-09-04 — `pytest -m cairo` 50 passed, plus a real smoke test of
  all four paths. First connector confirmed working end-to-end against a
  real service. **Not person-scoped** — the identifier is a vendor or
  application name, and it is excluded from the Stage 7 person aggregate by
  the explicit plugin list documented there. Three separate `status`-ish
  fields exist on this API and mean different things; see the CAIRO section
  of `docs/STAGES.md` before touching them.
- Stage 3 (Jira): **verified against the live instance** 2026-09-07 --
  `pytest -m jira` 61 passed. Uses a **dedicated service account**, not a personal token.
  Three API gotchas are documented in `docs/STAGES.md`: `/rest/api/3/search`
  is 410 Gone, unbounded JQL is refused, and the replacement returns **no
  `total`** -- so a count is only real when `complete` is true, otherwise
  the CLI says "at least N". JQL needs an accountId, never a username.
- Whole suite: 608 tests, 98% coverage, single `pytest` run.
- Stages 3-8: not started. Both contract decisions (async `fetch()`,
  injected `PluginConfig`) are resolved and implemented, so Jira (Stage 3)
  is a straight copy of the Okta shape.
- Okta credentials: currently a **personal read-only API token**, not a
  service account (decided 2026-08-25 to unblock development). Okta SSWS
  tokens act as their creating user and expire after ~30 days of
  inactivity — swap to a dedicated service account before this goes to
  more than one operator. Tracked in the Open Decisions Log.
- Okta and Jira have real credentials available now; Jamf, ABM, and
  allwhere are mock-first until credentials are provisioned.

## When picking up a task from docs/STAGES.md

1. Find the task row, note its "Depends on."
2. Write the test(s) for it first.
3. Implement.
4. Run that stage's marker, then the full suite.
5. Update the checkbox in `docs/STAGES.md` in the same commit/PR.
6. If you hit a decision not already covered (naming, TTL values, auth
   approach, etc.), add it to the Open Decisions Log rather than
   guessing silently.
