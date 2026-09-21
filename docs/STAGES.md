# Project plan: stages, tasks, and ownership

Each stage is an epic. Each task below is sized to be picked up
independently by one developer and closed with a passing test run
under that stage's `pytest` marker. Copy tasks into GitHub Issues (or
your tracker of choice) 1:1 -- the checkboxes here double as a
lightweight board if you'd rather not stand up tooling yet.

Suggested labels: `stage:0`..`stage:8`, `plugin:okta`/`jira`/`jamf`/`allwhere`,
`type:test`, `type:impl`, `type:docs`.

Legend: **[ ]** not started **[~]** in progress **[x]** done

## CLI shape (decided 2026-09-02 — applies to every connector stage)

```
add-it-all <service> <identifier> [flags]
```

The identifier is a direct argument; flags select which sections to show.
There are **no noun subcommands** (`okta status jdoe`, `jamf devices jdoe`).
They cannot coexist with `okta jdoe`: a person whose login is literally
`status` or `devices` would silently resolve to the subcommand instead of
being looked up — a wrong answer rather than an error.

- Bare `<service> <identifier>` shows that service's primary view.
- Flags are additive selectors and bundle (`-sd`, `-sdau`).
- Flags may precede or follow the identifier. This needs
  `context_settings={"allow_interspersed_args": True}` on the plugin's
  `typer.Typer`, because Click groups otherwise stop parsing options at the
  first positional and `<service> jdoe -d` fails while `<service> -d jdoe`
  works.

### Flag spelling (decided 2026-09-02)

Each section declares **three** spellings: a single character, a
multi-character single-dash form, and a long form — e.g. apps is
`-a` / `-apps` / `--apps`, authenticators is
`-u` / `-authenticators` / `--authenticators`.

The single-character form is what makes bundling work; the longer forms are
what make a script readable. Both are wanted, so both are declared.

There is a **third category**, added 2026-09-07: a *mode* flag, which is
long-only and composes with nothing. `--find` is the first. Short flags
select sections and compose (`-sdau`); a mode flag replaces the whole
operation, so it gets no short form — that removes every bundled spelling
(`-sdf`, `-fd`) at the parser level for free, since an undeclared `-f` makes
the bundle unparseable. The remaining case (`--find -d`) has to be caught at
runtime; Click has no parser-level mutual exclusion.

Two consequences to know before adding a stage's flags:

- **A multi-character single-dash flag cannot appear inside a bundle.**
  `-sdapp` is a usage error, not a bug. When Click can't match a single-dash
  string as a whole it decomposes it character by character, so `-sdapp` reads
  as `-s -d -a -p -p` and there is no `-p`. The bundled spelling is `-sdau`;
  the readable spelling is `-sd -apps`. Failing loudly is the correct outcome
  and there is a test pinning it.
- **Never declare a multi-char form that is also a valid bundle of declared
  single chars.** With `-a` and `-u` both declared, `-au` already means
  apps+authenticators. Declaring `-au` as an authenticators alias would make
  `-au` mean authenticators-only while `-sdau` still meant apps+authenticators
  — the same two letters meaning two different things depending on position.
  This is why `-au` and `-app` are deliberately *not* declared.

Okta (Stage 2) is the reference implementation. Stage 7's aggregate command
`add-it-all lookup <identifier>` takes the same shape.

---

## Stage 0 -- Plugin Framework
**Status: verified green** -- installed and run 2026-08-19 on Python 3.14.0
(macOS/arm64): `pytest -m plugin_framework` 5 passed, `add-it-all plugins
list` shows both `echo` and `echo_standalone`. Reproduce with
`./scripts/bootstrap.sh`.

| Task | Depends on | Notes |
|---|---|---|
| [x] Define `ConnectorPlugin` ABC + `ConnectorResult` dataclass | -- | `src/add_it_all/plugins/base.py` |
| [x] Implement entry-point-based `discover_plugins()` | ABC | `src/add_it_all/plugins/registry.py` |
| [x] Built-in `echo` plugin proving discovery works | registry | `src/add_it_all/plugins/echo_builtin.py` |
| [x] Standalone installable `echo_plugin` template package | registry | `plugins/echo_plugin/` -- copy this for every real connector |
| [x] `add-it-all plugins list` command | registry, CLI skeleton | `src/add_it_all/cli.py` |
| [x] **Run `pytest -m plugin_framework` in a real environment and confirm green** | all above | 5 passed on Python 3.14.0, 2026-08-19 |
| [x] Scripted one-command bootstrap so this is reproducible for every dev | above | `scripts/bootstrap.sh` -- installs core + all `plugins/*` packages, verifies CLI/discovery/tests, exits non-zero on failure. Referenced as the first step in `README.md` |
| [x] Set up CI (GitHub Actions) running `pytest` on every PR | above | `.github/workflows/tests.yml` confirmed green on the first real run (2026-08-19, Python 3.11, 43s): 13 passed, 87% coverage -- byte-identical to the local 3.14 run (152 stmts / 20 miss), which lowers the version-skew concern in the decisions log |
| [x] Fix `testpaths` so per-plugin test suites are collected | above | Fixed 2026-08-25. `testpaths = ["tests", "plugins"]` plus `--import-mode=importlib`. The import mode is required, not cosmetic: root `tests/` and every `plugins/*/tests/` are both packages named `tests`, which collide under pytest's default prepend mode (`ModuleNotFoundError: No module named 'tests.test_plugin'`). Also added `--strict-markers`, and gave `echo_plugin`'s tests a `pytestmark` so `pytest -m <stage>` covers plugin packages too. Bootstrap's separate per-plugin loop is now redundant and was removed |
| [x] Fix plugin-vs-plugin test shadowing | above | **The above fix was incomplete and looked complete with only one plugin installed.** Adding `okta_plugin` revealed that two `plugins/*/tests/` packages both resolve to the module name `tests.test_plugin`, so one silently shadowed the other: collection reported 102 tests when there were 122, and 20 tests stopped running while the suite still went green. Fixed by deleting `__init__.py` from every plugin `tests/` directory — importlib mode then derives a unique module name per file. `tests/unit/framework/test_plugin_test_layout.py` fails the build if one is re-added, and `CONNECTOR_GUIDE.md` §3 warns against it. Worth remembering as a pattern: a collection bug hides itself, because the tests that would fail are the ones not running |
| [x] Make `fetch()` async | ABC | Done 2026-08-25 per the decision below. `base.py`, both echo plugins, and their tests converted; `pytest-asyncio` added with `asyncio_mode = "auto"` |
| [x] Inject credentials via `PluginConfig` instead of per-plugin `os.getenv` | ABC, registry | Done 2026-08-25 per the decision below. New `src/add_it_all/plugins/config.py`; `discover_plugins(config)` injects; `required_credentials` + `mock_mode` + `configured` on the base class; `plugins list` gained a status column. The registry raises an actionable `PluginLoadError` if a plugin overrides `__init__` without calling `super().__init__(config)` |
| [x] Guard the entry-point handshake between core and every plugin package | registry | Added 2026-09-21. `tests/unit/framework/test_entry_point_registration.py`. A plugin whose entry-point group drifts from `registry.ENTRY_POINT_GROUP` **disappears silently** -- core searches a group it isn't in, finds nothing, and continues with one fewer connector. Verified the gap before writing the guard: pointing one installed package at another group left `plugins list` short a row and `add-it-all okta ...` reporting no such command, while the **full suite still passed**. Two reasons it hid: the only other discovery assertion is `discover_plugins()["echo"]`, and `echo` is declared in *core's own* pyproject so it is renamed by the same edit that renames `registry.py`; and every connector test builds its app with an explicit dict, never going through discovery. The guard derives its expectations by globbing `plugins/*/pyproject.toml` rather than hardcoding a list -- a hardcoded set would protect existing connectors and stay silent about the next one, which is the case that actually matters. `ENTRY_POINT_GROUP` is imported, not written out, so the test asserts agreement rather than a particular string and survives a project rename untouched |
| [x] Route connector errors through a scrubbing helper | ABC | Added 2026-08-25. `src/add_it_all/redaction.py::safe_error()`. Rule 4 of the contract turns every ordinary failure into `ConnectorResult(error=str(exc))`, which `Cache.put()` then writes to SQLite and the CLI prints -- so an httpx exception carrying a URL or auth header became a durable plaintext credential. Redacts `Bearer`/`SSWS`/`Basic` credentials, URL userinfo, secret query params, JWTs, and the literal values of secret-named env vars. The `echo_plugin` template now models the pattern; every connector must follow it |

