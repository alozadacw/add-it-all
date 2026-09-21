# Adding a new connector plugin

Follow this for Jamf/allwhere, and for any future service. This
project is TDD: write the test file before the implementation file for
every step below that produces code.

## 1. Copy the template

```bash
cp -r plugins/echo_plugin plugins/<service>_plugin
mv plugins/<service>_plugin/echo_plugin plugins/<service>_plugin/<service>_plugin
```

## 2. Update `pyproject.toml` in the new package

- `project.name` -> `lookup-cli-<service>-plugin`
- entry point -> `<service> = "<service>_plugin.plugin:<Service>Plugin"`

The entry point group name (`lookup_cli.plugins`) must stay identical --
that's the contract the registry scans for.

## 3. Write tests first

Start the file with that stage's marker, so `pytest -m <service>` covers the
plugin package and not just core:

```python
pytestmark = pytest.mark.<service>
```

(`testpaths` includes `plugins`, so these run in the default suite and in CI
automatically. `--strict-markers` is on: register the marker in the root
`pyproject.toml` first or collection fails.)

**Do not put an `__init__.py` in your plugin's `tests/` directory.** Two
plugins whose test dirs are both packages named `tests` resolve to the same
module name, and one silently shadows the other — the suite stays green
while a connector's tests quietly stop running. `test_plugin_test_layout.py`
in core fails the build if one reappears.

In `plugins/<service>_plugin/tests/test_plugin.py`, write cases for:

