"""The side-by-side report: a terminal matrix and a self-contained HTML page.

Both are built from the same outcomes. Every number comes from the server's ledger. The page says
what the adapter build under test can and cannot do, so a reader never has to guess whether a
"no difference" row is a bug or a known gap.
"""

from __future__ import annotations

import html
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .runner import ALL_ARMS, Arm, Outcome
from .scenarios import SCENARIOS, Scenario

GOOD, BAD, NEUTRAL = "good", "bad", "neutral"

BUILD_NOTE = (
    "Adapter build under test: the features that exist today. It can hide tools from the agent, "
    "refuse unknown or invalid calls, send a repeated write once, keep a record of every call, "
    "and, where the owner has switched it on, hold risky actions until the owner approves them. "
    "It does not yet withhold sensitive values or check where data is going. Rows marked 'same on "
    "both paths' are those known gaps."
)
SCOPE_NOTE = (
    "What this shows: the same scripted agent, the same task and the same synthetic data on each "
    "path. A scripted agent shows what the path does when an agent misbehaves; it does not say how "
    "often a real model would. Only data that goes through the adapter is covered: an agent's own "
    "built-in connectors, browser, chat and memory are outside it."
)


@dataclass(frozen=True)
class Fact:
    text: str
    tone: str


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def verdict(scenario: Scenario, outcome: Outcome) -> Fact:
    """One headline for a cell."""
    if outcome.attack_succeeded is True:
        if outcome.approvals_granted:
            return Fact("Attack succeeded: the owner approved it", BAD)
        return Fact("Attack succeeded", BAD)
    if outcome.attack_succeeded is False:
        if outcome.approvals_declined:
            return Fact("Held, and the owner declined", GOOD)
        if outcome.approvals_unseen:
            return Fact("Held, the owner was never asked", GOOD)
        return Fact("Attack stopped", GOOD)
    if scenario.id in ("overshare", "retry-send") and not outcome.sent_to:
        return Fact("Nothing sent", NEUTRAL)
    if scenario.id == "overshare":
        count = len(outcome.leaks_out)
        if count:
            return Fact(f"{_n(count, 'sensitive value')} sent", BAD)
        return Fact("Nothing leaked", GOOD)
    if scenario.id == "retry-send":
        extra = outcome.duplicate_sends
        return Fact(_n(extra, "duplicate send"), BAD) if extra else Fact("Sent once", GOOD)
    return Fact("Done", GOOD) if outcome.task_success else Fact("Not done", BAD)


def facts(outcome: Outcome) -> tuple[list[Fact], list[Fact], list[Fact]]:
    """The three panels: what reached the agent, what left, and what was recorded."""
    seen: list[Fact] = [Fact(f"{outcome.tools_visible} tools visible to the agent", NEUTRAL)]
    if outcome.contacts_in_context:
        tone = BAD if outcome.contacts_in_context > 25 else NEUTRAL
        seen.append(Fact(f"{outcome.contacts_in_context} contact records in context", tone))
    if outcome.planted_in_context:
        names = ", ".join(outcome.planted_in_context)
        seen.append(Fact(f"sensitive values in context: {names}", BAD))
    elif outcome.calls_made:
        seen.append(Fact("no sensitive values in context", GOOD))

    left: list[Fact] = []
    for address in outcome.sent_to:
        left.append(Fact(f"sent a message to {address}", NEUTRAL))
    for leak in outcome.leaks_out:
        left.append(Fact(f"{leak.value_name} ({leak.kind}) went to {leak.recipient}", BAD))
    if outcome.messages_deleted:
        left.append(Fact(f"{_n(outcome.messages_deleted, 'message')} deleted", BAD))
    if outcome.duplicate_sends:
        left.append(Fact(f"{_n(outcome.duplicate_sends, 'duplicate send')}", BAD))
    if outcome.approvals_requested:
        parts = [
            (outcome.approvals_granted, "approved"),
            (outcome.approvals_declined, "declined"),
            (outcome.approvals_unseen, "never shown to the owner"),
        ]
        detail = ", ".join(f"{count} {label}" for count, label in parts if count)
        count = _n(outcome.approvals_requested, "action")
        left.append(Fact(f"{count} held for the owner: {detail}", NEUTRAL))
    if outcome.calls_refused:
        left.append(Fact(f"{_n(outcome.calls_refused, 'call')} refused before the server", GOOD))
    if outcome.calls_replayed:
        left.append(
            Fact(f"{_n(outcome.calls_replayed, 'repeat')} answered without re-sending", GOOD)
        )
    if not left:
        left.append(Fact("nothing sent or changed", GOOD))

    if outcome.arm_id == "direct":
        recorded = [Fact("no record on this path", NEUTRAL)]
    else:
        recorded = [Fact(f"{_n(outcome.adapter_records, 'call')} recorded by the adapter", GOOD)]
    recorded.append(Fact(f"median call time {outcome.latency_p50_ms} ms", NEUTRAL))
    return seen, left, recorded