**Stage 0 is done when:** a developer can install the package, run
`pytest -m plugin_framework`, see it pass, run `add-it-all plugins list`,
and see `echo` in the output.

---

## Stage 1 -- Cache & Data Model
**Status: verified green** -- `pytest -m cache` 8 passed, 2026-08-19.
`cache.py` at 100% line coverage, `models.py` at 95%. `config.py` is at 0% --
no test exercises `Settings` yet (see task below).

| Task | Depends on | Notes |
|---|---|---|
| [x] `Cache` class (SQLite, per plugin+identifier, TTL) | -- | `src/add_it_all/cache.py` |
| [x] `UnifiedRecord` merge/error model | -- | `src/add_it_all/models.py` |
| [x] `Settings` config loader (env vars / `.env`) | -- | `src/add_it_all/config.py` |
| [x] **Run `pytest -m cache` and confirm green** | Cache, model | 8 passed on Python 3.14.0, 2026-08-19 |
| [x] Write tests for `Settings` config loader | Settings | Done 2026-08-25 -- `tests/unit/config/test_settings.py`, `config.py` 0% -> 100%. Covers defaults, env-var override, `.env` fallback, env-beats-`.env` precedence, `Path` coercion, validation failure, and that unprefixed service creds (`OKTA_*`/`JIRA_*`) are ignored rather than absorbed. Note for future tests: `Settings` reads `.env` relative to **cwd**, so any test touching it must `chdir` to a tmp dir or it silently picks up the repo's real `.env` |
| [x] Cache retention: expiry must delete, not just hide | Cache | Done 2026-08-25. `get()` past TTL previously returned `None` but left the row on disk forever -- the cache holds employee PII (status, serials, ticket history) in plaintext. `get()` now deletes on expiry; added `purge_expired()` and `clear()`, plus `add-it-all cache path\|clear\|purge`. DB is created `0600` inside a `0700` directory |
| [x] Fix `~` not being expanded in `cache_db_path` | Settings | Found 2026-08-25 while smoke-testing `add-it-all cache path`. `.env.example` ships `ADD_IT_ALL_CACHE_DB_PATH=~/.add-it-all/cache.sqlite3`; pydantic coerced that to `Path("~/...")` verbatim, so the cache was created at `./~/.add-it-all/cache.sqlite3` — a plaintext store of employee PII **inside the git checkout** rather than in `$HOME`. Latent since Stage 1; nothing called `get_settings()` until now. Fixed with a `field_validator` calling `.expanduser()` |
| [ ] Decide & document per-plugin default TTLs (Okta status probably shorter than, say, Jamf device assignment) | none yet -- open decision | add to `docs/ARCHITECTURE.md` once decided |

**Stage 1 is done when:** `pytest -m cache` passes, and cache/model
behavior is exercised by at least one real plugin in Stage 2.

---

## Stage 2 -- Okta Connector (real API, credentials available)
**Status: mocks green** -- `pytest -m okta` 339 passed, 2026-09-15 (was 208
on 2026-09-02; the `-g` groups section and the `--team` group-comparison view
with `--html` export were added since). The unchecked tasks are all
live-org confirmations blocked on a real token in `.env`: the manual smoke
test, `--find` returning DEPROVISIONED users, and how `managerId` keys direct
reports.

| Task | Depends on |
|---|---|
| [x] Write mocked-response tests: active, suspended, deprovisioned, not-found, timeout/5xx | Stage 0 |
| [x] Implement `okta_plugin` package (copy `echo_plugin` template) | tests above |
| [x] Implement real Okta API client (`GET /api/v1/users/{login}`) behind `_call_backend` | tests above |
| [x] `add-it-all okta <user>` command (status is the default view) | plugin implemented |
| [x] Add `OKTA_ORG_URL` / `OKTA_API_TOKEN` to `.env.example` | plugin implemented |
| [ ] **Confirm a real token works in a manual smoke test** | a real token in `.env` |
| [x] Add `okta` marker to `pyproject.toml` pytest markers | -- |
| [x] `-d/--devices` — devices registered to a user | plugin implemented |
| [x] `-s/--status` — account status, flagging deactivated explicitly | plugin implemented |
| [x] Surface the `access_blocked` custom profile attribute under status | plugin implemented |
| [x] `--last-signin` — per-device last sign-in, opt-in, with `--since` | devices |
| [x] `-a`/`-apps`/`--apps` — applications assigned to a user (`/appLinks`) | plugin implemented |
| [x] `-u`/`-authenticators`/`--authenticators` — enrolled authenticators (`/factors`) | plugin implemented |
| [x] `--find` — resolve a partial name to a username (long-only, not chainable) | plugin implemented |
| [x] `--find` multi-token narrowing + `--all` to see past the display cap | `--find` |
| [ ] **Verify `--find` returns DEPROVISIONED users against the real org** | a real token in `.env` |
| [x] `-g`/`-groups`/`--groups` — a user's Okta group memberships (`/users/{id}/groups`) | plugin implemented |
| [x] `--team` — compare group memberships across a person's manager cohort (peers), `--html` writes a page | groups; `--find` shape |
| [ ] **Confirm what `managerId` holds against the real org** (login/email/id — keys the cohort search; see Open Decisions Log) | a real token in `.env` |

