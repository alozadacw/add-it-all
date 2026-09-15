"""
Okta connector: account status for one person.

Endpoint: `GET {OKTA_ORG_URL}/api/v1/users/{login}` with an
`Authorization: SSWS <token>` header.

**Not-found semantics.** A user Okta has never heard of returns a
*successful* result with `data["found"] is False`, not `error=`. The guide
asks each connector to decide this explicitly: for an offboarding lookup,
"this person has no Okta account" is a real answer, whereas an `error=`
would make `UnifiedRecord.field_for("okta")` return None and be
indistinguishable from "Okta was unreachable".

**Devices.** `fetch_devices()` (surfaced as `okta <user> -d`) lists the
devices Okta associates with a user, via
`GET /api/v1/users/{userId}/devices`. Scope caveat worth repeating to
users: this is Okta's own device registry -- machines enrolled through
Okta Verify / device trust -- and NOT the Jamf or ABM hardware inventory
that Stages 4-5 will add. Someone can hold a laptop that Okta has never
seen. It is deliberately a separate call, not part of `fetch()`, because
Stage 7 runs `fetch()` for every plugin on every lookup and shouldn't pay
for a second round trip nobody asked for.

**Applications.** `fetch_apps()` (`okta <user> -a`) reads
`GET /api/v1/users/{userId}/appLinks`, the list behind the user's Okta
dashboard. It answers "what can this person open", not "how were they
granted it" -- direct-vs-group assignment lives on
`/apps/{appId}/users/{userId}` and would cost one request per app, so it
is not fetched. Hidden tiles are included: a hidden app is still an
assignment, and an offboarding check that skipped them would under-report.

**Authenticators.** `fetch_authenticators()` (`okta <user> -u`) reads
`GET /api/v1/users/{userId}/factors`. The API says "factor", the Okta
admin console says "authenticator"; the CLI follows the console and the
API's word is kept for anything touching the wire. `profile.questionText`
is deliberately dropped -- that a security question is enrolled is the
useful fact, while the question itself is a recovery-credential hint with
no operational value here. Phone numbers and emails are shown exactly as
Okta returns them, which is already partially masked for SMS.

**Name search.** `fetch_search()` (`okta --find <name>`) resolves a partial
name to a username via `GET /api/v1/users?search=...`, for when someone
knows a colleague's first or surname but not their login. `--find` is
long-only and composes with nothing: short flags here are section
selectors, and search is not a section -- it answers "who is this person",
not "what do you want to see about them". Results are never cached, and no
status filter is sent (see `build_search_expression`).

Required env vars (see `.env.example`):
    OKTA_ORG_URL        e.g. https://acme.okta.com
    OKTA_API_TOKEN      an SSWS token
Optional:
    OKTA_TIMEOUT_SECONDS        per-request timeout (default 10)
    OKTA_ACCESS_ATTRIBUTE       custom profile attribute carrying this org's
                                access decision (default `access_blocked`)
    LOOKUP_CLI_MOCK_OKTA=1      serve a fixture instead of calling out

**Custom profile attribute.** This org's Universal Directory defines an
attribute displayed in the Profile Editor as "ACCESS BLOCKED", variable
name `access_blocked`. It arrives inside the `profile` object of the user
payload we already fetch, so reading it costs no extra request. Its value
is reported verbatim -- a boolean stays `true`/`false` rather than becoming
yes/no -- so an operator sees exactly what the Okta admin UI shows. Only
the one configured attribute is read: Okta profiles routinely carry
manager, employee id and personal contact details, and everything in
`data` is written to the plaintext local cache.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from html import escape as _escape
from pathlib import Path
from urllib.parse import quote

import httpx
import typer
from rich.console import Console
from rich.table import Table

from lookup_cli.plugins.base import ConnectorPlugin, ConnectorResult
from lookup_cli.redaction import safe_error

DEFAULT_TIMEOUT_SECONDS = 10.0

#: Okta statuses that mean the account can actually be used.
_ACTIVE_STATUSES = frozenset({"ACTIVE"})

#: Hard bound on Link-header following, so a looping or malformed `next`
#: can't hang the CLI. Far above any real user's device count.
_MAX_PAGES = 20

#: Custom Universal Directory attribute carrying this org's access decision.
#: Shown in the Okta Profile Editor as "ACCESS BLOCKED" with the variable
#: name `access_blocked`. Custom attribute names are org-specific, so the
#: name is overridable via OKTA_ACCESS_ATTRIBUTE rather than hardcoded.
DEFAULT_ACCESS_ATTRIBUTE = "access_blocked"

#: Label for that value in CLI output.
ACCESS_FIELD_LABEL = "access blocked"

#: Okta retains System Log data for roughly 90 days. Asking for longer cannot
#: return longer, and letting a caller believe otherwise would put a window in
#: the column header that the data does not actually cover.
MAX_LOG_WINDOW = timedelta(days=90)

#: Sign-in events that can carry device identity.
_SIGNIN_EVENT_TYPES = ("user.session.start", "user.authentication.sso")

_SINCE_RE = re.compile(r"^\s*(\d+)\s*([dh])\s*$", re.IGNORECASE)

#: Profile fields a searcher might plausibly know. `sw` (startsWith) is what
#: Okta's `search` supports broadly; it means "dennis" finds Dennis but "enn"
#: finds nobody, which is an acceptable trade for covering "I know their first
#: or surname".
_SEARCH_FIELDS = ("profile.firstName", "profile.lastName", "profile.login", "profile.email")

#: One page, requested once. Search is interactive, not an audit -- following
#: Link headers to page thousands of users to render a 15-row table would burn
#: rate-limit budget for nothing. A full page back is reported as truncated
#: rather than passed off as the complete answer.
MAX_SEARCH_RESULTS = 200

#: Rows the chooser prints before it starts saying "N more".
MAX_MATCHES_SHOWN = 15


def build_search_expression(query: str) -> str:
    """Okta `search` expression matching `query` against the name fields.

    **Whitespace splits into AND-ed groups.** Each token must match *some*
    field, so "dennis luo" is `(any field starts with dennis) and (any field
    starts with luo)` -- which also means token order doesn't matter, and
    nobody has to know whether the directory stores "Dennis Luo" or "Luo,
    Dennis". Treating the whole string as one term instead made a full-name
    search match nobody, which was doubly bad because adding a surname is
    exactly the advice the "too many matches" message gives.

    **No status clause, deliberately.** Okta's List Users endpoint excludes
    `DEPROVISIONED` users by default, and any status predicate added here
    risks reproducing that exclusion. For a tool whose central question is
    "did this person's access actually get revoked", a search that silently
    omits the deactivated person is worse than no search -- it looks
    complete. Whether `search=` itself inherits the default exclusion is
    server-side behaviour that mocks cannot prove; it is flagged in
    docs/STAGES.md for the live smoke test, and if it does, the fix is an
    explicit all-statuses clause added here.
    """
    tokens = (query or "").split()
    if not tokens:
        raise ValueError("--find needs a name to search for.")

    groups = []
    for token in tokens:
        # Escape backslashes then quotes, so a name can't terminate the
        # filter string or smuggle an operator into it.
        safe = token.replace("\\", "\\\\").replace('"', '\\"')
        groups.append(" or ".join(f'{field} sw "{safe}"' for field in _SEARCH_FIELDS))

    if len(groups) == 1:
        return groups[0]
    # Parenthesised: `a or b and c or d` binds wrongly and the AND would
    # silently stop narrowing.
    return " and ".join(f"({group})" for group in groups)


def parse_since(raw: str) -> timedelta:
    """Parse a `--since` window like `90d` or `12h`, clamped to retention."""
    match = _SINCE_RE.match(raw or "")
    if not match:
        raise ValueError(
            f"could not parse --since {raw!r}. Use a number followed by "
            f"'d' (days) or 'h' (hours), e.g. 30d or 12h."
        )
    amount = int(match.group(1))
    if amount <= 0:
        raise ValueError("--since must be greater than zero.")
    window = timedelta(days=amount) if match.group(2).lower() == "d" else timedelta(hours=amount)
    return min(window, MAX_LOG_WINDOW)


def describe_window(window: timedelta) -> str:
    """Short label for a window, for the column header."""
    if window >= timedelta(days=1) and window.total_seconds() % 86400 == 0:
        return f"{int(window.total_seconds() // 86400)}d"
    return f"{int(window.total_seconds() // 3600)}h"

#: Okta's status enum has eight values, and the raw name is not always what
#: an operator needs to read. "Deactivated" in the Okta admin UI means
#: DEPROVISIONED specifically -- SUSPENDED also blocks login but is a
#: different state, and conflating them would mislead someone checking
#: whether an offboarding actually completed.
_DEACTIVATED_STATUS = "DEPROVISIONED"


def format_profile_value(raw: object) -> str:
    """Render a profile attribute exactly as Okta returned it.

    No interpretation: a boolean stays a boolean rather than becoming
    yes/no, so an operator sees the same value the Okta admin UI shows.
    `json.dumps` rather than `str` for non-strings, because Okta's JSON says
    `true` while Python's `str(True)` says `True`.

    An absent or null attribute has nothing to render verbatim, so it falls
    back to the table's usual empty marker -- which keeps it distinct from
    an explicit `false`.
    """
    if raw is None:
        return "-"
    if isinstance(raw, str):
        return raw
    return json.dumps(raw)


#: Okta's `factorType` values are wire identifiers, not something an operator
#: should have to decode -- `token:software:totp` is the clearest example.
#: Anything absent falls through to the raw value: Okta keeps adding
#: authenticator types, and inventing a label for one we don't recognise
#: would be worse than showing what the API actually said.
_FACTOR_LABELS: dict[str, str] = {
    "push": "Okta Verify push",
    "signed_nonce": "Okta FastPass",
    "webauthn": "WebAuthn / passkey",
    "u2f": "Security key (U2F)",
    "sms": "SMS",
    "call": "Voice call",
    "email": "Email",
    "question": "Security question",
    "token:software:totp": "TOTP app",
    "token:hardware": "Hardware token",
    "token": "Token",
    "password": "Password",
}

#: Profile keys that identify *which* authenticator this is, best first. A
#: named device beats the credential id behind it, because the name is what an
#: operator recognises. `questionText` is deliberately absent -- see the module
#: docstring.
_FACTOR_DETAIL_FIELDS = ("name", "authenticatorName", "phoneNumber", "email", "credentialId")

#: Anything unrecognised stays yellow rather than green: an unknown state is
#: not evidence that an authenticator is fine.
_FACTOR_STATUS_COLOURS: dict[str, str] = {
    "ACTIVE": "green",
    "PENDING_ACTIVATION": "yellow",
    "NOT_SETUP": "yellow",
    "INACTIVE": "red",
    "DISABLED": "red",
    "EXPIRED": "red",
}


def factor_label(factor_type: str | None, provider: str | None) -> str | None:
    """Readable name for an authenticator, keeping a non-Okta provider visible.

    A Duo push and an Okta Verify push are different systems to go and revoke,
    so the provider is named whenever it isn't Okta's own.
    """
    if not factor_type:
        return None
    label = _FACTOR_LABELS.get(factor_type, factor_type)
    if provider and provider.upper() != "OKTA":
        label = f"{label} ({provider})"
    return label


def factor_detail(profile: dict | None) -> str | None:
    """The most identifying value on a factor profile, or None."""
    for key in _FACTOR_DETAIL_FIELDS:
        value = (profile or {}).get(key)
        if value:
            return str(value)
    return None


_STATUS_NOTES: dict[str, tuple[str, str]] = {
    "ACTIVE": ("green", ""),
    "DEPROVISIONED": ("red", "deactivated"),
    "SUSPENDED": ("red", "suspended"),
    "LOCKED_OUT": ("yellow", "locked out"),
    "PASSWORD_EXPIRED": ("yellow", "password expired"),
    "RECOVERY": ("yellow", "in password recovery"),
    "STAGED": ("yellow", "not yet activated"),
    "PROVISIONED": ("yellow", "activation pending"),
}


#: Okta stores the manager relationship on a Universal Directory attribute.
#: `managerId` is the stock one, but which value it holds (the manager's
#: login, email or employee id) is an org-specific schema decision, so the
#: attribute name is overridable rather than hardcoded. See the Open
#: Decisions Log in docs/STAGES.md.
DEFAULT_MANAGER_ATTRIBUTE = "managerId"

#: Universal Directory attribute holding a person's hire/start date, used to
#: order the team columns oldest-first in the HTML comparison. Org-specific
#: like the manager attribute, so it is overridable; when absent on a member,
#: `fetch_team` falls back to the Okta account `created` timestamp.
DEFAULT_HIRE_DATE_ATTRIBUTE = "hireDate"


def build_comparison(members: list[dict]) -> dict:
    """Line up several members' group memberships into a comparison matrix.

    `members` is an ordered list (subject first) of
    ``{"login", "name", "is_subject", "groups": list[str] | None, "error"}``.
    A member whose groups could not be fetched carries ``groups=None`` and is
    kept in the roster but **excluded from the coverage denominator** -- an
    unreadable member is not evidence they are missing from a group, and
    counting them as absent would invent drift that may not exist.

    Returns ``{"members", "groups", "summary"}`` where each group row records
    per-member coverage plus whether every readable member has it (`everyone`)
    or only some do (`drift`). Pure and side-effect free, so it is tested
    directly without any network.
    """
    present = [m for m in members if m.get("groups") is not None]
    names = sorted({g for m in present for g in m["groups"]}, key=str.lower)

    rows: list[dict] = []
    for name in names:
        coverage = {m["login"]: name in set(m["groups"]) for m in present}
        count = sum(coverage.values())
        everyone = bool(present) and count == len(present)
        rows.append(
            {
                "name": name,
                "coverage": coverage,
                "count": count,
                "everyone": everyone,
                # Drift = some-but-not-all: the rows an operator actually needs
                # to look at. A group everyone shares and a group nobody here
                # has are both uninteresting for a "who diverges" question.
                "drift": 0 < count < len(present),
            }
        )

    return {
        "members": members,
        "groups": rows,
        "summary": {
            "member_count": len(members),
            "present_count": len(present),
            "group_count": len(names),
            "shared_by_all": sum(1 for r in rows if r["everyone"]),
            "drift_count": sum(1 for r in rows if r["drift"]),
            "error_count": sum(1 for m in members if m.get("groups") is None),
        },
    }


#: Self-contained styles for the exported comparison page. Inlined so the file
#: opens straight from disk with no network -- a strict "no external resources"
#: rule the test pins by asserting no http(s) URL appears in the output.
_HTML_STYLE = """
:root {
  --bg:#f4f6fa; --panel:#fff; --panel2:#f8fafc; --ink:#1a2233; --soft:#59647a;
  --line:#e1e6ef; --line2:#cbd3e1; --accent:#4f5bd5; --accent-soft:#ecedfb;
  --ok:#1f9d6b; --ok-bg:#e2f4ec; --warn:#c1810b; --warn-bg:#faf0d7;
  --crit:#d0453b; --crit-bg:#fbe6e4;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0e1220; --panel:#161c2d; --panel2:#1b2234; --ink:#e7ecf5; --soft:#93a0ba;
  --line:#263048; --line2:#35415e; --accent:#8b93ff; --accent-soft:#232a52;
  --ok:#4cc38a; --ok-bg:#14311f; --warn:#e0a94a; --warn-bg:#3a2c0f;
  --crit:#f0776c; --crit-bg:#3a1815;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);font-size:13px;line-height:1.5}
.wrap{max-width:100%;margin:0;padding:28px 32px 64px}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);margin:0 0 8px}
h1{font-size:20px;margin:0 0 6px;letter-spacing:-.01em}
.sub{color:var(--soft);margin:0;max-width:80ch}
.tiles{display:grid;grid-template-columns:repeat(4,minmax(110px,1fr));gap:12px;margin:22px 0;max-width:680px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px 14px}
.tile .n{font-family:var(--mono);font-size:21px;font-weight:600;font-variant-numeric:tabular-nums}
.tile .l{font-size:11px;color:var(--soft);margin-top:2px}
.tile.warn{border-color:var(--warn);background:var(--warn-bg)}.tile.warn .n{color:var(--warn)}
/* Cap the height so the body scrolls inside the box; that is what lets the
   header row below stay frozen (position:sticky needs a scrolling ancestor). */
