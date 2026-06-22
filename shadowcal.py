#!/usr/bin/env python3
"""
shadowcal — mirror "busy" shadows from your private ICS calendars into another
calendar via Apple Calendar (EventKit) on your Mac.

Idea:
  Source  = one or more calendars exposed as ICS links (webcal:// or https://),
            or a calendar you ALREADY subscribe to in Apple Calendar.
  Target  = a writable calendar in Apple Calendar (e.g. a work or shared account).
  Effect  = generic "busy" blocks in the target calendar, so others see you as
            busy at those times without seeing what the appointments are about.

Everything happens locally as YOU through the native client — only blocks are
written into the target calendar, and no data from the target account leaves
your Mac.

Usage:
  shadowcal                 # open the TUI (requires textual)
  shadowcal tui             # the same
  shadowcal sync [--once]   # run all enabled syncs once (called by launchd)
  shadowcal sync <id>       # run one specific sync
  shadowcal list            # show configured syncs
  shadowcal calendars       # show writable destination calendars
  shadowcal add ...         # add a sync (see --help)
  shadowcal remove <id>     # remove a sync (and its blocks)
  shadowcal test <id|url>   # fetch an ICS and show how many events it yields
  shadowcal install-agent   # install the launchd job (runs every 15 min.)
  shadowcal uninstall-agent # remove the launchd job

Dependencies: icalendar, recurring-ical-events, pyobjc-framework-EventKit,
pyobjc-framework-Cocoa  (+ textual for the TUI). See requirements.txt.
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

# ---------------------------------------------------------------------------
# Constants and path setup
# ---------------------------------------------------------------------------

APP = "shadowcal"
__version__ = "1.1"
CONFIG_DIR = Path(os.path.expanduser(f"~/.config/{APP}"))
CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = CONFIG_DIR / "state.json"
LOG_OUT = CONFIG_DIR / "shadowcal.out.log"
LOG_ERR = CONFIG_DIR / "shadowcal.err.log"

# Marker that makes us only touch OUR own generated events.
MARKER = "shadowcal-sync"


def _resolve_local_zone():
    """
    The local time zone as a DST-AWARE zone (zoneinfo), not a fixed offset.

    Important: dt.datetime.now().astimezone().tzinfo gives a FIXED offset matching
    the offset right now. Using that to interpret a floating winter time during
    summer makes it an hour wrong. We therefore resolve the real IANA zone.
    """
    name = os.environ.get("TZ")
    if not name:
        try:                                   # macOS/Linux: /etc/localtime -> .../zoneinfo/Area/City
            link = os.readlink("/etc/localtime")
            if "zoneinfo/" in link:
                name = link.split("zoneinfo/", 1)[1]
        except OSError:
            pass
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:                      # unknown name -> fall back
            pass
    # Last resort: fixed offset (loses DST for floating times, but does not crash).
    return dt.datetime.now().astimezone().tzinfo


def _zone_from_name(name: str | None):
    """IANA name -> zoneinfo, or None if empty/unknown."""
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None


def _tzname_of(d: dt.datetime) -> str | None:
    """IANA name for an aware datetime's zone (to set on the EKEvent), else None."""
    tz = d.tzinfo
    if tz is None:
        return None
    key = getattr(tz, "key", None)             # zoneinfo.ZoneInfo
    if key:
        return key
    if str(tz) in ("UTC", "UTC+00:00") or d.utcoffset() == dt.timedelta(0):
        return "UTC"
    return None                                # pure fixed offset -> let EK be floating


LOCAL_TZ = _resolve_local_zone()

DEFAULT_TITLE = "Private – busy"
DEFAULT_BACK_DAYS = 7
DEFAULT_FORWARD_DAYS = 365

# Show-as options (how the block counts in other people's free/busy lookups).
SHOW_AS = ("busy", "tentative", "oof", "free")
DEFAULT_SHOW_AS = "busy"

# Safety net against an empty/half source feed wiping out all your blocks.
DEFAULT_SAFETY = {
    "block_empty_source": True,    # never delete everything if the source is suddenly empty
    "max_delete_fraction": 0.5,    # suspicious if > 50% of blocks would be deleted at once
    "min_owned_for_guard": 4,      # the fraction guard only applies from this block count
}

# 'status' warns if the most recent SUCCESSFUL run is older than this.
STALE_WARN_HOURS = 3

# Shown in the UI.
CREATOR = "Michael S. Dahl"
CREDIT = "App vibe-coded by Michael S. Dahl with Claude Opus 4.8."
DESCRIPTION = ("Mirrors your private ICS calendars into another calendar as generic "
               "\"busy\" blocks, so others cannot book on top of your appointments — "
               "without being able to see what they are about. Everything happens "
               "locally via Apple Calendar; no data leaves your Mac.")
LOGO = ("    _            _                    _ \n"
        " __| |_  __ _ __| |_____ __ ____ __ _| |\n"
        "(_-< ' \\/ _` / _` / _ \\ V  V / _/ _` | |\n"
        "/__/_||_\\__,_\\__,_\\___/\\_/\\_/\\__\\__,_|_|")


# ---------------------------------------------------------------------------
# Marker logic (shared + unit-testable) — decides what we "own" and prevents
# us from ever making two copies of the same source event.
# ---------------------------------------------------------------------------

def make_notes(sync_id: str, key: str) -> str:
    """The note stamped onto every generated block."""
    return (f"Auto-generated by {APP}. Do not edit/delete manually.\n"
            f"{MARKER}: {sync_id}\n"
            f"key: {key}")


def owned_for_sync(notes, sync_id: str) -> tuple[bool, str | None]:
    """
    Decide whether a block belongs to exactly this sync, and extract its key.

    Returns (belongs, key):
      - belongs=False           : not ours (or not this sync).
      - belongs=True, key=str   : ours, with a valid key.
      - belongs=True, key=None  : ours, but the key is missing/corrupt (stray).

    The match on the sync line is EXACT (the whole line), not a substring —
    otherwise 'sync1' would wrongly match 'sync10' and delete/recreate its blocks.
    """
    if not notes:
        return (False, None)
    tag = f"{MARKER}: {sync_id}"
    belongs = False
    key = None
    for raw in str(notes).splitlines():
        line = raw.strip()
        if line == tag:
            belongs = True
        elif line.startswith("key:"):
            key = line[len("key:"):].strip() or None
    return (belongs, key if belongs else None)


def notes_have_marker(notes) -> bool:
    """True if the note carries ANY shadowcal-sync marker (i.e. it is one of our
    generated blocks, for any sync). Used to avoid mirroring our own blocks back
    in when a source calendar contains shadowcal output."""
    if not notes:
        return False
    tag = f"{MARKER}:"
    return any(raw.strip().startswith(tag) for raw in str(notes).splitlines())


# ---------------------------------------------------------------------------
# Configuration and state
# ---------------------------------------------------------------------------

def _ensure_dir() -> None:
    # 0700: config.json holds secret ICS links (calendar credentials), so neither
    # the directory nor the files may be readable by other users on the machine.
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)            # mkdir mode is ignored if the dir existed
    except OSError:
        pass


def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    """Write via temp file + os.replace so an interrupted/concurrent write never
    leaves a half (corrupt) config/state. Sets restrictive permissions."""
    _ensure_dir()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)                  # atomic on the same filesystem
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _backup_corrupt(path: Path) -> None:
    """Move an unreadable JSON file aside to *.bad instead of silently losing it,
    so a hand-edit mishap does not look like 'everything disappeared'."""
    try:
        bad = path.with_name(path.name + ".bad")
        os.replace(path, bad)
        print(f"WARNING: {path} could not be read as JSON — moved to {bad}. "
              "Continuing with an empty configuration.", file=sys.stderr)
    except OSError:
        pass


def _harden_perms(path: Path) -> None:
    """Lock down an existing file to 0600 (and its dir to 0700) on read, so a
    pre-existing world-readable config/state created before hardening, or by an
    external editor, gets tightened without waiting for the next write."""
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {"syncs": []}
    _harden_perms(CONFIG_FILE)
    try:
        return json.loads(CONFIG_FILE.read_text("utf-8"))
    except Exception:
        _backup_corrupt(CONFIG_FILE)
        return {"syncs": []}


def save_config(cfg: dict) -> None:
    _atomic_write(CONFIG_FILE, json.dumps(cfg, indent=2, ensure_ascii=False))


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    _harden_perms(STATE_FILE)
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except Exception:
        _backup_corrupt(STATE_FILE)
        return {}


def save_state(state: dict) -> None:
    _atomic_write(STATE_FILE, json.dumps(state, indent=2, ensure_ascii=False))


class SyncLockBusy(Exception):
    """Raised when another shadowcal sync already holds the lock."""


