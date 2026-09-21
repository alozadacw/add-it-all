"""
Guards the entry-point handshake between core and every plugin package.

Core never imports a connector. Each package *advertises* itself under a
group name in its own `pyproject.toml`, and `registry.py` goes looking for
that same string:

    [project.entry-points."add_it_all.plugins"]     <- in each package
    ENTRY_POINT_GROUP = "add_it_all.plugins"        <- in registry.py

That string is a rendezvous point duplicated across core plus every plugin
package, and nothing but agreement makes it work. A mismatch does not
raise: core simply looks in a group the plugin is not in, finds nothing,
and carries on with one fewer connector.

**Why this needs a dedicated guard.** The failure is invisible to the rest
of the suite, which was verified rather than assumed:

  * The only other discovery assertion checks `discover_plugins()["echo"]`,
    and `echo` is declared in *core's own* `pyproject.toml` -- so it is
    renamed in the same edit that renames `registry.py` and always
    survives. The canary shares a cage with the change.
  * Every connector test builds its app with an explicit dict --
    `build_app({"okta": OktaPlugin(config)})` -- which constructs the
    plugin directly and never asks whether it is registered.

Pointing one installed package at a different group produced: `plugins
list` missing that connector, `add-it-all okta ...` reporting no such
command, and **the full suite still passing**. This file closes that gap.

**Deliberately derived, not listed.** An earlier sketch hardcoded the set
of expected plugins. That protects the connectors that already exist and
does nothing for the next one: whoever adds Jamf would have to remember to
add it to the list, and forgetting leaves the guard silent about precisely
the plugin they just built. Globbing `plugins/*/pyproject.toml` means a new
package is covered the moment it exists.

`ENTRY_POINT_GROUP` is imported rather than written out here for the same
reason -- this file asserts that core and the packages *agree*, not that
they say any particular string, so a future rename does not need to edit
this test.

Run just this stage:  pytest -m plugin_framework
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from add_it_all.plugins.registry import ENTRY_POINT_GROUP, discover_plugins

pytestmark = pytest.mark.plugin_framework

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PLUGINS_DIR = _REPO_ROOT / "plugins"
_CORE_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def _pyprojects() -> list[Path]:
    """Core plus every plugin package. Core is included because it declares
    the built-in `echo` plugin and can drift from `registry.py` too."""
    return [_CORE_PYPROJECT, *sorted(_PLUGINS_DIR.glob("*/pyproject.toml"))]


def _entry_point_groups(pyproject: Path) -> dict[str, dict]:
    data = tomllib.loads(pyproject.read_text())
    return data.get("project", {}).get("entry-points", {})


def _declared_names(pyproject: Path) -> set[str]:
    """Plugin names this package advertises under the group core searches."""
    return set(_entry_point_groups(pyproject).get(ENTRY_POINT_GROUP, {}))


def _rel(path: Path) -> str:
    return str(path.relative_to(_REPO_ROOT))


# --- Sanity: keep the guard itself honest ------------------------------------


def test_the_plugins_directory_is_where_we_think_it_is():
    """If the repo is restructured, the globs below would silently match
    nothing and every assertion would pass vacuously."""
    assert _PLUGINS_DIR.is_dir()
    assert _CORE_PYPROJECT.is_file()


def test_at_least_one_plugin_package_is_present():
    assert sorted(_PLUGINS_DIR.glob("*/pyproject.toml")), (
        "no plugin packages found -- the guard would pass vacuously"
    )


# --- Static: the declared group must match the one core searches -------------


@pytest.mark.parametrize("pyproject", _pyprojects(), ids=_rel)
def test_every_package_declares_the_group_core_searches(pyproject: Path):
    """The handshake, checked at its source.

    Catches a rename that missed a package, and catches a brand-new
    connector whose entry-point group was typo'd -- before it can present
    as "my plugin doesn't show up and I don't know why".
    """
    groups = _entry_point_groups(pyproject)

    assert groups, (
        f"{_rel(pyproject)} declares no entry points at all. Every package "
        f"here must advertise under [project.entry-points.\"{ENTRY_POINT_GROUP}\"] "
        "or core cannot find it."
    )
    assert ENTRY_POINT_GROUP in groups, (
        f"{_rel(pyproject)} advertises under {sorted(groups)}, but core searches "
        f"{ENTRY_POINT_GROUP!r}. These must match exactly -- a mismatch does not "
        "raise, it just makes the plugin invisible."
    )


@pytest.mark.parametrize("pyproject", _pyprojects(), ids=_rel)
def test_no_package_declares_a_near_miss_group(pyproject: Path):
    """A half-finished rename leaves a group that looks almost right.

    `add_it_all.plugins` next to `add_it_all.plugins` reads as fine at a
    glance, so this names the stragglers rather than leaving someone to
    diff two TOML files.
    """
    stray = [
        group for group in _entry_point_groups(pyproject)
        if group != ENTRY_POINT_GROUP and group.endswith(".plugins")
    ]

    assert not stray, (
        f"{_rel(pyproject)} declares {stray}, which looks like a plugin group but "
        f"is not the one core searches ({ENTRY_POINT_GROUP!r}). Left over from an "
        "incomplete rename?"
    )


def test_every_package_declares_at_least_one_plugin():
    empty = [_rel(p) for p in _pyprojects() if not _declared_names(p)]

    assert not empty, f"these declare the group but register no plugin: {empty}"


# --- Runtime: what is declared must actually be reachable --------------------


def test_every_declared_plugin_is_discoverable():
    """Closes the other half: the TOML can be right while the installed
    metadata is stale, because entry points are baked at install time.
    Editing `pyproject.toml` changes nothing until the package is
    reinstalled, and the symptom is identical to a typo'd group.
    """
    declared: set[str] = set()
    for pyproject in _pyprojects():
        declared |= _declared_names(pyproject)

    missing = declared - set(discover_plugins())

    assert not missing, (
        f"declared in pyproject.toml but not discovered at runtime: {sorted(missing)}. "
        "Entry points are recorded when a package is installed, so a pyproject edit "
        "needs a reinstall -- try ./scripts/bootstrap.sh."
    )


def test_discovery_finds_the_real_connectors_not_just_the_built_in():
    """`discover_plugins()["echo"]` -- the assertion this file exists to
    back up -- is satisfied by core alone, since core declares `echo`. This
    insists at least one *separately packaged* connector is reachable, so
    the plugin mechanism is proven end to end rather than in-process.
    """
    found = set(discover_plugins())
    from_packages = set()
    for pyproject in sorted(_PLUGINS_DIR.glob("*/pyproject.toml")):
        from_packages |= _declared_names(pyproject)

    assert from_packages & found, (
        "no separately-packaged connector is discoverable; only core's built-in "
        "plugin was found. Entry-point discovery is not actually working."
    )