.scroll{overflow:auto;width:100%;max-height:calc(100vh - 240px);border:1px solid var(--line);border-radius:12px;background:var(--panel)}
table{border-collapse:collapse;width:100%}
th,td{text-align:center;padding:6px 10px;border-bottom:1px solid var(--line)}
/* Frozen header row. box-shadow (not just border) draws the bottom edge,
   which border-collapse otherwise drops from a sticky cell as it scrolls. */
thead th{position:sticky;top:0;z-index:2;background:var(--panel2);border-bottom:1px solid var(--line2);box-shadow:inset 0 -1px var(--line2);vertical-align:bottom;font-size:11.5px}
th.grp{text-align:left;min-width:210px}
tbody th{text-align:left;font-family:var(--mono);font-size:11.5px;font-weight:500;border-left:0}
td{border-left:1px solid var(--line)}
tbody tr:hover{background:var(--panel2)}
.who{font-weight:600;font-size:12px}.who.subj{color:var(--accent)}
.nm{font-size:10.5px;color:var(--soft)}
.hire{font-size:10px;color:var(--soft);font-family:var(--mono);font-variant-numeric:tabular-nums;margin-top:1px}
.role{font-size:9.5px;color:var(--soft);font-family:var(--mono);text-transform:uppercase;letter-spacing:.04em;margin-top:1px}
code{font-family:var(--mono);font-size:.9em}
td.na{color:var(--soft);font-family:var(--mono)}
.subjcol{background:var(--accent-soft)}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:14px;font-size:11.5px;color:var(--soft)}
.legend span{display:inline-flex;align-items:center;gap:7px}
.cnt{font-family:var(--mono);font-size:10px;color:var(--soft);margin-left:6px;font-variant-numeric:tabular-nums}
td.hit{color:#fff;font-weight:700}
tr.divider th{font-family:var(--mono);font-size:10px;letter-spacing:.06em;color:var(--soft);background:var(--panel2);text-transform:uppercase}
.chip{width:12px;height:12px;border-radius:3px;display:inline-block;vertical-align:middle;margin-right:8px;flex:0 0 auto}
"""


def _short_login(login: str | None) -> str:
    """Drop the `@domain` from a login for display.

    Everyone on a team shares the domain, so it is pure width -- redundant in
    the column headers and the heading. The full login stays in the data.
    """
    return (login or "?").split("@", 1)[0]


def _html_member_head(member: dict) -> str:
    """Column header for one member: login, display name, and a role/state note."""
    login = _escape(_short_login(member.get("login")))
    if member.get("groups") is None:
        role = "unavailable"
    elif member.get("is_subject"):
        role = "subject"
    else:
        role = "teammate"
    who_class = "who subj" if member.get("is_subject") else "who"
    name = member.get("name")
    name_line = f'<div class="nm">{_escape(name)}</div>' if name else ""
    # Hire date sits with the name; date part only. Unknown shows an em dash.
    hire = member.get("hire_date")
    hire_txt = _escape(str(hire)[:10]) if hire else "&mdash;"
    hire_line = f'<div class="hire">{hire_txt}</div>'
    return (
        f'<div class="{who_class}">{login}</div>'
        f'{name_line}{hire_line}<div class="role">{role}</div>'
    )


def _share_color(count: int, present_count: int) -> str:
    """Heat-map fill for a group shared by `count` of `present_count` members.

    A group everyone has is the calm baseline (green); the fewer people share
    it, the hotter it runs (through amber to red), so individual/unique access
    is what draws the eye. Returned as an `hsl()` string used inline, which
    reads acceptably on both the light and dark grounds.
    """
    frac = count / present_count if present_count else 0
    hue = int(round(150 * frac))  # 150 = green (all) -> 0 = red (rare)
    return f"hsl({hue} 60% 44%)"


def _share_label(count: int, present_count: int) -> str:
    if count >= present_count:
        return f"shared by all {present_count}"
    if count == 1:
        return "unique to 1 member"
    return f"shared by {count}"


def render_team_html(
    comparison: dict,
    *,
    subject_login: str,
    manager: str | None = None,
    cohort: str | None = None,
    excluded_deactivated: int = 0,
) -> str:
    """Render `comparison` (from `build_comparison`) as a standalone web page.

    `manager` / `cohort` describe whose team this is (a peer cohort keyed on a
    manager, or the subject's own direct reports), and are woven into the
    subtitle so the page states what it is comparing. Everything from the
    directory -- logins, display names, group names, the manager reference --
    is HTML-escaped: it is attacker-influenceable data being written into a
    file a human then opens in a browser. The page embeds all of its CSS and
    uses no external resources so it works offline, straight from disk.
    """
    rows = comparison["groups"]
    summary = comparison["summary"]
    present_count = summary["present_count"]

    # Columns run oldest hire date on the left; a member with no determinable
    # date sorts last. ISO-ish date strings sort chronologically as text.
    ordered_members = sorted(
        comparison["members"],
        key=lambda m: (m.get("hire_date") is None, m.get("hire_date") or ""),
    )

    # Rows run most-shared first, cooling down to the rare/unique groups at the
    # bottom. Sorted here rather than in build_comparison so the terminal view
    # (which uses build_comparison directly) keeps its own alphabetical order.
    ordered_rows = sorted(rows, key=lambda r: (-r["count"], (r["name"] or "").lower()))

    heads = "".join(
        f'<th class="subjcol">{_html_member_head(m)}</th>' if m.get("is_subject")
        else f"<th>{_html_member_head(m)}</th>"
        for m in ordered_members
    )

    body_rows = []
    divider_done = False
    for row in ordered_rows:
        count = row["count"]
        # A one-off divider announcing the individual-access block, echoing the
        # reference sheet. Only meaningful when there's more than one member.
        if not divider_done and count == 1 and present_count > 1:
            body_rows.append(
                f'<tr class="divider"><th>── unique to one member ──</th>'
                f'<td colspan="{len(ordered_members)}"></td></tr>'
            )
            divider_done = True

        fill = _share_color(count, present_count)
        cells = []
        for m in ordered_members:
            # The subject's whole column carries the purple highlight, wherever
            # hire-date ordering places it.
            subj = ["subjcol"] if m.get("is_subject") else []
            if m.get("groups") is None:
                cells.append(f'<td class="{" ".join([*subj, "na"])}">?</td>')
                continue
            if row["coverage"].get(m["login"], False):
                # Filled with the row's band colour + a check, so membership
                # reads without relying on colour alone.
                cells.append(
                    f'<td class="{" ".join([*subj, "hit"])}" style="background:{fill}">&check;</td>'
                )
            else:
                attr = f' class="{subj[0]}"' if subj else ""
                cells.append(f"<td{attr}></td>")
        body_rows.append(
            f'<tr><th><span class="chip" style="background:{fill}"></span>'
            f'{_escape(row["name"])}'
            f'<span class="cnt">{count}/{present_count}</span></th>'
            f'{"".join(cells)}</tr>'
        )

    matrix = (
        '<div class="scroll"><table><thead><tr>'
        f'<th class="grp">group</th>{heads}</tr></thead>'
        f'<tbody>{"".join(body_rows)}</tbody></table></div>'
        if ordered_rows
        else '<p class="sub">No groups to compare.</p>'
    )

    # Legend: one swatch per share-count actually present, most-shared first.
    legend_bands = "".join(
        f'<span><span class="chip" style="background:{_share_color(c, present_count)}"></span>'
        f'{_share_label(c, present_count)}</span>'
        for c in sorted({r["count"] for r in ordered_rows}, reverse=True)
    )

    notes = []
    if excluded_deactivated:
        notes.append(
            f"{excluded_deactivated} deactivated teammate(s) excluded "
            "(pass <code>--include-deactivated</code> to show them)."
        )
    if summary["error_count"]:
        notes.append(
            f"{summary['error_count']} member(s) could not be read and are shown "
            "as <code>?</code> -- they are excluded from the shared counts."
        )
    note = f'<p class="sub">{" ".join(notes)}</p>' if notes else ""

    subject = _escape(_short_login(subject_login))
    if cohort == "peers" and manager:
        whose = (
            f"Everyone who reports to <b>{_escape(_short_login(manager))}</b> "
            f"&mdash; {subject} and their teammates."
        )
    elif cohort == "reports":
        whose = f"{subject} has no manager on file, so this compares their own direct reports."
    else:
        whose = f"{subject}'s team."
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Okta team &middot; {subject}</title>
<style>{_HTML_STYLE}</style>
</head>
<body>
<div class="wrap">
  <p class="eyebrow">lookup-cli &middot; okta &middot; team group comparison</p>
  <h1>Group comparison &mdash; team of {subject}</h1>
  <p class="sub">{whose} Columns run oldest hire date on the left; group rows
  run from those shared by everyone down to individual access, shaded by how
  many teammates share each one.</p>
  <div class="tiles">
    <div class="tile"><div class="n">{summary["member_count"]}</div><div class="l">team members</div></div>
    <div class="tile"><div class="n">{summary["group_count"]}</div><div class="l">distinct groups</div></div>
    <div class="tile"><div class="n">{summary["shared_by_all"]}</div><div class="l">shared by all</div></div>
    <div class="tile warn"><div class="n">{summary["drift_count"]}</div><div class="l">groups with drift</div></div>
  </div>
  {matrix}
  <div class="legend">{legend_bands}</div>
  {note}
</div>
</body>
</html>
"""


class OktaPlugin(ConnectorPlugin):
    name = "okta"
    required_credentials = ("OKTA_ORG_URL", "OKTA_API_TOKEN")

    @property
    def _access_attribute(self) -> str:
        return self.config.get("OKTA_ACCESS_ATTRIBUTE") or DEFAULT_ACCESS_ATTRIBUTE

    @property
    def _manager_attribute(self) -> str:
        return self.config.get("OKTA_MANAGER_ATTRIBUTE") or DEFAULT_MANAGER_ATTRIBUTE

    @property
    def _hire_date_attribute(self) -> str:
        return self.config.get("OKTA_HIRE_DATE_ATTRIBUTE") or DEFAULT_HIRE_DATE_ATTRIBUTE

    async def fetch(self, identifier: str) -> ConnectorResult:
        try:
            raw = await self._call_backend(identifier)
        except Exception as exc:  # noqa: BLE001 - contract: never crash aggregation
            return ConnectorResult(
                plugin_name=self.name,
                identifier=identifier,
                # The token is passed explicitly as well as being picked up
                # from the environment: in mock/test runs it may only exist
                # in the injected config.
                error=safe_error(exc, secrets=[self.config.get("OKTA_API_TOKEN")]),
            )

        if raw is None:
            return ConnectorResult(
                plugin_name=self.name,
                identifier=identifier,
                data={"found": False, "status": None},
                tags=["not-found"],
            )

        return self._to_result(identifier, raw)

    async def fetch_devices(self, identifier: str, *, okta_id: str | None = None) -> ConnectorResult:
        """List the devices Okta associates with `identifier`.

        Pass `okta_id` when the caller already resolved the user (as the CLI
        does) to skip a redundant lookup. Like `fetch()`, this never raises
        for ordinary failures.
        """
        try:
            okta_id = await self._resolve_okta_id(identifier, okta_id)
            if okta_id is None:
                return ConnectorResult(
                    plugin_name=self.name,
                    identifier=identifier,
                    data={"found": False, "devices": [], "count": 0},
                    tags=["not-found"],
                )

            raw_devices = await self._call_devices_backend(okta_id)
        except Exception as exc:  # noqa: BLE001 - contract: never crash aggregation
            return ConnectorResult(
                plugin_name=self.name,
                identifier=identifier,
                error=safe_error(exc, secrets=[self.config.get("OKTA_API_TOKEN")]),
            )

        devices = [self._to_device(entry) for entry in raw_devices]
        return ConnectorResult(
            plugin_name=self.name,
            identifier=identifier,
            data={"found": True, "devices": devices, "count": len(devices)},
            properties={"okta_id": okta_id},
            tags=["no-devices"] if not devices else ["has-devices"],
        )

    async def fetch_apps(self, identifier: str, *, okta_id: str | None = None) -> ConnectorResult:
        """List the applications assigned to `identifier` in Okta.

        Answers "what can this person open", not "how were they granted it" --
        see the module docstring. Like `fetch()`, never raises for ordinary
        failures.
        """
        try:
            resolved = await self._resolve_okta_id(identifier, okta_id)
            if resolved is None:
                return ConnectorResult(
                    plugin_name=self.name,
                    identifier=identifier,
                    data={"found": False, "apps": [], "count": 0},
                    tags=["not-found"],
                )
            raw_apps = await self._call_apps_backend(resolved)
        except Exception as exc:  # noqa: BLE001 - contract: never crash aggregation
            return ConnectorResult(
                plugin_name=self.name,
                identifier=identifier,
                error=safe_error(exc, secrets=[self.config.get("OKTA_API_TOKEN")]),
            )

        # Okta returns dashboard sort order, which is per-user and arbitrary.
        # Alphabetical means two people's app lists can actually be compared.
        apps = sorted(
            (self._to_app(entry) for entry in raw_apps),
            key=lambda app: (app["label"] or "").lower(),
        )
        return ConnectorResult(
            plugin_name=self.name,
            identifier=identifier,
            data={"found": True, "apps": apps, "count": len(apps)},
            properties={"okta_id": resolved},
            tags=["no-apps"] if not apps else ["has-apps"],
        )

    async def fetch_authenticators(
        self, identifier: str, *, okta_id: str | None = None
    ) -> ConnectorResult:
        """List the authenticators (API: "factors") enrolled by `identifier`.

        Includes inactive and half-finished enrolments: a disabled
        authenticator is still enrolled, and an offboarding check wants the
        whole picture rather than only what currently works.
        """
        try:
            resolved = await self._resolve_okta_id(identifier, okta_id)
            if resolved is None:
                return ConnectorResult(
                    plugin_name=self.name,
                    identifier=identifier,
                    data={"found": False, "authenticators": [], "count": 0},
                    tags=["not-found"],
                )
            raw_factors = await self._call_factors_backend(resolved)
        except Exception as exc:  # noqa: BLE001 - contract: never crash aggregation
            return ConnectorResult(
                plugin_name=self.name,
                identifier=identifier,
                error=safe_error(exc, secrets=[self.config.get("OKTA_API_TOKEN")]),
            )

        factors = sorted(
            (self._to_factor(entry) for entry in raw_factors),
            key=lambda f: ((f["label"] or "").lower(), (f["detail"] or "").lower()),
        )
        return ConnectorResult(
            plugin_name=self.name,
            identifier=identifier,
            data={"found": True, "authenticators": factors, "count": len(factors)},
            properties={"okta_id": resolved},
            tags=["no-authenticators"] if not factors else ["has-authenticators"],
        )

    async def fetch_groups(self, identifier: str, *, okta_id: str | None = None) -> ConnectorResult:
        """List the Okta groups `identifier` belongs to.

        `GET /api/v1/users/{userId}/groups`. This is the per-person building
        block the team comparison fans out over, and a section in its own
        right (`okta <user> -g`). Like `fetch()`, never raises for ordinary
        failures. Pass `okta_id` to skip re-resolving a user the caller
        already looked up.
        """
        try:
            resolved = await self._resolve_okta_id(identifier, okta_id)
            if resolved is None:
                return ConnectorResult(
                    plugin_name=self.name,
                    identifier=identifier,
                    data={"found": False, "groups": [], "count": 0},
                    tags=["not-found"],
                )
            raw_groups = await self._call_groups_backend(resolved)
        except Exception as exc:  # noqa: BLE001 - contract: never crash aggregation
            return ConnectorResult(
                plugin_name=self.name,
                identifier=identifier,
                error=safe_error(exc, secrets=[self.config.get("OKTA_API_TOKEN")]),
            )

        # Okta returns groups in an arbitrary order; alphabetical is what lets
        # two people's memberships be lined up side by side.
        groups = sorted(
            (self._to_group(entry) for entry in raw_groups),
            key=lambda g: (g["name"] or "").lower(),
        )
        return ConnectorResult(
            plugin_name=self.name,
            identifier=identifier,
            data={"found": True, "groups": groups, "count": len(groups)},
            properties={"okta_id": resolved},
            tags=["no-groups"] if not groups else ["has-groups"],
        )

    async def fetch_team(
        self, identifier: str, *, include_deactivated: bool = False
    ) -> ConnectorResult:
        """Resolve `identifier` to the team they sit in, for comparison.

        The subject is *not* assumed to be a manager. We read the subject's
        own manager from `profile.<managerAttr>` (default `managerId`) and
        gather everyone who reports to that same manager --
        `GET /users?search=profile.<managerAttr> eq "<managerRef>"` -- i.e. the
        subject and their peers. That way looking up an IC compares them
        against their teammates, and looking up a manager compares them against
        *their* peer managers, consistently.

        Fallback: a subject with no manager on their profile (the top of a
        tree) has no peer cohort, so we compare their own direct reports
        instead and tag the result `cohort="reports"` so the CLI can say which
        it did. What `<managerAttr>` actually holds (login/email/id) is
        org-specific -- see the Open Decisions Log. Like `fetch()`, never
        raises for ordinary failures.
        """
        try:
            subject_raw = await self._call_backend(identifier)
            if subject_raw is None:
                return ConnectorResult(
                    plugin_name=self.name,
                    identifier=identifier,
                    data={"found": False, "members": [], "count": 0, "cohort": None},
                    tags=["not-found"],
                )
            subject_profile = subject_raw.get("profile") or {}
            subject_login = subject_profile.get("login") or identifier
            manager_ref = subject_profile.get(self._manager_attribute)
            manager_ref = manager_ref.strip() if isinstance(manager_ref, str) else manager_ref

            if manager_ref:
                cohort_kind = "peers"
                cohort_raw = await self._call_cohort_backend(manager_ref)
            else:
                # No manager to key on: compare the subject's own reports so the
                # command still answers something useful for a top-of-tree user.
                cohort_kind = "reports"
                cohort_raw = await self._call_cohort_backend(subject_login)
        except Exception as exc:  # noqa: BLE001 - contract: never crash aggregation
            return ConnectorResult(
                plugin_name=self.name,
                identifier=identifier,
                error=safe_error(exc, secrets=[self.config.get("OKTA_API_TOKEN")]),
            )

        subject_id = subject_raw.get("id")
        hire_attr = self._hire_date_attribute
        members: list[dict] = []
        seen: set = set()
        excluded_deactivated = 0
        for raw in cohort_raw:
            member_id = raw.get("id")
            # A directory that lists the same person twice must not double-count
            # them in the matrix.
            if member_id in seen:
                continue
            is_subject = member_id == subject_id
            # Deactivated (DEPROVISIONED) teammates are dropped by default: the
            # comparison is about who *currently* has access. The subject is
            # never dropped -- they were asked for by name -- and
            # include_deactivated opts everyone back in, to audit whether a
            # leaver's access was actually removed.
            if (
                not is_subject
                and not include_deactivated
                and raw.get("status") == _DEACTIVATED_STATUS
            ):
                excluded_deactivated += 1
                continue
            seen.add(member_id)
            members.append(self._to_member(raw, is_subject=is_subject, hire_attr=hire_attr))

        # The queried person is always in the comparison. In peer mode the
        # cohort search normally already includes them; in reports mode (they
        # are the manager) it never does, so add them here.
        if subject_id not in seen:
            members.append(self._to_member(subject_raw, is_subject=True, hire_attr=hire_attr))

        # Subject first, then teammates alphabetically -- a stable order two
        # runs can be diffed against.
        members.sort(key=lambda m: (not m["is_subject"], (m["login"] or "").lower()))

        return ConnectorResult(
            plugin_name=self.name,
            identifier=identifier,
            data={
                "found": True,
                "members": members,
                "count": len(members),
                # The manager the cohort is keyed on (None in reports mode), so
                # the CLI can name whose team this is.
                "manager": manager_ref if cohort_kind == "peers" else None,
                "cohort": cohort_kind,
                # How many teammates were dropped for being deactivated, so the
                # CLI/HTML can say so rather than silently shrinking the team.
                "excluded_deactivated": excluded_deactivated,
            },
            properties={"subject_okta_id": subject_id},
            tags=["solo"] if len(members) == 1 else [f"cohort-{cohort_kind}"],
        )

    async def fetch_team_groups(
        self, identifier: str, *, include_deactivated: bool = False
    ) -> ConnectorResult:
        """Build the roster, then fetch every member's groups and compare them.

        The per-member groups calls run concurrently -- this is exactly the
        fan-out `fetch()` being async exists for, so a team of six costs one
        round trip's worth of wall-clock, not six. A single member's failure
        degrades that member's column (rendered `?`, excluded from the drift
        maths) rather than sinking the whole comparison; a failure building the
        roster itself is a hard error, because an empty matrix would read as
        "this manager has no team".
        """
        team = await self.fetch_team(identifier, include_deactivated=include_deactivated)
        if not team.ok:
            return ConnectorResult(
                plugin_name=self.name, identifier=identifier, error=team.error
            )
        if not team.data.get("found"):
            return ConnectorResult(
                plugin_name=self.name,
                identifier=identifier,
                data={"found": False, "members": [], "groups": [], "summary": {}},
                tags=["not-found"],
            )

        members = team.data["members"]
        group_results = await asyncio.gather(
            *(self.fetch_groups(m["login"], okta_id=m["okta_id"]) for m in members)
        )

        entries: list[dict] = []
        for member, result in zip(members, group_results):
            if result.ok and result.data.get("found", True):
                names = [g["name"] for g in result.data["groups"] if g["name"]]
                entries.append({**member, "groups": sorted(names, key=str.lower), "error": None})
            else:
                entries.append({**member, "groups": None, "error": result.error or "not found"})

        comparison = build_comparison(entries)
        return ConnectorResult(
            plugin_name=self.name,
            identifier=identifier,
            data={
                "found": True,
                "subject": next((m["login"] for m in members if m["is_subject"]), identifier),
                "manager": team.data.get("manager"),
                "cohort": team.data.get("cohort"),
                "excluded_deactivated": team.data.get("excluded_deactivated", 0),
                **comparison,
            },
            properties={"subject_okta_id": team.properties.get("subject_okta_id")},
        )

    async def fetch_search(self, query: str, *, fetch_all: bool = False) -> ConnectorResult:
        """Find users whose name, login or email starts with `query`.

        Returns candidates and never picks one: a single hit is not the same
        claim as the right person, and the section flags act on whoever the
        operator names next. Like `fetch()`, never raises for ordinary
        failures.
        """
        try:
            expression = build_search_expression(query)
            raw_users, truncated = await self._call_search_backend(expression, fetch_all)
        except ValueError as exc:
            # Bad input, not a service failure -- but still an error result
            # rather than an exception, per the connector contract.
            return ConnectorResult(plugin_name=self.name, identifier=query, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - contract: never crash aggregation
            return ConnectorResult(
                plugin_name=self.name,
                identifier=query,
                error=safe_error(exc, secrets=[self.config.get("OKTA_API_TOKEN")]),
            )

        matches = sorted(
            (self._to_match(user) for user in raw_users),
            key=lambda m: (m["login"] or "").lower(),
        )
        return ConnectorResult(
            plugin_name=self.name,
            identifier=query,
            data={
                "matches": matches,
                "count": len(matches),
                # Without --all: a full page back means Okta may be holding
                # more. With --all: we ran out of page budget while a `next`
                # link still existed. Either way the answer is incomplete, and
                # saying nothing would let it read as "that is everyone".
                "truncated": truncated,
                # Read by Stage 7's cache integration. A cached hit could
                # report someone ACTIVE minutes after they were deactivated --
                # wrong in exactly the case that matters.
                "cacheable": False,
            },
            tags=["no-matches"] if not matches else ["has-matches"],
        )

    async def fetch_device_signins(
        self,
        okta_id: str,
        *,
        since: timedelta,
        device_ids: set[str] | None = None,
    ) -> ConnectorResult:
        """Most recent successful sign-in per device, from the System Log.

        `/users/{id}/devices` has no last-login field -- its `lastUpdated`
        tracks changes to the device *record*, not sign-ins -- so this is a
        separate source correlated on `device.id`.

        Pass `device_ids` when the caller knows which devices it cares about:
        results come back newest-first, so once every device has been seen the
        remaining pages cannot change the answer and paging stops early. That
        matters because /api/v1/logs is Okta's most rate-limited endpoint.
        """
        try:
            signins = await self._call_logs_backend(okta_id, since, device_ids)
        except Exception as exc:  # noqa: BLE001 - contract: never crash aggregation
            return ConnectorResult(
                plugin_name=self.name,
                identifier=okta_id,
                error=safe_error(exc, secrets=[self.config.get("OKTA_API_TOKEN")]),
            )

        return ConnectorResult(
            plugin_name=self.name,
            identifier=okta_id,
            data={"signins": signins, "window": describe_window(since)},
        )

    # -- backend seam ---------------------------------------------------------

    async def _call_logs_backend(
        self, okta_id: str, since: timedelta, device_ids: set[str] | None
    ) -> dict[str, str]:
        if self.mock_mode:
            return self._mock_signins_fixture()

        org_url = self.config.require("OKTA_ORG_URL").rstrip("/")
        event_filter = " or ".join(f'eventType eq "{e}"' for e in _SIGNIN_EVENT_TYPES)
        params = {
            "since": (datetime.now(timezone.utc) - since).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "filter": f'actor.id eq "{okta_id}" and ({event_filter})',
            "sortOrder": "DESCENDING",
            "limit": "1000",
        }

        url = f"{org_url}/api/v1/logs"
        signins: dict[str, str] = {}
        seen_urls: set[str] = set()
        first = True

        async with self._client() as client:
            for _ in range(_MAX_PAGES):
                if url in seen_urls:
                    break
                seen_urls.add(url)

                response = await client.get(
                    url, headers=self._headers(), params=params if first else None
                )
                first = False
                if response.status_code in (401, 403):
                    raise RuntimeError(
                        "Okta refused the System Log request. This API token may lack "
                        "System Log read access, which is granted separately from user "
                        "read access."
                    )
                response.raise_for_status()

                for event in response.json():
                    if (event.get("outcome") or {}).get("result") != "SUCCESS":
                        continue
                    device_id = (event.get("device") or {}).get("id")
                    if not device_id:
                        # Not every auth event stamps a device. Guessing from
                        # the user agent could not tell two MacBooks apart, so
                        # an unattributable event is dropped rather than
                        # assigned to the wrong machine.
                        continue
                    # DESCENDING: the first sighting is the most recent.
                    signins.setdefault(device_id, event.get("published"))

                if device_ids and device_ids.issubset(signins):
                    break

                next_url = response.links.get("next", {}).get("url")
                if not next_url:
                    break
                url = next_url

        return signins

    def _mock_signins_fixture(self) -> dict[str, str]:
        return {"guoMOCK00000000000001": "2026-01-01T09:15:00.000Z"}


    async def _resolve_okta_id(self, identifier: str, okta_id: str | None) -> str | None:
        """Turn a login into an Okta id, or None when there's no such user.

        Callers that already resolved the user (as the CLI does when several
        sections are on screen) pass `okta_id` and skip the round trip.
        """
        if okta_id is not None:
            return okta_id
        user = await self._call_backend(identifier)
        return None if user is None else user.get("id")

    def _user_url(self, okta_id: str, suffix: str) -> str:
        org_url = self.config.require("OKTA_ORG_URL").rstrip("/")
        # quote(safe="") so an id or login can't walk off /api/v1/users.
        return f"{org_url}/api/v1/users/{quote(okta_id, safe='')}/{suffix}"

    async def _fetch_all_pages(
        self, url: str, *, on_404: str | None = None, on_denied: str | None = None
    ) -> list[dict]:
        """Follow Okta's `Link: rel="next"` pagination and concatenate pages.

        Shared by every list endpoint here. Under-reporting someone's devices,
        apps or authenticators is the worst failure this tool has, so stopping
        at page one is never the answer -- and one implementation means the
        three call sites can't quietly drift apart.

        `on_404` / `on_denied` turn a status code into an actionable message
        for endpoints where the generic HTTP error would mislead.
        """
        entries: list[dict] = []
        seen_urls: set[str] = set()

        async with self._client() as client:
            for _ in range(_MAX_PAGES):
                if url in seen_urls:
                    break  # self-referential `next`; stop rather than loop
                seen_urls.add(url)

                response = await client.get(url, headers=self._headers())
                if response.status_code == 404 and on_404:
                    raise RuntimeError(on_404)
                if response.status_code in (401, 403) and on_denied:
                    raise RuntimeError(on_denied)
                response.raise_for_status()

                entries.extend(response.json())

                next_url = response.links.get("next", {}).get("url")
                if not next_url:
                    break
                url = next_url

        return entries

    async def _call_devices_backend(self, okta_id: str) -> list[dict]:
        if self.mock_mode:
            return self._mock_devices_fixture()

        return await self._fetch_all_pages(
            self._user_url(okta_id, "devices"),
            # The user exists (we just resolved them), so a 404 here means the
            # device API itself is unavailable -- typically an Okta Classic
            # org. Say that, don't say "not found".
            on_404=(
                "Okta returned 404 for the device endpoint. This org may not "
                "have Okta Identity Engine device management enabled, or the "
                "API token may lack the devices scope."
            ),
        )

    async def _call_search_backend(
        self, expression: str, fetch_all: bool = False
    ) -> tuple[list[dict], bool]:
        """Return (users, truncated).

        One page by default: search is interactive, not an audit, and paging
        thousands of users to render a 15-row table would burn rate-limit
        budget nobody asked to spend. `--all` opts into the extra requests.
        """
        if self.mock_mode:
            return self._mock_search_fixture(), False

        org_url = self.config.require("OKTA_ORG_URL").rstrip("/")
        url = f"{org_url}/api/v1/users"
        params: dict[str, str] | None = {
            "search": expression,
            "limit": str(MAX_SEARCH_RESULTS),
        }

        users: list[dict] = []
        seen_urls: set[str] = set()
        pages = _MAX_PAGES if fetch_all else 1
        truncated = False

        async with self._client() as client:
            for _ in range(pages):
                if url in seen_urls:
                    # Self-referential `next`: stop rather than loop. We were
                    # told more exists but can't safely reach it, so this is
                    # an incomplete answer, not a finished one.
                    truncated = True
                    break
                seen_urls.add(url)

                response = await client.get(url, headers=self._headers(), params=params)
                params = None  # the `next` URL already carries the query

                if response.status_code in (401, 403):
                    raise RuntimeError(
                        "Okta refused the user search. This API token may lack user "
                        "read access across the directory, which is broader than "
                        "reading a single known user."
                    )
                if response.status_code == 400:
                    # Okta answers 400 for a filter it can't parse. Its raw body
                    # is not actionable, and the expression is ours, so name that.
                    raise RuntimeError(
                        "Okta rejected the search expression. This is a bug in how "
                        "the search filter is built, not something a different name "
                        "will fix."
                    )
                response.raise_for_status()

                page = response.json()
                users.extend(page)

                next_url = response.links.get("next", {}).get("url")
                if not next_url:
                    break
                url = next_url
            else:
                # Ran out of page budget with a `next` still outstanding. A
                # hard bound keeps a looping `next` from hanging the CLI, but
                # the answer is incomplete and must say so.
                truncated = True

        if not fetch_all:
            truncated = len(users) >= MAX_SEARCH_RESULTS
        return users, truncated

    async def _call_apps_backend(self, okta_id: str) -> list[dict]:
        if self.mock_mode:
            return self._mock_apps_fixture()

        return await self._fetch_all_pages(
            self._user_url(okta_id, "appLinks"),
            on_denied=(
                "Okta refused the app list request. This API token may lack "
                "application read access, which is granted separately from "
                "user read access."
            ),
        )

    async def _call_groups_backend(self, okta_id: str) -> list[dict]:
        if self.mock_mode:
            return self._mock_groups_fixture(okta_id)

        return await self._fetch_all_pages(
            self._user_url(okta_id, "groups"),
            on_denied=(
                "Okta refused the group membership request. This API token may "
                "lack group read access, which is granted separately from user "
                "read access."
            ),
        )

    async def _call_cohort_backend(self, manager_ref: str) -> list[dict]:
        """Users whose manager attribute points at `manager_ref`.

        `manager_ref` is whatever `profile.<managerAttr>` holds -- normally the
        manager's login/email/id -- so this returns everyone reporting to that
        manager (the subject and their peers). Paginated like the other list
        endpoints, because an under-reported team would silently drop a person
        from the comparison -- the same "never stop at page one" reasoning the
        app/device lists follow.
        """
        if self.mock_mode:
            return self._mock_cohort_fixture()

        org_url = self.config.require("OKTA_ORG_URL").rstrip("/")
        # Escape backslashes then quotes so a value cannot terminate the filter
        # string or smuggle an operator into it (same guard as the name search).
        safe = manager_ref.replace("\\", "\\\\").replace('"', '\\"')
        expression = f'profile.{self._manager_attribute} eq "{safe}"'

        url = f"{org_url}/api/v1/users"
        params: dict[str, str] | None = {"search": expression, "limit": str(MAX_SEARCH_RESULTS)}
        reports: list[dict] = []
        seen_urls: set[str] = set()

        async with self._client() as client:
            for _ in range(_MAX_PAGES):
                if url in seen_urls:
                    break  # self-referential `next`; stop rather than loop
                seen_urls.add(url)

                response = await client.get(url, headers=self._headers(), params=params)
                params = None  # the `next` URL already carries the query

                if response.status_code in (401, 403):
                    raise RuntimeError(
                        "Okta refused the team-cohort search. This API token may lack "
                        "user read access across the directory, which is broader than "
                        "reading a single known user."
                    )
                response.raise_for_status()

                reports.extend(response.json())

                next_url = response.links.get("next", {}).get("url")
                if not next_url:
                    break
                url = next_url

        return reports

    async def _call_factors_backend(self, okta_id: str) -> list[dict]:
        if self.mock_mode:
            return self._mock_factors_fixture()

        return await self._fetch_all_pages(
            self._user_url(okta_id, "factors"),
            on_denied=(
                "Okta refused the authenticator request. This API token may lack "
                "factor read access, which is granted separately from user read "
                "access."
            ),
        )

    async def _call_backend(self, identifier: str) -> dict | None:
        """Return the raw Okta user payload, or None if there's no such user."""
        if self.mock_mode:
            return self._mock_fixture(identifier)

        org_url = self.config.require("OKTA_ORG_URL").rstrip("/")

        # `identifier` is user input. quote(safe="") keeps an email's `@`
        # working while stopping `../` from walking off /api/v1/users.
        url = f"{org_url}/api/v1/users/{quote(identifier, safe='')}"

        async with self._client() as client:
            response = await client.get(url, headers=self._headers())

        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def _client(self) -> httpx.AsyncClient:
        """A timeout-bounded client, so one slow call can't hang a lookup."""
        timeout = float(self.config.get("OKTA_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS)
        return httpx.AsyncClient(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"SSWS {self.config.require('OKTA_API_TOKEN')}",
            "Accept": "application/json",
        }

    def _mock_fixture(self, identifier: str) -> dict:
        return {
            "id": "00uMOCK0000000000000",
            "status": "ACTIVE",
            "created": "2024-01-01T00:00:00.000Z",
            "activated": "2024-01-01T00:05:00.000Z",
            "statusChanged": "2024-01-01T00:05:00.000Z",
            "lastLogin": "2026-01-01T00:00:00.000Z",
            "profile": {
                "firstName": "Mock",
                "lastName": "User",
                "email": f"{identifier}@example.com",
                "login": identifier,
                # Fictional value; the real attribute's type is org-defined
                # and this connector does not care which it is.
                DEFAULT_ACCESS_ATTRIBUTE: False,
                # A manager so `--team` in mock mode exercises the peer-cohort
                # path (compare against teammates who share this manager), which
                # is the real behaviour, rather than the no-manager fallback.
                DEFAULT_MANAGER_ATTRIBUTE: "mmanager",
                DEFAULT_HIRE_DATE_ATTRIBUTE: "2024-03-15",
            },
        }

    def _mock_devices_fixture(self) -> list[dict]:
        return [
            {
                "id": "guoMOCK00000000000001",
                "managementStatus": "MANAGED",
                "device": {
                    "id": "guoMOCK00000000000001",
                    "status": "ACTIVE",
                    "lastUpdated": "2026-01-01T00:00:00.000Z",
                    "profile": {
                        "displayName": "Mock MacBook Pro",
                        "platform": "MACOS",
                        "manufacturer": "Apple",
                        "model": "MacBookPro18,3",
                        "osVersion": "15.6.0",
                        "serialNumber": "C02MOCK00001",
                    },
                },
            }
        ]

    def _mock_search_fixture(self) -> list[dict]:
        """Three hits including a deactivated one, so the mock demo shows the
        chooser and the case the feature exists for rather than a single hit."""
        return [
            {"id": "00uMOCK1", "status": "ACTIVE",
             "profile": {"login": "dluo", "firstName": "Dennis", "lastName": "Luo",
                         "email": "dluo@example.com"}},
            {"id": "00uMOCK2", "status": "DEPROVISIONED",
             "profile": {"login": "dcarter", "firstName": "Dennis", "lastName": "Carter",
                         "email": "dcarter@example.com"}},
            {"id": "00uMOCK3", "status": "ACTIVE",
             "profile": {"login": "mdennison", "firstName": "Marta", "lastName": "Dennison",
                         "email": "mdennison@example.com"}},
        ]

    def _mock_apps_fixture(self) -> list[dict]:
        return [
            {
                "id": "0oaMOCK00000000000001",
                "label": "Mock Google Workspace",
                "appName": "google",
                "hidden": False,
            },
            {
                "id": "0oaMOCK00000000000002",
                "label": "Mock Slack",
                "appName": "slack",
                "hidden": True,
            },
        ]

    def _mock_factors_fixture(self) -> list[dict]:
        return [
            {
                "id": "opfMOCK00000000000001",
                "factorType": "push",
                "provider": "OKTA",
                "status": "ACTIVE",
                "created": "2025-06-11T08:12:00.000Z",
                "profile": {"name": "Mock iPhone"},
            },
            {
                "id": "opfMOCK00000000000002",
                "factorType": "token:software:totp",
                "provider": "OKTA",
                "status": "ACTIVE",
                "created": "2025-06-11T08:14:00.000Z",
                "profile": {"credentialId": "mock@example.com"},
            },
        ]

    #: Fixture memberships keyed by the mock okta ids below, chosen so the demo
    #: matrix shows both shared rows (Everyone, Eng-GitHub) and drift rows
    #: (VPN-Users, Okta-Admins, PagerDuty-OnCall) rather than a flat grid where
    #: everyone matches -- the flat case is not the one the feature exists for.
    _MOCK_TEAM_GROUPS = {
        "00uMOCK0000000000000": ["Everyone", "Eng-GitHub", "VPN-Users", "Okta-Admins"],
        "00uMOCKREPORT00000001": ["Everyone", "Eng-GitHub", "VPN-Users"],
        "00uMOCKREPORT00000002": ["Everyone", "Eng-GitHub"],
        "00uMOCKREPORT00000003": ["Everyone", "Eng-GitHub", "VPN-Users", "PagerDuty-OnCall"],
    }

    def _mock_groups_fixture(self, okta_id: str) -> list[dict]:
        names = self._MOCK_TEAM_GROUPS.get(okta_id, ["Everyone", "Eng-GitHub"])
        return [
            {"id": f"00g{i}", "type": "OKTA_GROUP", "profile": {"name": name}}
            for i, name in enumerate(names)
        ]

    def _mock_cohort_fixture(self) -> list[dict]:
        """Three teammates in the mock manager's cohort, with distinct ids so
        the fixture memberships above give each a different column. The queried
        subject is added by `fetch_team` itself, so it is not repeated here."""
        return [
            {"id": "00uMOCKREPORT00000001", "status": "ACTIVE",
             "profile": {"login": "arivera", "firstName": "Ana", "lastName": "Rivera",
                         "email": "arivera@example.com", "hireDate": "2023-06-01"}},
            {"id": "00uMOCKREPORT00000002", "status": "ACTIVE",
             "profile": {"login": "dsingh", "firstName": "Dev", "lastName": "Singh",
                         "email": "dsingh@example.com", "hireDate": "2025-02-20"}},
            {"id": "00uMOCKREPORT00000003", "status": "ACTIVE",
             "profile": {"login": "kobrien", "firstName": "Kit", "lastName": "O'Brien",
                         "email": "kobrien@example.com", "hireDate": "2026-01-10"}},
        ]

    # -- shaping --------------------------------------------------------------

    @staticmethod
    def _to_match(user: dict) -> dict:
        """One search candidate: only what the chooser needs to let someone
        pick, plus the status that usually settles which one they meant."""
        profile = user.get("profile") or {}
        names = [profile.get("firstName"), profile.get("lastName")]
        return {
            "login": profile.get("login"),
            "name": " ".join(part for part in names if part) or None,
            "email": profile.get("email"),
            "status": user.get("status"),
        }

    @staticmethod
    def _to_app(entry: dict) -> dict:
        return {
            "app_id": entry.get("id") or entry.get("appInstanceId"),
            "label": entry.get("label"),
            "app_name": entry.get("appName"),
            # A hidden tile is still an assignment. Recorded rather than
            # filtered, so the CLI can say so instead of silently omitting it.
            "hidden": bool(entry.get("hidden")),
        }

    @staticmethod
    def _to_group(entry: dict) -> dict:
        profile = entry.get("profile") or {}
        return {
            "group_id": entry.get("id"),
            # The display name is what an operator recognises and what the
            # comparison lines members up by; the API's `type` (OKTA_GROUP /
            # BUILT_IN / APP_GROUP) is kept because "Everyone" being BUILT_IN
            # explains why it is shared by all.
            "name": profile.get("name"),
            "type": entry.get("type"),
        }

    @staticmethod
    def _to_member(raw: dict, *, is_subject: bool, hire_attr: str) -> dict:
        profile = raw.get("profile") or {}
        names = [profile.get("firstName"), profile.get("lastName")]
        # Hire date orders the columns oldest-first in the HTML. The custom
        # attribute is preferred; Okta's account `created` is a reasonable
        # proxy when a person's hire date isn't populated, so ordering still
        # works. Left as None (sorts last) only when neither exists.
        hire_date = profile.get(hire_attr) or raw.get("created")
        return {
            "okta_id": raw.get("id"),
            "login": profile.get("login"),
            "name": " ".join(part for part in names if part) or None,
            "is_subject": is_subject,
            "hire_date": hire_date,
        }

    @staticmethod
    def _to_factor(entry: dict) -> dict:
        factor_type = entry.get("factorType")
        provider = entry.get("provider")
        return {
            "factor_id": entry.get("id"),
            "factor_type": factor_type,
            "provider": provider,
            "label": factor_label(factor_type, provider),
            "detail": factor_detail(entry.get("profile")),
            "status": entry.get("status"),
            "created": entry.get("created"),
        }

    @staticmethod
    def _to_device(entry: dict) -> dict:
        """Normalise one device entry.

        Okta returns a link object wrapping a `device`; tolerate a flat
        device object too, since not every response nests it.
        """
        device = entry.get("device") or entry
        profile = device.get("profile") or {}
        return {
            "device_id": device.get("id") or entry.get("id"),
            "display_name": profile.get("displayName"),
            "platform": profile.get("platform"),
            "manufacturer": profile.get("manufacturer"),
            "model": profile.get("model"),
            "os_version": profile.get("osVersion"),
            "serial_number": profile.get("serialNumber"),
            "status": device.get("status"),
            "management_status": entry.get("managementStatus"),
            "last_updated": device.get("lastUpdated"),
        }


    def _to_result(self, identifier: str, raw: dict) -> ConnectorResult:
        profile = raw.get("profile") or {}
        status = raw.get("status")
        names = [profile.get("firstName"), profile.get("lastName")]
        display_name = " ".join(part for part in names if part) or None

        return ConnectorResult(
            plugin_name=self.name,
            identifier=identifier,
            data={
                "found": True,
                "status": status,
                # Verbatim: whatever Okta returned, uninterpreted. Placed
                # right after `status` so the CLI's ordered walk renders the
                # row directly beneath it.
                "access_blocked": profile.get(self._access_attribute),
                # Derived, but worth carrying: it is the single question
                # offboarding actually asks, and it keeps every consumer
                # (CLI, Stage 7 aggregation, JSON output) from re-deriving
                # which of eight enum values means "deactivated".
                "deactivated": status == _DEACTIVATED_STATUS,
                "login": profile.get("login"),
                "email": profile.get("email"),
                "display_name": display_name,
            },
            # Optional detail goes here rather than growing `data`'s schema.
            properties={
                "okta_id": raw.get("id"),
                "created": raw.get("created"),
                "activated": raw.get("activated"),
                "status_changed": raw.get("statusChanged"),
                "last_login": raw.get("lastLogin"),
            },
            tags=["active" if status in _ACTIVE_STATUSES else "inactive"],
        )

    # -- CLI ------------------------------------------------------------------

    def cli(self) -> typer.Typer:
        """`lookup-cli okta <identifier> [-s] [-d]`.

        Shape: `<service> <person> [what you want]`, with no noun
        subcommands. `okta status jdoe` and `okta devices jdoe` were removed
        on 2026-09-02 because they cannot coexist with `okta jdoe` -- a
        person whose Okta login is literally "status" or "devices" would
        silently resolve to the subcommand instead of being looked up.
        Stages 4-6 follow the same shape.

        Lives here, not in core `cli.py`, so this connector required no edit
        to `src/lookup_cli/`.
        """
        sub_app = typer.Typer(
            help="Okta account lookups.",
            # Required: Click groups stop parsing options once they hit a
            # positional, so without this `okta jdoe -d` fails while
            # `okta -d jdoe` works -- a confusing split for users.
            context_settings={"allow_interspersed_args": True},
        )
        console = Console()

        @sub_app.callback(invoke_without_command=True)
        def okta(
            identifier: str = typer.Argument(..., help="Okta username or email address."),
            status: bool = typer.Option(
                False,
                "--status",
                "-s",
                help="Show account status, including whether the user is deactivated. "
                "This is the default when no other flag is given.",
            ),
            devices: bool = typer.Option(
                False,
                "--devices",
                "-d",
                help="List devices registered to this user in Okta "
                "(Okta Verify / device trust -- not the Jamf or ABM inventory).",
            ),
            apps: bool = typer.Option(
                False,
                "--apps",
                "-apps",
                "-a",
                help="List applications assigned to this user, hidden tiles "
                "included. Shows what they can open, not how it was granted.",
            ),
            authenticators: bool = typer.Option(
                False,
                "--authenticators",
                "-authenticators",
                "-u",
                help="List authenticators (MFA factors) this user has enrolled, "
                "including inactive ones.",
            ),
            groups: bool = typer.Option(
                False,
                "--groups",
                "-groups",
                "-g",
                help="List the Okta groups this user belongs to. Bundles with "
                "the other sections (e.g. -sg, -sdaug).",
            ),
            team: bool = typer.Option(
                False,
                "--team",
                help="Compare group memberships across this person's team "
                "(them plus their direct reports) instead of looking one person "
                "up. Long-only and composes with nothing -- it changes the whole "
                "operation. Pair with --html to write the comparison as a page.",
            ),
            html_path: str = typer.Option(
                "",
                "--html",
                metavar="PATH",
                help="With --team, also write the comparison to PATH as a "
                "standalone HTML page you can open in a browser.",
            ),
            include_deactivated: bool = typer.Option(
                False,
                "--include-deactivated",
                help="With --team, keep deactivated (deprovisioned) teammates in "
                "the comparison. They are excluded by default, since the point is "
                "who currently has access. The queried person is always shown, "
                "whatever their status.",
            ),
            find: bool = typer.Option(
                False,
                "--find",
                help="Search for a person by name instead of looking one up. "
                "Use when you know their first or surname but not their Okta "
                "username. Long-only and cannot be combined with the section "
                "flags -- it finds a person, it does not describe one.",
            ),
            show_all: bool = typer.Option(
                False,
                "--all",
                help="With --find, show every match instead of the first "
                "screenful, following pagination. Long-only: -a already means "
                "applications.",
            ),
            last_signin: bool = typer.Option(
                False,
                "--last-signin",
                help="Add each device's most recent sign-in, from the Okta System "
                "Log. Opt-in: it costs an extra call to a rate-limited endpoint. "
                "Implies --devices.",
            ),
            since: str = typer.Option(
                "90d",
                "--since",
                help="Window for --last-signin, e.g. 30d or 12h. Okta retains "
                "System Log data for about 90 days, which is the maximum.",
            ),
        ) -> None:
            """Look one person up in Okta, or search for them by name."""
            # --find is a mode, not a section: it answers "who is this
            # person", while every section flag answers "what do you want to
            # see about this person". There is nothing to describe until one
            # has been picked, so the combination is rejected rather than
            # silently dropping the section the user typed -- which is the
            # failure mode this CLI keeps designing out.
            if find:
                conflicting = [
                    name
                    for name, on in (
                        ("--status", status), ("--devices", devices), ("--apps", apps),
                        ("--authenticators", authenticators), ("--groups", groups),
                        ("--team", team), ("--last-signin", last_signin),
                        ("--include-deactivated", include_deactivated),
                    )
                    if on
                ]
                if conflicting:
                    console.print(
                        f"[red]--find cannot be combined with[/red] {', '.join(conflicting)}[red].[/red]\n"
                        "--find locates a person; the section flags describe one already found.\n"
                        f"Find the username first, then: [bold]lookup-cli okta <username> "
                        f"{conflicting[0]}[/bold]"
                    )
                    raise typer.Exit(code=2)
                _print_search(identifier, fetch_all=show_all)
                return

            # --team is a mode like --find: it replaces "describe one person"
            # with "compare a team", so a section flag alongside it is the same
            # silently-dropped-request failure and is rejected the same way.
            if team:
                conflicting = [
                    name
                    for name, on in (
                        ("--status", status), ("--devices", devices), ("--apps", apps),
                        ("--authenticators", authenticators), ("--groups", groups),
                        ("--find", find), ("--all", show_all), ("--last-signin", last_signin),
                    )
                    if on
                ]
                if conflicting:
                    console.print(
                        f"[red]--team cannot be combined with[/red] {', '.join(conflicting)}[red].[/red]\n"
                        "--team compares a whole team; the section flags describe one person."
                    )
                    raise typer.Exit(code=2)
                _print_team(
                    identifier,
                    html_path=html_path or None,
                    include_deactivated=include_deactivated,
                )
                return

            # --html renders the team comparison; outside --team there is
            # nothing to render, so accept it silently would leave someone
            # believing they had asked for a file.
            if html_path:
                console.print(
                    "[red]--html only applies to --team.[/red]\n"
                    f"Did you mean: [bold]lookup-cli okta {identifier} --team --html {html_path}[/bold]?"
                )
                raise typer.Exit(code=2)

            # Same reasoning for the deactivated-teammate toggle: it only shapes
            # the team comparison, so silently accepting it elsewhere would leave
            # someone believing it had an effect.
            if include_deactivated:
                console.print(
                    "[red]--include-deactivated only applies to --team.[/red]\n"
                    f"Did you mean: [bold]lookup-cli okta {identifier} --team --include-deactivated[/bold]?"
                )
                raise typer.Exit(code=2)

            if show_all:
                # --all modifies the search; there is no search to modify.
                # Accepting it silently would leave someone believing they
                # had asked for something.
                console.print(
                    "[red]--all only applies to --find.[/red]\n"
                    f"Did you mean: [bold]lookup-cli okta --find {identifier} --all[/bold]?"
                )
                raise typer.Exit(code=2)

            # Asking for per-device sign-ins obviously means you want the
            # device table; requiring -d as well would just be pedantry.
            if last_signin:
                devices = True

            window = None
            if last_signin:
                try:
                    window = parse_since(since)
                except ValueError as exc:
                    console.print(f"[red]Invalid --since:[/red] {exc}")
                    raise typer.Exit(code=2)

            # Flags select sections. With none given, status is what people
            # want; `-d` alone means devices only.
            show_status = status or not (devices or apps or authenticators or groups)

            # A section that is the *whole* answer must fail the command, so
            # scripts can trust the exit code. Alongside other sections a dead
            # endpoint degrades its own row instead of discarding good output.
            sole_section = sum((show_status, devices, apps, authenticators, groups)) == 1

            result = asyncio.run(self.fetch(identifier))
            if not result.ok:
                console.print(f"[red]Okta lookup failed:[/red] {result.error}")
                raise typer.Exit(code=1)

            if not result.data.get("found"):
                console.print(f"[yellow]No Okta account found for[/yellow] {identifier}")
                # A hint, not an implicit fallback: --find stays explicit and
                # the miss path stays one API call. Without this, someone who
                # does not know --find exists still hits a dead end -- and not
                # knowing the username is exactly the situation in which you
                # would not know the flag either.
                console.print(
                    f"[dim]Try:[/dim] [bold]lookup-cli okta --find {identifier}[/bold]"
                    "[dim]   to search by name[/dim]"
                )
                return

            okta_id = result.properties.get("okta_id")

            if show_status:
                _print_status(identifier, result)

            if devices:
                _print_devices(identifier, okta_id=okta_id, window=window, primary=sole_section)

            if apps:
                _print_apps(identifier, okta_id=okta_id, primary=sole_section)

            if authenticators:
                _print_authenticators(identifier, okta_id=okta_id, primary=sole_section)

            if groups:
                _print_groups(identifier, okta_id=okta_id, primary=sole_section)

        def _print_groups(identifier: str, okta_id: str | None, primary: bool) -> None:
            result = asyncio.run(self.fetch_groups(identifier, okta_id=okta_id))

            if not result.ok:
                console.print(f"[red]Groups unavailable:[/red] {result.error}")
                if primary:
                    raise typer.Exit(code=1)
                return

            if not result.data.get("found", True):
                console.print(f"[yellow]No Okta account found for[/yellow] {identifier}")
                return

            found = result.data["groups"]
            if not found:
                console.print(f"[yellow]No Okta groups for[/yellow] {identifier}")
                return

            table = Table(title=f"Groups ({result.data['count']}) - {identifier}")
            table.add_column("group")
            table.add_column("type")
            for group in found:
                table.add_row(group["name"] or "-", group["type"] or "-")
            console.print(table)

        def _print_team(
            identifier: str, html_path: str | None, include_deactivated: bool = False
        ) -> None:
            """Render the team's group comparison, and optionally write it as HTML."""
            result = asyncio.run(
                self.fetch_team_groups(identifier, include_deactivated=include_deactivated)
            )

            if not result.ok:
                console.print(f"[red]Team comparison failed:[/red] {result.error}")
                raise typer.Exit(code=1)

            if not result.data.get("found"):
                console.print(f"[yellow]No Okta account found for[/yellow] {identifier}")
                console.print(
                    f"[dim]Try:[/dim] [bold]lookup-cli okta --find {identifier}[/bold]"
                    "[dim]   to search by name[/dim]"
                )
                return

            members = result.data["members"]
            rows = result.data["groups"]
            summary = result.data["summary"]
            cohort = result.data.get("cohort")
            manager = result.data.get("manager")
            subject_login = result.data.get("subject") or identifier

            # Say whose team this is up front, so it is clear the comparison is
            # against the subject's peers -- not, say, their reports.
            if cohort == "peers":
                console.print(
                    f"[dim]Team = everyone who reports to[/dim] [bold]{manager}[/bold]"
                    f"[dim]; comparing[/dim] [bold]{subject_login}[/bold] "
                    "[dim]against their teammates.[/dim]"
                )
            elif cohort == "reports":
                console.print(
                    f"[yellow]No manager on {subject_login}'s Okta profile[/yellow] "
                    "[dim]- comparing their direct reports instead.[/dim]"
                )

            table = Table(
                title=f"Group comparison - team of {subject_login} ({summary['member_count']} members)"
            )
            table.add_column("group")
            for member in members:
                label = member["login"] or "?"
                if member["is_subject"]:
                    label += " (subject)"
                if member["groups"] is None:
                    label += " (n/a)"
                table.add_column(label, no_wrap=True)

            for row in rows:
                tag = "  [all]" if row["everyone"] else ("  [drift]" if row["drift"] else "")
                cells = [f"{row['name']}{tag}"]
                for member in members:
                    if member["groups"] is None:
                        cells.append("?")
                    else:
                        cells.append("yes" if row["coverage"].get(member["login"]) else "-")
                table.add_row(*cells)
            console.print(table)

            # One-line takeaway, so the answer to "does the team diverge" does
            # not require reading every cell. Names the drift count explicitly.
            console.print(
                f"[dim]{summary['drift_count']} group(s) with drift, "
                f"{summary['shared_by_all']} shared by all, "
                f"across {summary['member_count']} members[/dim]"
            )
            if summary["error_count"]:
                console.print(
                    f"[yellow]{summary['error_count']} member(s) could not be read[/yellow] "
                    "(shown as ?) and are excluded from the shared/drift counts."
                )
            excluded = result.data.get("excluded_deactivated", 0)
            if excluded:
                console.print(
                    f"[dim]{excluded} deactivated teammate(s) excluded; "
                    "use --include-deactivated to show them.[/dim]"
                )

            if html_path:
                try:
                    Path(html_path).write_text(
                        render_team_html(
                            result.data,
                            subject_login=subject_login,
                            manager=manager,
                            cohort=cohort,
                            excluded_deactivated=excluded,
                        ),
                        encoding="utf-8",
                    )
                except OSError as exc:
                    console.print(f"[red]Could not write {html_path}:[/red] {safe_error(exc)}")
                    raise typer.Exit(code=1)
                console.print(f"[green]Wrote comparison to[/green] {html_path}")

        def _print_search(query: str, fetch_all: bool = False) -> None:
            """Candidate chooser for `--find`.

            Deliberately the same shape as the CAIRO connector's vendor
            chooser: same problem (fuzzy input, several candidates, never
            guess), so an operator learns one idiom rather than two. It is
            reimplemented rather than shared because `plugins/CLAUDE.md`
            forbids importing across plugin packages -- extracting this into
            core is a bigger decision than a connector task should make, and
            is logged in docs/STAGES.md instead.
            """
            result = asyncio.run(self.fetch_search(query, fetch_all=fetch_all))

            if not result.ok:
                console.print(f"[red]Okta search failed:[/red] {result.error}")
                raise typer.Exit(code=1)

            matches = result.data["matches"]
            if not matches:
                console.print(f"[yellow]No Okta user matches[/yellow] {query}")
                console.print(
                    "[dim]Search matches the start of a first name, surname, login or "
                    "email -- so 'dennis' finds Dennis, but 'ennis' finds nobody. "
                    "Multiple words narrow: 'dennis luo' needs both to match.[/dim]"
                )
                return

            shown = matches if fetch_all else matches[:MAX_MATCHES_SHOWN]
            console.print(
                f"[yellow]{result.data['count']} "
                f"{'person' if result.data['count'] == 1 else 'people'} match[/yellow] "
                f"'{query}'[yellow]:[/yellow]"
            )

            table = Table()
            # Login never wraps: it is the value you copy into the next
            # command, and a truncated username is worse than useless.
            table.add_column("username", no_wrap=True)
            table.add_column("name")
            table.add_column("email")
            table.add_column("status", no_wrap=True)
            for match in shown:
                status_value = match["status"] or "UNKNOWN"
                colour, _note = _STATUS_NOTES.get(status_value, ("yellow", ""))
                table.add_row(
                    match["login"] or "-",
                    match["name"] or "-",
                    match["email"] or "-",
                    f"[{colour}]{status_value}[/{colour}]",
                )
            console.print(table)

            if len(matches) > len(shown):
                # Never truncate silently -- a short list reads as "that's all"
                # -- and always name the way out. Telling someone to narrow
                # without mentioning --all repeats the dead end that the
                # --find hint exists to prevent.
                console.print(
                    f"[yellow]{len(matches) - len(shown)} more not shown[/yellow] - "
                    "narrow the search (try adding a surname), or use [bold]--all[/bold]"
                )
            if result.data["truncated"]:
                console.print(
                    "[yellow]Okta may be holding more matches than it returned[/yellow] - "
                    + ("narrow the search to be sure you are seeing everyone."
                       if fetch_all else
                       "narrow the search, or use [bold]--all[/bold] to follow pagination.")
                )

            # Make the two-step flow copy-paste rather than retype.
            example = shown[0]["login"] or "<username>"
            console.print(
                f"[dim]Then:[/dim] [bold]lookup-cli okta {example} -sdau[/bold]"
                "[dim]   (or -s / -d / -a / -u)[/dim]"
            )

        def _print_status(identifier: str, result: ConnectorResult) -> None:
            status_value = result.data.get("status") or "UNKNOWN"
            colour, note = _STATUS_NOTES.get(status_value, ("yellow", ""))
            changed = (result.properties.get("status_changed") or "")[:10]

            suffix = ""
            if note:
                suffix = f" ({note}{' ' + changed if changed else ''})"
            console.print(
                f"[bold]{identifier}[/bold] - [{colour}]{status_value}[/{colour}]{suffix}"
            )

            table = Table(title=f"Okta - {identifier}")
            table.add_column("field")
            table.add_column("value")
            # `found` and `deactivated` are derived and already stated in the
            # line above; repeating them here is noise.
            for key, value in result.data.items():
                if key in ("found", "deactivated"):
                    continue
                if key == "access_blocked":
                    table.add_row(ACCESS_FIELD_LABEL, format_profile_value(value))
                else:
                    table.add_row(key, str(value) if value is not None else "-")
            for key, value in result.properties.items():
                table.add_row(key, str(value) if value is not None else "-")
            console.print(table)

        def _print_apps(identifier: str, okta_id: str | None, primary: bool) -> None:
            result = asyncio.run(self.fetch_apps(identifier, okta_id=okta_id))

            if not result.ok:
                console.print(f"[red]Applications unavailable:[/red] {result.error}")
                if primary:
                    raise typer.Exit(code=1)
                return

            if not result.data.get("found", True):
                console.print(f"[yellow]No Okta account found for[/yellow] {identifier}")
                return

            apps = result.data["apps"]
            if not apps:
                console.print(
                    f"[yellow]No applications assigned in Okta to[/yellow] {identifier}"
                )
                return

            table = Table(title=f"Applications ({result.data['count']}) - {identifier}")
            table.add_column("app")
            table.add_column("type")
            # Named for the API field rather than inverted to "visible": an
            # operator reading the table should not have to flip the sense of
            # the column in their head to match what Okta told us.
            table.add_column("hidden")
            for app in apps:
                table.add_row(
                    app["label"] or "-",
                    app["app_name"] or "-",
                    "yes" if app["hidden"] else "no",
                )
            console.print(table)

        def _print_authenticators(identifier: str, okta_id: str | None, primary: bool) -> None:
            result = asyncio.run(self.fetch_authenticators(identifier, okta_id=okta_id))

            if not result.ok:
                console.print(f"[red]Authenticators unavailable:[/red] {result.error}")
                if primary:
                    raise typer.Exit(code=1)
                return

            if not result.data.get("found", True):
                console.print(f"[yellow]No Okta account found for[/yellow] {identifier}")
                return

            factors = result.data["authenticators"]
            if not factors:
                console.print(f"[yellow]No authenticators enrolled in Okta by[/yellow] {identifier}")
                return

            table = Table(title=f"Authenticators ({result.data['count']}) - {identifier}")
            table.add_column("type")
            # `detail` is the field that says *which* authenticator this is --
            # two Okta Verify pushes are only distinguishable by device name --
            # so it never wraps. The type label gives way instead.
            table.add_column("detail", no_wrap=True)
            table.add_column("status")
            table.add_column("enrolled", no_wrap=True)
            for factor in factors:
                colour = _FACTOR_STATUS_COLOURS.get(factor["status"], "yellow")
                status_value = factor["status"] or "UNKNOWN"
                table.add_row(
                    factor["label"] or "-",
                    factor["detail"] or "-",
                    f"[{colour}]{status_value}[/{colour}]",
                    (factor["created"] or "")[:10] or "-",
                )
            console.print(table)

        def _print_devices(
            identifier: str, okta_id: str | None, primary: bool, window: timedelta | None = None
        ) -> None:
            result = asyncio.run(self.fetch_devices(identifier, okta_id=okta_id))

            if not result.ok:
                console.print(f"[red]Devices unavailable:[/red] {result.error}")
                if primary:
                    raise typer.Exit(code=1)
                return

            if not result.data.get("found", True):
                console.print(f"[yellow]No Okta account found for[/yellow] {identifier}")
                return

            found = result.data["devices"]
            if not found:
                console.print(f"[yellow]No devices registered in Okta for[/yellow] {identifier}")
                return

            # Five columns, not seven: at a stock 80-column terminal rich
            # squeezes seven down until the serial renders as an empty cell.
            # Serial is the field an offboarding operator actually needs, so
            # it never wraps -- the name gives way instead.
            signins: dict[str, str] = {}
            signins_failed = False
            if window is not None:
                # Only the devices we are about to print, so paging can stop
                # as soon as they are all accounted for.
                wanted = {d["device_id"] for d in found if d.get("device_id")}
                signin_result = asyncio.run(
                    self.fetch_device_signins(
                        result.properties.get("okta_id") or okta_id or identifier,
                        since=window,
                        device_ids=wanted or None,
                    )
                )
                if signin_result.ok:
                    signins = signin_result.data["signins"]
                else:
                    # The inventory is a real answer on its own; losing sign-in
                    # times degrades one column rather than discarding it.
                    signins_failed = True
                    console.print(f"[yellow]Sign-in times unavailable:[/yellow] {signin_result.error}")

            table = Table(title=f"Devices ({result.data['count']}) - {identifier}")
            table.add_column("name")
            table.add_column("platform")
            # `model` gives way when the sign-in column is present. Six columns
            # re-create the 80-column squeeze that dropping from seven to five
            # fixed: model truncates to "MacBook..." and status wraps to three
            # lines. Of the two, model is the least actionable -- serial
            # identifies the machine and platform says what it is.
            if window is None:
                table.add_column("model")
            table.add_column("serial", no_wrap=True)
            table.add_column("status")
            if window is not None:
                # The window is in the header, not a footnote: a blank cell
                # means "not in this window", never "never used".
                table.add_column(f"last sign-in ({describe_window(window)})", no_wrap=True)
            for device in found:
                platform = " ".join(
                    part for part in (device["platform"], device["os_version"]) if part
                )
                state = " / ".join(
                    part for part in (device["status"], device["management_status"]) if part
                )
                row = [device["display_name"] or "-", platform or "-"]
                if window is None:
                    row.append(device["model"] or "-")
                row += [device["serial_number"] or "-", state or "-"]
                if window is not None:
                    if signins_failed:
                        row.append("?")
                    else:
                        stamp = signins.get(device.get("device_id") or "")
                        row.append(stamp[:10] if stamp else "-")
                table.add_row(*row)
            console.print(table)

        return sub_app
