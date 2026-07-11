# Adding teammates to the Hermes iMessage agent

The agent runs on one Sendblue number. Who can use it is controlled by **two
Railway env vars that must stay in sync**:

| Var | Role |
| --- | --- |
| `SENDBLUE_ALLOWED_USERS` | Security gate. Comma-separated E.164. The gateway matches an inbound sender's number against this **exactly** — anyone not listed is silently ignored. |
| `SENDBLUE_USER_NAMES` | Display roster, `+E164=Name,...`. The agent sees `[Name] message` so it knows who is texting on the shared number. Display only — never affects auth or routing. |

If the two drift (a name in the roster with no allowlist entry), that person can
never actually reach the agent. Don't edit them by hand — use the script, which
always writes both together.

## Add or update a teammate

```bash
scripts/add_teammate.py "Alice Smith" +14155550123
```

It reads the current Railway vars, merges the person into both, previews the
change, and (on confirm) sets the vars and offers to redeploy. Idempotent —
re-running updates the name and never duplicates.

```bash
scripts/add_teammate.py --list                          # show team + flag drift
scripts/add_teammate.py "Alice" +14155550123 --deploy   # apply and redeploy now
scripts/add_teammate.py "Alice" +14155550123 --yes      # no prompts (scripting)
```

Requires the railway CLI linked to the `hermes-imessage` service (`railway status`).

## Notes

- The `[Name] ...` prefix needs the adapter's `SENDBLUE_USER_NAMES` support
  deployed. The **allowlist grant is live** the moment you redeploy regardless.
- `hermes pairing approve sendblue <CODE>` is a second, independent way in:
  it grants access via the **pairing store** (on the volume), which unions with
  the allowlist. Such users are authorized even though they won't appear in
  `SENDBLUE_ALLOWED_USERS`, so `--list` can't see them — a name-without-allowlist
  entry here is only a real problem if that person actually can't reach the agent.
- To remove someone, drop their number from `SENDBLUE_ALLOWED_USERS` and their
  `+E164=Name` from `SENDBLUE_USER_NAMES` (Railway dashboard) and redeploy. If
  they were pairing-approved, also revoke the pairing grant.
- `SENDBLUE_ALLOW_ALL_USERS=true` opens the agent to anyone who can reach the
  webhook — do not use it in production.