@contextmanager
def _sync_lock():
    """Exclusive lock so the launchd agent, 'Sync now' and a manual 'sync' cannot
    run at the same time and create duplicate blocks (a race in reconciliation)."""
    import fcntl
    _ensure_dir()
    fd = os.open(str(CONFIG_DIR / "sync.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SyncLockBusy()
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def new_sync_id(cfg: dict) -> str:
    used = {s["id"] for s in cfg.get("syncs", [])}
    n = 1
    while f"sync{n}" in used:
        n += 1
    return f"sync{n}"


def normalize_sync(s: dict) -> dict:
    """Fill in missing fields with sensible defaults."""
    s.setdefault("title", DEFAULT_TITLE)
    s.setdefault("back_days", DEFAULT_BACK_DAYS)
    s.setdefault("forward_days", DEFAULT_FORWARD_DAYS)
    s.setdefault("skip_all_day", True)
    s.setdefault("skip_transparent", True)
    s.setdefault("enabled", True)
    # Options:
    s.setdefault("url", "")              # ICS link source (if no source_cal)
    s.setdefault("source_cal", None)     # use an already-subscribed calendar as source
    s.setdefault("pad_before", 0)        # minutes of buffer before each block
    s.setdefault("pad_after", 0)         # minutes of buffer after each block
    s.setdefault("show_as", DEFAULT_SHOW_AS)
    s.setdefault("include", [])          # only titles matching one of these words
    s.setdefault("exclude", [])          # skip titles matching one of these
    s.setdefault("min_minutes", 0)       # skip events shorter than this
    s.setdefault("copy_title", False)    # show the source title instead of the generic one
    s.setdefault("copy_location", False) # copy location / meeting link (Teams, Zoom)
    s.setdefault("allow_insecure_http", False)  # permit a plain http:// source link
    s.setdefault("assume_tz", None)      # assumed zone for FLOATING times (IANA name)
    if s.get("show_as") not in SHOW_AS:
        s["show_as"] = DEFAULT_SHOW_AS
    for key in ("back_days", "forward_days", "pad_before", "pad_after", "min_minutes"):
        try:
            value = int(s.get(key, 0))
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be an integer")
        if value < 0:
            raise ValueError(f"{key} must not be negative")
        s[key] = value
    if s.get("assume_tz") and _zone_from_name(s["assume_tz"]) is None:
        raise ValueError(f"Unknown time zone: {s['assume_tz']!r}")
    return s


def _safe_normalize(s: dict) -> tuple[dict | None, str | None]:
    """normalize_sync that never raises — returns (normalized, None) or
    (None, error). Lets display/run paths skip one bad sync instead of failing
    on the whole list."""
    try:
        return normalize_sync(dict(s)), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def get_safety(cfg: dict) -> dict:
    s = dict(DEFAULT_SAFETY)
    s.update(cfg.get("safety", {}) or {})
    return s


def evaluate_delete_guard(n_src: int, n_owned: int, n_delete: int,
                          safety: dict) -> tuple[bool, str | None]:
    """
    Decide whether a proposed amount of deletions looks like an anomaly (e.g. the
    source returned empty due to a server error). Pure function -> unit-testable.

    Returns (trip, reason). trip=True means: SKIP the deletions.
    """
    if safety.get("block_empty_source", True) and n_src == 0 and n_owned > 0:
        return True, f"source was empty, but {n_owned} block(s) exist — deletions skipped"
    frac_limit = float(safety.get("max_delete_fraction", 0.5))
    min_owned = int(safety.get("min_owned_for_guard", 4))
    if 0 < frac_limit < 1 and n_owned >= min_owned and n_delete / n_owned > frac_limit:
        pct = int(round(n_delete / n_owned * 100))
        return True, (f"{n_delete}/{n_owned} blocks ({pct}%) would be deleted "
                      f"— over the limit, deletions skipped (use --force)")
    return False, None


def notify(title: str, message: str) -> None:
    """macOS notification; fails silently on other platforms."""
    try:
        # Pass text as argv to 'on run {t, m}' instead of interpolating into the
        # script — so quotes/backslashes/newlines in error messages cannot break
        # out and inject AppleScript.
        subprocess.run(
            ["osascript", "-e",
             "on run {t, m}\ndisplay notification m with title t\nend run",
             "--", str(title), str(message)],
            stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def calendar_permission_status() -> tuple[str, str]:
    """
    Look up the EventKit permission WITHOUT asking for it (class method).
    Returns (key, label). key in {ok, denied, restricted, writeonly,
    notdetermined, unknown}.
    """
    try:
        import EventKit as EK
        status = EK.EKEventStore.authorizationStatusForEntityType_(
            EK.EKEntityTypeEvent)
    except Exception:
        return ("unknown", "cannot be checked here")
    # 0 NotDetermined, 1 Restricted, 2 Denied, 3 Authorized/FullAccess, 4 WriteOnly
    if status == 3:
        return ("ok", "full access")
    if status == 4:
        return ("writeonly", "write-only — cannot reconcile (grant full access)")
    if status == 2:
        return ("denied", "denied — enable in System Settings → Privacy → Calendars")
    if status == 1:
        return ("restricted", "restricted by system policy")
    return ("notdetermined", "not decided yet — run a sync to grant access")


def launchd_loaded(label: str):
    """True/False whether a launchd job is loaded; None if it cannot be checked."""
    try:
        r = subprocess.run(["launchctl", "list", label],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5)
        return r.returncode == 0
    except Exception:
        return None


# ---------------------------------------------------------------------------
# ICS: fetching and expanding events (incl. recurrence)
# ---------------------------------------------------------------------------

MAX_ICS_BYTES = 25 * 1024 * 1024     # hard upper bound on an ICS response (anti-OOM)
MAX_EXPANDED_EVENTS = 5000           # upper bound per sync/window (anti calendar-storm)
MAX_SOURCE_COMPONENTS = 2000         # raw VEVENT components before RRULE expansion

# Hosts that indicate an online-meeting join link worth keeping.
_MEETING_HOSTS = ("teams.microsoft.com", "teams.live.com", "zoom.us", "zoom.com",
                  "meet.google.com", "webex.com", "gotomeeting.com", "whereby.com")
_URL_RE = re.compile(r'https?://[^\s<>"\\]+', re.I)


def _is_meeting_host(url: str) -> bool:
    """True if the URL's HOSTNAME is (a subdomain of) a known meeting host.

    Hostname-based, not substring — so 'https://evil.com/?x=zoom.us' is rejected.
    """
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in _MEETING_HOSTS)


def _meeting_link(*texts) -> str | None:
    """Find the first online-meeting link (Teams/Zoom/Meet/…) in the given texts."""
    for t in texts:
        if not t:
            continue
        for m in _URL_RE.findall(str(t)):
            cleaned = m.rstrip('.,;)')
            if _is_meeting_host(cleaned):
                return cleaned
    return None


class _NoHTTPDowngradeRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect that downgrades https → http (unless allow_http), so a
    token-bearing https link can't be bounced to clear-text http mid-request."""

    allow_http = False

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        scheme = newurl.split("://", 1)[0].lower() if "://" in newurl else ""
        if scheme == "http" and not self.allow_http:
            raise urllib.error.HTTPError(
                req.full_url, code,
                "refusing redirect to insecure http:// (token would leak)",
                headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_ics(url: str, timeout: int = 30, allow_http: bool = False) -> bytes:
    u = url.strip()
    if u.lower().startswith("webcal://"):
        u = "https://" + u[len("webcal://"):]
    scheme = u.split("://", 1)[0].lower() if "://" in u else ""
    if scheme not in ("http", "https"):
        # Only allow network links. Otherwise e.g. file:///etc/passwd could be
        # read by urllib via a (mistyped) link.
        raise ValueError(
            f"Only http/https/webcal links are supported (got: {scheme or u[:30]!r}).")
    if scheme == "http" and not allow_http:
        # ICS links often carry a secret token; plain HTTP would send it (and the
        # calendar contents) in clear text. Require explicit opt-in per sync.
        raise ValueError(
            "Refusing an insecure http:// link — the secret token would travel in "
            "clear text. Use an https/webcal link, or set allow_insecure_http for "
            "this sync (CLI: --allow-insecure-http) if you really must.")
    # Build an opener that also blocks https→http downgrades during redirects.
    guard = _NoHTTPDowngradeRedirect()
    guard.allow_http = allow_http
    opener = urllib.request.build_opener(guard)
    req = urllib.request.Request(u, headers={"User-Agent": "shadowcal/1.0"})
    with opener.open(req, timeout=timeout) as resp:
        data = resp.read(MAX_ICS_BYTES + 1)    # read one byte over the limit to detect overflow
    if len(data) > MAX_ICS_BYTES:
        raise ValueError(
            f"The ICS source is over {MAX_ICS_BYTES // (1024 * 1024)} MB — rejected "
            "(protects against a broken/malicious source eating memory).")
    return data


def _mask_url(url: str) -> str:
    """Hide the secret token parts of ICS links for status/logs/list.

    Shows only scheme://host/… — never any path or query component, since some
    providers (e.g. Proton) put the secret in the PATH, not just the query string.
    """
    try:
        u = url.strip()
        shown_scheme = "webcal" if u.lower().startswith("webcal://") else None
        if shown_scheme:
            u = "https://" + u[len("webcal://"):]
        p = urllib.parse.urlsplit(u)
        if not p.scheme or not p.netloc:
            return "<hidden>"
        scheme = shown_scheme or p.scheme
        has_path = bool(p.path and p.path != "/") or bool(p.query)
        return f"{scheme}://{p.netloc}/…" if has_path else f"{scheme}://{p.netloc}"
    except Exception:
        return "<hidden>"


def _secret_forms(secret: str) -> list[str]:
    """All on-the-wire forms a secret URL can appear as in an error string.

    fetch_ics rewrites webcal:// to https:// before handing it to urllib, so an
    error may contain the https form even though the config stored webcal (or
    vice-versa). Redact every variant. Longest first so the fuller form is
    replaced before a prefix of it."""
    s = secret.strip()
    forms = {s}
    low = s.lower()
    if low.startswith("webcal://"):
        forms.add("https://" + s[len("webcal://"):])
    elif low.startswith("https://"):
        forms.add("webcal://" + s[len("https://"):])
    return sorted((f for f in forms if f), key=len, reverse=True)


def _safe_error(exc: Exception, *secrets: str | None) -> str:
    """Error text for state/log/notification without known secret URLs (in any of
    the forms a URL may have been rewritten into before reaching urllib)."""
    text = str(exc)
    for secret in secrets:
        if not secret:
            continue
        masked = _mask_url(secret)
        for form in _secret_forms(secret):
            text = text.replace(form, masked)
    return text


def _parse_sync_summary(text: str) -> dict:
    """Pull source/created/updated/deleted counts out of a run summary line like
    'source=28 new=0 updated=0 deleted=0'. Returns {} unless all four are found."""
    out = {}
    for key, pat in (("source", "source="), ("created", "new="),
                     ("updated", "updated="), ("deleted", "deleted=")):
        m = re.search(pat + r"(\d+)", text)
        if m:
            out[key] = int(m.group(1))
    return out if len(out) == 4 else {}


def _rrule_first(rrule, key: str, default=None):
    if not rrule:
        return default
    val = rrule.get(key)
    if val is None:
        return default
    if isinstance(val, (list, tuple)):
        return val[0] if val else default
    return val


def _rrule_int(rrule, key: str, default: int | None = None) -> int | None:
    val = _rrule_first(rrule, key, default)
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _preflight_calendar_size(cal, win_start: dt.datetime, win_end: dt.datetime) -> None:
    """Reject feeds that are very likely to explode during RRULE expansion."""
    seconds = max(1.0, (win_end - win_start).total_seconds())
    freq_seconds = {
        "SECONDLY": 1,
        "MINUTELY": 60,
        "HOURLY": 3600,
        "DAILY": 86400,
        "WEEKLY": 7 * 86400,
        "MONTHLY": 31 * 86400,
        "YEARLY": 366 * 86400,
    }
    components = 0
    estimated = 0
    for comp in cal.walk("VEVENT"):
        components += 1
        if components > MAX_SOURCE_COMPONENTS:
            raise ValueError(
                f"The ICS source has over {MAX_SOURCE_COMPONENTS} events — rejected.")
        # Explicit RDATE occurrences count, but only those that actually fall in
        # the sync window — so a feed with thousands of old, out-of-window RDATEs
        # isn't rejected when only a few matter now.
        rdate = comp.get("RDATE")
        if rdate is not None:
            for rd in (rdate if isinstance(rdate, (list, tuple)) else [rdate]):
                for v in (getattr(rd, "dts", None) or [rd]):
                    try:
                        val = getattr(v, "dt", v)
                        if isinstance(val, tuple):      # VALUE=PERIOD -> (start, end)
                            val = val[0]
                        if win_start <= _to_aware(val) <= win_end:
                            estimated += 1
                    except Exception:
                        estimated += 1                  # unpar_seable -> count to be safe
        rrule = comp.get("RRULE")
        if not rrule:
            estimated += 1
        else:
            # Estimate how many occurrences could fall in the WINDOW from the
            # frequency. BY* lists multiply occurrences within each base period
            # (e.g. FREQ=DAILY;BYHOUR=0..23;BYMINUTE=0..59 = 1440 per day), so fold
            # their cardinality in — this catches dense rules BEFORE between()
            # expands them into memory. (Slight over-estimate — safe.)
            freq = str(_rrule_first(rrule, "FREQ", "")).upper()
            interval = max(1, _rrule_int(rrule, "INTERVAL", 1) or 1)
            step = freq_seconds.get(freq, 86400) * interval
            windowed = int(math.ceil(seconds / step)) + 2
            mult = 1
            # BYSETPOS selects (narrows), so it's excluded; the rest can each
            # multiply occurrences within a period.
            for k in ("BYSECOND", "BYMINUTE", "BYHOUR", "BYDAY",
                      "BYMONTHDAY", "BYYEARDAY", "BYWEEKNO", "BYMONTH"):
                v = rrule.get(k)
                if isinstance(v, (list, tuple)) and v:
                    mult *= len(v)
            windowed *= mult
            # A COUNT can only ever yield COUNT occurrences total — so the in-window
            # count is at most min(COUNT, windowed). This stops a harmless old feed
            # with e.g. COUNT=10000 (but few/none in the window) being rejected.
            count = _rrule_int(rrule, "COUNT")
            estimated += min(max(count, 0), windowed) if count is not None else windowed
        if estimated > MAX_EXPANDED_EVENTS:
            raise ValueError(
                f"The ICS source would yield over {MAX_EXPANDED_EVENTS} occurrences "
                "in the sync window — rejected to protect memory and calendar.")


def _to_aware(x, assume_zone=None) -> dt.datetime:
    """
    date -> midnight in assume_zone; naive datetime -> assume_zone; aware -> unchanged.

    assume_zone is only relevant for FLOATING times (without TZID/Z). It must be a
    DST-aware zone (zoneinfo), so e.g. 09:00 floating gets +01 in winter and +02 in
    summer — not one fixed offset.
    """
    zone = assume_zone or LOCAL_TZ
    if isinstance(x, dt.datetime):
        return x.replace(tzinfo=zone) if x.tzinfo is None else x
    return dt.datetime(x.year, x.month, x.day, tzinfo=zone)


def expand_events(ics_bytes: bytes, win_start: dt.datetime, win_end: dt.datetime,
                  skip_all_day: bool = True, skip_transparent: bool = True,
                  min_minutes: int = 0, include: list[str] | None = None,
                  exclude: list[str] | None = None,
                  assume_tz: str | None = None) -> list[dict]:
    """Expand ICS into concrete occurrences in the window [win_start, win_end]."""
    inc = [t.lower() for t in (include or []) if t.strip()]
    exc = [t.lower() for t in (exclude or []) if t.strip()]
    assume_zone = _zone_from_name(assume_tz)
    if assume_tz and assume_zone is None:
        raise ValueError(f"Unknown time zone: {assume_tz!r}")
    assume_zone = assume_zone or LOCAL_TZ

    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(ics_bytes)
    _preflight_calendar_size(cal, win_start, win_end)
    occurrences = recurring_ical_events.of(cal).between(win_start, win_end)
    if len(occurrences) > MAX_EXPANDED_EVENTS:
        raise ValueError(
            f"The ICS source yielded {len(occurrences)} occurrences in the sync "
            f"window — over the limit of {MAX_EXPANDED_EVENTS}.")

    out: list[dict] = []
    for comp in occurrences:
        if getattr(comp, "name", None) != "VEVENT":
            continue

        if skip_transparent and str(comp.get("TRANSP", "")).upper() == "TRANSPARENT":
            continue
        if str(comp.get("STATUS", "")).upper() == "CANCELLED":
            continue

        dtstart = comp.get("DTSTART")
        if dtstart is None:
            continue
        raw_start = dtstart.dt
        all_day = not isinstance(raw_start, dt.datetime)
        if all_day and skip_all_day:
            continue

        # Floating = naive datetime (neither TZID nor Z) — the only type whose
        # interpretation depends on an assumed zone. Flag it so it can be shown/warned.
        floating = isinstance(raw_start, dt.datetime) and raw_start.tzinfo is None

        start = _to_aware(raw_start, assume_zone)

        dtend = comp.get("DTEND")
        if dtend is not None:
            end = _to_aware(dtend.dt, assume_zone)
        else:
            dur = comp.get("DURATION")
            if dur is not None:
                end = start + dur.dt
            else:
                end = start + (dt.timedelta(days=1) if all_day else dt.timedelta(hours=1))

        summary = str(comp.get("SUMMARY", ""))
        low = summary.lower()
        if inc and not any(t in low for t in inc):
            continue
        if exc and any(t in low for t in exc):
            continue
        if min_minutes and not all_day:
            if (end - start).total_seconds() < min_minutes * 60:
                continue

        location = str(comp.get("LOCATION", "")).strip()
        # Only a RECOGNIZED meeting link is copied — never an arbitrary URL: field
        # (which could be a private agenda/document link, leaking despite the
        # "busy" privacy intent). See _meeting_link's hostname allowlist.
        link = _meeting_link(comp.get("URL"), location, comp.get("DESCRIPTION"),
                             comp.get("X-MICROSOFT-SKYPETEAMSMEETINGURL"))

        uid = str(comp.get("UID", "")) or hashlib.sha1(
            (summary + start.isoformat()).encode("utf-8")).hexdigest()
        # Stable key: UID + start as an absolute INSTANT in UTC (whole seconds).
        # Instant-based => unaffected by how the source represents the zone, and
        # (with a DST-aware zone) stable across DST for floating times.
        start_utc = start.astimezone(dt.timezone.utc).replace(microsecond=0)
        key = f"{uid}|{start_utc.isoformat()}"

        out.append({
            "key": key, "uid": uid, "summary": summary,
            "start": start, "end": end, "all_day": all_day,
            "floating": floating, "tzname": _tzname_of(start),
            "location": location, "url": link,
        })
    return out


# ---------------------------------------------------------------------------
# EventKit layer (imported lazily so headless 'sync' does not require the TUI)
# ---------------------------------------------------------------------------

class CalStore:
    """Thin wrapper around EKEventStore. macOS only."""

    def __init__(self):
        import EventKit as EK
        from Foundation import NSDate  # noqa: F401  (required indirectly)
        self.EK = EK
        self.store = EK.EKEventStore.alloc().init()
        granted, err = self._request_access()
        if not granted:
            raise PermissionError(
                "Calendar access was not granted. Grant Terminal/Python access under "
                "System Settings → Privacy & Security → Calendars "
                f"(detail: {err})."
            )

    def _request_access(self):
        import threading
        done = threading.Event()
        res = {"granted": False, "error": None}

        def handler(granted, error):
            res["granted"] = bool(granted)
            res["error"] = error
            done.set()

        if hasattr(self.store, "requestFullAccessToEventsWithCompletion_"):
            self.store.requestFullAccessToEventsWithCompletion_(handler)   # macOS 14+
        else:
            self.store.requestAccessToEntityType_completion_(
                self.EK.EKEntityTypeEvent, handler)
        done.wait(timeout=60)
        return res["granted"], res["error"]

    # --- helpers ----------------------------------------------------------

    def _nsdate(self, d: dt.datetime):
        from Foundation import NSDate
        return NSDate.dateWithTimeIntervalSince1970_(d.timestamp())

    def _from_nsdate(self, nsdate) -> dt.datetime:
        return dt.datetime.fromtimestamp(nsdate.timeIntervalSince1970(),
                                         tz=dt.timezone.utc)

    def writable_calendars(self) -> list[dict]:
        cals = self.store.calendarsForEntityType_(self.EK.EKEntityTypeEvent)
        out = []
        for c in cals:
            if not c.allowsContentModifications():
                continue
            src = c.source().title() if c.source() else ""
            out.append({
                "identifier": str(c.calendarIdentifier()),
                "title": str(c.title()),
                "source": str(src),
            })
        out.sort(key=lambda x: (x["source"], x["title"]))
        return out

    def all_calendars(self) -> list[dict]:
        """Every event calendar, including read-only/subscribed ones (for sources)."""
        cals = self.store.calendarsForEntityType_(self.EK.EKEntityTypeEvent)
        out = []
        for c in cals:
            src = c.source().title() if c.source() else ""
            out.append({
                "identifier": str(c.calendarIdentifier()),
                "title": str(c.title()),
                "source": str(src),
                "writable": bool(c.allowsContentModifications()),
            })
        out.sort(key=lambda x: (x["source"], x["title"]))
        return out

    def calendar(self, dest: str):
        """dest can be a calendarIdentifier or a title (writable only)."""
        c = self.store.calendarWithIdentifier_(dest)
        if c is not None:
            return c
        for c in self.store.calendarsForEntityType_(self.EK.EKEntityTypeEvent):
            if str(c.title()) == dest and c.allowsContentModifications():
                return c
        return None

    def any_calendar(self, ident: str):
        """Resolve any calendar (incl. read-only) by identifier or title."""
        c = self.store.calendarWithIdentifier_(ident)
        if c is not None:
            return c
        for c in self.store.calendarsForEntityType_(self.EK.EKEntityTypeEvent):
            if str(c.title()) == ident:
                return c
        return None

    def read_source_events(self, source_ident: str,
                           win_start: dt.datetime, win_end: dt.datetime,
                           skip_all_day: bool = True, skip_transparent: bool = True,
                           min_minutes: int = 0, include: list[str] | None = None,
                           exclude: list[str] | None = None) -> list[dict]:
        """Read occurrences from an already-subscribed EventKit calendar, in the
        same shape expand_events returns. Recurrence is expanded natively by EK.

        The window is queried in ~120-day chunks rather than all at once, so a very
        busy source calendar doesn't materialize the whole window in memory, and the
        MAX_EXPANDED_EVENTS cap aborts early instead of after a giant fetch."""
        cal = self.any_calendar(source_ident)
        if cal is None:
            raise RuntimeError(
                f"The source calendar '{source_ident}' was not found in Apple "
                "Calendar (is it still subscribed/added?).")
        inc = [t.lower() for t in (include or []) if t.strip()]
        exc = [t.lower() for t in (exclude or []) if t.strip()]
        free = self.EK.EKEventAvailabilityFree
        cancelled = getattr(self.EK, "EKEventStatusCanceled", 3)
        chunk = dt.timedelta(days=120)
        result: dict = {}                       # key -> dict (also de-dups boundaries)
        cs = win_start
        while cs < win_end:
            ce = min(cs + chunk, win_end)
            pred = self.store.predicateForEventsWithStartDate_endDate_calendars_(
                self._nsdate(cs), self._nsdate(ce), [cal])
            for ev in (self.store.eventsMatchingPredicate_(pred) or []):
                # Never treat OUR OWN generated blocks as source events — otherwise a
                # source calendar containing shadowcal output (or source == dest)
                # would create a feedback loop / "busy of busy" duplicates.
                if notes_have_marker(ev.notes()):
                    continue
                if skip_transparent and ev.availability() == free:
                    continue
                if ev.status() == cancelled:
                    continue
                all_day = bool(ev.isAllDay())
                if all_day and skip_all_day:
                    continue
                start = self._from_nsdate(ev.startDate())
                end = self._from_nsdate(ev.endDate())
                summary = str(ev.title() or "")
                low = summary.lower()
                if inc and not any(t in low for t in inc):
                    continue
                if exc and any(t in low for t in exc):
                    continue
                if min_minutes and not all_day:
                    if (end - start).total_seconds() < min_minutes * 60:
                        continue
                tz = ev.timeZone()
                tzname = str(tz.name()) if tz is not None else ("UTC" if not all_day else None)
                location = str(ev.location() or "").strip()
                ev_url = ""
                try:
                    if ev.URL() is not None:
                        ev_url = str(ev.URL().absoluteString())
                except Exception:
                    ev_url = ""
                # Only copy a RECOGNIZED meeting link, never an arbitrary event URL.
                link = _meeting_link(ev_url, location, ev.notes())
                uid = (str(ev.calendarItemExternalIdentifier() or "")
                       or str(ev.eventIdentifier() or "")
                       or hashlib.sha1((summary + start.isoformat()).encode()).hexdigest())
                start_utc = start.astimezone(dt.timezone.utc).replace(microsecond=0)
                key = f"{uid}|{start_utc.isoformat()}"
                result[key] = {
                    "key": key, "uid": uid, "summary": summary,
                    "start": start, "end": end, "all_day": all_day,
                    "floating": False, "tzname": tzname,
                    "location": location, "url": link,
                }
                if len(result) > MAX_EXPANDED_EVENTS:
                    raise ValueError(
                        f"The source calendar yielded over {MAX_EXPANDED_EVENTS} "
                        f"occurrences in the sync window — over the limit.")
            cs = ce
        return list(result.values())

    def owned_events(self, cal, sync_id: str,
                     win_start: dt.datetime, win_end: dt.datetime) -> dict:
        """Return map: key -> EKEvent for the blocks WE made for this sync.

        If several destination events share the same source key, one is kept on
        the right key and the rest get synthetic __duplicate__ keys, so the next
        reconciliation can delete them instead of hiding them in a dict overwrite.
        """
        pred = self.store.predicateForEventsWithStartDate_endDate_calendars_(
            self._nsdate(win_start), self._nsdate(win_end), [cal])
        events = self.store.eventsMatchingPredicate_(pred) or []
        owned: dict = {}
        for ev in events:
            belongs, key = owned_for_sync(ev.notes(), sync_id)
            if not belongs:
                continue
            if key is None:
                # Our block, but the key is gone (manually edited note etc.).
                # Give it a unique stray key so it never matches a source and gets
                # cleaned up in reconciliation instead of piling up.
                key = f"__stray__:{ev.eventIdentifier()}"
            elif key in owned:
                key = f"__duplicate__:{ev.eventIdentifier()}"
            owned[key] = ev
        return owned

    def _avail(self, show_as: str):
        m = {
            "busy": self.EK.EKEventAvailabilityBusy,
            "free": self.EK.EKEventAvailabilityFree,
            "tentative": self.EK.EKEventAvailabilityTentative,
            "oof": self.EK.EKEventAvailabilityUnavailable,
        }
        return m.get(show_as, self.EK.EKEventAvailabilityBusy)

    def _nstz(self, tzname: str | None):
        if not tzname:
            return None
        from Foundation import NSTimeZone
        return NSTimeZone.timeZoneWithName_(tzname)

    def _nsurl(self, url: str | None):
        if not url:
            return None
        try:
            from Foundation import NSURL
            return NSURL.URLWithString_(url)
        except Exception:
            return None

    def create_block(self, cal, title: str, start: dt.datetime, end: dt.datetime,
                     key: str, sync_id: str, show_as: str = DEFAULT_SHOW_AS,
                     tzname: str | None = None, all_day: bool = False,
                     location: str = "", url: str | None = None) -> None:
        ev = self.EK.EKEvent.eventWithEventStore_(self.store)
        ev.setTitle_(title)
        ev.setStartDate_(self._nsdate(start))
        ev.setEndDate_(self._nsdate(end))
        ev.setCalendar_(cal)
        ev.setAvailability_(self._avail(show_as))
        # Pin the block to the source's zone so it does not "float" if the Mac
        # changes time zone (travel). Floating sources stay floating (tz=nil).
        ev.setTimeZone_(self._nstz(tzname))
        ev.setAllDay_(bool(all_day))
        ev.setLocation_(location or None)
        ev.setURL_(self._nsurl(url))
        ev.setNotes_(make_notes(sync_id, key))
        ev.setAlarms_(None)          # no reminders on our own blocks
        ok, err = self.store.saveEvent_span_error_(
            ev, self.EK.EKSpanThisEvent, None)
        if not ok:
            raise RuntimeError(f"Could not save event: {err}")

    def needs_update(self, ev, start: dt.datetime, end: dt.datetime,
                     title: str, show_as: str = DEFAULT_SHOW_AS,
                     tzname: str | None = None, all_day: bool = False,
                     location: str = "", url: str | None = None) -> bool:
        cur_s = ev.startDate().timeIntervalSince1970()
        cur_e = ev.endDate().timeIntervalSince1970()
        if abs(cur_s - start.timestamp()) > 1 or abs(cur_e - end.timestamp()) > 1:
            return True
        if str(ev.title()) != title:
            return True
        if ev.availability() != self._avail(show_as):
            return True
        if bool(ev.isAllDay()) != bool(all_day):
            return True
        if str(ev.location() or "") != (location or ""):
            return True
        cur_url = ""
        try:
            cur_url = str(ev.URL().absoluteString()) if ev.URL() is not None else ""
        except Exception:
            cur_url = ""
        if cur_url != (url or ""):
            return True
        # Only when the source HAS a zone: otherwise (floating) EK's own default
        # zone would cause perpetual churn. If we pin a zone, it must match.
        if tzname:
            cur_tz = ev.timeZone()
            cur_name = str(cur_tz.name()) if cur_tz is not None else None
            if cur_name != tzname:
                return True
        return False

    def update_block(self, ev, start: dt.datetime, end: dt.datetime,
                     title: str, show_as: str = DEFAULT_SHOW_AS,
                     tzname: str | None = None, all_day: bool = False,
                     location: str = "", url: str | None = None) -> None:
        ev.setStartDate_(self._nsdate(start))
        ev.setEndDate_(self._nsdate(end))
        ev.setTitle_(title)
        ev.setAvailability_(self._avail(show_as))
        ev.setTimeZone_(self._nstz(tzname))
        ev.setAllDay_(bool(all_day))
        ev.setLocation_(location or None)
        ev.setURL_(self._nsurl(url))
        ev.setAlarms_(None)
        ok, err = self.store.saveEvent_span_error_(
            ev, self.EK.EKSpanThisEvent, None)
        if not ok:
            raise RuntimeError(f"Could not update event: {err}")

    def delete(self, ev) -> None:
        ok, err = self.store.removeEvent_span_error_(
            ev, self.EK.EKSpanThisEvent, None)
        if not ok:
            raise RuntimeError(f"Could not delete event: {err}")


# ---------------------------------------------------------------------------
# The synchronization itself
# ---------------------------------------------------------------------------

def run_sync(sync: dict, store: "CalStore", *, dry_run: bool = False,
             force: bool = False, safety: dict | None = None) -> dict:
    sync = normalize_sync(sync)
    safety = safety or dict(DEFAULT_SAFETY)
    now = dt.datetime.now(LOCAL_TZ)
    win_start = now - dt.timedelta(days=sync["back_days"])
    win_end = now + dt.timedelta(days=sync["forward_days"])

    cal = store.calendar(sync["dest"])
    if cal is None:
        raise RuntimeError(
            f"The destination calendar '{sync['dest']}' was not found "
            "(is the account added in Apple Calendar, and is it writable?).")

    if sync.get("source_cal"):
        # Refuse to shadow a calendar into itself — it would mirror the calendar's
        # own events back in as busy blocks (and the marker-skip would only stop
        # the *generated* ones). Compare by calendarIdentifier, since source_cal and
        # dest may name the same calendar via different forms (id vs title).
        src_cal = store.any_calendar(sync["source_cal"]) \
            if hasattr(store, "any_calendar") else None
        try:
            same = (src_cal is not None and cal is not None and
                    str(src_cal.calendarIdentifier()) == str(cal.calendarIdentifier()))
        except Exception:
            same = False
        if same:
            raise RuntimeError(
                "The source calendar is the same as the destination calendar — "
                "that would shadow the calendar into itself. Pick a different target.")
        src = store.read_source_events(
            sync["source_cal"], win_start, win_end,
            sync["skip_all_day"], sync["skip_transparent"],
            sync["min_minutes"], sync["include"], sync["exclude"])
    elif sync.get("url"):
        ics = fetch_ics(sync["url"], allow_http=sync.get("allow_insecure_http", False))
        src = expand_events(ics, win_start, win_end,
                            sync["skip_all_day"], sync["skip_transparent"],
                            sync["min_minutes"], sync["include"], sync["exclude"],
                            sync["assume_tz"])
    else:
        raise RuntimeError(
            "This sync has no source — set an ICS link (url) or a source calendar.")

    pad_b = dt.timedelta(minutes=sync["pad_before"])
    pad_a = dt.timedelta(minutes=sync["pad_after"])
    show_as = sync["show_as"]
    copy_loc = sync.get("copy_location", False)

    # Desired block per source event. The key is the source's ORIGINAL start time,
    # but the block times are padded — so the buffer can change without churn.
    desired: dict = {}
    floating = 0
    for e in src:
        if e.get("floating"):
            floating += 1
        title = (e["summary"] or sync["title"]) if sync["copy_title"] else sync["title"]
        desired[e["key"]] = {
            "start": e["start"] - pad_b,
            "end": e["end"] + pad_a,
            "title": title,
            "tzname": e.get("tzname"),
            "all_day": e.get("all_day", False),
            "location": (e.get("location") or "") if copy_loc else "",
            "url": (e.get("url") or None) if copy_loc else None,
        }

    owned = store.owned_events(cal, sync["id"], win_start, win_end)

    duplicate_keys = [k for k in owned if k.startswith("__duplicate__:")]
    to_create = [k for k in desired if k not in owned]
    to_update = [k for k in desired if k in owned and store.needs_update(
        owned[k], desired[k]["start"], desired[k]["end"], desired[k]["title"], show_as,
        desired[k]["tzname"], desired[k]["all_day"],
        desired[k]["location"], desired[k]["url"])]
    missing_keys = [k for k in owned if k not in desired and k not in duplicate_keys]
    to_delete = list(duplicate_keys) + list(missing_keys)

    # Safety guard: block anomalous mass deletions (unless --force).
    guard_msg = None
    blocked = 0
    if not force:
        trip, guard_msg = evaluate_delete_guard(
            len(src), len(owned) - len(duplicate_keys), len(missing_keys), safety)
        if trip:
            blocked = len(missing_keys)
            to_delete = list(duplicate_keys)

    if not dry_run:
        for k in to_create:
            d = desired[k]
            store.create_block(cal, d["title"], d["start"], d["end"],
                               k, sync["id"], show_as, d["tzname"], d["all_day"],
                               d["location"], d["url"])
        for k in to_update:
            d = desired[k]
            store.update_block(owned[k], d["start"], d["end"], d["title"],
                               show_as, d["tzname"], d["all_day"],
                               d["location"], d["url"])
        for k in to_delete:
            store.delete(owned[k])

    return {
        "source": len(src),
        "created": len(to_create),
        "updated": len(to_update),
        "deleted": len(to_delete),
        "blocked_deletes": blocked,
        "floating": floating,
        "guard": guard_msg,
        "dry_run": dry_run,
    }


def run_all(only_id: str | None = None, *, dry_run: bool = False,
            force: bool = False) -> int:
    # Dry-run writes nothing and needs no lock. Real runs take an exclusive lock
    # so two concurrent syncs cannot create duplicate blocks.
    if dry_run:
        return _run_all_locked(only_id, dry_run=True, force=force)
    try:
        with _sync_lock():
            return _run_all_locked(only_id, dry_run=False, force=force)
    except SyncLockBusy:
        print("Another shadowcal sync is already running — skipping.")
        return 0


def _run_all_locked(only_id: str | None = None, *, dry_run: bool = False,
                    force: bool = False) -> int:
    cfg = load_config()
    state = load_state()
    safety = get_safety(cfg)
    # Iterate the RAW syncs; each is normalized inside run_sync within the per-sync
    # try/except below. That way one bad saved sync (e.g. a hand-edited invalid
    # timezone) is recorded as an error for itself and the others still run.
    syncs = list(cfg.get("syncs", []))
    if only_id:
        syncs = [s for s in syncs if s.get("id") == only_id]
        if not syncs:
            print(f"No sync with id '{only_id}'.", file=sys.stderr)
            return 1

    store = None
    rc = 0
    for s in syncs:
        sid = s.get("id")
        if not sid:
            rc = 1
            print("Skipping a sync with no 'id' in config.", file=sys.stderr)
            continue
        name = s.get("name", sid)
        if not s.get("enabled", True) and not only_id:
            continue
        stamp = dt.datetime.now(LOCAL_TZ).isoformat(timespec="seconds")
        prev = state.get(sid, {})
        try:
            if store is None:
                store = CalStore()
            stats = run_sync(s, store, dry_run=dry_run, force=force, safety=safety)
            status = "warning" if stats.get("guard") else "ok"
            entry = {"last_run": stamp, "status": status, **stats, "error": None}
            # Keep the timestamp of the most recent SUCCESSFUL (non-dry) run.
            entry["last_success"] = (stamp if not dry_run
                                     else prev.get("last_success"))
            state[sid] = entry
            tag = " [DRY]" if dry_run else ""
            line = (f"[{stamp}]{tag} {sid} ({name}): "
                    f"source={stats['source']} new={stats['created']} "
                    f"updated={stats['updated']} deleted={stats['deleted']}")
            if stats.get("guard"):
                line += f"  ⚠ {stats['guard']}"
                if not dry_run:
                    notify(f"shadowcal: {name}", stats["guard"])
            print(line)
        except Exception as exc:  # noqa: BLE001
            rc = 1
            err_msg = _safe_error(exc, s.get("url"))
            state[sid] = {"last_run": stamp, "status": "error",
                          "error": err_msg,
                          "last_success": prev.get("last_success")}
            print(f"[{stamp}] {sid} ({name}): ERROR: {err_msg}", file=sys.stderr)
            if os.environ.get("SHADOWCAL_DEBUG"):
                import traceback
                traceback.print_exc()
            if not dry_run:
                notify(f"shadowcal: {name}", f"Sync failed: {err_msg}")
        if not dry_run:
            save_state(state)
    return rc


# ---------------------------------------------------------------------------
# launchd agent
# ---------------------------------------------------------------------------

def agent_label() -> str:
    return f"com.{getpass.getuser()}.{APP}"


def agent_plist_path() -> Path:
    return Path(os.path.expanduser(
        f"~/Library/LaunchAgents/{agent_label()}.plist"))


def write_agent(interval: int = 900) -> Path:
    _ensure_dir()
    # XML-escape everything interpolated: a path with &, < or > would otherwise
    # produce an invalid plist that launchd cannot load.
    py = _xml_escape(sys.executable)
    script = _xml_escape(os.path.abspath(__file__))
    label = _xml_escape(agent_label())
    log_out = _xml_escape(str(LOG_OUT))
    log_err = _xml_escape(str(LOG_ERR))
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{py}</string>
        <string>{script}</string>
        <string>sync</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>StartInterval</key>
    <integer>{interval}</integer>
    <key>StandardOutPath</key>
    <string>{log_out}</string>
    <key>StandardErrorPath</key>
    <string>{log_err}</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
"""
    path = agent_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plist, "utf-8")
    return path


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def _source_label(s: dict, show_secrets: bool = False) -> str:
    if s.get("source_cal"):
        return f"calendar: {s['source_cal']}"
    url = s.get("url", "")
    return url if show_secrets else _mask_url(url)


def cmd_list(args) -> int:
    cfg = load_config()
    state = load_state()
    syncs = cfg.get("syncs", [])
    if not syncs:
        print("No syncs configured. Add one with 'shadowcal add' or in the TUI.")
        return 0
    for raw in syncs:
        s, err = _safe_normalize(raw)
        if err:
            sid = raw.get("id", "?")
            print(f"{sid:<8} [BROKEN] {raw.get('name', sid)}\n"
                  f"          error:  invalid config — {err}")
            continue
        st = state.get(s["id"], {})
        flag = "on " if s["enabled"] else "off"
        extras = [f"show-as={s['show_as']}"]
        if s["pad_before"] or s["pad_after"]:
            extras.append(f"buffer={s['pad_before']}/{s['pad_after']} min")
        if s["include"]:
            extras.append(f"only={','.join(s['include'])}")
        if s["exclude"]:
            extras.append(f"exclude={','.join(s['exclude'])}")
        if s["min_minutes"]:
            extras.append(f"min={s['min_minutes']} min")
        if s["copy_title"]:
            extras.append("copy-title")
        if s["copy_location"]:
            extras.append("copy-location")
        source = _source_label(s, getattr(args, "show_secrets", False))
        line = (f"{s['id']:<8} [{flag}] {s['name']}\n"
                f"          source: {source}\n"
                f"          target: {s['dest']}\n"
                f"          title:  {s['title']}\n"
                f"          opts:   {', '.join(extras)}\n"
                f"          status: {st.get('status', '—')} "
                f"(last: {st.get('last_run', 'never')}, "
                f"ok: {st.get('last_success', 'never')})")
        if st.get("error"):
            line += f"\n          error:  {st['error']}"
        if st.get("guard"):
            line += f"\n          ⚠:      {st['guard']}"
        print(line)
    return 0


def cmd_calendars(_args) -> int:
    try:
        store = CalStore()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not open the calendar store: {exc}", file=sys.stderr)
        return 1
    cals = store.writable_calendars()
    if not cals:
        print("No writable calendars found.")
        return 0
    print("Writable destination calendars (use 'identifier' as --dest):\n")
    for c in cals:
        print(f"  {c['title']}  [{c['source']}]")
        print(f"      identifier: {c['identifier']}\n")
    return 0


def cmd_permissions(_args) -> int:
    """Trigger the calendar permission prompt and report status."""
    try:
        CalStore()                      # the constructor asks for access (prompt)
    except Exception as exc:  # noqa: BLE001
        print(f"Access not granted: {exc}", file=sys.stderr)
    key, label = calendar_permission_status()
    print(f"Calendar access: {label}")
    return 0 if key == "ok" else 1


def cmd_add(args) -> int:
    if not (args.url or args.source_cal):
        print("Provide a source: either --url <ICS link> or --source-cal <id|title>.",
              file=sys.stderr)
        return 1
    cfg = load_config()
    sync = normalize_sync({
        "id": new_sync_id(cfg),
        "name": args.name,
        "url": args.url or "",
        "source_cal": args.source_cal,
        "dest": args.dest,
        "title": args.title or DEFAULT_TITLE,
        "back_days": args.back_days,
        "forward_days": args.forward_days,
        "skip_all_day": not args.include_all_day,
        "skip_transparent": not args.include_free,
        "pad_before": args.pad_before,
        "pad_after": args.pad_after,
        "show_as": args.show_as,
        "include": args.include or [],
        "exclude": args.exclude or [],
        "min_minutes": args.min_minutes,
        "copy_title": args.copy_title,
        "copy_location": args.copy_location,
        "allow_insecure_http": args.allow_insecure_http,
        "assume_tz": args.tz,
        "enabled": True,
    })
    cfg.setdefault("syncs", []).append(sync)
    save_config(cfg)
    print(f"Added {sync['id']}: {sync['name']}")
    if sync.get("copy_title"):
        print("NOTE: --copy-title writes the source's REAL titles into the target "
              "calendar. Make sure the calendar's sharing is set to free/busy only, "
              "or colleagues can read them.", file=sys.stderr)
    if sync.get("copy_location"):
        print("NOTE: --copy-location writes the source's location and meeting join "
              "links (e.g. room names, Teams/Zoom URLs) into the target calendar. "
              "Only use it when that calendar's sharing is free/busy only.",
              file=sys.stderr)
    return 0


def cmd_remove(args) -> int:
    try:
        with _sync_lock():
            return _cmd_remove_locked(args)
    except SyncLockBusy:
        print("Another shadowcal sync is already running — try again shortly.",
              file=sys.stderr)
        return 1


def cleanup_sync_blocks(sync: dict, store: "CalStore | None" = None,
                        span_years: int | None = None) -> int:
    """Delete all blocks marked as belonging to the sync, over a wide range.

    The range is scanned in ~1-year chunks rather than one giant query, so large
    calendars don't have to materialize decades of events at once. Events are
    de-duplicated across chunk boundaries by their identifier before deletion.

    span_years defaults to the sync's own window (back/forward days) plus a year,
    floored at 10 — so a normal sync needs ~20 chunked queries instead of 100,
    while a sync configured with a multi-year window still scans far enough out.
    """
    s = normalize_sync(sync)
    if span_years is None:
        needed = max(s["back_days"], s["forward_days"]) / 365.0
        span_years = max(10, int(math.ceil(needed)) + 1)
    if store is None:
        store = CalStore()
    cal = store.calendar(s["dest"])
    if cal is None:
        return 0
    now = dt.datetime.now(LOCAL_TZ)
    chunk_start = now - dt.timedelta(days=365 * span_years)
    seen: dict = {}
    for i in range(2 * span_years):
        ws = chunk_start + dt.timedelta(days=365 * i)
        we = ws + dt.timedelta(days=366)        # 1-day overlap; dedup handles it
        for ev in store.owned_events(cal, s["id"], ws, we).values():
            try:
                eid = str(ev.eventIdentifier())
            except Exception:
                eid = str(ev)
            seen[eid] = ev
    for ev in seen.values():
        store.delete(ev)
    return len(seen)


def _cmd_remove_locked(args) -> int:
    cfg = load_config()
    syncs = cfg.get("syncs", [])
    target = next((s for s in syncs if s.get("id") == args.id), None)
    if target is None:
        print(f"No sync with id '{args.id}'.", file=sys.stderr)
        return 1
    # Clean up the blocks we made before removing the configuration. If cleanup
    # fails (no calendar access, EventKit error), ABORT by default — otherwise we
    # would orphan generated blocks with no easy way to find them again. The user
    # can override with --force-remove-config when the calendar is truly gone.
    try:
        n = cleanup_sync_blocks(target)
        print(f"Removed {n} blocks from '{normalize_sync(target)['dest']}'.")
    except Exception as exc:  # noqa: BLE001
        msg = _safe_error(exc, target.get('url'))
        if not getattr(args, "force_remove_config", False):
            print(f"ERROR: could not clean up blocks: {msg}\n"
                  "Aborted — the sync config was kept so the blocks can still be "
                  "removed later. Fix calendar access and retry, or use "
                  "'shadowcal remove --force-remove-config' to drop the config anyway "
                  "(the blocks will then have to be deleted by hand).",
                  file=sys.stderr)
            return 1
        print(f"Warning: could not clean up blocks: {msg}\n"
              "Removing the config anyway (--force-remove-config); any leftover "
              "blocks must be deleted manually.", file=sys.stderr)

    cfg["syncs"] = [s for s in syncs if s.get("id") != args.id]
    save_config(cfg)
    state = load_state()
    state.pop(args.id, None)
    save_state(state)
    print(f"Removed {args.id}.")
    return 0


def cmd_cleanup(args) -> int:
    try:
        with _sync_lock():
            cfg = load_config()
            target = next((s for s in cfg.get("syncs", []) if s.get("id") == args.id), None)
            if target is None:
                print(f"No sync with id '{args.id}'.", file=sys.stderr)
                return 1
            n = cleanup_sync_blocks(target)
            print(f"Removed {n} blocks for {args.id}; the sync configuration was kept.")
            return 0
    except SyncLockBusy:
        print("Another shadowcal sync is already running — try again shortly.",
              file=sys.stderr)
        return 1


def cmd_disable(args) -> int:
    try:
        with _sync_lock():
            cfg = load_config()
            target = next((s for s in cfg.get("syncs", []) if s.get("id") == args.id), None)
            if target is None:
                print(f"No sync with id '{args.id}'.", file=sys.stderr)
                return 1
            # Remove the blocks BEFORE marking disabled, and abort cleanly if that
            # fails — otherwise we'd leave a disabled sync (which normal runs skip)
            # with orphaned blocks. --keep-blocks is the escape hatch.
            n = 0
            if not args.keep_blocks:
                try:
                    n = cleanup_sync_blocks(target)
                except Exception as exc:  # noqa: BLE001
                    print(f"ERROR: could not remove blocks: "
                          f"{_safe_error(exc, target.get('url'))}\n"
                          "Aborted — the sync is still enabled, so its blocks aren't "
                          "orphaned. Fix calendar access and retry, or use "
                          "'shadowcal disable {} --keep-blocks' to disable without "
                          "removing them.".format(args.id),
                          file=sys.stderr)
                    return 1
            target["enabled"] = False
            save_config(cfg)
            print(f"Disabled {args.id}." +
                  (f" Removed {n} existing blocks." if not args.keep_blocks
                   else " Existing blocks were left in place."))
            return 0
    except SyncLockBusy:
        print("Another shadowcal sync is already running — try again shortly.",
              file=sys.stderr)
        return 1


def cmd_enable(args) -> int:
    try:
        with _sync_lock():
            cfg = load_config()
            target = next((s for s in cfg.get("syncs", []) if s.get("id") == args.id), None)
            if target is None:
                print(f"No sync with id '{args.id}'.", file=sys.stderr)
                return 1
            target["enabled"] = True
            save_config(cfg)
            print(f"Enabled {args.id}.")
            return 0
    except SyncLockBusy:
        print("Another shadowcal sync is already running — try again shortly.",
              file=sys.stderr)
        return 1


def cmd_test(args) -> int:
    target = args.target
    cfg = load_config()
    sync = next((s for s in cfg.get("syncs", []) if s.get("id") == target), None)
    if sync is not None:
        sync = normalize_sync(sync)
    else:
        sync = normalize_sync({"id": "_", "name": "_", "url": target, "dest": "_"})
    if getattr(args, "tz", None):
        sync["assume_tz"] = args.tz
    now = dt.datetime.now(LOCAL_TZ)
    try:
        if sync.get("source_cal") and not sync.get("url"):
            store = CalStore()
            ev = store.read_source_events(
                sync["source_cal"], now - dt.timedelta(days=7),
                now + dt.timedelta(days=365),
                sync["skip_all_day"], sync["skip_transparent"],
                sync["min_minutes"], sync["include"], sync["exclude"])
        else:
            ics = fetch_ics(sync["url"],
                            allow_http=sync.get("allow_insecure_http", False))
            ev = expand_events(ics, now - dt.timedelta(days=7),
                               now + dt.timedelta(days=365),
                               sync["skip_all_day"], sync["skip_transparent"],
                               sync["min_minutes"], sync["include"], sync["exclude"],
                               sync["assume_tz"])
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    floating = [e for e in ev if e.get("floating")]
    print(f"OK — {len(ev)} events in the window (-7/+365 days) after filters.")
    print(f"Local zone: {LOCAL_TZ}"
          + (f" | assumed zone for floating: {sync['assume_tz']}"
             if sync["assume_tz"] else "") + "\n")
    print(f"{'Local start':19} {'UTC':17} {'offset':7} zone")
    for e in ev[:15]:
        loc = e["start"].astimezone(LOCAL_TZ)
        utc = e["start"].astimezone(dt.timezone.utc)
        secs = int(e["start"].utcoffset().total_seconds())
        off_txt = f"{'+' if secs >= 0 else '-'}{abs(secs)//3600:02d}:{abs(secs)%3600//60:02d}"
        zone = ("FLOATING→" + str(LOCAL_TZ)) if e.get("floating") else (e.get("tzname") or "?")
        print(f"  {loc:%Y-%m-%d %H:%M}  {utc:%Y-%m-%d %H:%MZ}  {off_txt:6} {zone}  "
              f"{e['summary']}")
    if len(ev) > 15:
        print(f"  … and {len(ev) - 15} more.")
    if floating:
        print(f"\n⚠ {len(floating)} event(s) have FLOATING time (no time zone in the "
              f"source). They are interpreted in {sync['assume_tz'] or LOCAL_TZ}. If that "
              f"is wrong, set a zone: 'shadowcal add … --tz Europe/Copenhagen' (or in config).")
    return 0


def cmd_status(_args) -> int:
    cfg = load_config()
    state = load_state()
    syncs = cfg.get("syncs", [])
    if not syncs:
        print("No syncs configured.")
        return 0
    now = dt.datetime.now(LOCAL_TZ)
    worst = 0  # 0 ok, 1 warning, 2 error/stale
    for raw in syncs:
        s, err = _safe_normalize(raw)
        if err:
            worst = max(worst, 2)
            sid = raw.get("id", "?")
            print(f"  ✗ {sid:<8} {raw.get('name', sid)}: broken config — {err}")
            continue
        st = state.get(s["id"], {})
        status = st.get("status", "—")
        last_ok = st.get("last_success")
        age_txt = "never"
        stale = False
        if last_ok:
            try:
                age = now - dt.datetime.fromisoformat(last_ok)
                hrs = age.total_seconds() / 3600
                age_txt = (f"{int(age.total_seconds() // 60)} min ago"
                           if hrs < 1 else f"{hrs:.1f} h ago")
                stale = hrs > STALE_WARN_HOURS
            except Exception:
                pass
        mark = "✓"
        if status == "error" or stale:
            mark, worst = "✗", max(worst, 2)
        elif status == "warning":
            mark, worst = "⚠", max(worst, 1)
        suffix = "  [STALE]" if stale else ""
        print(f"  {mark} {s['id']:<8} {s['name']}: {status}, "
              f"last ok {age_txt}{suffix}")
        if st.get("guard"):
            print(f"        ⚠ {st['guard']}")
        if st.get("error"):
            print(f"        error: {st['error']}")
    if not state:
        print("\n(No runs yet — run 'shadowcal sync'.)")
    return 0 if worst == 0 else (1 if worst == 1 else 2)


def cmd_sync(args) -> int:
    return run_all(only_id=args.id, dry_run=args.dry_run, force=args.force)


def cmd_install_agent(args) -> int:
    path = write_agent(interval=args.interval)
    label = agent_label()
    # Reload if already active.
    subprocess.run(["launchctl", "unload", str(path)],
                   stderr=subprocess.DEVNULL)
    rc = subprocess.run(["launchctl", "load", "-w", str(path)])
    print(f"Wrote {path}")
    if rc.returncode == 0:
        print(f"Loaded as {label} — runs every {args.interval} sec.")
        print("Tip: 'launchctl kickstart -k gui/$(id -u)/" + label +
              "' runs it immediately.")
    else:
        print("Wrote the plist, but 'launchctl load' failed — try manually:")
        print(f"  launchctl load -w {path}")
    return 0


def cmd_uninstall_agent(_args) -> int:
    path = agent_plist_path()
    subprocess.run(["launchctl", "unload", str(path)],
                   stderr=subprocess.DEVNULL)
    if path.exists():
        path.unlink()
        print(f"Removed {path}")
    else:
        print("No agent installed.")
    return 0


def cmd_tui(_args) -> int:
    try:
        from shadowcal_tui import run_tui  # if split out (not bundled here)
    except Exception:
        run_tui = _builtin_tui
    return run_tui()


# ---------------------------------------------------------------------------
# TUI (Textual) — defined inline and imported lazily
# ---------------------------------------------------------------------------

def _builtin_tui(_return_app: bool = False) -> int:
    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Vertical, Horizontal, VerticalScroll
        from textual.screen import ModalScreen
        from textual.widgets import (
            Header, Footer, DataTable, Static, Input, Button, Select, Label, Switch,
            OptionList,
        )
        from textual.widgets.option_list import Option
        from rich.text import Text
    except Exception:
        print("The TUI requires 'textual' (pip install textual). "
              "You can still use all the CLI commands.", file=sys.stderr)
        return 1

    # Try to fetch calendars for the pickers (may fail without access).
    def writable_options():
        try:
            store = CalStore()
            return [(f"{c['title']}  [{c['source']}]", c["identifier"])
                    for c in store.writable_calendars()]
        except Exception:
            return []

    def source_options():
        try:
            store = CalStore()
            return [(f"{c['title']}  [{c['source']}]"
                     + ("" if c["writable"] else " (read-only)"), c["identifier"])
                    for c in store.all_calendars()]
        except Exception:
            return []

    class EditScreen(ModalScreen):
        """Add/edit a sync."""

        BINDINGS = [Binding("escape", "cancel", "Cancel")]

        def action_cancel(self) -> None:
            self.dismiss(None)

        def __init__(self, sync: dict | None):
            super().__init__()
            self.sync = sync or {}
            self.cals = writable_options()
            self.src_cals = source_options()

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Label("Edit sync" if self.sync.get("id") else "New sync",
                            id="dlg-title")
                with VerticalScroll(id="dlg-scroll"):
                    yield Label("Name")
                    yield Input(value=self.sync.get("name", ""), id="f-name",
                                placeholder="e.g. Private / Meetings")
                    with Horizontal(classes="switchrow"):
                        yield Label("Enabled")
                        yield Switch(value=bool(self.sync.get("enabled", True)),
                                     id="f-enabled")
                    yield Label("Source calendar (if you already subscribe to the ICS)")
                    if self.src_cals:
                        src_vals = {v for _, v in self.src_cals}
                        cur_src = self.sync.get("source_cal")
                        src_kw = {"value": cur_src} if cur_src in src_vals else {}
                        yield Select(self.src_cals, id="f-srccal", allow_blank=True,
                                     prompt="— none (use ICS link below) —", **src_kw)
                    else:
                        yield Input(value=self.sync.get("source_cal") or "",
                                    id="f-srccal",
                                    placeholder="calendar identifier (optional)")
                    yield Label("ICS link (source) — leave empty if using a source calendar")
                    yield Input(value=self.sync.get("url", ""), id="f-url",
                                placeholder="https://… or webcal://…")
                    yield Label("Destination calendar")
                    if self.cals:
                        dest_vals = {v for _, v in self.cals}
                        cur = self.sync.get("dest")
                        # NB: across Textual versions, "nothing selected" must be
                        # expressed by omitting value (default = Select.NULL) +
                        # allow_blank, NOT by passing Select.BLANK (illegal in 8.x).
                        sel_kw = {"value": cur} if cur in dest_vals else {}
                        yield Select(self.cals, id="f-dest", allow_blank=True,
                                     prompt="Choose destination calendar", **sel_kw)
                    else:
                        yield Input(value=self.sync.get("dest", ""), id="f-dest",
                                    placeholder="calendarIdentifier or title")
                    yield Label("Block title")
                    yield Input(value=self.sync.get("title", DEFAULT_TITLE),
                                id="f-title")
                    with Horizontal(id="dlg-pads"):
                        with Vertical():
                            yield Label("Buffer before (min)")
                            yield Input(value=str(self.sync.get("pad_before", 0)),
                                        id="f-padb", type="integer")
                        with Vertical():
                            yield Label("Buffer after (min)")
                            yield Input(value=str(self.sync.get("pad_after", 0)),
                                        id="f-pada", type="integer")
                    yield Label("Show as")
                    yield Select([("Busy", "busy"), ("Tentative", "tentative"),
                                  ("Out of office", "oof"), ("Free", "free")],
                                 id="f-showas",
                                 value=self.sync.get("show_as", DEFAULT_SHOW_AS),
                                 allow_blank=False)
                    yield Label("Assume time zone for floating times (optional, IANA)")
                    yield Input(value=self.sync.get("assume_tz") or "", id="f-tz",
                                placeholder="e.g. Europe/Copenhagen")
                    yield Label("Include only titles containing (comma-separated, optional)")
                    yield Input(value=", ".join(self.sync.get("include", [])),
                                id="f-include", placeholder="e.g. Meetings, Board")
                    yield Label("Skip titles containing (comma-separated, optional)")
                    yield Input(value=", ".join(self.sync.get("exclude", [])),
                                id="f-exclude", placeholder="e.g. Tentative")
                    yield Label("Minimum duration (min, 0 = no limit)")
                    yield Input(value=str(self.sync.get("min_minutes", 0)),
                                id="f-min", type="integer")
                    with Horizontal(classes="switchrow"):
                        yield Label("Copy the source title (not advised for sensitive)")
                        yield Switch(value=bool(self.sync.get("copy_title", False)),
                                     id="f-copytitle")
                    with Horizontal(classes="switchrow"):
                        yield Label("Copy location / meeting link "
                                    "(not advised — reveals rooms & join URLs)")
                        yield Switch(value=bool(self.sync.get("copy_location", False)),
                                     id="f-copyloc")
                    with Horizontal(classes="switchrow"):
                        yield Label("Skip all-day events")
                        yield Switch(value=bool(self.sync.get("skip_all_day", True)),
                                     id="f-skipallday")
                    with Horizontal(classes="switchrow"):
                        yield Label("Skip 'free'-marked events")
                        yield Switch(value=bool(self.sync.get("skip_transparent", True)),
                                     id="f-skipfree")
                with Horizontal(id="dlg-buttons"):
                    yield Button("Save", variant="primary", id="save")
                    if self.sync.get("id"):
                        yield Button("Delete shadow", variant="error", id="delete")
                    yield Button("Cancel", id="cancel")

        def _read_select(self, widget_id):
            """Read a Select/Input value, mapping 'nothing selected' to ''."""
            w = self.query_one(widget_id)
            raw = w.value if hasattr(w, "value") else ""
            if raw in (Select.BLANK, getattr(Select, "NULL", None), None):
                raw = ""
            return str(raw).strip()

        def _reject(self, msg: str) -> None:
            """Flag a validation problem without dismissing the dialog."""
            self.app.bell()
            try:
                self.notify(msg, title="Can't save", severity="error", timeout=6)
            except Exception:
                pass

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "cancel":
                self.dismiss(None)
                return
            if event.button.id == "delete":
                self.dismiss({"__delete__": True})
                return
            name = self.query_one("#f-name", Input).value.strip()
            url = self.query_one("#f-url", Input).value.strip()
            source_cal = self._read_select("#f-srccal") or None
            # Mutually exclusive: a source calendar wins and we drop the URL so no
            # (possibly secret) link lingers in the config.
            if source_cal:
                url = ""
            dest = self._read_select("#f-dest")
            title = self.query_one("#f-title", Input).value.strip() or DEFAULT_TITLE

            def _int(widget_id):
                # Empty -> 0; a non-empty non-integer or a negative -> None (invalid),
                # so we can reject instead of silently zeroing the user's value.
                raw = self.query_one(widget_id, Input).value.strip()
                if raw == "":
                    return 0
                try:
                    v = int(raw)
                except ValueError:
                    return None
                return v if v >= 0 else None
            pad_b = _int("#f-padb")
            pad_a = _int("#f-pada")
            show_as = self.query_one("#f-showas", Select).value
            if show_as not in SHOW_AS:
                show_as = DEFAULT_SHOW_AS
            tz = self.query_one("#f-tz", Input).value.strip() or None

            def _csv(widget_id):
                raw = self.query_one(widget_id, Input).value
                return [t.strip() for t in raw.split(",") if t.strip()]
            include = _csv("#f-include")
            exclude = _csv("#f-exclude")
            min_minutes = _int("#f-min")
            copy_title = self.query_one("#f-copytitle", Switch).value
            copy_location = self.query_one("#f-copyloc", Switch).value
            skip_all_day = self.query_one("#f-skipallday", Switch).value
            skip_transparent = self.query_one("#f-skipfree", Switch).value
            enabled = self.query_one("#f-enabled", Switch).value

            # Validate before saving (mirror the CLI), with an inline error instead
            # of a silent save that only fails later during sync.
            if not name:
                self._reject("Name is required.")
                return
            if not dest:
                self._reject("Choose a destination calendar.")
                return
            if not (url or source_cal):
                self._reject("Provide an ICS link or pick a source calendar.")
                return
            for label, val in (("Buffer before", pad_b), ("Buffer after", pad_a),
                               ("Minimum duration", min_minutes)):
                if val is None:
                    self._reject(f"{label} must be a whole number ≥ 0.")
                    return
            if tz and _zone_from_name(tz) is None:
                self._reject(f"Unknown time zone {tz!r}. Use an IANA name "
                             "like 'Europe/Copenhagen'.")
                return
            result = dict(self.sync)   # keep hidden fields (back_days/forward_days)
            result.update({"name": name, "url": url, "source_cal": source_cal,
                           "dest": dest, "title": title,
                           "pad_before": pad_b, "pad_after": pad_a,
                           "show_as": show_as, "assume_tz": tz,
                           "include": include, "exclude": exclude,
                           "min_minutes": min_minutes, "copy_title": copy_title,
                           "copy_location": copy_location,
                           "skip_all_day": skip_all_day,
                           "skip_transparent": skip_transparent,
                           "enabled": enabled})
            # Final guard: catch any other normalize complaint (numbers, etc.).
            try:
                normalize_sync(dict(result))
            except ValueError as exc:
                self._reject(str(exc))
                return
            self.dismiss(result)

    MENU = [
        ("new",     "1", "Create new shadow",   "Add a new calendar sync"),
        ("edit",    "2", "Edit a shadow",       "Change an existing sync"),
        ("perm",    "3", "Calendar access",     "Grant access to Calendar (prompt)"),
        ("menubar", "4", "Menu-bar indicator",  "Turn the status icon on/off"),
        ("agent",   "5", "Sync agent",          "Turn the 15-min auto-sync on/off"),
        ("deps",    "6", "Install packages",    "pip install rumps, textual, …"),
    ]

    def _menu_text(meta, selected):
        _id, num, name, desc = meta
        t = Text()
        t.append("➤ " if selected else "  ", style="bold #7fd6c0")
        t.append(f"{num}. ", style="bold #7fd6c0")
        t.append(f"{name:<22}", style="bold" if selected else "")
        t.append(desc, style="#7fd6c0" if selected else "dim")
        return t

    class SyncPicker(ModalScreen):
        """Choose which shadow to edit."""

        BINDINGS = [Binding("escape", "cancel", "Cancel")]

        def action_cancel(self) -> None:
            self.dismiss(None)

        def __init__(self, syncs):
            super().__init__()
            self._syncs = syncs

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Label("Choose shadow", id="dlg-title")
                yield OptionList(
                    *[Option(f"{s.get('name', s.get('id'))}  —  "
                             f"{s.get('dest', '(no target)')}", id=s["id"])
                      for s in self._syncs], id="pick")
                with Horizontal(id="dlg-buttons"):
                    yield Button("Cancel", id="cancel")

        def on_mount(self) -> None:
            self.query_one("#pick", OptionList).focus()

        def on_option_list_option_selected(self, event) -> None:
            event.stop()
            self.dismiss(event.option_id)

        def on_button_pressed(self, event) -> None:
            self.dismiss(None)

    class ConfirmScreen(ModalScreen):
        """Generic two-choice confirmation; dismisses with yes_value/no_value."""

        BINDINGS = [Binding("escape", "cancel", "Cancel")]

        def __init__(self, title, message, yes_label, no_label,
                     yes_value="yes", no_value="no"):
            super().__init__()
            self._title = title
            self._message = message
            self._yes_label = yes_label
            self._no_label = no_label
            self._yes_value = yes_value
            self._no_value = no_value

        def action_cancel(self) -> None:
            self.dismiss(self._no_value)        # Esc = the safe (non-destructive) choice

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Label(self._title, id="dlg-title")
                yield Static(self._message)
                with Horizontal(id="dlg-buttons"):
                    yield Button(self._yes_label, variant="error", id="yes")
                    yield Button(self._no_label, variant="primary", id="no")

        def on_mount(self) -> None:
            self.query_one("#no", Button).focus()   # default highlight = keep/safe

        def on_button_pressed(self, event: Button.Pressed) -> None:
            self.dismiss(self._yes_value if event.button.id == "yes"
                         else self._no_value)

    class HomeApp(App):
        CSS = """
        #logo { color: #6b7a99; padding: 1 2 0 2; }
        #title { padding: 0 2; margin-top: 1; }
        #tagline { color: #86b384; padding: 0 2; margin: 1 0 0 0; }
        #credit { color: $text-muted; text-style: italic; padding: 0 2; margin: 1 0 2 0; }
        #menu { height: auto; border: none; background: transparent;
                padding: 1 2; margin-bottom: 1; }
        #sec-ind, #sec-syncs { color: $text-muted; text-style: bold;
                               padding: 0 2; margin-top: 1; }
        #indicators { padding: 1 2; }
        #syncs { height: auto; max-height: 12; margin: 1 2; }
        #status { color: $text-muted; padding: 0 2; height: 1; margin-top: 1; }
        #footer { color: $text-muted; padding: 1 2; margin-top: 1; }
        #dialog { width: 84; height: auto; max-height: 90%; padding: 1 2;
                  border: round $accent; background: $surface; }
        #dlg-scroll { height: auto; max-height: 30; }
        #dlg-title { text-style: bold; color: $accent; margin-bottom: 1; }
        #dlg-buttons { height: auto; margin-top: 1; }
        #dlg-buttons Button { margin-right: 2; }
        #pick { height: auto; max-height: 16; }
        #dlg-pads { height: auto; }
        #dlg-pads Vertical { height: auto; width: 1fr; }
        .switchrow { height: auto; }
        .switchrow Label { width: 1fr; padding: 1 0; }
        """
        BINDINGS = [
            Binding("1", "pick('new')", "", show=False),
            Binding("2", "pick('edit')", "", show=False),
            Binding("3", "pick('perm')", "", show=False),
            Binding("4", "pick('menubar')", "", show=False),
            Binding("5", "pick('agent')", "", show=False),
            Binding("6", "pick('deps')", "", show=False),
            Binding("r", "refresh", "Refresh", show=False),
            Binding("q", "quit", "Quit", show=False),
        ]

        def compose(self) -> ComposeResult:
            yield Static(LOGO, id="logo")
            title_parts = [("shadowcal", "bold #7fd6c0")]
            yield Static(Text.assemble(*title_parts), id="title")
            yield Static(DESCRIPTION, id="tagline")
            yield Static(CREDIT, id="credit")
            yield OptionList(
                *[Option(_menu_text(m, i == 0), id=m[0]) for i, m in enumerate(MENU)],
                id="menu")
            yield Static("INDICATORS", id="sec-ind")
            yield Static("", id="indicators", markup=True)
            yield Static("ACTIVE SHADOWS", id="sec-syncs")
            yield DataTable(id="syncs", cursor_type="none", zebra_stripes=True)
            yield Static("", id="status")
            yield Static(
                "↑↓ Select    ⏎ Run    1–6 Direct choice    r Refresh    q Quit",
                id="footer")

        def on_mount(self) -> None:
            t = self.query_one("#syncs", DataTable)
            t.add_columns("ID", "On", "Name", "Target", "Status", "Last")
            self.query_one("#menu", OptionList).focus()
            self.refresh_syncs()
            self.refresh_indicators()
            self.set_interval(10, self.refresh_indicators)

        # ---- menu: draw the arrow on the selected line ----
        def on_option_list_option_highlighted(self, event) -> None:
            try:
                if event.control.id != "menu":
                    return
                ol = self.query_one("#menu", OptionList)
                for i, m in enumerate(MENU):
                    ol.replace_option_prompt(m[0], _menu_text(m, i == event.option_index))
            except Exception:
                pass

        def on_option_list_option_selected(self, event) -> None:
            try:
                if event.control.id == "menu":
                    self.activate(event.option_id)
            except Exception:
                pass

        def action_pick(self, option_id: str) -> None:
            self.activate(option_id)

        def action_refresh(self) -> None:
            self.refresh_syncs()
            self.refresh_indicators()
            self.set_status("Refreshed.")

        def set_status(self, msg: str) -> None:
            self.query_one("#status", Static).update(msg)

        # ---- indicators ----
        def refresh_indicators(self) -> None:
            def dot(kind):
                return {"ok": "[green]●[/green]", "bad": "[red]●[/red]",
                        "unk": "[dim]●[/dim]"}[kind]
            pk, plabel = calendar_permission_status()
            perm = dot("ok" if pk == "ok" else ("unk" if pk == "unknown" else "bad"))

            def agent_line(loaded, ok_txt, bad_txt):
                if loaded is True:
                    return dot("ok"), ok_txt
                if loaded is False:
                    return dot("bad"), bad_txt
                return dot("unk"), "cannot be checked here"
            mb_dot, mb_txt = agent_line(launchd_loaded(menubar_agent_label()),
                                        "running (choose 4 to turn off)",
                                        "off (choose 4 to turn on)")
            ag_dot, ag_txt = agent_line(launchd_loaded(agent_label()),
                                        "running every 15 min (choose 5 to turn off)",
                                        "off (choose 5 to turn on)")
            self.query_one("#indicators", Static).update(
                f"{perm} Calendar access: {plabel}\n"
                f"{mb_dot} Menu-bar indicator: {mb_txt}\n"
                f"{ag_dot} Sync agent: {ag_txt}")

        # ---- active shadows ----
        def refresh_syncs(self) -> None:
            t = self.query_one("#syncs", DataTable)
            t.clear()
            cfg = load_config()
            state = load_state()
            syncs = cfg.get("syncs", [])
            if not syncs:
                t.add_row("—", "", "(no shadows yet — choose 1)", "", "", "")
                return
            for raw in syncs:
                s, err = _safe_normalize(raw)
                sid = (raw.get("id") if isinstance(raw, dict) else None) or "?"
                if err:
                    t.add_row(sid, "!", raw.get("name", sid), "(broken config)",
                              "broken", "", key=sid)
                    continue
                st = state.get(s["id"], {})
                t.add_row(
                    s["id"], "✓" if s["enabled"] else "·", s["name"],
                    (s["dest"][:18] + "…") if len(s["dest"]) > 19 else s["dest"],
                    st.get("status", "—"),
                    (st.get("last_run", "—") or "—")[:16],
                    key=s["id"])

        # ---- actions ----
        def activate(self, option_id: str) -> None:
            if option_id == "new":
                self.add_sync()
            elif option_id == "edit":
                self.edit_sync()
            elif option_id == "perm":
                self.run_cmd(["permissions"], "Requesting calendar access …",
                             "Calendar access updated.")
            elif option_id == "menubar":
                if launchd_loaded(menubar_agent_label()):
                    self.run_cmd(["uninstall-menubar-agent"],
                                 "Stopping menu-bar indicator …",
                                 "Menu-bar indicator stopped.")
                else:
                    self.run_cmd(["install-menubar-agent"],
                                 "Starting menu-bar indicator …",
                                 "Menu-bar indicator started.")
            elif option_id == "agent":
                if launchd_loaded(agent_label()):
                    self.run_cmd(["uninstall-agent"], "Stopping sync agent …",
                                 "Sync agent stopped.")
                else:
                    self.run_cmd(["install-agent"], "Starting sync agent …",
                                 "Sync agent started (every 15 min).")
            elif option_id == "deps":
                self.install_deps()

        def add_sync(self) -> None:
            def done(result):
                if result and not result.get("__delete__"):
                    cfg = load_config()
                    result = normalize_sync(result)
                    result["id"] = new_sync_id(cfg)
                    # Save DISABLED first, so the scheduled agent can't run it before
                    # the preview finishes. It's enabled only after a successful
                    # preview (Apply or Later); a failed preview leaves it disabled.
                    result["enabled"] = False
                    cfg.setdefault("syncs", []).append(result)
                    save_config(cfg)
                    self.refresh_syncs()
                    self.run_preview(result["id"], result.get("name", result["id"]))
            self.push_screen(EditScreen(None), done)

        def run_preview(self, sid, name) -> None:
            """Dry-run the new sync in a worker; result handled in
            on_worker_state_changed (worker name 'preview')."""
            self._preview = (sid, name)
            self.set_status(f"Dry-run preview for {sid} …")

            def work():
                try:
                    r = subprocess.run(
                        [sys.executable, os.path.abspath(__file__),
                         "sync", sid, "--dry-run"],
                        capture_output=True, text=True, timeout=600)
                    return (r.returncode, ((r.stdout or "") + (r.stderr or "")).strip())
                except Exception as exc:  # noqa: BLE001
                    return (1, str(exc))
            self.run_worker(work, thread=True, exclusive=True, name="preview")

        def edit_sync(self) -> None:
            cfg = load_config()
            # Use RAW syncs (not normalized) so a hand-edited broken sync can still
            # be listed and opened — to fix it. The editor reads fields with
            # defaults, and validates on save.
            syncs = [s for s in cfg.get("syncs", []) if s.get("id")]
            if not syncs:
                self.set_status("No shadows to edit — choose 1 to create one.")
                return

            def open_editor(sid):
                if not sid:
                    return
                c = load_config()
                sync = next((s for s in c.get("syncs", []) if s.get("id") == sid), None)
                if not sync:
                    return

                def done(result):
                    if not result:
                        return
                    if result.get("__delete__"):
                        # Use CLI 'remove' so the generated blocks are ALSO cleared
                        # from the calendar. Otherwise they would be orphaned (no
                        # sync owns them anymore -> never cleaned up).
                        self.run_cmd(
                            ["remove", sid],
                            f"Removing {sid} and clearing its blocks …",
                            f"Removed {sid} and its blocks.")
                        return
                    cc = load_config()
                    was_enabled = bool(sync.get("enabled", True))   # raw, never raises
                    disabling = was_enabled and not result.get("enabled", True)
                    to_save = dict(result)
                    if disabling:
                        # Defer the disabled state to the CLI 'disable' path, which
                        # removes blocks FIRST and aborts safely if that fails. Persist
                        # the other edits now but keep it enabled, so we never commit
                        # "disabled with orphaned blocks".
                        to_save["enabled"] = True
                    for i, s in enumerate(cc.get("syncs", [])):
                        if s.get("id") == sid:
                            s.update(to_save)
                            cc["syncs"][i] = s
                            break
                    save_config(cc)
                    self.refresh_syncs()
                    if disabling:
                        # Ask what to do with the blocks, then let 'disable' do it
                        # (delete = cleanup-then-disable; keep = disable --keep-blocks).
                        def after_choice(choice):
                            if choice == "delete":
                                self.run_cmd(
                                    ["disable", sid],
                                    f"Disabling {sid} and removing its blocks …",
                                    f"Disabled {sid} and removed its blocks.")
                            else:
                                self.run_cmd(
                                    ["disable", sid, "--keep-blocks"],
                                    f"Disabling {sid} (keeping its blocks) …",
                                    f"Disabled {sid}; its blocks were kept.")
                        self.push_screen(ConfirmScreen(
                            "Disable shadow",
                            "Delete the generated busy-blocks from the destination "
                            "calendar, or keep them?",
                            "Delete blocks", "Keep blocks",
                            yes_value="delete", no_value="keep"), after_choice)
                    else:
                        self.set_status(f"Saved {sid}.")
                # Open the RAW sync (not normalized) so a broken one can be fixed.
                self.push_screen(EditScreen(dict(sync)), done)

            if len(syncs) == 1:
                open_editor(syncs[0].get("id"))
            else:
                self.push_screen(SyncPicker(syncs), open_editor)

        # ---- run external commands in the background ----
        def run_cmd(self, args, busy_msg, done_msg) -> None:
            self.set_status(busy_msg)
            self._done_msg = done_msg

            def work():
                try:
                    r = subprocess.run(
                        [sys.executable, os.path.abspath(__file__)] + args,
                        capture_output=True, text=True, timeout=600)
                    return (r.returncode, ((r.stdout or "") + (r.stderr or "")).strip())
                except Exception as exc:  # noqa: BLE001
                    return (1, str(exc))
            self.run_worker(work, thread=True, exclusive=True, name="cmd")

        def install_deps(self) -> None:
            # Prefer pinned/known-good versions over blindly upgrading to whatever
            # is newest: install from requirements-lock.txt, else requirements.txt,
            # else the bare package list (without --upgrade) as a last resort.
            here = os.path.dirname(os.path.abspath(__file__))
            lock = os.path.join(here, "requirements-lock.txt")
            reqs = os.path.join(here, "requirements.txt")
            if os.path.exists(lock):
                pip_args = ["-r", lock]
            elif os.path.exists(reqs):
                pip_args = ["-r", reqs]
            else:
                pip_args = ["icalendar", "recurring-ical-events",
                            "pyobjc-framework-EventKit", "pyobjc-framework-Cocoa",
                            "textual", "rumps"]
            self.set_status("Installing packages (may take a moment) …")
            self._done_msg = "Packages installed."

            def work():
                try:
                    r = subprocess.run(
                        [sys.executable, "-m", "pip", "install"] + pip_args,
                        capture_output=True, text=True, timeout=900)
                    return (r.returncode, ((r.stdout or "") + (r.stderr or "")).strip())
                except Exception as exc:  # noqa: BLE001
                    return (1, str(exc))
            self.run_worker(work, thread=True, exclusive=True, name="cmd")

        def _handle_preview_result(self, event, WorkerState) -> None:
            sid, name = getattr(self, "_preview", ("", ""))
            if event.state == WorkerState.ERROR:
                self.set_status(f"Dry-run error: {event.worker.error}")
                return
            if event.state != WorkerState.SUCCESS:
                return
            rc, out = event.worker.result
            if rc != 0:
                # The sync was saved DISABLED; a failed preview just leaves it that
                # way, so the agent never touches it. User fixes it (edit) + enables.
                tail = (out.splitlines() or ["unknown error"])[-1]
                self.set_status(f"Created {sid} but the preview failed — left disabled. "
                                f"Edit to fix, then enable. ({tail[:80]})")
                self.refresh_syncs()
                return

            def _enable():
                cc = load_config()
                for s in cc.get("syncs", []):
                    if s.get("id") == sid:
                        s["enabled"] = True
                        break
                save_config(cc)
                self.refresh_syncs()

            nums = _parse_sync_summary(out)
            if nums:
                msg = (f"Dry-run for '{name}' — nothing written yet:\n\n"
                       f"  source events:  {nums['source']}\n"
                       f"  to create:      {nums['created']}\n"
                       f"  to update:      {nums['updated']}\n"
                       f"  to delete:      {nums['deleted']}\n\n"
                       "Apply now? (Otherwise it's enabled and runs on the next sync.)")
            else:
                msg = (f"Dry-run for '{name}' completed (nothing written yet).\n\n"
                       f"{out[-280:]}\n\n"
                       "Apply now? (Otherwise it's enabled and runs on the next sync.)")

            def after(choice):
                _enable()                        # preview succeeded -> safe to enable
                if choice == "apply":
                    self.run_cmd(["sync", sid], f"Syncing {sid} …",
                                 f"Synced {sid}.")
                else:
                    self.set_status(f"Enabled {sid}; it will sync on the next run.")
            self.push_screen(ConfirmScreen(
                "Apply new shadow?", msg, "Apply now", "Later",
                yes_value="apply", no_value="later"), after)

        def on_worker_state_changed(self, event) -> None:
            from textual.worker import WorkerState
            if event.worker.name == "preview":
                self._handle_preview_result(event, WorkerState)
                return
            if event.worker.name != "cmd":
                return
            if event.state == WorkerState.SUCCESS:
                rc, out = event.worker.result
                if rc == 0:
                    self.set_status(getattr(self, "_done_msg", "Done."))
                else:
                    tail = (out.splitlines() or ["unknown error"])[-1]
                    self.set_status(f"Error ({rc}): {tail[:120]}")
                self.refresh_indicators()
                self.refresh_syncs()
            elif event.state == WorkerState.ERROR:
                self.set_status(f"Error: {event.worker.error}")

    app = HomeApp()
    if _return_app:
        return app
    app.run()
    return 0


# ---------------------------------------------------------------------------
# Menu-bar app (macOS) — calendar icon with a status dot via rumps
# ---------------------------------------------------------------------------

def menubar_status() -> tuple[str, str, str, list[str]]:
    """
    Compute the overall status from state.json.
    Returns (state_key, header, last-ok-text, detail-lines).
    state_key: 'ok' | 'warn' | 'error' | 'idle'.
    """
    cfg = load_config()
    state = load_state()
    syncs = []
    broken: list[tuple[str, str]] = []          # (id, error) for un-normalizable syncs
    for r in cfg.get("syncs", []):
        if not r.get("enabled", True):
            continue
        n, err = _safe_normalize(r)
        if err:
            broken.append((r.get("id", "?"), err))
        else:
            syncs.append(n)
    now = dt.datetime.now(LOCAL_TZ)

    # Permission first: without it nothing works.
    perm_key, perm_label = calendar_permission_status()
    if perm_key in ("denied", "restricted"):
        return ("error", "Calendar access missing", "—",
                [f"Calendar access: {perm_label}"])
    if perm_key == "writeonly":
        return ("warn", "Write-only access", "—",
                [f"Calendar access: {perm_label}"])

    if not syncs and not broken:
        return ("idle", "No syncs configured", "—",
                ["Add a sync via the TUI or 'shadowcal add …'."])

    n_err = n_warn = n_stale = n_ok = 0
    latest_ok = None
    details: list[str] = []
    for sid, err in broken:                     # broken config counts as an error
        n_err += 1
        details.append(f"✗ {sid}: broken config — {str(err)[:80]}")
    for s in syncs:
        st = state.get(s["id"], {})
        status = st.get("status")
        last_ok = st.get("last_success")
        stale = False
        if last_ok:
            try:
                if now - dt.datetime.fromisoformat(last_ok) > dt.timedelta(hours=STALE_WARN_HOURS):
                    stale = True
                if latest_ok is None or last_ok > latest_ok:
                    latest_ok = last_ok
            except Exception:
                pass
        if status == "error":
            n_err += 1; mark = "✗"
        elif stale:
            n_stale += 1; mark = "✗"
        elif status == "warning":
            n_warn += 1; mark = "⚠"
        elif status == "ok":
            n_ok += 1; mark = "✓"
        else:
            mark = "•"
        line = f"{mark} {s['name']}: {status or 'not run yet'}"
        if st.get("error"):
            line += f" — {str(st['error'])[:80]}"
        elif stale:
            line += " — stale (no successful run in a while)"
        elif st.get("guard"):
            line += f" — {str(st['guard'])[:80]}"
        details.append(line)

    total = len(syncs) + len(broken)
    if n_err or n_stale:
        key, header = "error", f"Error in {n_err + n_stale} of {total} sync(s)"
    elif n_warn:
        key, header = "warn", f"Warning in {n_warn} of {total} sync(s)"
    elif n_ok:
        key, header = "ok", "All running"
    else:
        key, header = "idle", "Ready (no runs yet)"

    last_txt = "never"
    if latest_ok:
        try:
            age = now - dt.datetime.fromisoformat(latest_ok)
            mins = int(age.total_seconds() // 60)
            last_txt = f"{mins} min ago" if mins < 90 else f"{age.total_seconds()/3600:.1f} h ago"
        except Exception:
            pass
    return (key, header, last_txt, details)


def _is_dark_mode() -> bool:
    """True if the menu bar is currently in dark appearance."""
    try:
        import AppKit
        app = AppKit.NSApplication.sharedApplication()
        ap = app.effectiveAppearance()
        name = ap.bestMatchFromAppearancesWithNames_(
            [AppKit.NSAppearanceNameAqua, AppKit.NSAppearanceNameDarkAqua])
        return str(name) == str(AppKit.NSAppearanceNameDarkAqua)
    except Exception:
        return False


def _render_icon_png(state_key: str, blink_on: bool, white: bool = True,
                     size: int = 20) -> str:
    """Draw a small calendar icon with a status dot and save as PNG. Return path.

    `white` chooses the glyph (outline) color: white when True, black when False.
    The status dot keeps its own color (green/orange/red); 'idle' follows the glyph.
    """
    import AppKit
    from Foundation import NSMakeRect, NSMakeSize

    NSColor = AppKit.NSColor
    NSBezierPath = AppKit.NSBezierPath
    fg = NSColor.whiteColor() if white else NSColor.blackColor()

    img = AppKit.NSImage.alloc().initWithSize_(NSMakeSize(size, size))
    img.lockFocus()
    try:
        fg.set()
        pad, top_gap = 2.0, 1.0
        body = NSMakeRect(pad, pad, size - 2 * pad, size - 2 * pad - top_gap)
        p = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(body, 3.0, 3.0)
        p.setLineWidth_(1.6)
        p.stroke()
        bandh = 3.5                          # the calendar's "header" bar
        band = NSMakeRect(body.origin.x + 0.8,
                          body.origin.y + body.size.height - bandh,
                          body.size.width - 1.6, bandh)
        NSBezierPath.bezierPathWithRect_(band).fill()

        colors = {
            "ok": NSColor.systemGreenColor(),
            "warn": NSColor.systemOrangeColor(),
            "error": NSColor.systemRedColor(),
            "idle": fg.colorWithAlphaComponent_(0.4),
        }
        show_dot = not (state_key == "error" and not blink_on)   # blink: hide in the off phase
        if show_dot:
            d = 8.0
            dot = NSMakeRect((size - d) / 2.0, (size - d) / 2.0 - 1.0, d, d)
            colors.get(state_key, fg).set()
            NSBezierPath.bezierPathWithOvalInRect_(dot).fill()
    finally:
        img.unlockFocus()
    img.setTemplate_(False)

    tiff = img.TIFFRepresentation()
    rep = AppKit.NSBitmapImageRep.imageRepWithData_(tiff)
    png_type = getattr(AppKit, "NSBitmapImageFileTypePNG", 4)
    data = rep.representationUsingType_properties_(png_type, {})
    _ensure_dir()
    theme = "white" if white else "black"
    path = str(CONFIG_DIR /
               f"menubar_{state_key}_{'on' if blink_on else 'off'}_{theme}.png")
    data.writeToFile_atomically_(path, True)
    return path


def _run_menubar(force_emoji: bool = False) -> int:
    try:
        import rumps
    except Exception:
        print("The menu-bar app requires 'rumps' (pip install rumps). "
              "All other commands work without it.", file=sys.stderr)
        return 1
    import threading

    EMOJI = {"error": ("🔴", "⚫"), "warn": ("🟠", "🟠"),
             "ok": ("🟢", "🟢"), "idle": ("⚪", "⚪")}

    class Bar(rumps.App):
        def __init__(self):
            super().__init__(APP, quit_button=None)
            self._blink = True
            self._rendered = None
            self._state = None
            self._details: list[str] = []
            self._emoji = force_emoji
            # Glyph color: "auto" (white in Dark Mode, black in Light), "white", or
            # "black". Set "menubar_icon" in config.json — handy when a translucent
            # menu bar looks dark even in Light Mode and the black glyph vanishes.
            self._icon_color = (load_config().get("menubar_icon") or "auto").lower()
            self._icon_cache: dict = {}      # (state, blink, white) -> png path
            self.hdr = rumps.MenuItem("…")
            self.last = rumps.MenuItem("Last successful sync: …")
            self.menu = [
                self.hdr, self.last, None,
                rumps.MenuItem("Sync now", callback=self.sync_now),
                rumps.MenuItem("Show details…", callback=self.show_details),
                rumps.MenuItem("Open log folder", callback=self.open_logs),
                rumps.MenuItem("About shadowcal…", callback=self.show_about),
                None,
                rumps.MenuItem("Quit", callback=rumps.quit_application),
            ]
            self.refresh(None)
            self._t_blink = rumps.Timer(self.blink, 0.6)
            self._t_blink.start()
            self._t_refresh = rumps.Timer(self.refresh, 5)
            self._t_refresh.start()
            self._observe_theme_changes()

        def _observe_theme_changes(self):
            """Redraw the icon the instant macOS switches light/dark appearance.

            The 5-sec refresh is the backstop; this makes the switch immediate.
            The block runs on the main queue, so the UI update is safe.
            """
            self._theme_obs = None
            try:
                from Foundation import (NSDistributedNotificationCenter,
                                        NSOperationQueue)
                self._theme_obs = NSDistributedNotificationCenter.defaultCenter() \
                    .addObserverForName_object_queue_usingBlock_(
                        "AppleInterfaceThemeChangedNotification", None,
                        NSOperationQueue.mainQueue(),
                        lambda _note: self._on_theme_change())
            except Exception:
                pass

        def _on_theme_change(self):
            try:
                self._rendered = None       # force a re-render with the new appearance
                self.apply_icon()
            except Exception:
                pass

        def blink(self, _timer):
            if self._state == "error":
                self._blink = not self._blink
                self.apply_icon()

        def refresh(self, _timer):
            try:
                state_key, header, last_txt, details = menubar_status()
            except Exception as exc:                 # never let the timer die
                state_key, header, last_txt, details = "error", f"Status error: {exc}", "—", []
            # Pick up a live change to the icon-color preference (no restart needed).
            self._icon_color = (load_config().get("menubar_icon") or "auto").lower()
            prev = self._state
            self._state, self._details = state_key, details
            mark = {"error": "✗", "warn": "⚠", "ok": "✓", "idle": "•"}[state_key]
            self.hdr.title = f"{mark} {header}"
            self.last.title = f"Last successful sync: {last_txt}"
            self._blink = True
            self.apply_icon()
            if state_key == "error" and prev not in (None, "error"):
                try:
                    rumps.notification(APP, "Something is wrong with the shadow sync", header)
                except Exception:
                    pass

        def apply_icon(self):
            if self._icon_color == "white":
                white = True
            elif self._icon_color == "black":
                white = False
            else:                                # "auto": match the system appearance
                white = _is_dark_mode()
            key = (self._state, self._blink, self._emoji, white)
            if key == self._rendered:
                return
            self._rendered = key
            if not self._emoji:
                try:
                    self.template = False
                    ck = (self._state, self._blink, white)
                    if ck not in self._icon_cache:   # avoid a disk write on every blink
                        self._icon_cache[ck] = _render_icon_png(
                            self._state, self._blink, white)
                    self.icon = self._icon_cache[ck]
                    self.title = None
                    return
                except Exception:
                    self._emoji = True          # drawing failed -> emoji fallback
            on, off = EMOJI.get(self._state, ("⚪", "⚪"))
            self.icon = None
            self.title = on if self._blink else off

        def sync_now(self, _item):
            self.hdr.title = "… Syncing …"

            def work():
                try:
                    subprocess.run([sys.executable, os.path.abspath(__file__), "sync"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=300)
                except Exception:
                    pass
            threading.Thread(target=work, daemon=True).start()

        def show_details(self, _item):
            try:
                rumps.alert(title=f"{APP} — status",
                            message="\n".join(self._details or ["—"]), ok="OK")
            except Exception:
                pass

        def open_logs(self, _item):
            subprocess.run(["open", str(CONFIG_DIR)], stderr=subprocess.DEVNULL)

        def show_about(self, _item):
            try:
                rumps.alert(title=f"{APP}", message=f"{DESCRIPTION}\n\n{CREDIT}", ok="OK")
            except Exception:
                pass

    Bar().run()
    return 0


def menubar_agent_label() -> str:
    return f"com.{getpass.getuser()}.{APP}.menubar"


def menubar_plist_path() -> Path:
    return Path(os.path.expanduser(f"~/Library/LaunchAgents/{menubar_agent_label()}.plist"))


def write_menubar_agent() -> Path:
    _ensure_dir()
    py = _xml_escape(sys.executable)
    script = _xml_escape(os.path.abspath(__file__))
    label = _xml_escape(menubar_agent_label())
    out = _xml_escape(str(CONFIG_DIR / "menubar.out.log"))
    err = _xml_escape(str(CONFIG_DIR / "menubar.err.log"))
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{py}</string>
        <string>{script}</string>
        <string>menubar</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict><key>SuccessfulExit</key><false/></dict>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>StandardOutPath</key>
    <string>{out}</string>
    <key>StandardErrorPath</key>
    <string>{err}</string>
</dict>
</plist>
"""
    path = menubar_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plist, "utf-8")
    return path


