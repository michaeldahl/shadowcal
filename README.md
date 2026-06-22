# shadowcal

A small TUI/CLI tool for your Mac that mirrors generic **"busy" shadows**
from your private ICS calendars into **another calendar** via **Apple Calendar (EventKit)**.

Purpose: others see you as busy at the times when you have private appointments —
without being able to see *what* the appointments are about.

## Why this, and not a cloud service

Many cloud-based bridges write the blocks into the target via an **OAuth/Graph
connection** to the target account — exactly the kind of third-party access many
workplaces lock down. `shadowcal` instead does it **locally as you** through the
native client: the source is ICS links (or a calendar you already subscribe to),
the target is a calendar already added in Apple Calendar. Only blocks are *written*
in — no data from the target account leaves the machine, and there is no third-party
app to revoke.

> A plain ICS *subscription* in the target client often won't do: subscribed
> internet calendars frequently don't show up in free/busy lookups and therefore
> don't block booking. The blocks must be real events in the target calendar
> itself — that's what `shadowcal` takes care of.

## Installation

Requires Python 3.10+.

```bash
cd ~/wherever-you-want-it
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt          # looser minimums
# …or, for a reproducible install (recommended when sharing with colleagues):
pip install -r requirements-lock.txt     # exact, tested versions
```

`requirements.txt` lists loose minimums; `requirements-lock.txt` pins the exact versions
shadowcal was tested against. The TUI's **Install packages** action installs from the lock
file when present (it no longer blindly upgrades to the newest releases).

(For a short command, add e.g. `alias shadowcal='~/path/.venv/bin/python ~/path/shadowcal.py'`
to your `~/.zshrc`.)

## 1) Grant calendar access (most important step)

The first time `shadowcal` touches the calendar, macOS asks for permission. So run
it once **interactively in Terminal** so you can click "Allow":

```bash
python3 shadowcal.py calendars
```

If you get no prompt (or a `PermissionError`), go to
**System Settings → Privacy & Security → Calendars** and enable access for Terminal
(or the Python you run). The launchd job uses the same Python binary and inherits the
permission. The command also lists your writable calendars and their `identifier` —
find your target calendar in the list.

## 2) Pick a source

You have two options (pick **one** — a sync uses either an ICS link *or* a source
calendar, never both; and the source must not be the same calendar as the target):

- **An ICS link.** Any `https://…ics` or `webcal://…` link works (`shadowcal`
  converts `webcal://` to `https://` itself). For example:
  - **iCloud:** Calendar app → hover the calendar → share icon → *Public Calendar* →
    copy the link.
  - **Proton Calendar / others:** Settings → share the calendar *via link* → copy the
    **secret** ICS link (read-only, includes details).
- **A calendar you already subscribe to in Apple Calendar.** If the ICS is already
  added to Calendar.app, you can shadow it directly with no extra link — pick it as
  the **source calendar** (in the TUI, or `--source-cal <identifier|title>` on the
  CLI). Recurrence is expanded natively by EventKit.

Test an ICS link before wiring it up:

```bash
python3 shadowcal.py test "https://…/my-calendar.ics"
```

## 3) Create a sync

In the TUI (recommended):

```bash
python3 shadowcal.py
```

The home screen is menu-driven: a small ASCII logo, a short description of what the
app does, and an **action menu**:

```
➤ 1. Create new shadow      Add a new calendar sync
  2. Edit a shadow          Change an existing sync
  3. Calendar access        Grant access to Calendar (prompt)
  4. Menu-bar indicator     Turn the status icon on/off
  5. Sync agent             Turn the 15-min auto-sync on/off
  6. Install packages       pip install rumps, textual, …
```

Use `↑↓` and `⏎`, or press the number `1`–`6` directly. Each item runs the necessary
command behind the scenes, so you don't have to remember them.

Below the menu are **indicators**:

```
● Calendar access: full access
● Menu-bar indicator: running (choose 4 to turn off)
● Sync agent: running every 15 min (choose 5 to turn off)
```

Green = OK, red = missing/off (with a hint to which menu item toggles it), grey =
can't determine. They update continuously. Items **4** and **5** are toggles: if the
agent/indicator is already running, choosing it again turns it off.

At the bottom are **Active shadows** — a list of your syncs with status and last run.

The edit dialog (menu item 2) covers **all** settings: source (ICS link *or* a
subscribed calendar), target, title, buffer, show-as, assumed time zone, keyword
filters, minimum duration, copy-title, copy-location, and the all-day/free filters —
and has a **Delete shadow** button. Delete removes both the configuration *and* the
generated blocks from the calendar (same cleanup as `shadowcal remove`), so no
orphaned "busy" blocks are left behind. `Esc` closes any dialog.