Notes from the implementation:

- **Not-found is a success, not an error.** `GET` returning 404 yields
  `data={"found": False}` with `ok is True`, not `error=`. The guide asks
  each connector to decide this explicitly: for an offboarding lookup "this
  person has no Okta account" is a real answer, whereas an `error=` makes
  `UnifiedRecord.field_for("okta")` return None -- indistinguishable
  from "Okta was unreachable".
- **The identifier is URL-encoded** (`quote(safe="")`). It is user input; an
  email's `@` must keep working while `../` must not walk off
  `/api/v1/users`. Covered by a test.
- **Requests are timeout-bounded** (`OKTA_TIMEOUT_SECONDS`, default 10), so a
  hanging service can't hang the whole aggregate.
- **Mock mode works with zero credentials** (`ADD_IT_ALL_MOCK_OKTA=1`), so the
  CLI can be demoed before a token is provisioned.
- **`-d` lists devices** via `GET /api/v1/users/{userId}/devices`, kept out of
  `fetch()` deliberately: Stage 7 runs `fetch()` for every plugin on every
  lookup and must not pay for a second round trip nobody asked for. `-sd`
  reuses the id it already resolved, so it costs two calls, not three. Link-header
  pagination is followed, bounded at 20 pages so a looping `next` can't hang.
- **Exit codes depend on what was asked for.** A section that is the *whole*
  answer fails the command: with `-d` alone a device-API failure exits 1, and
  scripts can trust it. Once more than one section is on screen the same
  failure degrades just that section and exits 0, because the others are real
  answers already printed. Generalised to all four sections on 2026-09-02
  (`sole_section` in `cli()`), so `-au` with a dead factors endpoint still
  prints the app list.
- **`access blocked` comes from the profile, not the status enum.** This org's
  Universal Directory defines a custom attribute (Profile Editor label
  "ACCESS BLOCKED", variable name `access_blocked`) that arrives inside the
  `profile` object of the payload `fetch()` already retrieves — so it costs no
  extra request. It is shown **verbatim**: a boolean renders `true`/`false`
  rather than being translated to yes/no, so an operator sees exactly what the
  Okta admin UI shows them. Absent or null renders `-`, kept deliberately
  distinct from an explicit `false` — "nobody ever set this" is not the same
  claim as "this person is not blocked". The attribute name is configurable via
  `OKTA_ACCESS_ATTRIBUTE`, since custom attribute names are org-specific and
  hardcoding ours would break the plugin for any other Okta org.
  - Only the one configured attribute is read, never the whole profile: Okta
    profiles routinely carry manager, employee id and personal contact details,
    and everything in `data` is written to the plaintext local cache.
  - It sits next to `status` rather than replacing it. If the attribute is
    synced from Workday/AD it can lag the real account state, and a
    disagreement between the two is exactly what an offboarding check wants to
    surface.
- **`--last-signin` is a two-source join, and opt-in.** `/users/{id}/devices`
  has no last-login field. Its `lastUpdated` is the trap: it moves when the
  device *record* changes (profile sync, management flip, OS bump), not when
  anyone signed in — wiring it up would have produced plausible, wrong answers,
  which in an offboarding tool is worse than showing nothing. Sign-in times come
  from `/api/v1/logs` instead, correlated on `device.id`.
  - **Opt-in, not automatic**, because `/api/v1/logs` is Okta's most
    rate-limited endpoint. A plain `-d` never touches it (there is a test).
  - **One query for all devices, not one per device.** Results come back
    `DESCENDING`, so the first sighting of a device is its most recent sign-in
    and paging stops as soon as every known device is accounted for.
  - **90-day wall.** Okta retains System Log data ~90 days, so `--since` is
    clamped to that. The window is printed in the column header rather than
    hidden in `--help`: a blank cell means "not in this window", never "never
    used", and for an offboarding review those are opposite conclusions.
  - **Unattributable events are dropped, not guessed.** Not every auth event
    stamps `device.id`. The tempting fallback — matching on `client.userAgent` —
    cannot tell two MacBooks apart and would assign one device's sign-in to
    another. An honest blank beats confident-wrong.
  - A log failure degrades the column to `?` and keeps the inventory; the
    devices list is a real answer on its own.
  - **Unverified against a real org.** Whether events populate `device.id`
    depends on Identity Engine and Okta Verify enrolment. If they do not, the
    column is honestly empty — worth confirming in the live smoke test.
