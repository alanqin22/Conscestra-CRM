"""Give an executive office to a named person - including when it changes hands.

    python -m scripts.provision_executive --role COO \
        --name "Alex Zhou" --email alex.zhou@agentorc.ca --by "Alan Qin (CEO)"

WHY THIS IS NOT AN UPDATE STATEMENT. An office and a person are different
things, and this system had them fused. `executives.full_name` was edited to
read "Alex Zhou" while `executives.owner_id` still pointed at the owner row of
Yongmei Qin, who held the office before. That row holds 48 activities and 2
opportunities. Had a credential been issued on the role mailbox, Alex Zhou would
have signed in, decided, and had every decision stamped with Yongmei Qin's owner
id - the shared-credential failure this whole identity separation exists to end,
except backwards and invisible, because the console would have shown a plausible
name throughout.

So: the incoming officer gets their OWN owner identity, and the outgoing one
keeps theirs, still owning everything they decided while they held the post.
Nothing is renamed, nothing is reassigned, and no history changes hands. The
succession is recorded as a fact rather than performed by overwriting.

The script refuses rather than guesses:
  * an email already resolving to somebody else's owner row  -> refuse
  * a role with no active `executives` row                   -> refuse
  * an owner row that already holds work, being re-pointed   -> refuse unless
    --i-am-renaming-the-same-person is passed, which is the OTHER case (the
    person did not change; their name was wrong or it changed) and is the only
    situation in which editing the existing row is correct.

Read-only unless --apply is given.
"""
from __future__ import annotations

import argparse
import secrets
import sys

from app.core import assignable
from app.core.database import get_connection


def _rows(sql, args=()):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            cols = [d[0] for d in cur.description] if cur.description else []
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def _exec(sql, args=()):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def _work_held(owner_id):
    """What would be silently reattributed if this row were handed to somebody
    else. Counted across every table that names an accountable owner, because
    the answer 'none' is only safe if it is complete."""
    held = {}
    for table, col in (("activities", "owner_id"), ("opportunities", "owner_id"),
                       ("accounts", "owner_id"), ("orders", "owner_id"),
                       ("action_approvals", "accountable_owner_id")):
        try:
            n = _rows("SELECT count(*) AS n FROM {} WHERE {} = %s::uuid".format(
                table, col), (owner_id,))[0]["n"]
        except Exception:
            continue
        if n:
            held[table + "." + col] = int(n)
    return held


def _make_credential(email, name, role):
    """Create a sign-in whose password nobody ever learns.

    The secret is generated here, used once to satisfy the signup contract, and
    discarded unread. The executive sets their own through Reset password. A
    password that passes through a terminal, a transcript or a chat message has
    been disclosed, and the whole point of an individual credential is that only
    one person can present it.

    Idempotent: an existing credential is left alone rather than reset, so
    re-running this script never locks somebody out of an account they have
    already set up.
    """
    existing = _rows("SELECT access_role, is_active FROM auth_credentials "
                     " WHERE lower(identifier) = lower(%s)", (email,))
    if existing:
        print("  credential for {} already exists ({}) - left alone".format(
            email, existing[0]))
        return
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            first, _, last = name.strip().partition(" ")
            throwaway = secrets.token_urlsafe(48)
            cur.execute(
                "SELECT public.sp_signup_with_lead"
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (email, throwaway, first, last or "", "Conscestra", "",
                 role, "", "", "", "", "", ""))
            cur.fetchone()
            del throwaway
        conn.commit()
    finally:
        conn.close()
    now = _rows("SELECT access_role, is_active FROM auth_credentials "
                " WHERE lower(identifier) = lower(%s)", (email,))
    print("  credential created for {} - access_role={!r}, NOT admin. The "
          "password is random and was never read; set it via Reset password "
          "on auth.html".format(email, now[0]["access_role"] if now else "?"))


