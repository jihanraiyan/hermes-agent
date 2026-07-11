#!/usr/bin/env python3
"""Repeatable teammate onboarding for the Hermes iMessage (Sendblue) agent.

Adds or updates one person across the TWO Railway env vars that control who can
use the agent over iMessage, keeping them in sync:

  SENDBLUE_ALLOWED_USERS   comma-separated E.164 — who may talk to the agent
  SENDBLUE_USER_NAMES      "+E164=Name,..."      — display roster the agent sees

The allowlist is the security gate: the deployed gateway matches an inbound
sender's E.164 against it exactly, so a teammate not on it is silently ignored.
The roster feeds the agent a friendly "[Name] ..." prefix so it knows who is
texting on the shared number (this half requires the Sendblue adapter's
SENDBLUE_USER_NAMES change to be deployed — see the note printed after a change).

Doing this by hand is what let the two vars drift (a name in the roster with no
allowlist entry can never actually reach the agent). This script always writes
both together so that can't happen.

Usage:
    scripts/add_teammate.py "Alice Smith" +14155550123   # add / update a teammate
    scripts/add_teammate.py --list                        # show state + flag drift
    scripts/add_teammate.py "Alice" +14155550123 --yes    # skip the confirm prompt
    scripts/add_teammate.py "Alice" +14155550123 --deploy # apply AND redeploy now

Idempotent: re-running for a number updates the name and never duplicates.
Requires the railway CLI linked to the hermes-imessage service (`railway status`).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from typing import Dict, List

ALLOW_VAR = "SENDBLUE_ALLOWED_USERS"
NAMES_VAR = "SENDBLUE_USER_NAMES"

# Same shape the Sendblue adapter enforces on inbound senders (adapter.py:57),
# so anything this script admits is a number the gateway can actually match.
E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def _run(args: List[str]) -> str:
    """Run a railway CLI command, returning stdout (raises on failure)."""
    proc = subprocess.run(
        ["railway", *args],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.exit(
            f"railway {' '.join(args)} failed (exit {proc.returncode}):\n"
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


def read_vars() -> Dict[str, str]:
    """Read the linked service's variables as a {KEY: VALUE} map."""
    out = _run(["variables", "--kv"])
    result: Dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def parse_allow(raw: str) -> List[str]:
    """Comma-separated E.164 list -> de-duplicated, order-preserving list."""
    seen: List[str] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if item and item not in seen:
            seen.append(item)
    return seen


def parse_names(raw: str) -> Dict[str, str]:
    """"+E164=Name,..." -> {number: name}. Mirrors adapter._parse_user_names."""
    mapping: Dict[str, str] = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        number, name = pair.split("=", 1)
        number, name = number.strip(), name.strip()
        if number and name:
            mapping[number] = name
    return mapping


def serialize_names(mapping: Dict[str, str]) -> str:
    return ",".join(f"{num}={name}" for num, name in mapping.items())


def cmd_list(current: Dict[str, str]) -> None:
    allowed = parse_allow(current.get(ALLOW_VAR, ""))
    names = parse_names(current.get(NAMES_VAR, ""))
    numbers = list(dict.fromkeys([*allowed, *names.keys()]))

    print(f"Team roster ({len(numbers)} number(s)):\n")
    for num in numbers:
        name = names.get(num, "(no name)")
        can_talk = "✅ allowed" if num in allowed else "⚠️  NOT allowed"
        print(f"  {num:<16} {name:<20} {can_talk}")

    drift = [n for n in names if n not in allowed]
    if drift:
        print(
            "\nℹ️  These have a display name but are NOT in SENDBLUE_ALLOWED_USERS.\n"
            "   That's fine IF they were approved via `hermes pairing approve`\n"
            "   (pairing-store grants union with the allowlist and aren't visible\n"
            "   here). If a number below can't actually reach the agent, add it:"
        )
        for num in drift:
            print(f"     scripts/add_teammate.py \"{names[num]}\" {num}")


def cmd_add(name: str, number: str, current: Dict[str, str], yes: bool, deploy: bool) -> None:
    name = name.strip()
    number = number.strip()

    if not E164_RE.match(number):
        sys.exit(f"'{number}' is not a valid E.164 number (expected e.g. +14155550123).")
    if not name:
        sys.exit("Name must not be empty.")
    if "," in name or "=" in name:
        sys.exit("Name must not contain ',' or '=' (those delimit the roster format).")

    allowed = parse_allow(current.get(ALLOW_VAR, ""))
    names = parse_names(current.get(NAMES_VAR, ""))

    existing = names.get(number)
    on_allowlist = number in allowed
    if existing == name and on_allowlist:
        print(f"{name} ({number}) is already onboarded and in sync. Nothing to do.")
        return

    if number not in allowed:
        allowed.append(number)
    names[number] = name

    new_allow = ",".join(allowed)
    new_names = serialize_names(names)

    verb = "Updating" if existing else "Adding"
    print(f"{verb} {name} ({number}).\n")
    print(f"  {ALLOW_VAR}:\n    {new_allow}\n")
    print(f"  {NAMES_VAR}:\n    {new_names}\n")

    if not yes:
        reply = input("Apply these to Railway? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("Aborted. No changes made.")
            return

    # Always set with --skip-deploys so changing config and shipping it are two
    # explicit steps; redeploy only when the operator asks (--deploy or prompt).
    _run([
        "variables",
        "--skip-deploys",
        "--set", f"{ALLOW_VAR}={new_allow}",
        "--set", f"{NAMES_VAR}={new_names}",
    ])
    print("✅ Railway variables updated.")

    if not deploy and not yes:
        reply = input("Redeploy now to apply? [y/N] ").strip().lower()
        deploy = reply in ("y", "yes")

    if deploy:
        print("Redeploying...")
        _run(["redeploy", "-y"])
        print("✅ Redeploy triggered.")
    else:
        print(
            "\nVariables saved but NOT yet live. Apply with:\n"
            "    railway redeploy -y"
        )

    if names[number] and not existing:
        print(
            "\nNote: the '[Name] ...' prefix the agent sees needs the Sendblue\n"
            "adapter's SENDBLUE_USER_NAMES change deployed. The allowlist grant is\n"
            "live regardless once you redeploy."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add or update a Hermes iMessage teammate (allowlist + roster).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("name", nargs="?", help='Display name, e.g. "Alice Smith".')
    parser.add_argument("number", nargs="?", help="E.164 number, e.g. +14155550123.")
    parser.add_argument("--list", action="store_true", help="Show current team + drift, then exit.")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompts.")
    parser.add_argument("--deploy", action="store_true", help="Redeploy immediately after setting vars.")
    args = parser.parse_args()

    current = read_vars()

    if args.list:
        cmd_list(current)
        return

    if not args.name or not args.number:
        parser.error("provide NAME and NUMBER, or use --list.")

    cmd_add(args.name, args.number, current, yes=args.yes, deploy=args.deploy)


if __name__ == "__main__":
    main()
