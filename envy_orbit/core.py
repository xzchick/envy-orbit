"""Encrypted recurring-cadence tracking: keep everything in orbit.

A self-contained engine for the ENVY BLACK VEIL Orbit app, packaged as a
standalone app. An *orbit* holds a set of *tracks* — recurring obligations, each
with a cadence (every N days) and an optional grace window — sealed in the same
AES-256-GCM envelope as ENVY Vault. You *log* a completion for a track; each log
entry is appended to a SHA-256 hash-chained journal, and the track's next due
date is computed as its last completion plus its cadence. A *status* read folds
the tracks and the log into what is overdue, due, coming up soon, or on track.

Every save is a new, hash-chained revision written with ``O_EXCL`` — an earlier
revision is never overwritten.

Honest by design: Orbit records when you logged something, not that you did it.
A cadence is a reminder, not an enforcement — nothing acts for you. Times are
assertions from your machine's clock; the hash chain orders entries but does not
prove when they happened.

Standard library only, apart from ``cryptography`` for the encrypted envelope.
"""

from __future__ import annotations

import csv
import datetime
import hashlib
import html
import io
import json
import os
import stat
import uuid
from pathlib import Path

# --- Shared errors and bounds ------------------------------------------------

MAGIC = b"ENVYVAULT2\x00"
MAX_ENVELOPE = 16 * 1024 * 1024        # the encrypted orbit
MAX_TRACKS = 1000
MAX_JOURNAL = 100000
MAX_REVISIONS = 1000000
MIN_PASSPHRASE = 12
MIN_CADENCE, MAX_CADENCE = 1, 3650     # one day to ten years
MAX_GRACE = 365
MAX_NOTE = 500
DEFAULT_LOOKAHEAD = 7                   # "soon" window, in days


class OrbitError(RuntimeError):
    """A clean, expected error: a bad passphrase, an invalid orbit, a chain mismatch."""


# --- Safe file primitives (never follow symlinks, never overwrite) -----------