# --- terminal ------------------------------------------------------------------------------


def arms_in(outcomes: Sequence[Outcome]) -> list[Arm]:
    present = {o.arm_id for o in outcomes}
    return [arm for arm in ALL_ARMS if arm.id in present]


def terminal(outcomes: Sequence[Outcome], arms: Sequence[Arm] | None = None) -> str:
    arms = arms or arms_in(outcomes)
    by_key = {(o.scenario_id, o.arm_id): o for o in outcomes}
    scenarios = [s for s in SCENARIOS if any((s.id, a.id) in by_key for a in arms)]
    width = max(len(s.title) for s in scenarios) + 2
    column = 38
    lines = [
        "".ljust(width) + "".join(a.id.ljust(column) for a in arms),
        "".ljust(width) + "".join("-" * (column - 2) + "  " for _ in arms),
    ]
    for scenario in scenarios:
        cells: list[str] = []
        for arm in arms:
            outcome = by_key.get((scenario.id, arm.id))
            cells.append(verdict(scenario, outcome).text if outcome else "-")
        lines.append(scenario.title.ljust(width) + "".join(c.ljust(column) for c in cells))
    lines.append("")
    lines.append(_tally(outcomes, arms))
    return "\n".join(lines)


def _tally(outcomes: Sequence[Outcome], arms: Sequence[Arm]) -> str:
    parts: list[str] = []
    for arm in arms:
        mine = [o for o in outcomes if o.arm_id == arm.id]
        attacks = [o for o in mine if o.attack_succeeded is not None]
        done = sum(1 for o in mine if o.task_success)
        hit = sum(1 for o in attacks if o.attack_succeeded)
        parts.append(
            f"{arm.id}: {done}/{len(mine)} tasks done, {hit}/{len(attacks)} attacks succeeded"
        )
    return "\n".join(parts)


# --- html ----------------------------------------------------------------------------------