- **`--find` is a mode, not a section.** Section flags answer "what do you
  want to see about this person"; `--find` answers "who is this person". There
  is nothing to describe until one has been picked, so combining them is a
  usage error (exit 2) with the two-step command printed, rather than silently
  dropping the section the user typed. Long-only by design — see the third
  flag category at the top of this file.
  - **⚠️ Unverified and load-bearing: Okta's List Users endpoint excludes
    `DEPROVISIONED` users by default.** For a tool whose central question is
    "did this person's access actually get revoked", a search that silently
    omits the deactivated person is worse than no search — it looks complete.
    `build_search_expression()` therefore sends **no status clause at all**,
    and there is a test pinning that the client never filters by status. But
    whether `search=` itself inherits the endpoint default is *server-side
    behaviour that mocks cannot prove*. **Check this in the live smoke test.**
    If deactivated users are missing, the fix is an explicit all-statuses
    clause in `build_search_expression()` — one function, one test.
  - **Whitespace splits the query into AND-ed groups.** `--find "dennis luo"`
    requires both tokens to match some field, so token order doesn't matter.
    Before this it matched *nobody*: the whole string became one `startsWith`
    term and no first name begins "dennis luo". That was doubly bad, because
    adding a surname is exactly the advice the "too many matches" message
    gives — the documented escape hatch was the one thing guaranteed to fail.
  - **Two different truncations, one flag.** The display cap
    (`MAX_MATCHES_SHOWN`, 15) hides rows already in memory and costs nothing
    to lift. The API cap (`MAX_SEARCH_RESULTS`, 200) is a page boundary and
    needs real requests to pass. `--all` lifts both: it prints every match
    and follows `Link: rel="next"` up to `_MAX_PAGES`. Long-only, because
    `-a` already means applications and a short `--all` would be genuinely
    ambiguous. `--all` outside `--find` is a usage error, not a no-op.
  - **Both truncation messages name the escape hatch.** An earlier version
    said only "narrow the search", which repeated the dead end the `--find`
    hint exists to prevent: it told you a way out existed without saying what
    it was.
  - **One request by default, no pagination following.** Search is interactive, not an
    audit; paging thousands of users to render a 15-row table would burn
    rate-limit budget. A full page back sets `truncated`, which the CLI
    reports rather than passing a capped list off as complete.
  - **Never auto-resolves, even on a single match.** One candidate is not the
    same claim as the right person, and the section flags act on whoever is
    named next.
  - **Results are never cached** (`data["cacheable"] is False`). A cached hit
    could report someone `ACTIVE` minutes after they were deactivated — wrong
    in exactly the case that matters. Stage 7's cache integration must honour
    that flag.
  - **The discoverability hint is not a fallback.** A failed exact lookup
    prints `Try: add-it-all okta --find <name>` but does *not* run a search —
    `--find` stays explicit and the miss path stays one API call. Not knowing
    the username is exactly the situation in which you also would not know
    the flag exists.
- **Flag convention:** short flags are section selectors (`-s`, `-d`, `-a`,
  `-u`, `-g`); long-only flags either modify how a section renders
  (`--last-signin`, `--since`, `--html`) or are *mode* flags that replace the
  whole operation (`--find`, `--team`). `-sdaug` now means "five sections";
  `-l` remains free. Spelling rules are in the CLI shape note at the top of
  this file — read them before adding a flag to another stage.
- **`-g` lists group memberships** via `GET /api/v1/users/{userId}/groups`,
  and is the per-person building block `--team` fans out over.
- **`--team` compares a team's group memberships.** The subject is *not*
  assumed to be a manager: the connector reads the subject's own manager from
  `profile.<managerAttr>` and gathers everyone who reports to that manager
  (the subject + their peers), so looking up an IC compares them against their
  teammates and looking up a manager compares them against their peer
  managers. A subject with no manager on file falls back to comparing their
  own direct reports (`cohort="reports"`). Each member's groups are fetched
  concurrently (`asyncio.gather` — the payoff of `fetch()` being async) and
  lined up into a matrix flagging *drift* rows (some-but-not-all coverage).
  `--html PATH` writes the same comparison as a self-contained page (all CSS
  inlined, no external resources; every directory value HTML-escaped). In that
  page, **columns are ordered oldest hire date first** (read from
  `OKTA_HIRE_DATE_ATTRIBUTE`, default `hireDate`, falling back to the account
  `created` date; unknown dates sort last) and **group rows are ordered
  shared-by-all first down to individual access, shaded as a prevalence heat
  map** with a legend — modelled on the reference sheet. (The terminal view is
  deliberately left as the plainer yes/– table.) **Deactivated
  (`DEPROVISIONED`) teammates are excluded by default** — the queried subject
  is always kept — and `--include-deactivated` opts them back in for an
  offboarding audit; the count excluded is surfaced in both the terminal and
  the page. One member's failed groups call degrades that column to `?` and is
  excluded from the maths rather than sinking the run; a failure building the
  roster is a hard error. Two org-specific unknowns remain — what the manager
  attribute holds (login/email/id) and whether `hireDate` is populated — both
  configurable; see the Open Decisions Log.
- **`-a` lists applications** via `GET /api/v1/users/{userId}/appLinks`, the
  same list that builds the user's Okta dashboard.
  - **It answers "what can they open", not "how were they granted it".**
    Direct-vs-group assignment lives on `/apps/{appId}/users/{userId}` and
    costs one request per app, so it is not fetched. Worth adding later behind
    its own opt-in flag if offboarding needs to know which group to remove
    someone from.
  - **Hidden tiles are listed, not filtered.** A hidden app is still a live
    assignment; skipping it would under-report access, which is the worst
    failure mode this tool has. The column is named `hidden` after the API
    field rather than inverted to `visible`, so nobody has to flip the sense
    of it in their head.
  - Sorted alphabetically, because Okta returns per-user dashboard sort order
    and two people's app lists are otherwise not comparable.
- **`-u` lists authenticators** via `GET /api/v1/users/{userId}/factors`. The
  API says "factor", the admin console says "authenticator"; the CLI follows
  the console and the API's word is kept for anything touching the wire.
  - **`profile.questionText` is deliberately dropped.** That a security
    question is enrolled is the useful fact; printing the question itself
    hands over a recovery-credential hint for no operational gain. There is a
    test asserting it never reaches the terminal.
  - **Inactive and half-finished enrolments are included.** A disabled
    authenticator is still enrolled, and `PENDING_ACTIVATION` is a real state
    — someone started an enrolment and never finished. Status is shown
    verbatim.
  - **A non-Okta provider is named in the label** (`Okta Verify push (DUO)`).
    A Duo push and an Okta Verify push are different systems to go and revoke.
    Unknown `factorType` values fall through to the raw wire value rather than
    getting an invented label.
- **All three list endpoints share one paging helper** (`_fetch_all_pages`),
  so devices, apps and authenticators cannot quietly drift apart on Link-header
  handling, the 20-page bound, or the self-referential-`next` guard.
- **`-s` translates the enum.** Okta has eight statuses; "deactivated" in the
  admin UI means `DEPROVISIONED` specifically, and `SUSPENDED` is a different
  state that also blocks login. The CLI prints a verdict line
  (`jdoe - DEPROVISIONED (deactivated 2025-11-02)`) so nobody has to translate
  in their head, and `data["deactivated"]` carries the boolean for Stage 7 and
  JSON consumers.
  - **Scope caveat to keep repeating to users:** this is Okta's own device
    registry (Okta Verify / device trust), *not* hardware inventory. Someone can
    hold a laptop Okta has never seen. Jamf (Stage 4) is the
    authoritative inventory sources, and once they exist the three views will
    disagree — that disagreement is itself useful for offboarding, but the CLI
    should never present Okta's list as "the devices this person has".