def read_regular(path, limit: int = MAX_ENVELOPE) -> bytes:
    path = Path(path).expanduser()
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        meta = os.fstat(stream.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_size > limit:
            raise OrbitError("Expected a regular file within the size limit")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise OrbitError("File exceeded the size limit")
    return data


def new_file(path, data: bytes, mode: int = 0o600) -> Path:
    """Write a brand-new file. O_EXCL means an existing file is never overwritten."""
    path = Path(path).expanduser()
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError:
        raise OrbitError(f"Refusing to overwrite an existing file: {path}") from None
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return path


# --- Encrypted envelope (byte-compatible with ENVY Vault) --------------------

def _key(secret: bytes, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(secret)


def encrypt_bytes(data: bytes, secret: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(16), os.urandom(12)
    return MAGIC + salt + nonce + AESGCM(_key(secret, salt)).encrypt(nonce, data, MAGIC)


def decrypt_bytes(data: bytes, secret: bytes) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not data.startswith(MAGIC) or len(data) < len(MAGIC) + 44:
        raise OrbitError("Not an ENVY encrypted envelope")
    blob = data[len(MAGIC):]
    try:
        return AESGCM(_key(secret, blob[:16])).decrypt(blob[16:28], blob[28:], MAGIC)
    except InvalidTag:
        raise OrbitError("Incorrect passphrase or modified encrypted data") from None


def check_passphrase(secret: bytes) -> bytes:
    if not isinstance(secret, (bytes, bytearray)) or len(secret) < MIN_PASSPHRASE:
        raise OrbitError(f"Use a passphrase of at least {MIN_PASSPHRASE} bytes")
    return bytes(secret)


# --- Canonical form and field validation -------------------------------------

def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _parse_utc(value, field="recorded_utc"):
    if not isinstance(value, str) or len(value) > 64:
        raise OrbitError("Invalid " + field)
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise OrbitError(field + " must be ISO 8601 with a UTC offset") from None
    if parsed.tzinfo is None or parsed.utcoffset() != datetime.timedelta(0):
        raise OrbitError(field + " must be expressed in UTC")
    return parsed


def _timestamp(value, field="recorded_utc"):
    return _parse_utc(value, field).isoformat()


def _text(value, field="text", limit=200, allow_empty=False):
    if not isinstance(value, str) or "\x00" in value or len(value) > limit:
        raise OrbitError("Invalid " + field)
    if not allow_empty and not value.strip():
        raise OrbitError("Invalid " + field)
    return value


def _uuid(value, field="identifier"):
    try:
        uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise OrbitError("Invalid " + field) from None
    return value


def _int_in(value, low, high, field):
    if type(value) is not int or not low <= value <= high:
        raise OrbitError(f"{field} must be an integer between {low} and {high}")
    return value


# --- Track and journal validation --------------------------------------------

def _validate_track(value):
    if not isinstance(value, dict) or set(value) != {"id", "title", "cadence_days", "grace_days",
                                                     "created_utc", "snoozed_until"}:
        raise OrbitError("Invalid track")
    snoozed = value["snoozed_until"]
    if snoozed is not None:
        snoozed = _timestamp(snoozed, "snoozed_until")
    return {"id": _uuid(value["id"], "track id"),
            "title": _text(value["title"], "track title", 200),
            "cadence_days": _int_in(value["cadence_days"], MIN_CADENCE, MAX_CADENCE, "Cadence"),
            "grace_days": _int_in(value["grace_days"], 0, MAX_GRACE, "Grace"),
            "created_utc": _timestamp(value["created_utc"], "created_utc"),
            "snoozed_until": snoozed}


def _validate_record(value):
    fields = {"recorded_utc", "track_id", "done_utc", "note", "previous_sha256", "record_sha256"}
    if not isinstance(value, dict) or set(value) != fields:
        raise OrbitError("Invalid journal record")
    out = {"recorded_utc": _timestamp(value["recorded_utc"]),
           "track_id": _uuid(value["track_id"], "track id"),
           "done_utc": _timestamp(value["done_utc"], "done_utc"),
           "note": _text(value["note"], "note", MAX_NOTE, allow_empty=True),
           "previous_sha256": _sha256(value["previous_sha256"], "previous_sha256"),
           "record_sha256": _sha256(value["record_sha256"], "record_sha256")}
    return out


def _sha256(value, field="sha256"):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise OrbitError(field + " must be 64 lowercase hexadecimal characters")
    return value


def _record_digest(record):
    core = {k: v for k, v in record.items() if k != "record_sha256"}
    return hashlib.sha256(_canonical(core)).hexdigest()


def _validate_orbit(data):
    fields = {"schema", "orbit_id", "title", "created_utc", "revision",
              "previous_envelope_sha256", "tracks", "journal"}
    if not isinstance(data, dict) or set(data) != fields or type(data["schema"]) is not int or data["schema"] != 1:
        raise OrbitError("Unsupported encrypted orbit schema")
    _text(data["title"], "orbit title", 200)
    _uuid(data["orbit_id"], "orbit identifier")
    _timestamp(data["created_utc"], "created_utc")

    tracks = data["tracks"]
    if not isinstance(tracks, list) or len(tracks) > MAX_TRACKS:
        raise OrbitError("Invalid track list")
    seen, previous_id = set(), None
    for track in tracks:
        norm = _validate_track(track)
        if norm != track:
            raise OrbitError("Track is not normalized")
        if track["id"] in seen:
            raise OrbitError("Duplicate track id")
        if previous_id is not None and track["id"] < previous_id:
            raise OrbitError("Tracks must be sorted by id")
        seen.add(track["id"]); previous_id = track["id"]

    journal = data["journal"]
    if not isinstance(journal, list) or len(journal) > MAX_JOURNAL:
        raise OrbitError("Invalid journal")
    previous = "0" * 64
    for record in journal:
        if _validate_record(record) != record:
            raise OrbitError("Journal record is not normalized")
        if record["previous_sha256"] != previous or _record_digest(record) != record["record_sha256"]:
            raise OrbitError("Orbit journal hash chain mismatch")
        previous = record["record_sha256"]

    revision = data["revision"]
    if type(revision) is not int or not 0 <= revision <= MAX_REVISIONS:
        raise OrbitError("Invalid revision")
    _sha256(data["previous_envelope_sha256"], "previous_envelope_sha256")
    return data


# --- Load / write ------------------------------------------------------------

def _migrate(data):
    """Bring an older orbit forward in memory. Tracks gained `snoozed_until` in v0.2;
    tracks that predate it get None. The journal is unaffected."""
    if isinstance(data, dict) and isinstance(data.get("tracks"), list):
        for track in data["tracks"]:
            if isinstance(track, dict) and "snoozed_until" not in track:
                track["snoozed_until"] = None
    return data


def _load(source, secret):
    encrypted = read_regular(source, MAX_ENVELOPE)
    try:
        data = json.loads(decrypt_bytes(encrypted, secret))
    except (json.JSONDecodeError, UnicodeError, RecursionError):
        raise OrbitError("Encrypted orbit does not contain valid JSON") from None
    return _validate_orbit(_migrate(data)), hashlib.sha256(encrypted).hexdigest()


def _write(destination, data, secret):
    check_passphrase(secret)
    encoded = _canonical(_validate_orbit(data))
    if len(encoded) + 128 > MAX_ENVELOPE:
        raise OrbitError("Orbit exceeds the encrypted size limit")
    new_file(destination, encrypt_bytes(encoded, secret))
    return {"output": str(destination), "orbit_id": data["orbit_id"], "revision": data["revision"],
            "tracks": len(data["tracks"]), "encrypted": True, "previous_revision_preserved": True}


def _save_revision(data, previous_envelope, destination, secret):
    data["revision"] += 1
    data["previous_envelope_sha256"] = previous_envelope
    return _write(destination, data, secret)


def create(destination, title, secret):
    data = {"schema": 1, "orbit_id": str(uuid.uuid4()), "title": _text(title, "orbit title", 200),
            "created_utc": _utc(), "revision": 0, "previous_envelope_sha256": "0" * 64,
            "tracks": [], "journal": []}
    return _write(destination, data, secret)


# --- Mutations (each writes a new, preserved revision) -----------------------

def add_track(source, destination, secret, title, cadence_days, grace_days=0):
    data, envelope = _load(source, secret)
    if len(data["tracks"]) >= MAX_TRACKS:
        raise OrbitError(f"An orbit holds at most {MAX_TRACKS} tracks")
    track = _validate_track({"id": str(uuid.uuid4()), "title": title,
                             "cadence_days": cadence_days, "grace_days": grace_days,
                             "created_utc": _utc(), "snoozed_until": None})
    data["tracks"] = sorted(data["tracks"] + [track], key=lambda t: t["id"])
    result = _save_revision(data, envelope, destination, secret)
    return dict(result, track_id=track["id"])


def remove_track(source, destination, secret, track_id):
    data, envelope = _load(source, secret)
    track_id = _uuid(track_id, "track id")
    kept = [t for t in data["tracks"] if t["id"] != track_id]
    if len(kept) == len(data["tracks"]):
        raise OrbitError("No such track in this orbit")
    data["tracks"] = kept   # the journal is append-only; past completions are preserved
    result = _save_revision(data, envelope, destination, secret)
    return dict(result, removed_track=track_id)


def snooze(source, destination, secret, track_id, until=None, days=None):
    """Defer a track's next due date, or clear a snooze (both `until` and `days` None).

    A snooze never pulls a due date earlier; it only pushes it out, and it expires
    on its own once the date passes. Writes a new, preserved revision.
    """
    data, envelope = _load(source, secret)
    track_id = _uuid(track_id, "track id")
    track = next((t for t in data["tracks"] if t["id"] == track_id), None)
    if track is None:
        raise OrbitError("No such track in this orbit")
    if until is None and days is None:
        track["snoozed_until"] = None
    elif days is not None:
        _int_in(days, 1, MAX_CADENCE, "Snooze days")
        when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)
        track["snoozed_until"] = when.isoformat()
    else:
        track["snoozed_until"] = _timestamp(until, "snoozed_until")
    _validate_track(track)
    result = _save_revision(data, envelope, destination, secret)
    return dict(result, snoozed_track=track_id, snoozed_until=track["snoozed_until"])


def log(source, destination, secret, track_id, done_utc=None, note=""):
    data, envelope = _load(source, secret)
    track_id = _uuid(track_id, "track id")
    if not any(t["id"] == track_id for t in data["tracks"]):
        raise OrbitError("No such track in this orbit")
    if len(data["journal"]) >= MAX_JOURNAL:
        raise OrbitError("Journal is full")
    record = {"recorded_utc": _utc(), "track_id": track_id,
              "done_utc": _timestamp(done_utc) if done_utc else _utc(),
              "note": _text(note, "note", MAX_NOTE, allow_empty=True),
              "previous_sha256": data["journal"][-1]["record_sha256"] if data["journal"] else "0" * 64}
    record["record_sha256"] = _record_digest(record)
    data["journal"].append(_validate_record(record))
    result = _save_revision(data, envelope, destination, secret)
    return dict(result, logged_track=track_id, done_utc=record["done_utc"])


# --- Status: fold tracks and the log into what is due ------------------------

def _last_done(journal, track_id):
    done = [r["done_utc"] for r in journal if r["track_id"] == track_id]
    return max(done, key=_parse_utc) if done else None


def _classify(track, last_done, now, lookahead):
    cadence = datetime.timedelta(days=track["cadence_days"])
    grace = datetime.timedelta(days=track["grace_days"])
    soon = datetime.timedelta(days=lookahead)
    if last_done is None:
        return {"state": "pending", "next_due_utc": None, "days": None, "snoozed": False}
    next_due = _parse_utc(last_done) + cadence
    # A live snooze defers the next due date forward (never earlier).
    snoozed = False
    if track.get("snoozed_until"):
        until = _parse_utc(track["snoozed_until"])
        if until > now and until > next_due:
            next_due, snoozed = until, True
    # Whole days until due (truncated toward zero so a track 1 day overdue reads
    # "1d overdue", not "2d" from timedelta.days flooring). Negative once past due.
    days = int((next_due - now).total_seconds() // 86400) if next_due >= now \
        else -int((now - next_due).total_seconds() // 86400)
    if now >= next_due + grace:
        state = "overdue"
    elif now >= next_due:
        state = "due"
    elif now >= next_due - soon:
        state = "soon"
    else:
        state = "on-track"
    return {"state": state, "next_due_utc": next_due.isoformat(), "days": days, "snoozed": snoozed}


# The order a status board should present tracks: the most urgent first.
_ORDER = {"overdue": 0, "due": 1, "pending": 2, "soon": 3, "on-track": 4}


def status(source, secret, now=None, lookahead=DEFAULT_LOOKAHEAD):
    """Fold tracks and the completion log into per-track state. Read-only."""
    data, _ = _load(source, secret)
    moment = _parse_utc(now) if now else datetime.datetime.now(datetime.timezone.utc)
    _int_in(lookahead, 0, MAX_CADENCE, "Lookahead")
    rows, counts = [], {k: 0 for k in _ORDER}
    for track in data["tracks"]:
        last = _last_done(data["journal"], track["id"])
        verdict = _classify(track, last, moment, lookahead)
        completions = sum(1 for r in data["journal"] if r["track_id"] == track["id"])
        counts[verdict["state"]] += 1
        rows.append({"id": track["id"], "title": track["title"], "cadence_days": track["cadence_days"],
                     "grace_days": track["grace_days"], "last_done_utc": last, "completions": completions,
                     "snoozed_until": track["snoozed_until"], **verdict})
    rows.sort(key=lambda r: (_ORDER[r["state"]], r["days"] if r["days"] is not None else -(10 ** 9), r["title"]))
    attention = counts["overdue"] + counts["due"] + counts["pending"]
    return {"orbit_id": data["orbit_id"], "title": data["title"], "revision": data["revision"],
            "evaluated_utc": moment.isoformat(), "lookahead_days": lookahead,
            "tracks": rows, "counts": counts, "needs_attention": attention, "all_clear": attention == 0,
            "basis": "Orbit records when you logged a completion, not that you did it. A cadence is a "
                     "reminder, not an enforcement, and times are assertions from your clock."}


def inspect(source, secret):
    data, envelope = _load(source, secret)
    return {"orbit_id": data["orbit_id"], "title": data["title"], "revision": data["revision"],
            "created_utc": data["created_utc"], "envelope_sha256": envelope,
            "tracks": data["tracks"],
            "journal": [{"recorded_utc": r["recorded_utc"], "track_id": r["track_id"],
                         "done_utc": r["done_utc"], "note": r["note"]} for r in data["journal"]],
            "warning": "The journal is append-only and hash-chained, so entries cannot be reordered or edited "
                       "without breaking the chain — but the chain orders entries, it does not prove when they "
                       "happened, and a logged completion is a self-report."}


def history(source, secret, track_id=None):
    """The completion log, newest first, with each entry's track title. With
    ``track_id``, only that track's completions. Read-only."""
    data, _ = _load(source, secret)
    titles = {t["id"]: t["title"] for t in data["tracks"]}
    if track_id is not None:
        track_id = _uuid(track_id, "track id")
        if track_id not in titles and not any(r["track_id"] == track_id for r in data["journal"]):
            raise OrbitError("No such track in this orbit")
    rows = []
    for record in data["journal"]:
        if track_id is not None and record["track_id"] != track_id:
            continue
        rows.append({"recorded_utc": record["recorded_utc"], "track_id": record["track_id"],
                     "track": titles.get(record["track_id"], "(removed track)"),
                     "done_utc": record["done_utc"], "note": record["note"]})
    rows.reverse()                                    # newest first
    return {"orbit_id": data["orbit_id"], "title": data["title"], "count": len(rows), "completions": rows,
            "basis": "Orbit records when you logged a completion, not that you did it. A logged completion is "
                     "a self-report, and the chain orders entries, it does not prove when they happened."}


# --- Plaintext export --------------------------------------------------------

def _safe_csv(value):
    """Guard a CSV field against spreadsheet formula injection."""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def export(source, destination, secret, format="html", now=None, lookahead=DEFAULT_LOOKAHEAD):
    if format not in ("html", "json", "csv"):
        raise OrbitError("Export format must be html, json or csv")
    data, envelope = _load(source, secret)
    report = status(source, secret, now, lookahead)
    out = {"orbit": {"orbit_id": data["orbit_id"], "title": data["title"], "revision": data["revision"],
                     "created_utc": data["created_utc"]},
           "evaluated_utc": report["evaluated_utc"], "lookahead_days": report["lookahead_days"],
           "all_clear": report["all_clear"], "needs_attention": report["needs_attention"],
           "counts": report["counts"], "tracks": report["tracks"], "envelope_sha256": envelope,
           "limitations": "Plaintext export. Orbit records when a completion was logged, not that it was "
                          "done; a cadence is a reminder, not an enforcement, and times are assertions from "
                          "the operator's clock."}
    if format == "json":
        output = (json.dumps(out, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    elif format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["state", "track", "cadence_days", "grace_days", "last_done_utc",
                         "next_due_utc", "days", "completions", "snoozed_until"])
        for row in report["tracks"]:
            writer.writerow([row["state"], _safe_csv(row["title"]), row["cadence_days"], row["grace_days"],
                             row["last_done_utc"] or "", row["next_due_utc"] or "",
                             "" if row["days"] is None else row["days"], row["completions"],
                             row["snoozed_until"] or ""])
        output = buf.getvalue().encode("utf-8")
    else:
        def esc(v):
            return html.escape(str(v), quote=True)

        def cell_days(row):
            if row["state"] == "pending":
                return "never logged"
            days = row["days"]
            if days is None:
                return "—"
            if days < 0:
                return esc(-days) + " day(s) overdue"
            if days == 0:
                return "due today"
            return "in " + esc(days) + " day(s)"

        body = "".join(
            "<tr><td>" + esc(r["state"]) + "</td><td>" + esc(r["title"]) + "</td><td>every " +
            esc(r["cadence_days"]) + " day(s)</td><td>" + esc(r["last_done_utc"] or "—") + "</td><td>" +
            esc(r["next_due_utc"] or "—") + "</td><td>" + cell_days(r) + "</td></tr>"
            for r in report["tracks"])
        verdict = "ALL CLEAR — nothing needs attention" if report["all_clear"] else \
            f"{report['needs_attention']} track(s) need attention"
        # No external content, executable script, clickable operator-supplied URLs, or active HTML.
        output = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                  "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
                  "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                  "<title>" + esc(data["title"]) + "</title><style>body{max-width:72rem;margin:2rem auto;padding:0 1rem;font:16px system-ui;line-height:1.5}"
                  "table{border-collapse:collapse;width:100%;margin:0 0 2rem}th,td{border:1px solid #777;padding:.4rem .6rem;text-align:left;vertical-align:top}"
                  "th{background:#eee}</style></head><body><h1>" + esc(data["title"]) +
                  "</h1><p>" + esc(out["limitations"]) + "</p><p>Orbit: " + esc(data["orbit_id"]) +
                  " · Revision: " + str(data["revision"]) +
                  "</p><p>Evaluated UTC: " + esc(report["evaluated_utc"]) + " · Encrypted orbit SHA-256: " + envelope +
                  "</p><h2>" + verdict + "</h2>"
                  "<table><thead><tr><th>Status</th><th>Track</th><th>Cadence</th><th>Last done</th>"
                  "<th>Next due</th><th>Due in</th></tr></thead><tbody>" + body + "</tbody></table>"
                  "</body></html>").encode("utf-8")
    new_file(destination, output)
    return {"output": str(destination), "format": format, "plaintext": True,
            "sha256": hashlib.sha256(output).hexdigest(),
            "all_clear": report["all_clear"], "needs_attention": report["needs_attention"]}


# --- iCal (.ics) export ------------------------------------------------------
# Each track's computed next-due date becomes a single timed VEVENT, so a
# calendar can remind you. Pending (never-logged) tracks have no due date and
# are skipped. The file is plaintext, like the HTML/JSON export.

def _ics_escape(text):
    return str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_dt(iso):
    return _parse_utc(iso).astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ical(source, secret, now=None, lookahead=DEFAULT_LOOKAHEAD):
    """Render each track's next due date as an iCal VEVENT. Read-only."""
    report = status(source, secret, now, lookahead)
    stamp = _ics_dt(report["evaluated_utc"])
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ENVY//Orbit//EN", "CALSCALE:GREGORIAN"]
    for r in report["tracks"]:
        if not r["next_due_utc"]:
            continue
        lines.extend([
            "BEGIN:VEVENT", f"UID:{r['id']}@envy-orbit", f"DTSTAMP:{stamp}",
            f"DTSTART:{_ics_dt(r['next_due_utc'])}",
            f"SUMMARY:{_ics_escape(r['title'])} (every {r['cadence_days']}d)",
            f"DESCRIPTION:{_ics_escape(r['state'] + ' · last done ' + (r['last_done_utc'] or 'never'))}",
            "END:VEVENT"])
    lines.append("END:VCALENDAR")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def write_ical(source, destination, secret, now=None, lookahead=DEFAULT_LOOKAHEAD):
    output = ical(source, secret, now, lookahead)
    new_file(destination, output)
    return {"output": str(destination), "events": output.count(b"BEGIN:VEVENT"),
            "sha256": hashlib.sha256(output).hexdigest()}
