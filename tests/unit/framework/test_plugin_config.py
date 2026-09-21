"""
Stage 0: how connector plugins receive credentials.

Decided 2026-08-25 (see the Open Decisions Log in docs/STAGES.md): core
builds a `PluginConfig` and injects it into every plugin, rather than each
plugin reaching into `os.environ` itself. That gives core a way to tell a
configured plugin from an unconfigured one, and lets tests inject values
instead of monkeypatching the environment.

Run just this stage:  pytest -m plugin_framework
"""

from __future__ import annotations

import pytest

from lookup_cli.plugins.base import ConnectorPlugin, ConnectorResult
from lookup_cli.plugins.config import MissingCredential, PluginConfig

pytestmark = pytest.mark.plugin_framework


class _FakePlugin(ConnectorPlugin):
    name = "fake"
    required_credentials = ("FAKE_API_TOKEN",)

    async def fetch(self, identifier: str) -> ConnectorResult:
        return ConnectorResult(plugin_name=self.name, identifier=identifier)


# --- Reading values ----------------------------------------------------------


def test_explicit_mapping_is_usable_without_touching_the_environment():
    config = PluginConfig({"OKTA_API_TOKEN": "fake-token"})
    assert config.get("OKTA_API_TOKEN") == "fake-token"


def test_get_returns_none_for_absent_key():
    assert PluginConfig({}).get("NOPE") is None


def test_get_honours_an_explicit_default():
    assert PluginConfig({}).get("NOPE", "fallback") == "fallback"


def test_require_returns_the_value_when_present():
    assert PluginConfig({"K": "v"}).require("K") == "v"


def test_require_raises_a_named_error_when_absent():
    with pytest.raises(MissingCredential) as excinfo:
        PluginConfig({}).require("OKTA_API_TOKEN")
    assert "OKTA_API_TOKEN" in str(excinfo.value)


def test_require_treats_an_empty_value_as_missing():
    """.env.example ships `OKTA_API_TOKEN=` -- an unfilled blank is not a value."""
    with pytest.raises(MissingCredential):
        PluginConfig({"OKTA_API_TOKEN": ""}).require("OKTA_API_TOKEN")


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_flag_recognises_truthy_spellings(raw):
    assert PluginConfig({"LOOKUP_CLI_MOCK_JAMF": raw}).flag("LOOKUP_CLI_MOCK_JAMF") is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "", "off"])
def test_flag_recognises_falsy_spellings(raw):
    assert PluginConfig({"LOOKUP_CLI_MOCK_JAMF": raw}).flag("LOOKUP_CLI_MOCK_JAMF") is False


def test_flag_is_false_when_absent():
    assert PluginConfig({}).flag("LOOKUP_CLI_MOCK_JAMF") is False


# --- Building from the environment -------------------------------------------


def test_from_env_reads_process_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OKTA_API_TOKEN", "from-shell")
    assert PluginConfig.from_env().get("OKTA_API_TOKEN") == "from-shell"


def test_from_env_reads_dotenv_file(monkeypatch, tmp_path):
    """Critical: bootstrap writes credentials into .env, and nobody exports them.

    If plugins only saw os.environ, filling in .env as the README instructs
    would leave every connector unconfigured.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OKTA_API_TOKEN", raising=False)
    (tmp_path / ".env").write_text("OKTA_API_TOKEN=from-dotenv\n", encoding="utf-8")

    assert PluginConfig.from_env().get("OKTA_API_TOKEN") == "from-dotenv"


def test_process_environment_beats_dotenv(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("OKTA_API_TOKEN=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("OKTA_API_TOKEN", "from-shell")

    assert PluginConfig.from_env().get("OKTA_API_TOKEN") == "from-shell"


def test_from_env_works_with_no_dotenv_present(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    PluginConfig.from_env()  # must not raise


# --- Injection into plugins ---------------------------------------------------


def test_plugin_uses_the_injected_config():
    plugin = _FakePlugin(PluginConfig({"FAKE_API_TOKEN": "abc"}))
    assert plugin.config.require("FAKE_API_TOKEN") == "abc"


def test_plugin_falls_back_to_the_environment_when_none_injected(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FAKE_API_TOKEN", "from-shell")
    assert _FakePlugin().config.get("FAKE_API_TOKEN") == "from-shell"


def test_configured_is_true_when_required_credentials_are_present():
    assert _FakePlugin(PluginConfig({"FAKE_API_TOKEN": "abc"})).configured is True


def test_configured_is_false_when_a_required_credential_is_missing():
    assert _FakePlugin(PluginConfig({})).configured is False


def test_configured_is_false_when_a_required_credential_is_blank():
    assert _FakePlugin(PluginConfig({"FAKE_API_TOKEN": ""})).configured is False


def test_plugin_with_no_required_credentials_is_always_configured():
    class NoCreds(ConnectorPlugin):
        name = "nocreds"

        async def fetch(self, identifier: str) -> ConnectorResult:
            return ConnectorResult(plugin_name=self.name, identifier=identifier)

    assert NoCreds(PluginConfig({})).configured is True


# --- Mock mode ----------------------------------------------------------------


def test_mock_mode_follows_the_documented_env_var_convention():
    """.env.example uses LOOKUP_CLI_MOCK_<PLUGIN>; bake it into the contract."""
    plugin = _FakePlugin(PluginConfig({"LOOKUP_CLI_MOCK_FAKE": "1"}))
    assert plugin.mock_mode is True


def test_mock_mode_is_false_by_default():
    assert _FakePlugin(PluginConfig({})).mock_mode is False


def test_a_mock_mode_plugin_counts_as_configured_without_credentials():
    """Jamf/allwhere run mock-first, with no credentials provisioned."""
    plugin = _FakePlugin(PluginConfig({"LOOKUP_CLI_MOCK_FAKE": "1"}))
    assert plugin.configured is True


# --- Secret hygiene -----------------------------------------------------------


def test_repr_does_not_leak_credential_values():
    """A PluginConfig can end up in a traceback or a debug log."""
    config = PluginConfig({"OKTA_API_TOKEN": "s3cr3t-value-here"})
    assert "s3cr3t-value-here" not in repr(config)
