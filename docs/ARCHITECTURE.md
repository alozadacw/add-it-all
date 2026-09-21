# Architecture

## Goals driving every decision here

1. Adding a new service must never require editing core code.
2. One service being down/slow/uncredentialed must never break lookups
   against the other services.
3. Every connector must be independently testable, and independently
   fake-able (mock mode), so development isn't blocked on credentials.

## Layers

```
CLI (typer subcommands)
   |
   v
Aggregator  --uses-->  Cache (SQLite, per plugin+identifier, TTL)
   |
   v
Plugin Registry (entry_points discovery)
   |
   v
ConnectorPlugin implementations (Okta, Jira, Jamf, allwhere, ...)
```

### Plugin contract (`src/lookup_cli/plugins/base.py`)

```python
class ConnectorPlugin(ABC):
    name: str
    def fetch(self, identifier: str) -> ConnectorResult: ...
```

`ConnectorResult` is the uniform envelope every plugin returns:

| field | purpose |
|---|---|
| `data` | the fields the plugin's docs promise (e.g. Okta's `status`) |
| `properties` | open-ended extra fields -- the "room to expand" the spec asked for |
| `tags` | free-form labels, also expandable without a schema change |
| `error` | set instead of raising, so aggregation degrades gracefully |

**Why a dataclass envelope instead of each plugin returning its own
shape:** the CLI's table/JSON renderer and the cache layer only need to
understand `ConnectorResult` once, ever. A new plugin slots into both
without either being touched.

### Plugin discovery (`src/lookup_cli/plugins/registry.py`)

Plugins register under the `lookup_cli.plugins` entry-point group in
their own `pyproject.toml`:

```toml
[project.entry-points."lookup_cli.plugins"]
okta = "okta_plugin.plugin:OktaPlugin"
```

`discover_plugins()` scans that group via `importlib.metadata`,
instantiates each class, and validates it against the contract at
**load time** (`PluginLoadError` if malformed) -- not lazily at first
lookup, so a broken plugin is caught in CI/startup, not by a confused
end user.

This is why each real connector lives in its own installable package
under `plugins/<name>_plugin/` rather than inside `src/lookup_cli/`:
it proves the "install a package, get a new command" story actually
works, rather than us privileging some plugins as "built-in."

### Cache (`src/lookup_cli/cache.py`)

SQLite table keyed on `(plugin_name, identifier)`. Each row has its own
`fetched_at`; TTL is checked at read time. This means:

- A fresh Okta result and a stale/expired Jamf result for the same
  person coexist correctly.
- Cache is fully explainable by inspecting one SQLite file --
  deliberately no background eviction daemon or process for v1.

### Aggregation & `UnifiedRecord` (`src/lookup_cli/models.py`)

`UnifiedRecord.from_results(identifier, [result, result, ...])`
merges N `ConnectorResult`s. `.field_for(plugin_name)` returns `None`
for any plugin that didn't run or errored, rather than raising --
callers (CLI renderer) are written to expect gaps.

### Mock mode

Each real connector plugin owns a `_call_backend`-style seam (see
`plugins/echo_plugin/echo_plugin/plugin.py`) so a mock/fixture-backed
implementation can stand in for the real HTTP client via an env var
(`LOOKUP_CLI_MOCK_<PLUGIN>=1`) without changing the plugin's public
`fetch()` contract, its tests, or anything upstream of it. This is how
Jamf/allwhere get built *before* credentials exist.

## Non-goals for v1

- No REPL/interactive shell (subcommands only, per project decision).
- No secrets manager/keychain integration (env vars/.env, per project
  decision) -- revisit if this becomes a multi-user shared tool.
  Credentials reach plugins through an injected `PluginConfig` (see
  `src/lookup_cli/plugins/config.py`), not via each plugin calling
  `os.getenv`; that is what lets core report an unconfigured plugin
  before a lookup rather than during one.

## Resolved since v1 scoping

- **`fetch()` is async.** Originally listed as a non-goal ("no
  async/concurrency in the aggregator yet"). Reversed 2026-08-25 and done
  up front instead: with five HTTP services a serial aggregate costs the
  *sum* of five round trips, and changing the signature after five
  connectors exist is a breaking change to every one of them. Stage 7
  gathers with `asyncio.gather`.
- **Connector errors are scrubbed.** `fetch()` never raising means every
  ordinary failure becomes a cached, printed string; `safe_error()` in
  `src/lookup_cli/redaction.py` strips credentials out of it first.