def main(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--role", required=True, help="CEO | CRO | CFO | CTO | COO")
    p.add_argument("--name", required=True, help="the person, e.g. Alex Zhou")
    p.add_argument("--email", required=True,
                   help="THEIR OWN address. A role mailbox transfers with the "
                        "office and cannot tell two post-holders apart.")
    p.add_argument("--by", required=True,
                   help="the human authorising this, e.g. Alan Qin (CEO)")
    p.add_argument("--credential", action="store_true",
                   help="also create a sign-in; the password is random and "
                        "discarded unread, so they set it via Reset password")
    p.add_argument("--i-am-renaming-the-same-person", action="store_true",
                   dest="same_person",
                   help="the post-holder did NOT change; correct the name in "
                        "place and keep the single owner identity")
    p.add_argument("--apply", action="store_true",
                   help="write; otherwise dry run")
    a = p.parse_args(argv)

    role = a.role.strip().upper()
    email = a.email.strip().lower()
    mode = "APPLY" if a.apply else "DRY RUN"
    print("[{}] {} -> {} <{}>, authorised by {}\n".format(
        mode, role, a.name, email, a.by))

    ex = _rows("SELECT role_code, full_name, email, owner_id::text AS owner_id, "
               "       employee_uuid::text AS employee_uuid "
               "  FROM executives WHERE role_code = %s AND is_active", (role,))
    if not ex:
        print("REFUSED: no active executives row for {}.".format(role))
        return 2
    ex = ex[0]
    current = ex["owner_id"] or ex["employee_uuid"]
    print("  authority row : {} <{}>".format(ex["full_name"], ex["email"]))
    print("  points at     : {}".format(current))

    incumbent = _rows(
        "SELECT owner_id::text AS owner_id, first_name, last_name, email "
        "  FROM owners WHERE owner_id = %s::uuid", (current,)) if current else []
    held = _work_held(current) if current else {}
    if incumbent:
        i = incumbent[0]
        print("  incumbent     : {} {} <{}>".format(
            i["first_name"], i["last_name"], i["email"]))
        print("  work held     : {}".format(held or "none"))

    # -- the same-person branch: correct a name, keep one identity ------------
    if a.same_person:
        if not current:
            print("REFUSED: nothing to rename - the role has no owner identity yet.")
            return 2
        first, _, last = a.name.strip().partition(" ")
        print("\n  RENAME IN PLACE - the office did not change hands, so the "
              "single owner identity is correct and its history stays valid.")
        if not a.apply:
            print("  (dry run) would update owners + assignable_identity + executives"
                  + (" -> create credential" if a.credential else ""))
            return 0
        _exec("UPDATE owners SET first_name=%s, last_name=%s, email=%s "
              " WHERE owner_id=%s::uuid", (first, last or "", email, current))
        _exec("UPDATE assignable_identity SET display_name=%s, email=%s, "
              "       updated_at=now() WHERE lower(email)=lower(%s)",
              (a.name, email, ex["email"]))
        _exec("UPDATE executives SET full_name=%s, email=%s, updated_at=now() "
              " WHERE role_code=%s AND is_active", (a.name, email, role))
        print("  done - one person, one identity, history untouched.")
        if a.credential:
            _make_credential(email, a.name, role)
        return 0

    # -- the succession branch: a new person takes the office -----------------
    clash = _rows("SELECT owner_id::text AS owner_id, first_name, last_name "
                  "  FROM owners WHERE lower(email) = %s", (email,))
    if clash and clash[0]["owner_id"] != current:
        c = clash[0]
        print("\nREFUSED: {} already belongs to owner {} ({} {}). Two people "
              "cannot share an identifier - that is the defect, not the fix."
              .format(email, c["owner_id"], c["first_name"], c["last_name"]))
        return 2
    if clash and clash[0]["owner_id"] == current and held:
        print("\nREFUSED: {} IS the incumbent's identifier, and that owner holds "
              "{} record(s). Issuing this address to a new person would silently "
              "reattribute every one of them. Give the incoming officer their "
              "own address, or pass --i-am-renaming-the-same-person if the "
              "post-holder did not actually change."
              .format(email, sum(held.values())))
        return 2

    print("\n  SUCCESSION - {} gets a NEW owner identity. The incumbent row is "
          "left exactly as it is, still accountable for its {} record(s)."
          .format(a.name, sum(held.values()) if held else 0))
    if not a.apply:
        print("  (dry run) would: grant membership -> provision owner -> "
              "re-point executives.owner_id"
              + (" -> create credential" if a.credential else ""))
        return 0

    g = assignable.grant(email, display_name=a.name, source="executive",
                         source_ref=role, added_by=a.by)
    print("  grant(): {}".format(g))
    if not g.get("ok"):
        return 1
    o = assignable.provision_owner(email, display_name=a.name, by=a.by)
    print("  provision_owner(): {}".format(o))
    if not o.get("ok"):
        return 1
    new_id = o["owner_id"]

    # E7 transition window: the truthfully named column and the misnamed one it
    # was copied from must carry the same value, or eligibility reads the stale
    # one and the succession only half lands.
    _exec("UPDATE executives SET owner_id=%s::uuid, employee_uuid=%s::uuid, "
          "       full_name=%s, email=%s, updated_at=now() "
          " WHERE role_code=%s AND is_active",
          (new_id, new_id, a.name, email, role))
    print("  executives.{} now points at {}".format(role, new_id))

    if a.credential:
        _make_credential(email, a.name, role)

    print("\n  The outgoing officer's owner row was not renamed, not merged and "
          "not deleted. Ask who decided something last quarter and the answer "
          "is still true.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
