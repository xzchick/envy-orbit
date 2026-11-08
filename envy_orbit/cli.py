"""Command-line entry point. The Orbit window is the default.

Status reads are read-only; every mutation (adding a track, logging a completion,
removing a track) writes a brand-new orbit revision and never overwrites the
previous one.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from . import __version__
from .core import (DEFAULT_LOOKAHEAD, OrbitError, add_track, check_passphrase, create,
                   export, inspect, log, remove_track, snooze, status, write_ical)


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="envy-orbit",
        description="Encrypted recurring-cadence tracking — keep everything in orbit. Define tracks with a "
                    "cadence, log completions, and see what is overdue, due, coming up soon or on track. Run "
                    "with no arguments to open the window. Local only: no network, no accounts.")
    root.add_argument("--version", action="version", version=f"envy-orbit {__version__}")
    commands = root.add_subparsers(dest="command")
    commands.add_parser("gui", help="open the Orbit window (default)")

    new = commands.add_parser("init", help="create a new, empty encrypted orbit")
    new.add_argument("destination", help="a new orbit file; never overwritten")
    new.add_argument("--title", required=True, help="a name for this orbit")

    add = commands.add_parser("add", help="add a recurring track (writes a new revision)")
    add.add_argument("orbit")
    add.add_argument("destination", help="a new orbit file; never overwritten")
    add.add_argument("--title", required=True, help="what recurs")
    add.add_argument("--every", type=int, required=True, metavar="DAYS", help="cadence in days")
    add.add_argument("--grace", type=int, default=0, metavar="DAYS", help="grace days after the due date")

    lg = commands.add_parser("log", help="log a completion for a track (writes a new revision)")
    lg.add_argument("orbit")
    lg.add_argument("destination", help="a new orbit file; never overwritten")
    lg.add_argument("--track", required=True, help="the track id")
    lg.add_argument("--at", default=None, metavar="UTC", help="completion time (ISO 8601 UTC); default now")
    lg.add_argument("--note", default="", help="an optional note")

    rm = commands.add_parser("remove", help="remove a track; its logged history is kept")
    rm.add_argument("orbit")
    rm.add_argument("destination", help="a new orbit file; never overwritten")
    rm.add_argument("--track", required=True)

    sn = commands.add_parser("snooze", help="defer a track's next due date (writes a new revision)")
    sn.add_argument("orbit")
    sn.add_argument("destination", help="a new orbit file; never overwritten")
    sn.add_argument("--track", required=True)
    when = sn.add_mutually_exclusive_group(required=True)
    when.add_argument("--days", type=int, metavar="N", help="snooze for N days from now")
    when.add_argument("--until", metavar="UTC", help="snooze until an ISO 8601 UTC time")
    when.add_argument("--clear", action="store_true", help="clear any snooze on the track")

    st = commands.add_parser("status", help="show what is due (read-only; exit 1 if anything needs attention)")
    st.add_argument("orbit")
    st.add_argument("--lookahead", type=int, default=DEFAULT_LOOKAHEAD, metavar="DAYS")

    insp = commands.add_parser("inspect", help="print the decrypted orbit and journal (read-only)")
    insp.add_argument("orbit")

    exp = commands.add_parser("export", help="write a plaintext HTML or JSON status report")
    exp.add_argument("orbit")
    exp.add_argument("destination", help="a new report file; never overwritten")
    exp.add_argument("--format", default="html", choices=("html", "json"))
    exp.add_argument("--lookahead", type=int, default=DEFAULT_LOOKAHEAD, metavar="DAYS")

    ic = commands.add_parser("ical", help="write each track's next due date to an iCal (.ics) file")
    ic.add_argument("orbit")
    ic.add_argument("destination", help="a new .ics file; never overwritten")
    ic.add_argument("--lookahead", type=int, default=DEFAULT_LOOKAHEAD, metavar="DAYS")
    return root


def _passphrase(confirm: bool = False) -> bytes:
    if not sys.stdin.isatty():
        raise OrbitError("Open this action in a terminal so the passphrase is not echoed")
    value = getpass.getpass("Orbit passphrase: ").encode()
    if confirm and value != getpass.getpass("Repeat passphrase: ").encode():
        raise OrbitError("Passphrases do not match")
    return check_passphrase(value)


def _run(args) -> int:
    if args.command == "init":
        result = create(args.destination, args.title, _passphrase(confirm=True))
        print(f"orbit created → {result['output']} (revision 0)")
        return 0
    if args.command == "add":
        result = add_track(args.orbit, args.destination, _passphrase(), args.title, args.every, args.grace)
        print(f"track added {result['track_id']} → {result['output']} (revision {result['revision']})")
        return 0
    if args.command == "log":
        result = log(args.orbit, args.destination, _passphrase(), args.track, args.at, args.note)
        print(f"logged {result['logged_track']} at {result['done_utc']} → {result['output']} "
              f"(revision {result['revision']})")
        return 0
    if args.command == "remove":
        result = remove_track(args.orbit, args.destination, _passphrase(), args.track)
        print(f"removed {result['removed_track']} → {result['output']} (revision {result['revision']})")
        return 0
    if args.command == "snooze":
        result = snooze(args.orbit, args.destination, _passphrase(), args.track,
                        until=None if args.clear else args.until, days=None if args.clear else args.days)
        where = result["snoozed_until"] or "cleared"
        print(f"snoozed {result['snoozed_track']} → {where} → {result['output']} (revision {result['revision']})")
        return 0
    if args.command == "status":
        report = status(args.orbit, _passphrase(), lookahead=args.lookahead)
        counts = report["counts"]
        for row in report["tracks"]:
            due = "never logged" if row["state"] == "pending" else (
                f"{-row['days']}d overdue" if row["days"] is not None and row["days"] < 0
                else f"in {row['days']}d" if row["days"] is not None else "—")
            tag = " · snoozed" if row.get("snoozed") else ""
            print(f"  [{row['state']:>8}] {row['title']}  (every {row['cadence_days']}d, {due}{tag})")
        print(f"overdue {counts['overdue']}, due {counts['due']}, pending {counts['pending']}, "
              f"soon {counts['soon']}, on-track {counts['on-track']}")
        print("ALL CLEAR" if report["all_clear"] else f"{report['needs_attention']} track(s) need attention")
        return 0 if report["all_clear"] else 1
    if args.command == "export":
        result = export(args.orbit, args.destination, _passphrase(), args.format, lookahead=args.lookahead)
        print(f"export ({result['format']}): wrote {result['output']}  sha256 {result['sha256']}")
        return 0
    if args.command == "ical":
        result = write_ical(args.orbit, args.destination, _passphrase(), lookahead=args.lookahead)
        print(f"ical: wrote {result['events']} event(s) → {result['output']}  sha256 {result['sha256']}")
        return 0
    if args.command == "inspect":
        print(json.dumps(inspect(args.orbit, _passphrase()), indent=2, ensure_ascii=False))
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in (None, "gui"):
        from .gui import run_app
        return run_app()
    try:
        return _run(args)
    except OrbitError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