def cmd_menubar(args) -> int:
    return _run_menubar(force_emoji=getattr(args, "emoji", False))


def cmd_install_menubar_agent(_args) -> int:
    path = write_menubar_agent()
    label = menubar_agent_label()
    subprocess.run(["launchctl", "unload", str(path)], stderr=subprocess.DEVNULL)
    rc = subprocess.run(["launchctl", "load", "-w", str(path)])
    print(f"Wrote {path}")
    if rc.returncode == 0:
        print(f"The menu-bar app now runs (and starts at login) as {label}.")
    else:
        print("Wrote the plist, but 'launchctl load' failed — try: "
              f"launchctl load -w {path}")
    return 0


def cmd_uninstall_menubar_agent(_args) -> int:
    path = menubar_plist_path()
    subprocess.run(["launchctl", "unload", str(path)], stderr=subprocess.DEVNULL)
    if path.exists():
        path.unlink()
        print(f"Removed {path}")
    else:
        print("No menu-bar agent installed.")
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _non_negative_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer")
    if n < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return n


def _positive_int(value: str) -> int:
    n = _non_negative_int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return n


def _iana_tz(value: str) -> str:
    if _zone_from_name(value) is None:
        raise argparse.ArgumentTypeError(f"unknown IANA time zone: {value!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=APP, description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("tui", help="Open the TUI").set_defaults(func=cmd_tui)
    pl = sub.add_parser("list", help="Show syncs")
    pl.add_argument("--show-secrets", action="store_true", dest="show_secrets",
                    help="Show full ICS links (otherwise secret links are masked)")
    pl.set_defaults(func=cmd_list)
    sub.add_parser("status", help="Short health overview (script-friendly)"
                   ).set_defaults(func=cmd_status)
    sub.add_parser("calendars", help="Show writable destination calendars"
                   ).set_defaults(func=cmd_calendars)
    sub.add_parser("permissions", help="Request calendar access and show status"
                   ).set_defaults(func=cmd_permissions)

    ps = sub.add_parser("sync", help="Run syncs")
    ps.add_argument("id", nargs="?", help="Only this sync")
    ps.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="Show what would happen — write nothing")
    ps.add_argument("--force", action="store_true",
                    help="Override the safety guard against mass deletion")
    ps.add_argument("--once", action="store_true", help="(no effect; for readability)")
    ps.set_defaults(func=cmd_sync)

    pa = sub.add_parser("add", help="Add a sync")
    pa.add_argument("--name", required=True)
    # Exactly one source: --url OR --source-cal (mutually exclusive; cmd_add also
    # enforces that at least one is given).
    src_grp = pa.add_mutually_exclusive_group()
    src_grp.add_argument("--url", help="ICS link (source)")
    src_grp.add_argument("--source-cal", dest="source_cal", default=None,
                         help="Use an already-subscribed calendar as the source "
                              "(identifier or title), instead of an ICS link")
    pa.add_argument("--dest", required=True,
                    help="calendarIdentifier or title of the destination calendar")
    pa.add_argument("--title", default=DEFAULT_TITLE)
    pa.add_argument("--back-days", type=_non_negative_int, default=DEFAULT_BACK_DAYS,
                    dest="back_days")
    pa.add_argument("--forward-days", type=_non_negative_int, default=DEFAULT_FORWARD_DAYS,
                    dest="forward_days")
    pa.add_argument("--include-all-day", action="store_true",
                    help="Include all-day events (otherwise they are skipped)")
    pa.add_argument("--include-free", action="store_true",
                    help="Include events marked as free (TRANSPARENT)")
    pa.add_argument("--pad-before", type=_non_negative_int, default=0, dest="pad_before",
                    help="Buffer in minutes BEFORE each block")
    pa.add_argument("--pad-after", type=_non_negative_int, default=0, dest="pad_after",
                    help="Buffer in minutes AFTER each block")
    pa.add_argument("--show-as", default=DEFAULT_SHOW_AS, dest="show_as",
                    choices=SHOW_AS, help="How the block appears in others' lookups")
    pa.add_argument("--include", action="append",
                    help="Only titles containing this word (repeatable)")
    pa.add_argument("--exclude", action="append",
                    help="Skip titles containing this word (repeatable)")
    pa.add_argument("--min-minutes", type=_non_negative_int, default=0, dest="min_minutes",
                    help="Skip events shorter than this")
    pa.add_argument("--copy-title", action="store_true", dest="copy_title",
                    help="Show the source's own title (NOT ADVISED for sensitive calendars)")
    pa.add_argument("--copy-location", action="store_true", dest="copy_location",
                    help="Copy the location / meeting link (Teams, Zoom) onto the block")
    pa.add_argument("--allow-insecure-http", action="store_true",
                    dest="allow_insecure_http",
                    help="Permit a plain http:// source link (NOT advised — the secret "
                         "token is sent in clear text). Default: https/webcal only.")
    pa.add_argument("--tz", type=_iana_tz, default=None,
                    help="IANA zone assumed for FLOATING times without a zone "
                         "(e.g. Europe/Copenhagen). Does not affect times with their own zone.")
    pa.set_defaults(func=cmd_add)

    pr = sub.add_parser("remove", help="Remove a sync (and its blocks)")
    pr.add_argument("id")
    pr.add_argument("--force-remove-config", action="store_true",
                    dest="force_remove_config",
                    help="Drop the sync config even if block cleanup fails "
                         "(leftover blocks must then be deleted by hand)")
    pr.set_defaults(func=cmd_remove)

    pc = sub.add_parser("cleanup", help="Remove a sync's blocks but keep the configuration")
    pc.add_argument("id")
    pc.set_defaults(func=cmd_cleanup)

    pd = sub.add_parser("disable", help="Disable a sync and remove its blocks")
    pd.add_argument("id")
    pd.add_argument("--keep-blocks", action="store_true",
                    help="Disable without removing existing blocks")
    pd.set_defaults(func=cmd_disable)

    pe = sub.add_parser("enable", help="Enable a disabled sync")
    pe.add_argument("id")
    pe.set_defaults(func=cmd_enable)

    pt = sub.add_parser("test", help="Test a source (id or URL) + time-zone diagnosis")
    pt.add_argument("target")
    pt.add_argument("--tz", type=_iana_tz, default=None,
                    help="Assume this IANA zone for floating times in this test")
    pt.set_defaults(func=cmd_test)

    pi = sub.add_parser("install-agent", help="Install the launchd job")
    pi.add_argument("--interval", type=_positive_int, default=900,
                    help="Seconds between runs (default 900 = 15 min.)")
    pi.set_defaults(func=cmd_install_agent)

    sub.add_parser("uninstall-agent", help="Remove the launchd job"
                   ).set_defaults(func=cmd_uninstall_agent)

    pm = sub.add_parser("menubar", help="Run the menu-bar app (calendar icon with status dot)")
    pm.add_argument("--emoji", action="store_true",
                    help="Use an emoji dot instead of the drawn calendar icon")
    pm.set_defaults(func=cmd_menubar)

    sub.add_parser("install-menubar-agent",
                   help="Start the menu-bar app and run it automatically at login"
                   ).set_defaults(func=cmd_install_menubar_agent)
    sub.add_parser("uninstall-menubar-agent", help="Remove the menu-bar agent"
                   ).set_defaults(func=cmd_uninstall_menubar_agent)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if not getattr(args, "func", None):
            return cmd_tui(args)        # default: open the TUI
        return args.func(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
