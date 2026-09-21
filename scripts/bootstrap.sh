#!/usr/bin/env bash
#
# One-command dev bootstrap for add-it-all.
#
# Creates a virtualenv, installs the core package plus every plugin
# package under plugins/, and then verifies the scaffold actually works:
# the CLI loads, plugin discovery finds plugins via entry points, and the
# test suite passes. Safe to re-run -- it reuses an existing .venv and
# never overwrites an existing .env.
#
# Usage:
#   ./scripts/bootstrap.sh              # install + verify
#   ./scripts/bootstrap.sh --skip-tests # install only
#   ./scripts/bootstrap.sh --recreate   # rebuild .venv from scratch
#   PYTHON=python3.11 ./scripts/bootstrap.sh   # pin the interpreter
#
set -euo pipefail
shopt -s nullglob

# Keep CI_PY in sync with .github/workflows/tests.yml.
readonly MIN_PY_MINOR=11
readonly CI_PY_VERSION="3.11"
readonly VENV_DIR=".venv"

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -t 1 ]]; then
    C_RESET=$'\033[0m'; C_RED=$'\033[31m'; C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'; C_BOLD=$'\033[1m'
else
    C_RESET=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BOLD=""
fi

step() { printf '\n%s==> %s%s\n' "$C_BOLD" "$*" "$C_RESET"; }
ok()   { printf '%s  ok%s  %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%swarn%s  %s\n' "$C_YELLOW" "$C_RESET" "$*"; }
fail() { printf '%sFAIL%s  %s\n' "$C_RED" "$C_RESET" "$*"; }
die()  { fail "$*"; exit 1; }

usage() {
    sed -n '3,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

skip_tests=0
recreate=0
for arg in "$@"; do
    case "$arg" in
        --skip-tests) skip_tests=1 ;;
        --recreate)   recreate=1 ;;
        -h|--help)    usage; exit 0 ;;
        *) printf 'unknown option: %s\n\n' "$arg" >&2; usage >&2; exit 2 ;;
    esac
done

# --- 1. Locate a usable interpreter -----------------------------------------
# Prefer the version CI pins so local results match CI; fall back to any
# interpreter satisfying requires-python (>= 3.11).
py_ok() {
    "$1" -c "import sys; sys.exit(0 if sys.version_info >= (3, $MIN_PY_MINOR) else 1)" \
        >/dev/null 2>&1
}

find_python() {
    local candidates=()
    [[ -n "${PYTHON:-}" ]] && candidates+=("$PYTHON")
    candidates+=("python${CI_PY_VERSION}" python3.12 python3.13 python3.14 python3 python)
    local candidate
    for candidate in "${candidates[@]}"; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        if py_ok "$candidate"; then printf '%s' "$candidate"; return 0; fi
    done
    return 1
}

step "Locating Python >= 3.${MIN_PY_MINOR}"
PY="$(find_python)" || die "no Python >= 3.${MIN_PY_MINOR} found. Install one, or set PYTHON=/path/to/python."
py_version="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
ok "using $(command -v "$PY") (Python ${py_version})"
if [[ "${py_version%.*}" != "$CI_PY_VERSION" ]]; then
    warn "CI pins Python ${CI_PY_VERSION}; you are on ${py_version%.*}. Version-specific"
    warn "failures may not reproduce in CI (and vice versa)."
fi

# --- 2. Virtualenv ----------------------------------------------------------
step "Setting up ${VENV_DIR}"
if (( recreate )) && [[ -d "$VENV_DIR" ]]; then
    rm -rf "$VENV_DIR"
    ok "removed existing ${VENV_DIR} (--recreate)"
fi

VENV_PY="${VENV_DIR}/bin/python"
if [[ -x "$VENV_PY" ]]; then
    py_ok "$VENV_PY" || die "existing ${VENV_DIR} runs Python < 3.${MIN_PY_MINOR}. Re-run with --recreate."
    ok "reusing existing ${VENV_DIR}"
else
    "$PY" -m venv "$VENV_DIR"
    ok "created ${VENV_DIR}"