When you **create a new shadow** in the TUI, it doesn't write to your calendar
immediately. It's saved **disabled**, then a **dry-run preview** runs and shows how many
blocks it would create / update / delete, and asks **Apply now** or **Later**. Either
choice **enables** it (Apply syncs immediately; Later leaves it for the next agent run).
If the preview *fails* (e.g. an unreachable source), the shadow stays **disabled** so the
agent never touches it — fix it (edit) and enable. (Creating via the CLI `add` never syncs
on its own — run `shadowcal sync` or wait for the agent.)

A sync with a hand-edited bad value (e.g. an invalid timezone) is shown as **broken** in
`list`/`status`/the TUI and skipped by scheduled runs, without breaking the others; open
it in the TUI editor to fix it.

Or from the command line:

```bash
python3 shadowcal.py add \
  --name "Private" \
  --url "https://…/private.ics" \
  --dest "YOUR-TARGET-CALENDAR-IDENTIFIER"

# …or shadow a calendar you already subscribe to:
python3 shadowcal.py add \
  --name "Board" \
  --source-cal "Board (subscribed)" \
  --dest "YOUR-TARGET-CALENDAR-IDENTIFIER"
```

You can make several syncs (e.g. one "Private" and one "Board") each with its own
title — all typically pointing at the same target calendar. `shadowcal` only tracks
its own blocks per sync, so they don't collide and your real meetings are never
touched.

## 4) Run automatically (your Mac is on anyway)

```bash
python3 shadowcal.py install-agent          # every 15 min.
python3 shadowcal.py install-agent --interval 600   # or every 10 min.
```

It writes a launchd agent to `~/Library/LaunchAgents/com.<user>.shadowcal.plist`
and loads it. Stop/remove again with:

```bash
python3 shadowcal.py uninstall-agent
```

(Or just toggle it from the TUI with menu item **5**.)

Logs live in `~/.config/shadowcal/` (`shadowcal.out.log`, `shadowcal.err.log`), and
per-sync status in `state.json`. Set `SHADOWCAL_DEBUG=1` for full stack traces in the
error log while troubleshooting.

If the agent, a manual `shadowcal sync`, and the menu bar's **Sync now** happen to run
at the same time, `shadowcal` takes an exclusive lock (`sync.lock`): the extra run
skips gracefully instead of risking duplicate blocks.

Disable a sync without forgetting its setup:

```bash
python3 shadowcal.py disable sync1          # stops the sync and removes its blocks
python3 shadowcal.py disable sync1 --keep-blocks   # disable but leave blocks
python3 shadowcal.py enable sync1           # turn it back on
python3 shadowcal.py cleanup sync1          # remove blocks, keep the configuration
```

`remove` deletes the generated blocks *first* and only then drops the config. If that
cleanup fails (e.g. no calendar access), it **aborts and keeps the config** so the
blocks can still be cleaned up later — use `remove --force-remove-config` to drop the
config anyway (leftover blocks then have to be deleted by hand).

`disable` behaves the same way: it removes the blocks before marking the sync disabled
and **aborts cleanly** if that fails (the sync stays enabled so nothing is orphaned) —
use `disable <id> --keep-blocks` to disable without removing them. In the **TUI**,
turning a shadow off (the *Enabled* switch) pops up a dialog asking whether to **delete**
the generated blocks from the destination or **keep** them.

## Per-sync options

Set as flags on `add` (or edited in the TUI / directly in `config.json`):

- `--source-cal ID` — use an already-subscribed Apple Calendar as the source instead
  of an ICS link (identifier or title).
- `--back-days N` / `--forward-days N` — how far back / ahead to mirror (default `7` /
  `365`). This is also the window reconciliation acts within: a source event you delete
  *outside* this window won't be auto-removed — widen the window, or use `remove`/`cleanup`
  (which sweep a much wider range).
- `--title TEXT` — the text shown on each block (default `Private – busy`). Ignored when
  `--copy-title` is on.
- `--pad-before N` / `--pad-after N` — buffer in minutes before/after each block, so no
  one books right up against e.g. a physical meeting. Doesn't affect dedup: the key is
  built from the source's original start time, so the buffer can change without churn.
- `--show-as busy|tentative|oof|free` — how the block counts in others' lookups.
  `oof` (out of office) is the strongest; `free` doesn't block (special cases only).
