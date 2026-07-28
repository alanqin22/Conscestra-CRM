"""Cases formatter — C1 Step 5. Executor result -> markdown for the chat UI."""

from __future__ import annotations

from typing import Any, Dict, List

_STATUS_ICON = {"new": "🆕", "in_progress": "🔧", "waiting": "⏳",
                "resolved": "✅", "closed": "🔒"}
_PRIO_ICON = {"urgent": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}


def _owner(r: Dict[str, Any]) -> str:
    if r.get("owner_email"):
        return r["owner_email"]
    if r.get("source_assignee"):
        # Says WHY it is unowned, not just that it is — the console/escalation
        # named someone the CRM could not identify.
        return f"unowned (source: {r['source_assignee']})"
    return "unowned"


def _line(r: Dict[str, Any]) -> str:
    return (f"- {_STATUS_ICON.get(r.get('status'), '•')} "
            f"{_PRIO_ICON.get(r.get('priority'), '')} **{r.get('subject')}** — "
            f"{r.get('status')} · {_owner(r)} · `{(r.get('case_id') or '')[:8]}`")


def format_response(result: Dict[str, Any]) -> Dict[str, Any]:
    action = result.get("action", "")
    if not result.get("ok"):
        # A refusal is an ANSWER — the lifecycle or the identity rules said no,
        # and the user should see the reason rather than a generic failure.
        prefix = "That isn't permitted" if result.get("refused") else "Error"
        return {"output": f"{prefix}: {result.get('error')}", "mode": action}

    rows: List[Dict[str, Any]] = result.get("rows") or []

    if action in ("list_cases", "case_queue"):
        if not rows:
            return {"output": "No matching cases.", "mode": action}
        head = ("**Live case queue**" if action == "case_queue"
                else f"**{len(rows)} case(s)**")
        unowned = sum(1 for r in rows if not r.get("owner_id"))
        tail = (f"\n\n_{unowned} of these are unowned._" if unowned else "")
        return {"output": head + "\n" + "\n".join(_line(r) for r in rows) + tail,
                "mode": action}

    if action == "get_case":
        r = rows[0]
        out = [f"### {r.get('subject')}",
               f"- **Status** {r.get('status')} "
               f"(next: {', '.join(r.get('next_states') or []) or 'terminal'})",
               f"- **Priority** {r.get('priority')}",
               f"- **Owner** {_owner(r)}",
               f"- **Opened** {(r.get('created_at') or '')[:16]}"]
        # NULL means UNKNOWN on a historical row, and "not yet" on a live one.
        # Saying which is the difference between an honest metric and a lie.
        if r.get("is_historical"):
            out.append("- _Pre-lifecycle record: response and resolution times "
                       "are unknown, not zero._")
        else:
            out.append(f"- **First response** "
                       f"{(r.get('first_response_at') or 'not yet')[:16]}")
            if r.get("resolved_at"):
                out.append(f"- **Resolved** {r['resolved_at'][:16]}")
        if r.get("reopen_count"):
            out.append(f"- **Reopened** {r['reopen_count']}×")
        if r.get("description"):
            out.append(f"\n{r['description'][:800]}")
        comments = r.get("comments") or []
        if comments:
            out.append(f"\n**{len(comments)} comment(s)**")
            for c in comments[-5:]:
                tag = "internal" if c.get("is_internal") else "public"
                out.append(f"- _{tag}_ {str(c.get('comment'))[:200]}")
        return {"output": "\n".join(out), "mode": action}

    if action == "case_history":
        if not rows:
            return {"output": "No recorded changes yet.", "mode": action}
        out = ["**Change history**"]
        for h in rows:
            old = h.get("old_value")
            out.append(f"- `{h.get('field')}` "
                       f"{'(unset)' if old is None else old} → "
                       f"{h.get('new_value')} — {h.get('actor')} "
                       f"({h.get('source') or 'n/a'}) "
                       f"{(h.get('changed_at') or '')[:16]}")
        return {"output": "\n".join(out), "mode": action}

    res = result.get("result") or {}
    if action == "open_case":
        return {"output": f"Opened case `{(res.get('case_id') or '')[:8]}` "
                          f"(status: new).", "mode": action}
    if action == "transition":
        return {"output": f"Moved to **{res.get('changed') and 'updated' or 'updated'}** — "
                          f"{', '.join(res.get('changed') or [])} changed.",
                "mode": action}
    if action == "assign":
        return {"output": "Owner assigned; the change is recorded in the "
                          "case history.", "mode": action}
    if action == "set_priority":
        return {"output": "Priority updated.", "mode": action}
    if action == "add_comment":
        return {"output": "Comment added.", "mode": action}
    return {"output": "Done.", "mode": action}