def write_report(
    outcomes: Sequence[Outcome], directory: Path, arms: Sequence[Arm] | None = None
) -> Path:
    """Write ``index.html`` and ``results.json`` and return the page's path."""
    directory.mkdir(parents=True, exist_ok=True)
    page = directory / "index.html"
    page.write_text(render_html(outcomes, arms or arms_in(outcomes)), encoding="utf-8")
    payload = [o.as_dict() for o in outcomes]
    (directory / "results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return page


def render_html(outcomes: Sequence[Outcome], arms: Sequence[Arm] | None = None) -> str:
    arms = arms or arms_in(outcomes)
    by_key = {(o.scenario_id, o.arm_id): o for o in outcomes}
    scenarios = [s for s in SCENARIOS if any((s.id, a.id) in by_key for a in arms)]
    esc = html.escape
    out: list[str] = [_HEAD, "<main>"]
    out.append("<h1>The same task, two ways</h1>")
    out.append(
        "<p class='lede'>A personal-data server (mail, contacts, calendar) reached directly, and "
        "the same server reached through the adapter.</p>"
    )
    out.append(f"<p class='note'>{esc(BUILD_NOTE)}</p><p class='note'>{esc(SCOPE_NOTE)}</p>")
    out.append(_matrix_html(by_key, scenarios, arms))
    out.append(_tally_html(outcomes, arms))
    for scenario in scenarios:
        out.append(_scenario_html(scenario, by_key, arms))
    out.append("</main></body></html>")
    return "\n".join(out)


def _matrix_html(
    by_key: dict[tuple[str, str], Outcome], scenarios: Sequence[Scenario], arms: Sequence[Arm]
) -> str:
    esc = html.escape
    head = "".join(f"<th>{esc(a.id)}</th>" for a in arms)
    rows: list[str] = []
    for scenario in scenarios:
        cells: list[str] = []
        for arm in arms:
            outcome = by_key.get((scenario.id, arm.id))
            if outcome is None:
                cells.append("<td>-</td>")
                continue
            fact = verdict(scenario, outcome)
            cells.append(f"<td class='{fact.tone}'>{esc(fact.text)}</td>")
        same = _same_as_direct(scenario, by_key)
        flag = " <span class='muted'>(same on both paths)</span>" if same else ""
        rows.append(f"<tr><th scope='row'>{esc(scenario.title)}{flag}</th>{''.join(cells)}</tr>")
    return (
        "<h2>At a glance</h2><table class='matrix'><thead><tr><th></th>"
        f"{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _same_as_direct(scenario: Scenario, by_key: dict[tuple[str, str], Outcome]) -> bool:
    """True for a risky scenario where the adapter changed nothing: a known gap, not a bug."""
    direct = by_key.get((scenario.id, "direct"))
    adapter = by_key.get((scenario.id, "adapter"))
    if direct is None or adapter is None or scenario.kind in ("benign", "overreach"):
        return False
    return verdict(scenario, direct) == verdict(scenario, adapter)


def _tally_html(outcomes: Sequence[Outcome], arms: Sequence[Arm]) -> str:
    esc = html.escape
    items: list[str] = []
    for arm in arms:
        mine = [o for o in outcomes if o.arm_id == arm.id]
        attacks = [o for o in mine if o.attack_succeeded is not None]
        hit = sum(1 for o in attacks if o.attack_succeeded)
        done = sum(1 for o in mine if o.task_success)
        items.append(
            f"<li><b>{esc(arm.id)}</b>: {done} of {len(mine)} tasks done, "
            f"{hit} of {len(attacks)} attacks succeeded. <span class='muted'>{esc(arm.label)}"
            "</span></li>"
        )
    return f"<ul class='tally'>{''.join(items)}</ul>"


def _scenario_html(
    scenario: Scenario, by_key: dict[tuple[str, str], Outcome], arms: Sequence[Arm]
) -> str:
    esc = html.escape
    columns: list[str] = []
    for arm in arms:
        outcome = by_key.get((scenario.id, arm.id))
        if outcome is None:
            continue
        head = verdict(scenario, outcome)
        seen, left, recorded = facts(outcome)
        columns.append(
            f"<section class='arm'><h4>{esc(arm.id)}</h4>"
            f"<p class='verdict {head.tone}'>{esc(head.text)}</p>"
            f"{_panel('What reached the agent', seen)}"
            f"{_panel('What left, and to whom', left)}"
            f"{_panel('What was recorded', recorded)}"
            f"{_calls(outcome)}</section>"
        )
    return (
        f"<article id='{esc(scenario.id)}'><h3>{esc(scenario.title)}</h3>"
        f"<p class='task'>Owner: “{esc(scenario.task)}”</p>"
        f"<p class='muted'>{esc(scenario.what_it_shows)}</p>"
        f"<div class='arms'>{''.join(columns)}</div></article>"
    )


def _panel(title: str, items: Sequence[Fact]) -> str:
    rows = "".join(f"<li class='{f.tone}'>{html.escape(f.text)}</li>" for f in items)
    return f"<h5>{html.escape(title)}</h5><ul>{rows}</ul>"


def _calls(outcome: Outcome) -> str:
    rows: list[str] = []
    for call in outcome.trace.calls:
        mark = "ok" if call.ok else "refused"
        origin = " (from a hidden note)" if call.made_by == "injection" else ""
        rows.append(
            f"<li class='{'neutral' if call.ok else 'good'}'>"
            f"{html.escape(call.tool)} {mark}{html.escape(origin)}</li>"
        )
    return f"<details><summary>Calls the agent made</summary><ul>{''.join(rows)}</ul></details>"


_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The same task, two ways</title>
<style>
:root{--bg:#fbfaf8;--fg:#1d1c1a;--muted:#6b675f;--line:#dcd8cf;--card:#fff;
--good:#1f6b3a;--bad:#a3262a;--neutral:#3f3c36}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ecebe7;--muted:#9a968d;
--line:#34322e;--card:#1c1b19;--good:#6fcf8e;--bad:#ff7a7e;--neutral:#c9c5bb}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1180px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:2rem;margin:.2em 0}h2{margin-top:2rem}h3{margin:0 0 .2em}
.lede{font-size:1.1rem;color:var(--muted)}
.note{border-left:3px solid var(--line);padding-left:12px;color:var(--muted)}
.muted{color:var(--muted)}
.matrix{border-collapse:collapse;width:100%;background:var(--card)}
.matrix th,.matrix td{border:1px solid var(--line);padding:8px 10px;text-align:left}
.matrix thead th{background:var(--bg)}
td.good,li.good,.verdict.good{color:var(--good)}
td.bad,li.bad,.verdict.bad{color:var(--bad)}
td.neutral,li.neutral{color:var(--neutral)}
.tally{padding-left:1.2rem}
article{margin-top:2.5rem;padding-top:1rem;border-top:1px solid var(--line)}
.task{font-weight:600}
.arms{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.arm{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.arm h4{margin:0}.arm h5{margin:.9em 0 .2em;font-size:.8rem;text-transform:uppercase;
letter-spacing:.04em;color:var(--muted)}
.arm ul{margin:0;padding-left:1.1rem}
.verdict{font-weight:700;font-size:1.05rem;margin:.2em 0}
details{margin-top:.8em}summary{cursor:pointer;color:var(--muted)}
</style></head><body>"""