- `--include WORD` / `--exclude WORD` — include only / skip titles containing the word
  (repeatable). For feeds with mixed content.
- `--min-minutes N` — skip very short appointments.
- `--include-all-day` / `--include-free` — by default all-day events and events marked
  "free"/transparent are skipped; these include them.
- `--copy-title` — show the source's own title instead of "Private – busy".
  **Not advised** for private/sensitive calendars; only for non-sensitive feeds. `add`
  prints an explicit warning when you enable it, as a reminder to set the calendar's
  sharing to free/busy only.
- `--allow-insecure-http` — permit a plain `http://` source link. **Off by default**:
  ICS links often carry a secret token, so shadowcal accepts only `https`/`webcal` and
  rejects `http://` (which would send the token in clear text) unless you opt in here.
- `--copy-location` — copy the **location** and any **meeting link** onto the block, so
  you keep the address / join link. Off by default for privacy. Only a **recognized
  meeting link** is copied — a host on the allowlist (`teams.microsoft.com`, `zoom.us`,
  `meet.google.com`, `webex.com`, …) found in the source's URL, location, or description.
  An arbitrary `URL:` field (e.g. a private agenda/document link) is **never** copied, and
  nothing is ever written into the hidden marker note.

Reminders are always stripped from the generated blocks, so you don't get
notifications about your own shadowcal events.

## Security and health checks

- **Empty-source / mass-delete guard.** If an ICS link returns empty one day (server
  error, expired link), a naive sync would delete *all* your blocks and leave you free.
  `shadowcal` refuses to delete if the source is empty, or if the deletions would
  remove more than half of your blocks at once — it flags it instead and sends a macOS
  notification. If it's a deliberate cleanup, run `shadowcal sync <id> --force`.
  The thresholds can be tuned under `"safety"` in `config.json`.
- **`shadowcal sync --dry-run`** — show exactly what would be created/updated/deleted
  without writing anything. Use it on first setup and when you change filters/buffer.
- **`shadowcal status`** — short overview: most recent *successful* run per sync, with a
  warning if it's stale (> 3 hours). Returns an exit code (0/1/2), so it can be used in
  scripts or a menu-bar indicator.
- **Notifications.** Errors and tripped guards trigger a macOS notification, so a sync
  that fails silently doesn't give you false confidence.
- **Protected secret links.** `config.json` holds your secret ICS links (effectively
  access keys to your private calendars). The folder `~/.config/shadowcal/` is written as
  `0700` and the files as `0600` — only your own user can read them; an older, looser file
  is also tightened the next time it's read. `shadowcal list` masks links to
  `scheme://host/…` (no path/query, since some providers put the secret in the path); use
  `shadowcal list --show-secrets` to reveal the full URLs.
- **Robust writes.** `config.json` and `state.json` are written atomically (temp file +
  `os.replace`), so an interrupted or concurrent write never leaves a half, corrupt
  file. If a file can't be read as JSON anyway (e.g. after a hand-edit mistake), it's
  moved aside to `*.bad` with a warning instead of being silently lost.
- **Hardened source fetching.** Only `https`/`webcal` links are accepted by default —
  `file://` etc. can't read local files, and plain `http://` is refused (it would send
  the secret token in clear text) unless you opt in with `--allow-insecure-http`. A
  redirect that would **downgrade** an `https` link to `http` is also refused (so the
  token can't leak via a sneaky 302).
  Responses over 25 MB are rejected, and feeds that would explode under recurrence
  expansion (dense `BY*` rules and large `RDATE` lists) are pre-flighted and capped
  *before* expansion. A `COUNT` is measured against the actual sync window, so an old
  feed with a huge `COUNT` but few in-window occurrences isn't needlessly rejected.
  Source **calendars** are read in ~120-day chunks (not all at once) with the same cap,
  so a very busy source can't spike memory. Secret links are redacted from error logs in
  every form (e.g. `webcal://` and its normalized `https://`).
- **One bad sync can't break the rest.** A sync with an invalid saved value (e.g. a
  hand-edited bad timezone) is reported as broken on its own line and skipped — `list`,
  `status`, the menu bar, and a full `sync` run all keep working for the others.
- **No feedback loops from a source calendar.** When the source is a subscribed
  calendar, `shadowcal` skips any event that already carries a shadowcal marker (so its
  own generated blocks are never mirrored back in), and it refuses to use the same
  calendar as both source and target.