**Done when:** `pytest -m okta` green on mocks *(done)*, and one manual
`add-it-all okta <realuser>` against real Okta returns a sane result
*(pending)*.

---

## Stage 3 -- Jira Connector (real API, service account)
**Status: verified against the live instance** 2026-09-07 -- `pytest -m jira`
61 passed, plus a real smoke test of every path (default, `-r`, `-tr`,
`--all`, unknown person). Credentials are a **dedicated service account**, not a personal token --
better than the Okta situation. (Account name omitted: this repo is public.)

| Task | Depends on |
|---|---|
| [x] Write mocked-response tests before implementation | Stage 0 |
| [x] Implement `jira_plugin` package | tests above |
| [x] JQL query by accountId (**reporter vs assignee resolved -- see below**) | tests above |
| [x] `add-it-all jira <user>` with `-t/--tickets` and `-r/--reported` | plugin implemented |
| [x] `--all` to page the cursor and get a real count | plugin implemented |
| [x] **Live smoke test against real Jira** | service account in `.env` |
| [x] Leave room in `properties` for future status/project filters | plugin implemented |
| [x] `add-it-all jira ENG-123` -- look one issue up by key | plugin implemented |

Notes from the implementation -- all three API facts were found by probing
the live instance, not from documentation:

- **`/rest/api/3/search` is GONE.** It answers `410 Gone` pointing at
  `/rest/api/3/search/jql`. The original Stage 3 spec was written against
  it, and most tutorials still show it. There is a test pinning that we
  never call the removed endpoint.
