# CLAUDE.md (plugins/)

You're working inside a connector plugin package. Scope rules for this directory:

- Each subdirectory here (`echo_plugin/`, and eventually `okta_plugin/`,
  `jira_plugin/`, `jamf_plugin/`, `allwhere_plugin/`) is
  its own independently installable Python package with its own
  `pyproject.toml` and its own `tests/`.
- Never import across plugin packages. Never import from one plugin
  into another. Shared code belongs in `src/lookup_cli/` core, and
  adding something there is a bigger decision -- flag it rather than
  doing it as a side effect of a plugin task.
- `echo_plugin/` is the copy-from-here template, not a real service --
  don't "improve" it as if it were production code; keep it minimal and
  readable since its job is to be read by the next person adding a plugin.
- Every real connector package needs: tests written before
  implementation, a `_call_backend`-style seam for mock/real swapping,
  and a `fetch()` that never raises for ordinary failures.
- Full walkthrough: `../docs/CONNECTOR_GUIDE.md`.