- **Meeting-link safety.** A copied meeting link must have a real meeting hostname
  (e.g. `teams.microsoft.com`, `zoom.us`); a decoy like `https://evil.com/?x=zoom.us`
  is rejected.

## Menu-bar app (macOS)

A small calendar icon in the menu bar shows status at a glance:

- **Green dot** — all running (all enabled syncs most recently OK).
- **Blinking red dot** — something is wrong: a sync failed, or there hasn't been a
  successful run in a while (stale).
- **Orange dot** — warning (e.g. the empty-source guard tripped).
- **Grey dot** — no syncs yet / not run.

The icon is drawn as a small calendar with the dot in the middle. The glyph **follows
the system appearance** — dark in Light Mode, white in Dark Mode — and flips the
**instant** you switch themes (it subscribes to the macOS theme-change notification, with
a periodic refresh as backstop). If drawing fails it automatically falls back to a
colored emoji dot (or force that with `--emoji`).

If a translucent menu bar looks dark even in Light Mode (so the black glyph is hard to
see), pin the glyph color: set `"menubar_icon": "white"` (or `"black"`, or `"auto"`, the
default) in `config.json`. The change is picked up live, no restart needed.

Click the icon for a menu: overall status, time of the last successful sync, **Sync
now**, **Show details…** (status per sync), **Open log folder**, **About shadowcal…**,
and **Quit**. If calendar access is missing, the dot turns red with a clear message —
so a silent permission problem doesn't go unnoticed.

Run it:

```bash
pip install rumps          # if not already installed
python3 shadowcal.py menubar
```

Start it automatically at login (or toggle it from the TUI with menu item **4**):

```bash
python3 shadowcal.py install-menubar-agent     # runs continuously, restarts at login
python3 shadowcal.py uninstall-menubar-agent    # stop again
```

The app and the synchronization are two separate processes: the menu-bar app only
*reads* status from `state.json`, while the launchd job (`install-agent`) does the
actual work every 15 minutes. Both should run. Tip: run one sync first (or click
**Sync now**) so the dot has a real status to show.

## Time zones (important for floating times)

Times are handled at the *instant* level, so a block always lands at the right time:

- **Times with their own zone** (ICS with `TZID=Europe/Copenhagen` + VTIMEZONE, or UTC
  `Z`) are interpreted precisely, including **DST per occurrence** — e.g. a weekly
  09:00 meeting stays at 09:00 local across the DST transition, while the UTC offset
  shifts from +01 to +02. Microsoft-named zones ("W. Europe Standard Time") are also
  recognized.