- a normal successful lookup (assert on `.data` shape)
- "identifier not found" (should this be `error=` or empty `data`? decide
  per-service and document it in the plugin's own docstring)
- an auth/HTTP error (assert `.error` is set, `.ok is False`, and that
  `fetch()` did NOT raise)
- (if the service paginates) a multi-page result assembles correctly

Mock the HTTP layer -- don't hit the real API in unit tests. Use
`respx` for `httpx`-based clients.

## 4. Implement `fetch()`

**`fetch()` is `async def`.** Stage 7 aggregates every plugin concurrently
with `asyncio.gather`, so a serial lookup would cost the sum of every
service's round trip. Use `httpx.AsyncClient`, and `await` your backend
call. Tests are plain `async def test_...` -- pytest runs them
automatically (`asyncio_mode = "auto"`).

Keep the actual HTTP/SDK call in its own method (see `_call_backend` in
the template) so:
- tests can monkeypatch just that method when a full HTTP mock isn't
  needed, and
- swapping in mock-mode vs. real-mode implementations later is a
  one-method change.

`fetch()` itself must never raise for ordinary failure modes -- catch
and return `ConnectorResult(error=...)` instead. Let unexpected bugs
propagate (don't swallow everything with a bare `except: pass`).

**Scrub the error string. Never `str(exc)` directly:**

```python
from lookup_cli.redaction import safe_error

async def fetch(self, identifier: str) -> ConnectorResult:
    try:
        data = await self._call_backend(identifier)
    except Exception as exc:
        return ConnectorResult(plugin_name=self.name, identifier=identifier,
                               error=safe_error(exc))
    return ConnectorResult(plugin_name=self.name, identifier=identifier, data=data)
```

That string is not ephemeral -- `Cache.put()` writes it into the SQLite
cache and the CLI prints it. An httpx exception routinely carries the
request URL, and a mishandled one can carry an auth header, so a plain
`str(exc)` turns a transient 401 into a durable plaintext credential on
disk. `safe_error()` strips `Bearer`/`SSWS`/`Basic` credentials, URL
userinfo, secret query parameters, JWTs, and the literal values of
secret-named environment variables.

If your client holds a credential that isn't exposed as an obviously-named
env var, pass it explicitly: `safe_error(exc, secrets=[self._token])`.

## 5. Declare credentials and support mock mode

Never call `os.getenv` in a plugin. Core builds one `PluginConfig` -- merging
`.env` and the process environment, with the environment winning -- and
injects it as `self.config`. Reading `.env` centrally matters: `bootstrap.sh`
writes credentials there and nothing exports them, so a plugin reading only
`os.environ` would see nothing after a developer followed the README.

Declare what you need in `required_credentials`, and core reports an
unconfigured plugin up front (`lookup-cli plugins list` shows a status
column) instead of letting it fail mid-lookup.

`mock_mode` is built in and follows the `LOOKUP_CLI_MOCK_<PLUGIN>`
convention already in `.env.example` -- no per-plugin flag plumbing, and a
mock-mode plugin counts as configured even with no credentials.

```python
class JamfPlugin(ConnectorPlugin):
    name = "jamf"
    required_credentials = ("JAMF_BASE_URL", "JAMF_CLIENT_ID", "JAMF_CLIENT_SECRET")

    async def _call_backend(self, identifier: str) -> dict:
        if self.mock_mode:                      # LOOKUP_CLI_MOCK_JAMF=1
            return self._mock_fixture(identifier)
        base_url = self.config.require("JAMF_BASE_URL")   # raises MissingCredential
        ...
```

If you override `__init__`, you must call `super().__init__(config)` -- the
registry raises a `PluginLoadError` telling you so if you forget.

In tests, inject instead of monkeypatching the environment:

```python
plugin = JamfPlugin(PluginConfig({"JAMF_BASE_URL": "https://jamf.test", ...}))
```

Keep fixtures in `plugins/<service>_plugin/<service>_plugin/fixtures/`
as small, realistic JSON files -- these double as the shape documentation
for whoever eventually wires up the real API.

## 6. Register optional fields via `properties`/`tags`

Don't grow `ConnectorResult.data`'s schema for one-off extra fields --
put them in `properties` (structured) or `tags` (labels). This is what
the original spec's "leave room for optionals" requirement maps to.

## 7. Wire the CLI

Return a `typer.Typer` from your plugin's `cli()` method. Core mounts it
under the plugin's name automatically, so **never add per-service wiring to
`src/lookup_cli/cli.py`** -- that is what keeps ground rule 2 true and makes
the Stage 8 claim (a new connector with zero core edits) hold.

The shape is `lookup-cli <service> <identifier> [flags]`: the identifier is
a direct argument on a callback, and flags select sections. No noun
subcommands -- see "CLI shape" at the top of `docs/STAGES.md` for why.

```python
def cli(self) -> typer.Typer:
    sub_app = typer.Typer(
        help="Jamf lookups.",
        # Without this, Click stops parsing options at the first positional:
        # `jamf jdoe -d` fails while `jamf -d jdoe` works.
        context_settings={"allow_interspersed_args": True},
    )

    @sub_app.callback(invoke_without_command=True)
    def jamf(
        identifier: str = typer.Argument(..., help="Username or email."),
        devices: bool = typer.Option(False, "--devices", "-d", help="..."),
    ) -> None:
        """Look one person up in Jamf."""
        ...

    return sub_app
```

Two conventions worth copying from `okta_plugin`:

- **Bare `<service> <identifier>` shows the primary view**; other flags are
  additive.
- **Exit codes follow what was asked for.** If a section the user explicitly
  requested is the only thing they asked for, its failure should exit 1 so
  scripts can trust it. If another section already answered them, degrade
  that one section and exit 0.

`asyncio.run(self.fetch(...))` bridges the async plugin to the sync CLI.

## 8. Install and verify

```bash
pip install -e plugins/<service>_plugin
pytest -m <service>          # add the marker to pyproject.toml first
lookup-cli plugins list      # confirm it shows up
lookup-cli <service> <identifier> [flags]
```

## 9. Document service-specific env vars

Add them to `.env.example` with a comment, following the existing
pattern for Okta/Jira/Jamf/allwhere.

---

**Proof this works:** Stage 8 of the project plan requires building a
throwaway 6th plugin using *only* this guide, with zero edits to
`src/lookup_cli/`. If that stage requires a core-code change, this
guide (or the plugin contract) has a gap that needs fixing before the
project is considered "done" architecturally.