fi

# --- 3. Install core + every plugin package --------------------------------
step "Installing core package and dev dependencies"
"$VENV_PY" -m pip install --upgrade pip --quiet
"$VENV_PY" -m pip install -e ".[dev]" --quiet
ok "add-it-all + dev extras installed (editable)"

step "Installing plugin packages"
# Globbed, not hardcoded: a new connector package under plugins/ is picked
# up here automatically, with no edit to this script.
plugin_count=0
for plugin_manifest in plugins/*/pyproject.toml; do
    plugin_dir="$(dirname "$plugin_manifest")"
    "$VENV_PY" -m pip install -e "$plugin_dir" --quiet
    ok "installed ${plugin_dir}"
    plugin_count=$((plugin_count + 1))
done
if (( plugin_count == 0 )); then
    warn "no plugin packages found under plugins/ -- entry-point discovery will only"
    warn "see the built-in echo plugin."
fi

# --- 4. Local env file ------------------------------------------------------
step "Checking .env"
if [[ -f .env ]]; then
    ok ".env already exists (left untouched)"
elif [[ -f .env.example ]]; then
    cp .env.example .env
    ok "created .env from .env.example"
    warn "fill in real OKTA_* and JIRA_* credentials before running those connectors."
else
    warn "no .env.example found; skipping"
fi

# .env holds live API tokens. `cp` inherits the umask (commonly 022, i.e.
# world-readable), so tighten it explicitly -- including on a pre-existing
# file, which may predate this check.
if [[ -f .env ]]; then
    chmod 600 .env
    ok ".env permissions set to 600 (owner read/write only)"
fi

# --- 5. Verify ---------------------------------------------------------------
failures=()
run_check() {
    local label="$1"; shift
    printf '\n--- %s\n' "$label"
    if "$@"; then
        ok "$label"
    else
        fail "$label"
        failures+=("$label")
    fi
}

step "Verifying the CLI loads and discovers plugins"
plugins_output=""
if plugins_output="$("${VENV_DIR}/bin/add-it-all" plugins list 2>&1)"; then
    printf '%s\n' "$plugins_output"
    if grep -q 'echo' <<<"$plugins_output"; then
        ok "entry-point discovery found the echo plugin"
    else
        fail "CLI ran but no echo plugin in the table -- entry-point discovery is broken"
        failures+=("plugin discovery")
    fi
else
    printf '%s\n' "$plugins_output"
    fail "add-it-all plugins list exited non-zero"
    failures+=("add-it-all plugins list")
fi

if (( skip_tests )); then
    step "Skipping tests (--skip-tests)"
else
    step "Running the test suite"
    run_check "pytest -m plugin_framework (Stage 0)" \
        "${VENV_DIR}/bin/pytest" -m plugin_framework
    run_check "pytest -m cache (Stage 1)" \
        "${VENV_DIR}/bin/pytest" -m cache
    # pyproject's testpaths now covers both tests/ and plugins/, so this
    # single run includes every connector package's own tests. (It used to
    # need a separate per-plugin loop here; see docs/STAGES.md Stage 0.)
    run_check "full suite + coverage" \
        "${VENV_DIR}/bin/pytest" --cov=src/add_it_all --cov-report=term-missing
fi

# --- 6. Summary -------------------------------------------------------------
if (( ${#failures[@]} > 0 )); then
    printf '\n%s%d check(s) failed:%s\n' "$C_RED" "${#failures[@]}" "$C_RESET"
    printf '  - %s\n' "${failures[@]}"
    cat <<'EOF'

Do not patch around these. A failure here means the scaffold has a real
bug, so fix it and re-run before starting feature work (see the
first-session checklist in CLAUDE.md).
EOF
    exit 1
fi

cat <<EOF

${C_GREEN}${C_BOLD}Bootstrap complete.${C_RESET} Activate the venv with:

    source ${VENV_DIR}/bin/activate

Next: fill in .env credentials, then pick up the next unchecked task in
docs/STAGES.md.
EOF