- **Floating times** (no zone in the source) are the only ambiguous type. They are
  interpreted in your **DST-aware local zone** (not a fixed offset, so a floating
  winter time synced in summer doesn't end up an hour wrong). To force a specific zone
  for a source — e.g. if the source sends floating times that should be a particular
  zone — set: `--tz Europe/Copenhagen` on `add` (or the "Assume time zone" field in the
  TUI).
- Each block is pinned to the source's zone in Apple Calendar, so it doesn't drift if
  your Mac changes time zone (travel).

**Verify your feed:** `shadowcal test <id|url>` shows each event with local time, UTC
time, offset, and zone — and **explicitly warns** about floating times. Run it across a
DST transition and confirm the offset shifts correctly. It's the fastest way to catch a
time-zone problem before it hits your calendar.

## Privacy

- Keep the **default sharing** on your target calendar at *free/busy only*, so others
  can't see titles. Use generic titles (`Private – busy`) just in case.
- All-day events and events marked "free" are skipped by default (enable with
  `--include-all-day` / `--include-free`).
- `--copy-title` and `--copy-location` move real details into the target calendar — use
  them only when that calendar's sharing is free/busy only.

## First-run rehearsal (do this before trusting it)

The sync logic is unit-tested, but EventKit, Calendar permissions, launchd, and iCloud /
Exchange / Google account behavior differ from machine to machine. So rehearse once on a
**disposable destination calendar** before pointing it at anything you care about:

1. In Apple Calendar, make a throwaway calendar (e.g. "shadowcal-test") to be the target.
2. Grant access: `shadowcal calendars` (click **Allow**); note your test calendar's id.
3. Add **one** sync against a low-stakes source (an ICS link *or* a subscribed calendar)
   targeting the test calendar.
4. Preview, don't write yet: `shadowcal sync <id> --dry-run` — check the create/update/delete
   counts look sane.
5. Apply for real: `shadowcal sync <id>` — confirm the busy-blocks appear correctly
   (times, DST, that titles/locations are hidden unless you enabled copying).
6. Automate: `shadowcal install-agent` and `shadowcal install-menubar-agent`.
7. **Reboot or log out/in**, then check `shadowcal status` and the menu-bar dot turn green.
8. Tear down: `shadowcal remove <id>` (removes its blocks too), and delete the test calendar.

Then add your real syncs **one at a time**, starting with `--dry-run` each.

**Sharing with colleagues:** treat it as a **beta**. Have them install from
`requirements-lock.txt`, grant Calendar access interactively once, and run the rehearsal
above on a disposable calendar before their real one. Installs, TCC permission prompts,
and launchd behavior vary per Mac.

## How dedup works

Each block gets a hidden marker in the note (`shadowcal-sync: <id>` + a key = source
UID + start time). On every run `shadowcal` reconciles the source against its own
blocks in the target calendar: new ones are created, changed times are updated, and
blocks whose source has disappeared are deleted. Hence no duplicates, and cleanup
happens by itself.

## Command reference

| Command | What it does |
|---|---|
| `shadowcal` / `shadowcal tui` | Open the TUI (the default with no arguments). |
| `shadowcal sync [id] [--dry-run] [--force] [--once]` | Run all enabled syncs (or one `id`). `--dry-run` previews without writing; `--force` overrides the mass-delete guard. |
| `shadowcal list [--show-secrets]` | Show configured syncs (secret links masked unless `--show-secrets`). |
| `shadowcal status` | Health overview per sync; exit code `0` ok / `1` warning / `2` error or stale. |
| `shadowcal calendars` | List writable destination calendars and their `identifier` (also triggers the access prompt the first time). |
| `shadowcal permissions` | Request Calendar access and report the current status. |
| `shadowcal add --name … (--url … \| --source-cal …) --dest … [options]` | Add a sync (see **Per-sync options**). |
| `shadowcal remove <id> [--force-remove-config]` | Remove a sync **and** its blocks (aborts if cleanup fails, unless `--force-remove-config`). |
| `shadowcal cleanup <id>` | Delete a sync's blocks but keep its configuration. |
| `shadowcal disable <id> [--keep-blocks]` | Disable a sync (removes its blocks unless `--keep-blocks`). |
| `shadowcal enable <id>` | Re-enable a disabled sync. |
| `shadowcal test <id\|url> [--tz ZONE]` | Fetch a source and show its events + a time-zone diagnosis (writes nothing). |
| `shadowcal install-agent [--interval N]` | Install the launchd auto-sync agent (default `900`s = 15 min). |
| `shadowcal uninstall-agent` | Remove the auto-sync agent. |
| `shadowcal menubar [--emoji]` | Run the menu-bar status app (`--emoji` forces emoji dots). |
| `shadowcal install-menubar-agent` / `uninstall-menubar-agent` | Run the menu-bar app at login / stop it. |
| `shadowcal --version` | Print the version. |

In the TUI, menu items **1–6** cover create/edit/permissions/menu-bar toggle/agent toggle/install-packages; the CLI commands above are the same actions for scripting.

## Files

- `shadowcal.py` — everything in one file (sync engine + TUI + CLI). `shadowcal --version`
  shows the version.
- `requirements.txt` — dependencies (loose minimums).
- `requirements-lock.txt` — exact, tested versions for reproducible installs.
- `test_shadowcal.py` — standalone test suite for the reconciliation/dedup/time-zone
  logic plus hardening checks (permissions, atomic writes, source validation, lock) and
  location/meeting-link import. Run it with `python3 test_shadowcal.py`.
- Configuration and state: `~/.config/shadowcal/` (`config.json`, `state.json`, logs,
  and `sync.lock`; permissions `0700`/`0600`).

## Limitations / notes

- Written to run on **macOS** (EventKit). The headless `sync` requires `icalendar`,
  `recurring-ical-events` and pyobjc-EventKit; the TUI additionally requires `textual`.
- The TUI works with both older (≥ 0.60) and newer Textual (tested up to 8.x). On a very
  different version a single widget detail may need adjusting — all features are also
  available as CLI commands.
- If the EventKit part can't be exercised in your environment, run the first sync
  manually and watch `shadowcal.err.log` before relying on the automation.
- `remove`/`cleanup`/`disable` scan a wide date range in ~1-year chunks to find every
  generated block. On a *very* large destination calendar that's a number of EventKit
  searches; it's a rare operation, but expect it to take a moment there.

---

App vibe-coded by Michael S. Dahl with Claude Opus 4.8.