- **The new endpoint refuses unbounded JQL** ("Please add a search
  restriction to your query"), so every query must carry a person filter.
  Fine here, but it rules out any "show me everything" convenience.
- **There is no `total`.** Paging is an opaque cursor (`nextPageToken` +
  `isLast`), and the response carries no count at all. So `count` is only a
  real count when `complete` is true; otherwise the CLI says **"at least
  N"**. Reporting a page size as a total would understate someone's
  workload, which is exactly the quietly-wrong answer this tool exists to
  catch. `--all` follows the cursor and yields a real number -- live, that
  was "at least 100" versus an actual **236**.
- **JQL cannot take a username.** GDPR-era changes removed usernames and
  emails from JQL, so a lookup is two steps: `/user/search` to get an
  `accountId`, then query with it. Passing a username matches nothing
  *without erroring* -- a silent empty answer.
- **Assignee is the default; reporter is `-r`.** Decided 2026-09-07 with
  real numbers in hand: for the same person the live instance returned 29
  reported versus 236 assigned. Assigned is the actionable set -- open work
  that needs reassigning when someone leaves, or that says what someone is
  stuck on. Reported is historical. They are shown under separate headings
  and never merged into one count.
- **An issue key is auto-detected, not hidden behind a flag.** `jira ENG-1`
  shows one ticket. The shapes are unambiguous -- an email always carries an
  `@`, a display name never has the letters-hyphen-digits form -- so this is
  detection rather than guessing. **Case-insensitive**, because the live API
  answers 200 for `eng-42`; a case-sensitive pattern would send the
  lowercase spelling down the person path and fail confusingly.
  - **No fallback from a failed key to a person search.** A string shaped
    like a key that Jira does not know is a typo, not a colleague.
  - **404 means "missing *or* no permission"** and the message says so. Jira
    deliberately does not distinguish, to avoid leaking issue existence, so
    reporting only "no such issue" would send someone hunting for a typo
    that isn't there.
  - **Section flags are rejected for a key** (exit 2, with the corrected
    command). `-r` means "reported by this person" and is meaningless for a
    single ticket.
  - **Reporter and creator are shown separately when they differ.** A ticket
    raised on a colleague's behalf has different people in each, and for a
    tool about people that is the interesting part. Identical values collapse
    to one row rather than duplicating.
  - **Descriptions are ADF, not text.** API v3 returns a nested Atlassian
    Document Format tree; `flatten_adf()` walks it. Block-level nodes emit a
    trailing space while inline runs concatenate -- joining everything with
    nothing welded a sentence end onto the next paragraph
    ("instance:1. Add/set up") on a real ticket. Unknown node types
    contribute nothing rather than raising, since Atlassian keeps adding them.
  - **Only rendered fields are requested**: the untrimmed issue is ~46KB
    (about 90 custom fields plus comments, worklog, attachments) against 6KB
    trimmed. Note descriptions routinely contain names and email addresses,
    and `data` reaches the plaintext local cache.
- **Several matching accounts is ambiguity, not a guess.** Picking the
  first would attribute someone else's tickets to the person asked about.
  A chooser is printed, same shape as `okta --find` and `cairo`.
- **Inactive accounts are resolved, not skipped.** A deactivated Jira user
  is exactly who an offboarding check is asking about.
- **Only rendered fields are requested** (`fields=key,summary,status,...`).
  Jira issues carry description bodies, comments and custom fields, and
  everything in `data` reaches the plaintext local cache.
- **Two bugs the mocks could not catch, both found live:**
  - Mock mode returned raw fixtures without passing them through
    `_to_issue`, so it exercised a different shape than the real path.
  - `--all` fetched 236 issues, rendered 15, and advised "use `--all`" --
    the flag already in use. Display policy was being inferred from whether
    the *fetch* was complete, which is a different question from what the
    user asked to see. Same dead-end class as the earlier "narrow the
    search" wording in `okta --find`; worth watching for a third time.

---|---|
| [ ] Write mocked-response tests: tickets found, zero results, pagination, auth error | Stage 0 |
| [ ] Implement `jira_plugin` package | tests above |
| [ ] JQL query `reporter = "<user>"` (confirm: reporter vs. assignee -- decide with team, document choice) | tests above |
| [ ] `add-it-all jira <user>` command with `-t/--tickets` (see the CLI shape note at the top of this file) | plugin implemented |
| [ ] Leave room in `properties` for future status/project filters (don't build the filter UI yet, just don't block it) | plugin implemented |

**Done when:** `pytest -m jira` green on mocks, manual smoke test against real Jira confirmed.

---

## Stage 4 -- Jamf Connector (mock-first, no credentials yet)

| Task | Depends on |
|---|---|
| [ ] Write tests against fixture data: devices found, zero devices, malformed fixture | Stage 0 |
| [ ] Build realistic fixture JSON (device name, serial, model, last check-in, assigned user) | -- |
| [ ] Implement `jamf_plugin` package with `ADD_IT_ALL_MOCK_JAMF` toggle | tests, fixtures |
| [ ] `add-it-all jamf <user>` command with `-d/--devices` (see the CLI shape note at the top of this file) | plugin implemented |
| [ ] **Blocked/parallel track:** once credentials exist, implement real `_call_backend` (Jamf Pro API) -- no test/CLI changes needed | credentials provisioned |

**Done when:** `pytest -m jamf` green against fixtures; real-API swap is a
separate, low-risk follow-up task once creds land.

---

## Stage 5 -- ABM Connector -- **DROPPED (2026-09-09)**

Removed from scope. Apple Business Manager does not tie in usefully to what
this tool answers: its value is purchase/enrollment provenance for Apple
hardware, and the questions this CLI actually gets asked -- who has what
access, is it still active, who owns it -- are already answered by Okta
(device trust), Jamf (managed inventory, Stage 4) and CAIRO (vendor and
application approval). ABM would have added a third partially-overlapping
device view whose disagreements with the other two would be noise rather
than signal.

Nothing was built, so nothing was removed beyond planning: there is no
`abm_plugin` package, no tests and no marker. The stage number is left in
place rather than renumbering 6-8, so existing `stage:N` labels and links
keep pointing at the same things.

**If it comes back:** the unresolved question was the auth path -- Apple's
Business Manager API (server-to-server, JWT via private key) versus going
through the MDM vendor's ABM proxy endpoints. That choice determines the
client's shape and should be settled before any code.

---

## Stage 6 -- allwhere Connector (mock-first, no credentials yet)

| Task | Depends on |
|---|---|
| [ ] Write tests against fixture data: shipments found, zero shipments, in-transit vs. delivered states | Stage 0 |
| [ ] Build realistic fixture JSON | -- |
| [ ] Implement `allwhere_plugin` package with `ADD_IT_ALL_MOCK_ALLWHERE` toggle | tests, fixtures |
| [ ] `add-it-all allwhere <user>` command with `-s/--shipments` (see the CLI shape note at the top of this file) | plugin implemented |

**Done when:** `pytest -m allwhere` green against fixtures.

---

## CAIRO -- TPRM vendor & application register (real API, credentials available)
**Status: verified against the live API** 2026-09-04 -- `pytest -m cairo`
50 passed, and `add-it-all cairo <name>` was smoke-tested against the real
dev instance across all four paths (exact match, ambiguous, no match,
vendor with no assessment). **This is the first connector confirmed working
end-to-end against a real service.**

Not person-scoped: the identifier is a vendor or application name. Excluded
from the Stage 7 person aggregate — see the person-scoped plugin list there.

| Task | Depends on |
|---|---|
| [x] Write mocked-response tests before implementation | Stage 0 |
| [x] Implement `cairo_plugin` package (copy `echo_plugin` template) | tests above |
| [x] Real client behind `_call_vendors_backend` / `_call_vendor_detail_backend` | tests above |
| [x] `add-it-all cairo <name>` command | plugin implemented |
| [x] Add `CAIRO_BASE_URL` / `CAIRO_API_KEY` to `.env.example` | -- |
| [x] Add `cairo` marker to `pyproject.toml` | -- |
| [x] **Live smoke test against the real org** | a real key in `.env` |
| [ ] Point at prod rather than the dev instance | a prod base URL |

Notes from the implementation:

- **The API has no server-side filtering.** `?search=`, `?name=`,
  `?status=`, `?limit=`, `?page=` are all accepted and all ignored —
  `/api/vendors` returns the entire register (713 records / ~730KB) every
  time. Matching is therefore ours to define and happens client-side:
  case-insensitive substring against `name` and `domain`. This makes the
  shared cache load-bearing rather than a nicety.
- **There is no `/api/applications`.** Application detail is nested inside
  the vendor detail: `GET /api/vendors/{id}` embeds `assessments[]`, each
  carrying an `engagement_context` describing one application and how it is
  used. A vendor can have several; 22 of 713 do.
- **Three fields are called some variant of "status" and none mean the same
  thing.** On live data they disagree on *every* record:

  | field | values | meaning |
  |---|---|---|
  | `vendor.status` | approved / denied / pending_approval / review_required / under_review | **approval — "allowed in our space"** (confirmed with the CAIRO owner) |
  | `assessment.status` | exempt / pending / pre_complete / post_complete | assessment workflow state |
  | `assessment.workflow_status` | draft / review_complete / tprm_review / … | review state |

  They are shown under distinct labels and never merged. Collapsing them
  would give a confident wrong answer to the one question this connector
  exists to answer.
- **"No assessment on file" is not "no applications".** 174 of 713 vendors
  have never been assessed, so the register simply has nothing to say about
  what they are used for. Rendered as an explicit message, never as an empty
  applications table.
- **Ambiguous matches are never auto-resolved.** Several candidates and no
  exact name match prints a chooser with each candidate's status. An
  approval answer for the wrong vendor is worse than making someone choose.
  An exact name still wins, so "Databright" resolves even when "Databright
  Plugin" also exists.
- **Personal data is dropped at the shaping step.** The register carries
  `primary_contact`, `owner`, `assigned_to`, `assigned_to_name` and
  `coupa_requester_email` for all 713 vendors. Everything in `data` is
  written to the plaintext local cache, and none of it is needed to answer
  "is this allowed", so none of it is carried. There are tests asserting it
  reaches neither `data` nor the terminal.
- **Descriptions are prose paragraphs and are summarised for the table.**
  Caught by the live smoke test, not by the mocks: a real description ran to
  several hundred characters and turned a one-row table into a fifteen-line
  block. `summarise()` prefers cutting at the first sentence, which on this
  data is reliably the "what is this" summary.
- **Adding this connector required zero changes to `src/add_it_all/`.**

---

## Stage 7 -- Aggregation & Output

| Task | Depends on |
|---|---|
| [ ] `add-it-all lookup <user>` -- runs the **person-scoped plugin list** (see below), merges via `UnifiedRecord` | Stages 2-6 (or however many are done) |
| [ ] One plugin erroring must not fail the whole command -- test with a deliberately broken mock plugin | above |
| [ ] `--format table\|json` output flag (default table via `rich`) | above |
| [ ] Cache integration: check cache before calling `fetch()`, write through after | Stage 1 cache |
| [ ] Snapshot/golden-file tests for table and JSON output | above |
| [ ] (Nice-to-have, not required for Stage 7 done-ness) concurrent fetch across plugins | above |

### Person-scoped plugin list (decided 2026-09-04)

`add-it-all lookup <identifier>` fans out to an **explicit list**, not to
every discovered plugin:

```
okta, jira, jamf, allwhere
```

**CAIRO is deliberately excluded.** It is keyed on a vendor/application
name, not a person, so `lookup dluo` would search it for *a vendor named
dluo* and report nothing found — a silent wrong answer in the aggregate
view, which is the one people trust most.

The exclusion is an explicit list in the aggregate command rather than a
flag on the plugin (a `person_scoped` / `subjects` attribute on
`ConnectorPlugin` was the alternative). Chosen because it required **no
change to core**: adding CAIRO touched only `plugins/cairo_plugin/`, docs,
and `.env.example`. Trade-off to be aware of: a new person-scoped connector
must be added to this list by hand, and forgetting means it silently never
runs in the aggregate. Whoever builds Stage 7 should put a test on the list
contents so that failure is loud.

**Done when:** `pytest -m cli` green, and `add-it-all lookup <user>`
against a mix of real + mocked plugins produces a readable combined result.

---

## Stage 8 -- Extensibility Proof & Docs Finalization

| Task | Depends on |
|---|---|
| [ ] A team member who did **not** write the plugin framework builds a throwaway 6th plugin using only `docs/CONNECTOR_GUIDE.md` | Stages 0-7 |
| [ ] Confirm zero edits were needed inside `src/add_it_all/` | above |
| [ ] Fold any friction points found into `docs/CONNECTOR_GUIDE.md` | above |
| [ ] Final pass on `README.md`, `docs/ARCHITECTURE.md` for accuracy vs. what actually got built | above |

**Done when:** the extensibility claim in the README is empirically true, not aspirational.

---

## Open decisions log

Track anything raised above that needs a team/product decision before
the relevant stage can finish, so it doesn't get lost in a task list:

- [ ] Per-plugin cache TTLs (Stage 1)
- [ ] Jira: reporter vs. assignee for "tickets submitted" (Stage 3)
- [x] ~~`fetch()` sync vs. async~~ **Resolved 2026-08-25: async.** Done before
      the first real connector, while the cost was one template plugin rather
      than five. `ConnectorPlugin.fetch()` is `async def`; Stage 7 will gather
      with `asyncio.gather`, so a lookup costs the slowest service rather than
      the sum of all five. `pytest-asyncio` with `asyncio_mode = "auto"` means
      connector authors write plain `async def test_...` with no decorator.
      A regression test asserts `inspect.iscoroutinefunction(plugin.fetch)`.
- [x] ~~How plugins receive credentials~~ **Resolved 2026-08-25: inject a
      `PluginConfig`.** See `src/add_it_all/plugins/config.py`. Core builds one
      config (merging `.env` and the process environment, environment winning)
      and passes it to every plugin at discovery. Plugins declare
      `required_credentials`; `plugins list` now shows configured / mock /
      missing-and-which. `mock_mode` is built into the base class following the
      existing `ADD_IT_ALL_MOCK_<PLUGIN>` convention. Non-obvious payoff:
      `bootstrap.sh` writes credentials to `.env` and nothing exports them, so
      plugins reading only `os.environ` would have seen nothing after a
      developer followed the README.
- [ ] **Okta token: personal read-only token vs. dedicated service account
      (Stage 2).** Decided 2026-08-25 to start with a personal read-only API
      token to unblock development. Okta SSWS tokens act as the creating user
      and inherit their permissions, and they expire after ~30 days of
      inactivity -- so before this tool goes to more than one operator, swap
      to a dedicated service account with a read-only admin role and a named
      rotation owner. Revisit at Stage 8, and do not skip it at rollout.
- [ ] **Audit logging (Stage 7).** Nothing records who looked up whom. A tool
      that queries HR/IT systems about named employees will likely need that
      for compliance, and the aggregator is the natural choke point -- much
      cheaper to add while Stage 7 is being written than afterwards.
- [ ] **Authorization model (out of scope for v1?).** The CLI has whatever its
      tokens have: anyone who can run it can read every user in Okta/Jira.
      Probably acceptable for a small IT team, but state it as an explicit
      scope boundary rather than leaving it implicit.
- [x] ~~Whether CLI subcommands for each plugin live in that plugin's own
      package or stay centralized in `src/add_it_all/cli.py`~~ **Resolved
      2026-08-25: in the plugin package.** Forced by Stage 2 -- adding
      `add-it-all okta` wiring to core `cli.py` would have broken ground rule
      2 for every future connector, and falsified the Stage 8 claim before
      Stage 8 ran. `ConnectorPlugin.cli()` returns an optional `typer.Typer`
      and `build_app()` mounts it under the plugin's name. One generic core
      change, made deliberately and flagged, so no connector edits core again.
      `okta_plugin` was built with **zero** edits to `src/add_it_all/` beyond
      that hook.
- [x] ~~Supported Python versions (Stage 0)~~ **Resolved 2026-08-25:** CI now
      runs a `["3.11", "3.13"]` matrix, so the floor declared by
      `requires-python` and the version developers actually use are both
      covered. 3.13 chosen over 3.14 as the local standard because it is
      available as a stock Homebrew formula. Evidence the skew was benign:
      the 3.13 run reported the same `152 stmts / 20 miss` as the earlier
      3.14 run.
- [x] ~~Whether `plugins/*/tests` should be collected by the root `pytest`
      run (Stage 0)~~ **Resolved 2026-08-25:** yes, via
      `testpaths = ["tests", "plugins"]` + `--import-mode=importlib`. See the
      Stage 0 task row for why the import mode is mandatory.
- [x] ~~How to spell multi-section short flags so they still bundle
      (Stage 2)~~ **Resolved 2026-09-02:** declare three spellings per
      section — `-a` / `-apps` / `--apps`. Verified against the real Click
      parser before writing tests, not assumed. `-sdapp` cannot be made to
      work in any declaration scheme, because Click decomposes an unmatched
      single-dash string character by character; `-sdau` is the bundled
      equivalent. The spike also killed the obvious-looking option of
      declaring `-au` as an authenticators alias: it would have made `-au`
      mean authenticators-only while `-sdau` still meant apps+authenticators,
      the same letters meaning different things by position. Full rules in the
      CLI shape note at the top of this file.
- [ ] **Should apps and authenticators become one combined "access" view?**
      Both answer "what can this person still get into", and an offboarding
      operator likely wants them together. Left as two flags for now because
      they are independent API calls with independent failure modes, and
      `-au` already composes them. Revisit once Stage 7 aggregation exists.
- [ ] **Direct-vs-group app assignment is not fetched.** `-a` says a user has
      an app, not which group granted it — that needs one
      `/apps/{appId}/users/{userId}` call per app. If offboarding needs to
      know *which group to remove someone from*, add it behind its own opt-in
      flag (same reasoning as `--last-signin`), never automatically.
- [x] ~~What "team" means (reports vs. peers) for Okta `--team`~~ **Resolved
      2026-09-15: peers (the subject's manager cohort).** `okta <user> --team`
      reads the subject's own manager from `profile.<managerAttr>` and compares
      everyone who reports to that manager — the subject and their peers — so
      the result is the same whether the queried user is a manager or an IC
      (an IC is compared to their teammates; a manager to their peer managers).
      A subject with no manager on file falls back to comparing their own
      direct reports, tagged `cohort="reports"`, so a top-of-tree lookup still
      answers something. The earlier direct-reports-only reading assumed the
      subject was always the manager, which is wrong for the common case of
      looking up a teammate.
- [ ] **How the manager link is keyed (Okta `--team`).** The cohort is found
      via `GET /users?search=profile.<attr> eq "<managerRef>"`, where
      `<attr>` is `OKTA_MANAGER_ATTRIBUTE` (default `managerId`) and
      `<managerRef>` is whatever that attribute holds on the subject's profile.
      Whether `managerId` holds the manager's login, email or employee id is a
      per-org Universal Directory decision the mocks cannot prove; if the value
      it holds is not what other users' `managerId` is compared against, the
      cohort comes back as just the subject. The attribute is configurable,
      but confirm the org's schema in the Stage 2 live smoke test before
      relying on the roster. Group memberships come from `GET /users/{id}/
      groups`, also new and exercised by the `-g` section.
- [x] ~~Whether `--team` should include deactivated teammates~~ **Resolved
      2026-09-15: exclude by default, `--include-deactivated` opts in.** The
      comparison answers "who currently has access", so a `DEPROVISIONED`
      teammate is dropped (and the count surfaced) rather than shown as a
      column of gaps. The queried subject is never dropped — looking up a
      leaver to check their own access was revoked is a legitimate use — and
      `--include-deactivated` restores everyone for an offboarding audit. The
      filter is **client-side**, so the default holds regardless of API
      behaviour; but note Okta's List Users endpoint is documented to exclude
      `DEPROVISIONED` by default (same caveat as `--find`), so
      `--include-deactivated` will only actually surface leavers if the search
      returns them — unverified against a real org, flag for the live smoke
      test. SUSPENDED/LOCKED_OUT are *not* filtered: those are active accounts
      that are merely blocked, not deactivated.
- [ ] **Authenticators and devices are not joined.** An Okta Verify push
      factor and an Okta device registry entry can refer to the same phone,
      but the factor `profile.name` and the device `displayName` are only
      correlatable by string match, which would be a guess. Left unjoined
      deliberately; revisit only if a reliable id links them.
- [x] ~~How to stop CAIRO being swept into the person aggregate~~
      **Resolved 2026-09-04:** an explicit person-scoped plugin list in the
      Stage 7 aggregate command, not a flag on `ConnectorPlugin`. See Stage 7.
- [x] ~~`UnifiedUserRecord` assumes every lookup is about a person~~
      **Resolved 2026-09-04:** renamed to `UnifiedRecord`. Done while it had
      zero callers (Stage 7 unbuilt), which is the cheapest it would ever be.
      The class was already subject-agnostic — `identifier: str` plus a dict
      of results — so only the name and docstrings taught the wrong model.
- [ ] **CAIRO points at a dev instance.** The real host lives in `.env`
      only -- `.env.example` carries a placeholder, because this repo is
      public. Switch to prod when a prod host exists; the var is already
      configurable, so it is a `.env` edit and no code change.
- [ ] **Duplicate vendor records with conflicting approval status.** The
      live register was observed holding two records for the same vendor —
      one `under_review`, one `approved` — i.e. two different answers to "is
      this allowed". The CLI surfaces both in the chooser rather than picking
      one, which is the correct behaviour, but it is a data-quality issue
      worth raising with the CAIRO owners rather than papering over in the
      client. (Specific vendor names deliberately omitted: this repo is
      public.)
- [ ] **The candidate chooser is now implemented twice.** `okta --find` and
      `cairo <name>` solve the same problem (fuzzy input, several candidates,
      never guess) and render the same shape: a table with each candidate's
      status, exact-match-wins, a cap with "N more not shown", and no
      auto-resolution on a single hit. They are deliberately duplicated
      because `plugins/CLAUDE.md` forbids importing across plugin packages,
      and extracting shared rendering into `src/add_it_all/` is a core
      decision that a connector task should not make as a side effect. If a
      third connector needs it, that is the signal to extract — a
      `add_it_all.chooser` helper taking rows plus column labels. Two copies
      is cheaper than the wrong abstraction; three is not.
- [x] ~~Okta uses a personal read-only API token rather than a service
      account (Stage 2)~~ **Resolved 2026-09-09:** swapped to a read-only
      service account. Verified it still reaches every endpoint the
      connector uses -- including the two most likely to be withheld from a
      restricted role, `/api/v1/logs` (`--last-signin`) and directory-wide
      `?search=` (`--find`). Both 200.
- [x] ~~Does Okta's `search=` inherit the List Users default that excludes
      DEPROVISIONED users?~~ **Resolved 2026-09-09: no, it includes them.**
      Verified against the live org by finding a known deprovisioned user
      and confirming a name search returns them. `--find` is therefore safe
      for offboarding, and `build_search_expression()` correctly sends no
      status clause. This was the load-bearing unknown behind `--find`.
- [x] ~~Do Okta log events populate `device.id`?~~ **Resolved 2026-09-09:
      yes** -- 61 of 200 sign-in events in a 90-day window carried one, so
      `--last-signin` populates rather than being honestly empty.
- [ ] **Okta device registry holds duplicate entries.** The live org returns
      the same device name more than once (re-enrolments), and Android
      devices report no serial, so `-d` rows can be hard to tell apart. The
      CLI shows them verbatim, which is correct, but it is worth raising
      with whoever owns Okta device trust -- same class as the duplicate
      vendor records in CAIRO.
- [ ] Which stage marker cross-cutting core utilities belong to.
      `test_redaction.py` was filed under `plugin_framework` because error
      handling is part of the plugin contract in `base.py`, but it is not
      registry/discovery. If more core utilities land, consider a
      `core`/`security` marker instead of stretching `plugin_framework`.
