"""
Adversarisk test af shadowcal's afstemnings-/dedup-logik.

Vi kan ikke køre EventKit her, men den DUPLIKAT-KRITISKE kode — nøgleberegning
(expand_events), markør-stempling (make_notes) og ejerskabsgenkendelse
(owned_for_sync) samt selve afstemningen i run_sync — er ren Python og deles
med produktionskoden. FakeStore implementerer samme interface som CalStore over
et in-memory "kalenderlager" og bruger de SAMME delte hjælpere.

Vi tester især: ingen dubletter, idempotens, stabilitet over gentagne kørsler,
gentagelser (RRULE), all-day/ledig-filtre, sletning ved fjernelse i kilden,
flytning af tid, prefix-kollision (sync1 vs sync10) og oprydning af strays.
"""
import datetime as dt
import shadowcal

UTC = dt.timezone.utc


# --------------------------------------------------------------------------
# In-memory erstatning for CalStore (samme metodesignaturer som run_sync bruger)
# --------------------------------------------------------------------------
class FakeStore:
    def __init__(self):
        self.events = {}      # eid -> dict(cal,title,start,end,notes)
        self._n = 0
        self.created = self.updated = self.deleted = 0

    def calendar(self, dest):
        return dest           # behandl dest-strengen som selve kalenderen

    def owned_events(self, cal, sync_id, ws, we):
        owned = {}
        for eid, ev in self.events.items():
            if ev["cal"] != cal:
                continue
            if ev["end"] < ws or ev["start"] > we:   # uden for vinduet
                continue
            belongs, key = shadowcal.owned_for_sync(ev["notes"], sync_id)
            if not belongs:
                continue
            if key is None:
                key = f"__stray__:{eid}"
            elif key in owned:
                key = f"__duplicate__:{eid}"
            owned[key] = eid
        return owned

    def create_block(self, cal, title, start, end, key, sync_id, show_as="busy",
                     tzname=None, all_day=False, location="", url=None):
        self._n += 1
        eid = f"e{self._n}"
        self.events[eid] = {
            "cal": cal, "title": title, "start": start, "end": end,
            "show_as": show_as, "tzname": tzname, "all_day": all_day,
            "location": location or "", "url": url,
            "notes": shadowcal.make_notes(sync_id, key),
        }
        self.created += 1

    def needs_update(self, eid, start, end, title, show_as="busy",
                     tzname=None, all_day=False, location="", url=None):
        ev = self.events[eid]
        return (abs(ev["start"].timestamp() - start.timestamp()) > 1
                or abs(ev["end"].timestamp() - end.timestamp()) > 1
                or ev["title"] != title
                or ev.get("show_as", "busy") != show_as
                or (bool(tzname) and ev.get("tzname") != tzname)
                or bool(ev.get("all_day", False)) != bool(all_day)
                or (ev.get("location") or "") != (location or "")
                or ev.get("url") != url)

    def update_block(self, eid, start, end, title, show_as="busy",
                     tzname=None, all_day=False, location="", url=None):
        self.events[eid].update(start=start, end=end, title=title, show_as=show_as,
                                tzname=tzname, all_day=all_day,
                                location=location or "", url=url)
        self.updated += 1

    def delete(self, eid):
        del self.events[eid]
        self.deleted += 1

    # testhjælpere
    def reset_counters(self):
        self.created = self.updated = self.deleted = 0

    def blocks_for(self, cal, sync_id):
        out = []
        for eid, ev in self.events.items():
            if ev["cal"] != cal:
                continue
            belongs, _ = shadowcal.owned_for_sync(ev["notes"], sync_id)
            if belongs:
                out.append(ev)
        return out


# --------------------------------------------------------------------------
# ICS-byggeri (relativt til NU, så det er uafhængigt af containerens ur)
# --------------------------------------------------------------------------
def _z(d):  # UTC -> ICS-tidsstempel
    return d.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def build_ics(now, timed_offset_min=0, include_timed=True):
    timed_start = (now + dt.timedelta(days=5)).replace(
        hour=9, minute=0, second=0, microsecond=0) + dt.timedelta(minutes=timed_offset_min)
    timed_end = timed_start + dt.timedelta(hours=1)
    weekly_start = (now + dt.timedelta(days=1)).replace(
        hour=14, minute=0, second=0, microsecond=0)
    weekly_end = weekly_start + dt.timedelta(hours=1)
    allday = (now + dt.timedelta(days=10)).date()
    transp = (now + dt.timedelta(days=6)).replace(
        hour=9, minute=0, second=0, microsecond=0)

    parts = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//EN"]
    if include_timed:
        parts += ["BEGIN:VEVENT", "UID:timed-1@test",
                  f"DTSTAMP:{_z(now)}", f"DTSTART:{_z(timed_start)}",
                  f"DTEND:{_z(timed_end)}", "SUMMARY:Tandlaege", "END:VEVENT"]
    parts += ["BEGIN:VEVENT", "UID:weekly-1@test",
              f"DTSTAMP:{_z(now)}", f"DTSTART:{_z(weekly_start)}",
              f"DTEND:{_z(weekly_end)}", "RRULE:FREQ=WEEKLY;COUNT=4",
              "SUMMARY:Bestyrelse", "END:VEVENT"]
    parts += ["BEGIN:VEVENT", "UID:allday-1@test",
              f"DTSTART;VALUE=DATE:{allday:%Y%m%d}",
              f"DTEND;VALUE=DATE:{allday + dt.timedelta(days=1):%Y%m%d}",
              "SUMMARY:Ferie", "END:VEVENT"]
    parts += ["BEGIN:VEVENT", "UID:free-1@test",
              f"DTSTAMP:{_z(now)}", f"DTSTART:{_z(transp)}",
              f"DTEND:{_z(transp + dt.timedelta(minutes=30))}",
              "TRANSP:TRANSPARENT", "SUMMARY:Tentativ", "END:VEVENT"]
    parts.append("END:VCALENDAR")
    return ("\r\n".join(parts)).encode("utf-8")


# --------------------------------------------------------------------------
# Test
# --------------------------------------------------------------------------
NOW = dt.datetime.now(UTC)
_current_ics = {"data": build_ics(NOW)}

# Gem den rigtige fetch_ics (til scheme/størrelses-tests) før vi monkeypatcher.
_real_fetch_ics = shadowcal.fetch_ics
# Monkeypatch netværkshentning til at returnere vores ICS i hukommelsen.
shadowcal.fetch_ics = lambda url, timeout=30, allow_http=False: _current_ics["data"]

store = FakeStore()
S1 = {"id": "sync1", "name": "Privat", "url": "mem://1", "dest": "AAU"}

passed = []
def check(label, cond):
    passed.append(cond)
    print(f"  [{'OK ' if cond else 'FEJL'}] {label}")


print("== Run 1: tom destination ==")
r = shadowcal.run_sync(S1, store)
print("   ", r)
# Forventet kilde: 1 timed + 4 ugentlige = 5 (all-day og TRANSPARENT filtreres væk)
check("kilde = 5 (all-day + ledig filtreret væk)", r["source"] == 5)
check("oprettet = 5", r["created"] == 5)
check("opdateret = 0", r["updated"] == 0)
check("slettet = 0", r["deleted"] == 0)
check("5 blokke i destinationen", len(store.blocks_for("AAU", "sync1")) == 5)

print("== Run 2: uændret kilde -> fuldstændig idempotent (INGEN dubletter) ==")
store.reset_counters()
r = shadowcal.run_sync(S1, store)
print("   ", r)
check("oprettet = 0", r["created"] == 0)
check("opdateret = 0", r["updated"] == 0)
check("slettet = 0", r["deleted"] == 0)
check("stadig præcis 5 blokke (ingen dubletter)",
      len(store.blocks_for("AAU", "sync1")) == 5)

print("== Run 3: kør 5 gange i træk -> tæller stadig 5, aldrig flere ==")
for _ in range(5):
    shadowcal.run_sync(S1, store)
check("stadig præcis 5 blokke efter 5 ekstra kørsler",
      len(store.blocks_for("AAU", "sync1")) == 5)

print("== Dublet-oprydning: to blokke med samme nøgle -> behold én, slet resten ==")
sample = next(iter(store.events.values())).copy()
store._n += 1
dup_id = f"e{store._n}"
store.events[dup_id] = dict(sample)
store.reset_counters()
r = shadowcal.run_sync(S1, store)
check("én dublet slettet", r["deleted"] == 1 and store.deleted == 1)
check("tilbage på præcis 5 blokke", len(store.blocks_for("AAU", "sync1")) == 5)

print("== Prefix-kollision: sync10 i samme kalender må IKKE røres af sync1 ==")
S10 = {"id": "sync10", "name": "Bestyrelse", "url": "mem://10", "dest": "AAU"}
# Giv sync10 sin egen kilde (kun det ene timed-event, andet UID).
def ics_for_10(now):
    s = (now + dt.timedelta(days=3)).replace(hour=11, minute=0, second=0, microsecond=0)
    return ("\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//t//EN",
        "BEGIN:VEVENT", "UID:board-x@test", f"DTSTAMP:{_z(now)}",
        f"DTSTART:{_z(s)}", f"DTEND:{_z(s + dt.timedelta(hours=2))}",
        "SUMMARY:Board", "END:VEVENT", "END:VCALENDAR"])).encode()
_current_ics["data"] = ics_for_10(NOW)
shadowcal.run_sync(S10, store)
sync10_before = len(store.blocks_for("AAU", "sync10"))
check("sync10 har 1 blok", sync10_before == 1)
# Kør sync1 igen (med sync1's kilde) — må ikke slette/adoptere sync10's blok.
_current_ics["data"] = build_ics(NOW)
store.reset_counters()
shadowcal.run_sync(S1, store)
check("sync1 rørte ikke sync10 (0 slettet)", store.deleted == 0)
check("sync10 stadig 1 blok", len(store.blocks_for("AAU", "sync10")) == 1)
check("sync1 stadig 5 blokke", len(store.blocks_for("AAU", "sync1")) == 5)
total_aau = len([e for e in store.events.values() if e["cal"] == "AAU"])
check("i alt 6 blokke i AAU (5+1, ingen overlap-dubletter)", total_aau == 6)

print("== Stray-oprydning: vores blok med ødelagt nøgle skal fjernes ==")
store._n += 1
stray_id = f"e{store._n}"
store.events[stray_id] = {
    "cal": "AAU", "title": "Privat – optaget",
    "start": NOW + dt.timedelta(days=2), "end": NOW + dt.timedelta(days=2, hours=1),
    # tag for sync1 til stede, men key-linjen mangler:
    "notes": f"Auto-genereret af shadowcal.\n{shadowcal.MARKER}: sync1\n(key mangler)",
}
store.reset_counters()
shadowcal.run_sync(S1, store)
check("stray blev ryddet op (slettet >= 1)", store.deleted >= 1)
check("sync1 tilbage på præcis 5 blokke", len(store.blocks_for("AAU", "sync1")) == 5)

print("== Fjernelse i kilden: timed-event forsvinder -> blok slettes ==")
_current_ics["data"] = build_ics(NOW, include_timed=False)
store.reset_counters()
r = shadowcal.run_sync(S1, store)
print("   ", r)
check("kilde nu = 4", r["source"] == 4)
check("1 blok slettet", r["deleted"] == 1)
check("ingen nye, ingen dubletter (4 blokke)",
      len(store.blocks_for("AAU", "sync1")) == 4)

print("== Flytning af tid: timed-event vender tilbage 30 min senere ==")
_current_ics["data"] = build_ics(NOW, timed_offset_min=30)
store.reset_counters()
r = shadowcal.run_sync(S1, store)
print("   ", r)
check("kilde = 5 igen", r["source"] == 5)
check("netto præcis 5 blokke (ingen dublet ved flytning)",
      len(store.blocks_for("AAU", "sync1")) == 5)
# Stabilitet efter flytning:
store.reset_counters()
shadowcal.run_sync(S1, store)
check("kørsel efter flytning er idempotent (0/0/0)",
      store.created == 0 and store.updated == 0 and store.deleted == 0)

# ----------------------------------------------------------------------
# NYE OPTIONS
# ----------------------------------------------------------------------
print("== Tom-kilde-værn: kilden svarer tomt -> INGEN sletninger ==")
empty_ics = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nEND:VCALENDAR"
_current_ics["data"] = empty_ics
before = len(store.blocks_for("AAU", "sync1"))
store.reset_counters()
r = shadowcal.run_sync(S1, store, safety=dict(shadowcal.DEFAULT_SAFETY))
print("   ", {k: r[k] for k in ("source", "deleted", "blocked_deletes", "guard")})
check("kilde = 0", r["source"] == 0)
check("0 faktisk slettet (værn slog til)", r["deleted"] == 0)
check("værnet rapporterede blokerede sletninger", r["blocked_deletes"] == before)
check("guard-besked sat", bool(r["guard"]))
check("alle blokke stadig intakte (ingen blev ledige)",
      len(store.blocks_for("AAU", "sync1")) == before)

print("== --force tilsidesætter værnet (bevidst oprydning) ==")
store.reset_counters()
r = shadowcal.run_sync(S1, store, force=True)
check("med --force slettes blokkene", r["deleted"] == before)
check("0 blokke tilbage", len(store.blocks_for("AAU", "sync1")) == 0)

print("== Delete-guard er en ren, testbar funktion ==")
sf = shadowcal.DEFAULT_SAFETY
check("tom kilde + ejede blokke -> trip",
      shadowcal.evaluate_delete_guard(0, 5, 5, sf)[0] is True)
check("normal lille sletning -> ingen trip",
      shadowcal.evaluate_delete_guard(10, 11, 1, sf)[0] is False)
check("stor andel (3/4 = 75%) -> trip",
      shadowcal.evaluate_delete_guard(1, 4, 3, sf)[0] is True)
check("få ejede (under min_owned) -> ingen trip trods 100%",
      shadowcal.evaluate_delete_guard(0, 2, 2, {**sf, "block_empty_source": False})[0] is False)

print("== Buffer (pad): blok-tider polstres, men nøgle/antal er stabilt ==")
store2 = FakeStore()
_current_ics["data"] = build_ics(NOW)
S_pad = {"id": "p1", "name": "Pad", "url": "mem://p", "dest": "AAU",
         "pad_before": 15, "pad_after": 30}
shadowcal.run_sync(S_pad, store2)
blocks = store2.blocks_for("AAU", "p1")
check("5 blokke med buffer", len(blocks) == 5)
# verificér at mindst én blok faktisk er polstret 15 min før kilden
src = shadowcal.expand_events(build_ics(NOW), NOW - dt.timedelta(days=7),
                           NOW + dt.timedelta(days=365))
src_starts = {e["start"] for e in src}
padded_ok = any((b["start"] + dt.timedelta(minutes=15)) in src_starts for b in blocks)
check("blok starter 15 min før kildens start", padded_ok)
store2.reset_counters()
shadowcal.run_sync(S_pad, store2)
check("buffer-kørsel er idempotent (ingen churn)",
      store2.created == 0 and store2.updated == 0 and store2.deleted == 0)
# Ændr buffer -> opdateres in-place (samme nøgle), IKKE slet+genskab
S_pad["pad_before"] = 20
store2.reset_counters()
r = shadowcal.run_sync(S_pad, store2)
check("ændret buffer => opdateret in-place, intet slettet/oprettet",
      r["updated"] == 5 and r["created"] == 0 and r["deleted"] == 0)

print("== Vis-som: foreløbig ==")
store3 = FakeStore()
S_sa = {"id": "sa", "name": "Tent", "url": "mem://sa", "dest": "AAU",
        "show_as": "tentative"}
shadowcal.run_sync(S_sa, store3)
check("blokke markeret 'tentative'",
      all(b["show_as"] == "tentative" for b in store3.blocks_for("AAU", "sa")))

print("== Søgeordsfilter + minimumsvarighed ==")
store4 = FakeStore()
S_f = {"id": "f", "name": "Filtered", "url": "mem://f", "dest": "AAU",
       "include": ["bestyrelse"]}
r = shadowcal.run_sync(S_f, store4)
check("kun 'Bestyrelse' medtaget (4 ugentlige, timed filtreret fra)",
      r["source"] == 4)
store5 = FakeStore()
S_x = {"id": "x", "name": "Excl", "url": "mem://x", "dest": "AAU",
       "exclude": ["bestyrelse"]}
r = shadowcal.run_sync(S_x, store5)
check("'Bestyrelse' ekskluderet (kun timed tilbage)", r["source"] == 1)

print("== Kopiér-titel ==")
store6 = FakeStore()
S_ct = {"id": "ct", "name": "Copy", "url": "mem://ct", "dest": "AAU",
        "copy_title": True}
shadowcal.run_sync(S_ct, store6)
titles = {b["title"] for b in store6.blocks_for("AAU", "ct")}
check("kildens titler kopieret (Tandlaege/Bestyrelse synlige)",
      "Tandlaege" in titles and "Bestyrelse" in titles)

print("== --dry-run skriver intet ==")
store7 = FakeStore()
S_d = {"id": "d", "name": "Dry", "url": "mem://d", "dest": "AAU"}
r = shadowcal.run_sync(S_d, store7, dry_run=True)
check("dry-run rapporterer planlagte oprettelser", r["created"] == 5)
check("dry-run skrev INTET", len(store7.events) == 0)
check("dry_run-flag sat i resultat", r["dry_run"] is True)

# ----------------------------------------------------------------------
# TIDSZONER (kerne-bekymring: Proton + DST)
# ----------------------------------------------------------------------
from zoneinfo import ZoneInfo

VTZ_CPH = ("BEGIN:VTIMEZONE\r\nTZID:Europe/Copenhagen\r\n"
           "BEGIN:DAYLIGHT\r\nTZOFFSETFROM:+0100\r\nTZOFFSETTO:+0200\r\nTZNAME:CEST\r\n"
           "DTSTART:19700329T020000\r\nRRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU\r\nEND:DAYLIGHT\r\n"
           "BEGIN:STANDARD\r\nTZOFFSETFROM:+0200\r\nTZOFFSETTO:+0100\r\nTZNAME:CET\r\n"
           "DTSTART:19701025T030000\r\nRRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\r\nEND:STANDARD\r\n"
           "END:VTIMEZONE")

def vcal(body):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//tz//EN\r\n"
            + body + "\r\nEND:VCALENDAR").encode()

print("== DST: ugentlig 09:00 Europe/Copenhagen hen over forårsovergangen ==")
# DST starter sidste søndag i marts (2026-03-29). 03-23 = CET(+1); 03-30+ = CEST(+2).
cph = vcal(VTZ_CPH + "\r\n" +
           "BEGIN:VEVENT\r\nUID:cph@x\r\n"
           "DTSTART;TZID=Europe/Copenhagen:20260323T090000\r\n"
           "DTEND;TZID=Europe/Copenhagen:20260323T100000\r\n"
           "RRULE:FREQ=WEEKLY;COUNT=4\r\nSUMMARY:Bestyrelse 09\r\nEND:VEVENT")
win = (dt.datetime(2026, 3, 20, tzinfo=UTC), dt.datetime(2026, 4, 20, tzinfo=UTC))
evs = sorted(shadowcal.expand_events(cph, *win), key=lambda e: e["start"])
offs = [e["start"].utcoffset() for e in evs]
utc_h = [e["start"].astimezone(UTC).hour for e in evs]
check("4 forekomster", len(evs) == 4)
check("09:00 lokal bevaret hele vejen",
      all(e["start"].astimezone(ZoneInfo("Europe/Copenhagen")).hour == 9 for e in evs))
check("før DST: offset +1 (08:00Z)", offs[0] == dt.timedelta(hours=1) and utc_h[0] == 8)
check("efter DST: offset +2 (07:00Z)",
      all(o == dt.timedelta(hours=2) for o in offs[1:]) and all(h == 7 for h in utc_h[1:]))
check("zone-navn sat på alle forekomster",
      all(e["tzname"] == "Europe/Copenhagen" for e in evs))
# Nøgler stabile (anden udfoldning giver præcis samme nøgler) og indbyrdes unikke
keys1 = [e["key"] for e in evs]
keys2 = [e["key"] for e in sorted(shadowcal.expand_events(cph, *win), key=lambda e: e["start"])]
check("nøgler stabile mellem to udfoldninger", keys1 == keys2)
check("nøgler indbyrdes unikke (ingen DST-kollision)", len(set(keys1)) == 4)

print("== Floating-fix: DST-bevidst zone fortolker vinter vs sommer korrekt ==")
cphz = ZoneInfo("Europe/Copenhagen")
winter = shadowcal._to_aware(dt.datetime(2026, 1, 15, 9, 0), cphz)
summer = shadowcal._to_aware(dt.datetime(2026, 7, 15, 9, 0), cphz)
check("floating vinter -> +1 (det gamle faste offset ville fejle her)",
      winter.utcoffset() == dt.timedelta(hours=1))
check("floating sommer -> +2", summer.utcoffset() == dt.timedelta(hours=2))

print("== Floating + --tz: antaget zone trådes igennem expand_events ==")
floating = vcal(
    "BEGIN:VEVENT\r\nUID:fa@x\r\nDTSTART:20260115T090000\r\nDTEND:20260115T100000\r\n"
    "SUMMARY:Vinter\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:fb@x\r\nDTSTART:20260715T090000\r\nDTEND:20260715T100000\r\n"
    "SUMMARY:Sommer\r\nEND:VEVENT")
fwin = (dt.datetime(2026, 1, 1, tzinfo=UTC), dt.datetime(2026, 12, 31, tzinfo=UTC))
fev = sorted(shadowcal.expand_events(floating, *fwin, assume_tz="Europe/Copenhagen"),
             key=lambda e: e["start"])
check("begge floating-flag sat", all(e["floating"] for e in fev) and len(fev) == 2)
check("antaget zone sat som tzname", all(e["tzname"] == "Europe/Copenhagen" for e in fev))
check("floating vinter +1 / sommer +2 via assume_tz",
      fev[0]["start"].utcoffset() == dt.timedelta(hours=1)
      and fev[1]["start"].utcoffset() == dt.timedelta(hours=2))

print("== tzname propageres til blokken (UTC-kilde -> tz='UTC') ==")
store_tz = FakeStore()
_current_ics["data"] = build_ics(NOW)   # build_ics bruger UTC 'Z'
shadowcal.run_sync({"id": "tz", "name": "Tz", "url": "mem://tz", "dest": "AAU"}, store_tz)
check("alle blokke fik tzname='UTC'",
      all(b["tzname"] == "UTC" for b in store_tz.blocks_for("AAU", "tz")))

# ----------------------------------------------------------------------
# LOCATION / MØDELINK-IMPORT (feature 2)
# ----------------------------------------------------------------------
print("== Location + Teams/Zoom-link udtrækkes fra ICS ==")
_loc_start = (NOW + dt.timedelta(days=2)).replace(microsecond=0)
_loc_ics = vcal(
    "BEGIN:VEVENT\r\nUID:loc1@x\r\n"
    f"DTSTART:{_z(_loc_start)}\r\nDTEND:{_z(_loc_start + dt.timedelta(hours=1))}\r\n"
    "SUMMARY:Standup\r\nLOCATION:Room 4\r\n"
    "DESCRIPTION:Join https://teams.microsoft.com/l/meetup-join/xyz now\r\n"
    "END:VEVENT")
_le = shadowcal.expand_events(_loc_ics, NOW - dt.timedelta(days=1),
                              NOW + dt.timedelta(days=5))[0]
check("location udtrukket", _le["location"] == "Room 4")
check("Teams-link udtrukket", bool(_le["url"]) and "teams.microsoft.com" in _le["url"])

print("== copy_location=False lækker hverken sted eller link ==")
_current_ics["data"] = _loc_ics
store_loc_off = FakeStore()
shadowcal.run_sync({"id": "lo", "name": "LocOff", "url": "m", "dest": "AAU"}, store_loc_off)
_b = store_loc_off.blocks_for("AAU", "lo")[0]
check("ingen location på blokken", (_b.get("location") or "") == "")
check("ingen url på blokken", _b.get("url") is None)

print("== copy_location=True kopierer sted + mødelink (og er idempotent) ==")
store_loc_on = FakeStore()
S_loc = {"id": "ln", "name": "LocOn", "url": "m", "dest": "AAU", "copy_location": True}
shadowcal.run_sync(S_loc, store_loc_on)
_b2 = store_loc_on.blocks_for("AAU", "ln")[0]
check("location kopieret", _b2.get("location") == "Room 4")
check("mødelink kopieret", bool(_b2.get("url")) and "teams" in _b2["url"])
store_loc_on.reset_counters()
_r = shadowcal.run_sync(S_loc, store_loc_on)
check("copy_location idempotent (0/0/0)",
      _r["created"] == 0 and _r["updated"] == 0 and _r["deleted"] == 0)

print("== normalize_sync har nye defaults (source_cal, copy_location) ==")
_n = shadowcal.normalize_sync({"id": "x", "name": "x", "url": "m", "dest": "d"})
check("source_cal default None", _n["source_cal"] is None)
check("copy_location default False", _n["copy_location"] is False)


# ----------------------------------------------------------------------
# HÆRDNING: sikkerhed / robusthed (uden EventKit)
# ----------------------------------------------------------------------
import os, stat, tempfile, sys, pathlib

print("== fetch_ics: kun http/https/webcal — file:// m.fl. afvises ==")
for bad in ("file:///etc/passwd", "ftp://host/x.ics", "/lokal/sti.ics"):
    try:
        _real_fetch_ics(bad)
        check(f"afviste {bad}", False)
    except ValueError:
        check(f"afviste {bad}", True)
    except Exception:
        check(f"afviste {bad} (ikke-ValueError)", False)

print("== config/state: atomisk skrivning med 0600/0700 + .bad-backup ==")
_tmp = tempfile.mkdtemp()
shadowcal.CONFIG_DIR = pathlib.Path(_tmp) / "cfg"
shadowcal.CONFIG_FILE = shadowcal.CONFIG_DIR / "config.json"
shadowcal.STATE_FILE = shadowcal.CONFIG_DIR / "state.json"
shadowcal.save_config({"syncs": [{"id": "sync1", "url": "https://x/y.ics"}]})
fmode = stat.S_IMODE(os.stat(shadowcal.CONFIG_FILE).st_mode)
dmode = stat.S_IMODE(os.stat(shadowcal.CONFIG_DIR).st_mode)
check("config.json er 0600 (hemmelige links ikke world-readable)", fmode == 0o600)
check("config-mappe er 0700", dmode == 0o700)
check("config kan læses tilbage", shadowcal.load_config()["syncs"][0]["id"] == "sync1")
shadowcal.CONFIG_FILE.write_text("{ikke gyldig json")
check("korrupt config -> tom config (ingen crash)", shadowcal.load_config() == {"syncs": []})
check("korrupt config flyttet til .bad (ikke tabt)",
      (shadowcal.CONFIG_DIR / "config.json.bad").exists())

print("== sync-lås er eksklusiv (forhindrer samtidige kørsler) ==")
with shadowcal._sync_lock():
    busy = False
    try:
        with shadowcal._sync_lock():
            pass
    except shadowcal.SyncLockBusy:
        busy = True
    check("anden lås afvises mens første holdes", busy)

print("== plist XML-escapes (sti med & giver gyldig plist) ==")
shadowcal.LOG_OUT = shadowcal.CONFIG_DIR / "a&b.out.log"
shadowcal.LOG_ERR = shadowcal.CONFIG_DIR / "a&b.err.log"
_real_exe = sys.executable
_real_agent_plist_path = shadowcal.agent_plist_path
_p = None
try:
    sys.executable = "/sti/med/&-tegn/python"
    shadowcal.agent_plist_path = lambda: shadowcal.CONFIG_DIR / "agent.plist"
    _p = shadowcal.write_agent(900)
    _txt = _p.read_text("utf-8")
    check("& er escapet til &amp; i plist", "&amp;" in _txt and "/&-tegn" not in _txt)
finally:
    sys.executable = _real_exe
    shadowcal.agent_plist_path = _real_agent_plist_path
    try:
        if _p is not None:
            _p.unlink()
    except OSError:
        pass

# ----------------------------------------------------------------------
# AUDIT ROUND 2: feedback loops, safe remove, hardening
# ----------------------------------------------------------------------
print("== #7 mødelink: hostname-suffix (ikke substring) ==")
check("ægte Teams-link genkendt",
      shadowcal._meeting_link("https://teams.microsoft.com/l/meetup-join/x")
      == "https://teams.microsoft.com/l/meetup-join/x")
check("zoom subdomæne genkendt",
      bool(shadowcal._meeting_link("join https://acme.zoom.us/j/123")))
check("falsk positiv afvist (zoom.us i query)",
      shadowcal._meeting_link("https://evil.example.com/?next=zoom.us") is None)
check("falsk positiv afvist (host-substring)",
      shadowcal._meeting_link("https://teams.microsoft.com.evil.com/x") is None)

print("== #6 _mask_url viser kun scheme://host (ingen sti-token) ==")
check("sti-token skjult",
      shadowcal._mask_url("https://cal.proton.me/SECRETTOKEN/basic.ics")
      == "https://cal.proton.me/…")
check("query skjult",
      shadowcal._mask_url("https://h.test/a?key=SECRET") == "https://h.test/…")
check("webcal vises som webcal",
      shadowcal._mask_url("webcal://h.test/x.ics").startswith("webcal://h.test"))
check("ingen SECRET lækket nogensinde",
      "SECRET" not in shadowcal._mask_url("https://h.test/SECRET/x.ics?t=SECRET"))

print("== #1 notes_have_marker genkender vores egne blokke ==")
check("egen blok genkendt",
      shadowcal.notes_have_marker(shadowcal.make_notes("sync1", "k|t")) is True)
check("fremmed note ikke genkendt",
      shadowcal.notes_have_marker("Lunch with Bob") is False)
check("tom note ikke genkendt", shadowcal.notes_have_marker("") is False)

print("== #4 preflight afviser tætte BY*-regler FØR udfoldning ==")
_dense = vcal(
    "BEGIN:VEVENT\r\nUID:dense@x\r\nDTSTART:20260101T000000Z\r\nDTEND:20260101T000500Z\r\n"
    "RRULE:FREQ=DAILY;BYHOUR=" + ",".join(str(i) for i in range(24)) +
    ";BYMINUTE=" + ",".join(str(i) for i in range(60)) + "\r\n"
    "SUMMARY:Storm\r\nEND:VEVENT")
_dense_rejected = False
try:
    shadowcal.expand_events(_dense, dt.datetime(2026, 1, 1, tzinfo=UTC),
                            dt.datetime(2026, 12, 31, tzinfo=UTC))
except ValueError:
    _dense_rejected = True
check("tæt BYHOUR×BYMINUTE afvist", _dense_rejected)
# A normal multi-BYDAY weekly rule must NOT be falsely rejected.
_ok_rule = vcal(
    "BEGIN:VEVENT\r\nUID:wk@x\r\nDTSTART:20260105T090000Z\r\nDTEND:20260105T100000Z\r\n"
    "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR\r\nSUMMARY:Daily standup\r\nEND:VEVENT")
_ok_count = len(shadowcal.expand_events(_ok_rule, dt.datetime(2026, 1, 1, tzinfo=UTC),
                                        dt.datetime(2026, 3, 1, tzinfo=UTC)))
check("normal BYDAY-uge ikke fejlagtigt afvist", _ok_count > 0)


class _Cal:
    def __init__(self, ident):
        self._id = ident
    def calendarIdentifier(self):
        return self._id


print("== #1 source==dest afvises (shadow ind i sig selv) ==")
class GuardStore:
    def calendar(self, dest):
        return _Cal(dest)
    def any_calendar(self, ident):
        return _Cal(ident)
_same_rejected = False
try:
    shadowcal.run_sync({"id": "g", "name": "g", "source_cal": "CAL", "dest": "CAL"},
                       GuardStore())
except RuntimeError as exc:
    _same_rejected = "same as the destination" in str(exc)
check("source_cal == dest afvist", _same_rejected)


print("== #1 source-kalender: vores egne blokke spejles IKKE ind igen ==")
def _src_event(uid, summary, notes=None):
    st = NOW + dt.timedelta(days=2)
    return {"key": f"{uid}|x", "uid": uid, "summary": summary,
            "start": st, "end": st + dt.timedelta(hours=1), "all_day": False,
            "floating": False, "tzname": "UTC", "location": "", "url": None,
            "notes": notes}

class SrcStore(FakeStore):
    def __init__(self, src):
        super().__init__()
        self._src = src
    def any_calendar(self, ident):
        return _Cal(ident)        # cal (dest) is a str here -> same-guard no-ops
    def read_source_events(self, ident, ws, we, skip_all_day=True,
                           skip_transparent=True, min_minutes=0,
                           include=None, exclude=None):
        keep = ("key", "uid", "summary", "start", "end", "all_day",
                "floating", "tzname", "location", "url")
        return [{k: e[k] for k in keep}
                for e in self._src if not shadowcal.notes_have_marker(e.get("notes"))]

_src = [_src_event("real@x", "Real meeting"),
        _src_event("mine@x", "Private – busy",
                   shadowcal.make_notes("other", "mine@x|x"))]
_ss = SrcStore(_src)
_r = shadowcal.run_sync({"id": "fromcal", "name": "FromCal",
                         "source_cal": "SRC", "dest": "AAU"}, _ss)
check("kun det ægte event blev til en blok (markeret kilde sprunget over)",
      _r["source"] == 1 and _r["created"] == 1)

# ----------------------------------------------------------------------
# AUDIT ROUND 3: redaction of both URL forms, COUNT-vs-window preflight
# ----------------------------------------------------------------------
print("== #1 _safe_error redacts BOTH webcal and normalized https forms ==")
_secret = "webcal://cal.example.com/SECRETTOKEN/basic.ics"
_norm = "https://cal.example.com/SECRETTOKEN/basic.ics"
check("normalized https form redacted (urllib error)",
      "SECRETTOKEN" not in shadowcal._safe_error(Exception(f"HTTP error: {_norm}"), _secret))
check("original webcal form redacted",
      "SECRETTOKEN" not in shadowcal._safe_error(Exception(f"bad {_secret}"), _secret))
check("https-configured secret: webcal variant also scrubbed",
      "TOK" not in shadowcal._safe_error(
          Exception("webcal://h.test/TOK/x.ics failed"), "https://h.test/TOK/x.ics"))
check("host kept so the error is still useful",
      "cal.example.com" in shadowcal._safe_error(Exception(_norm), _secret))

print("== #5 preflight: COUNT is bounded by the window (no false reject) ==")
_count_rule = vcal(
    "BEGIN:VEVENT\r\nUID:cnt@x\r\nDTSTART:20260105T090000Z\r\nDTEND:20260105T093000Z\r\n"
    "RRULE:FREQ=DAILY;COUNT=10000\r\nSUMMARY:Standup\r\nEND:VEVENT")
_cn = len(shadowcal.expand_events(_count_rule, dt.datetime(2026, 1, 1, tzinfo=UTC),
                                  dt.datetime(2026, 2, 1, tzinfo=UTC)))
check("daily COUNT=10000 NOT rejected for a 1-month window", _cn > 0)
# A genuinely dense rule is still rejected before expansion.
_minutely = vcal(
    "BEGIN:VEVENT\r\nUID:m@x\r\nDTSTART:20260101T000000Z\r\nDTEND:20260101T000100Z\r\n"
    "RRULE:FREQ=MINUTELY\r\nSUMMARY:S\r\nEND:VEVENT")
_min_rej = False
try:
    shadowcal.expand_events(_minutely, dt.datetime(2026, 1, 1, tzinfo=UTC),
                            dt.datetime(2026, 12, 31, tzinfo=UTC))
except ValueError:
    _min_rej = True
check("FREQ=MINUTELY over a year still rejected", _min_rej)

# ----------------------------------------------------------------------
# SAFETY/SIZE: generated blocks carry no source notes/description or
# attachments — only the shadowcal marker. copy_title/copy_location gate
# the only real content that can ever reach the destination.
# ----------------------------------------------------------------------
_SECRET = "AGENDA-SECRET-TEXT"
_sst = (NOW + dt.timedelta(days=2)).replace(microsecond=0)
_safety_ics = vcal(
    "BEGIN:VEVENT\r\nUID:safe@x\r\n"
    f"DTSTART:{_z(_sst)}\r\nDTEND:{_z(_sst + dt.timedelta(hours=1))}\r\n"
    "SUMMARY:Standup\r\nLOCATION:Room 4\r\n"
    f"DESCRIPTION:{_SECRET} join https://teams.microsoft.com/l/meetup-join/zzz\r\n"
    "END:VEVENT")

def _all_text(b):
    return " | ".join(str(b.get(k)) for k in ("title", "notes", "location", "url"))

print("== Default: no source notes/description or attachments on the block ==")
_current_ics["data"] = _safety_ics
_sd = FakeStore()
shadowcal.run_sync({"id": "safe", "name": "S", "url": "m", "dest": "AAU"}, _sd)
_b = _sd.blocks_for("AAU", "safe")[0]
check("source DESCRIPTION text never leaks into the block", _SECRET not in _all_text(_b))
check("title is the generic block title", _b["title"] == shadowcal.DEFAULT_TITLE)
check("no location copied by default", (_b.get("location") or "") == "")
check("no meeting URL copied by default", _b.get("url") is None)
check("note is ONLY the shadowcal marker (no source content)",
      _b["notes"].startswith("Auto-generated by shadowcal")
      and f"{shadowcal.MARKER}:" in _b["notes"]
      and _SECRET not in _b["notes"])
check("block has no attachments", "attachments" not in _b)

print("== copy_title reveals the title but still no description/notes leak ==")
_ct = FakeStore()
shadowcal.run_sync({"id": "ct", "name": "C", "url": "m", "dest": "AAU",
                    "copy_title": True}, _ct)
_bc = _ct.blocks_for("AAU", "ct")[0]
check("copy_title shows the source title", _bc["title"] == "Standup")
check("copy_title still leaks no DESCRIPTION text", _SECRET not in _all_text(_bc))

print("== copy_location copies location + meeting URL only — not the description ==")
_cl = FakeStore()
shadowcal.run_sync({"id": "cl", "name": "L", "url": "m", "dest": "AAU",
                    "copy_location": True}, _cl)
_bl = _cl.blocks_for("AAU", "cl")[0]
check("location copied", _bl.get("location") == "Room 4")
check("only the meeting URL copied", _bl.get("url") and "teams.microsoft.com" in _bl["url"])
check("DESCRIPTION text still NOT copied anywhere", _SECRET not in _all_text(_bl))
check("note is still only the marker", _SECRET not in _bl["notes"]
      and f"{shadowcal.MARKER}:" in _bl["notes"])

# ----------------------------------------------------------------------
# AUDIT ROUND 4: bad-sync isolation, http opt-in, RDATE preflight
# ----------------------------------------------------------------------
print("== #1 _safe_normalize isolates a bad sync instead of raising ==")
_ok, _e = shadowcal._safe_normalize(
    {"id": "a", "name": "A", "url": "https://h/x", "dest": "d",
     "assume_tz": "Europe/Copenhagen"})
check("valid sync normalizes", _ok is not None and _e is None)
_bad, _be = shadowcal._safe_normalize(
    {"id": "b", "name": "B", "url": "https://h/x", "dest": "d",
     "assume_tz": "Not/ARealZone"})
check("invalid tz -> (None, error), does not raise", _bad is None and bool(_be))

print("== #2 http rejected by default; allowed only with allow_http ==")
_http_blocked = False
try:
    _real_fetch_ics("http://nonexistent.invalid/cal.ics")
except ValueError as exc:
    _http_blocked = "clear text" in str(exc)
check("plain http rejected by default", _http_blocked)
_insecure_gate = False
try:
    _real_fetch_ics("http://nonexistent.invalid/cal.ics", allow_http=True)
except ValueError as exc:
    _insecure_gate = "clear text" in str(exc)   # would mean still blocked
except Exception:
    _insecure_gate = False                       # network error = past the gate (good)
check("allow_http lets http past the security gate", not _insecure_gate)

print("== #3 preflight rejects a huge RDATE list before expansion ==")
_base = dt.datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
_rd = ",".join((_base + dt.timedelta(days=i)).strftime("%Y%m%dT%H%M%SZ")
               for i in range(shadowcal.MAX_EXPANDED_EVENTS + 1))
_rd_ics = vcal(
    "BEGIN:VEVENT\r\nUID:rd@x\r\n"
    f"DTSTART:{_base:%Y%m%dT%H%M%SZ}\r\nDTEND:{_base + dt.timedelta(hours=1):%Y%m%dT%H%M%SZ}\r\n"
    f"RDATE:{_rd}\r\nSUMMARY:R\r\nEND:VEVENT")
_rd_rej = False
try:
    shadowcal.expand_events(_rd_ics, dt.datetime(2026, 1, 1, tzinfo=UTC),
                            dt.datetime(2050, 1, 1, tzinfo=UTC))
except ValueError:
    _rd_rej = True
check("huge RDATE list rejected by preflight", _rd_rej)
# A handful of RDATEs is fine.
_small_rd = vcal(
    "BEGIN:VEVENT\r\nUID:rd2@x\r\nDTSTART:20260101T090000Z\r\nDTEND:20260101T100000Z\r\n"
    "RDATE:20260108T090000Z,20260115T090000Z\r\nSUMMARY:R\r\nEND:VEVENT")
check("a few RDATEs are accepted",
      len(shadowcal.expand_events(_small_rd, dt.datetime(2026, 1, 1, tzinfo=UTC),
                                  dt.datetime(2026, 2, 1, tzinfo=UTC))) >= 1)

print("== TUI new-shadow dry-run preview: summary parser ==")
_line = "[2026-06-17T11:42:08+02:00] [DRY] sync2 (X): source=28 new=5 updated=2 deleted=1"
_p = shadowcal._parse_sync_summary(_line)
check("parses all four counts from a dry-run line",
      _p == {"source": 28, "created": 5, "updated": 2, "deleted": 1})
check("incomplete summary -> {} (falls back to raw text in UI)",
      shadowcal._parse_sync_summary("oops new=3") == {})

# ----------------------------------------------------------------------
# AUDIT ROUND 5: location leak, id-safety, https-downgrade
# ----------------------------------------------------------------------
print("== #1 copy_location copies meeting links only, never arbitrary URLs ==")
_doc_start = (NOW + dt.timedelta(days=2)).replace(microsecond=0)
_doc_ics = vcal(
    "BEGIN:VEVENT\r\nUID:doc@x\r\n"
    f"DTSTART:{_z(_doc_start)}\r\nDTEND:{_z(_doc_start + dt.timedelta(hours=1))}\r\n"
    "SUMMARY:Review\r\nLOCATION:Room 9\r\n"
    "URL:https://docs.example.com/secret-agenda\r\nEND:VEVENT")
_de = shadowcal.expand_events(_doc_ics, NOW - dt.timedelta(days=1),
                              NOW + dt.timedelta(days=5))[0]
check("private URL: field is NOT captured as a meeting link", _de["url"] is None)
check("location still captured", _de["location"] == "Room 9")
# copy_location run must not leak the private URL onto the block
_current_ics["data"] = _doc_ics
_ds = FakeStore()
shadowcal.run_sync({"id": "doc", "name": "D", "url": "m", "dest": "AAU",
                    "copy_location": True}, _ds)
_db = _ds.blocks_for("AAU", "doc")[0]
check("block gets no URL from a non-meeting source URL", _db.get("url") is None)
check("block still gets the location", _db.get("location") == "Room 9")
# a real meeting URL in the URL: field is still copied
_mt_ics = vcal(
    "BEGIN:VEVENT\r\nUID:mt@x\r\n"
    f"DTSTART:{_z(_doc_start)}\r\nDTEND:{_z(_doc_start + dt.timedelta(hours=1))}\r\n"
    "SUMMARY:Call\r\nURL:https://acme.zoom.us/j/999\r\nEND:VEVENT")
_me = shadowcal.expand_events(_mt_ics, NOW - dt.timedelta(days=1),
                              NOW + dt.timedelta(days=5))[0]
check("a real meeting URL in URL: is still captured",
      _me["url"] == "https://acme.zoom.us/j/999")

print("== #5 management lookups tolerate a malformed (id-less) sync ==")
import argparse as _ap, pathlib as _pl, tempfile as _tf
_md = _pl.Path(_tf.mkdtemp())
shadowcal.CONFIG_DIR = _md
shadowcal.CONFIG_FILE = _md / "config.json"
shadowcal.STATE_FILE = _md / "state.json"
shadowcal.save_config({"syncs": [{"name": "broken, no id"},
                                 {"id": "sync1", "name": "ok", "url": "https://h/x",
                                  "dest": "d", "enabled": False}]})
_rc = shadowcal.cmd_enable(_ap.Namespace(id="sync1"))
check("cmd_enable doesn't KeyError past an id-less sync", _rc == 0)
check("target was enabled",
      any(s.get("id") == "sync1" and s.get("enabled")
          for s in shadowcal.load_config()["syncs"]))

print("== #4 https→http redirect is refused (no token downgrade) ==")
class _FakeReq:
    full_url = "https://cal.example.com/SECRET/basic.ics"
_guard = shadowcal._NoHTTPDowngradeRedirect()
_guard.allow_http = False
_downgrade_blocked = False
try:
    _guard.redirect_request(_FakeReq(), None, 302, "Found", {},
                            "http://cal.example.com/SECRET/basic.ics")
except shadowcal.urllib.error.HTTPError:
    _downgrade_blocked = True
check("redirect to http:// refused when allow_http is False", _downgrade_blocked)

# ----------------------------------------------------------------------
# AUDIT ROUND 6: RDATE counted only within the window
# ----------------------------------------------------------------------
print("== #4 thousands of OUT-OF-WINDOW RDATEs do not cause rejection ==")
_old = dt.datetime(2000, 1, 1, 9, 0, tzinfo=UTC)
_old_rd = ",".join((_old + dt.timedelta(days=i)).strftime("%Y%m%dT%H%M%SZ")
                   for i in range(shadowcal.MAX_EXPANDED_EVENTS + 1000))   # all in year ~2000s
_old_ics = vcal(
    "BEGIN:VEVENT\r\nUID:old@x\r\n"
    f"DTSTART:{_old:%Y%m%dT%H%M%SZ}\r\nDTEND:{_old + dt.timedelta(hours=1):%Y%m%dT%H%M%SZ}\r\n"
    f"RDATE:{_old_rd}\r\nSUMMARY:Ancient\r\nEND:VEVENT")
_ow_ok = True
try:
    shadowcal.expand_events(_old_ics, dt.datetime(2026, 1, 1, tzinfo=UTC),
                            dt.datetime(2027, 1, 1, tzinfo=UTC))
except ValueError:
    _ow_ok = False
check("out-of-window RDATEs are ignored (feed not rejected)", _ow_ok)

print("== #4 thousands of IN-WINDOW RDATEs are still rejected ==")
_now0 = dt.datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
_in_rd = ",".join((_now0 + dt.timedelta(days=i)).strftime("%Y%m%dT%H%M%SZ")
                  for i in range(shadowcal.MAX_EXPANDED_EVENTS + 1))
_in_ics = vcal(
    "BEGIN:VEVENT\r\nUID:in@x\r\n"
    f"DTSTART:{_now0:%Y%m%dT%H%M%SZ}\r\nDTEND:{_now0 + dt.timedelta(hours=1):%Y%m%dT%H%M%SZ}\r\n"
    f"RDATE:{_in_rd}\r\nSUMMARY:Dense\r\nEND:VEVENT")
_in_rej = False
try:
    shadowcal.expand_events(_in_ics, dt.datetime(2026, 1, 1, tzinfo=UTC),
                            dt.datetime(2050, 1, 1, tzinfo=UTC))
except ValueError:
    _in_rej = True
check("in-window dense RDATEs still rejected", _in_rej)

print("\n" + ("ALLE TESTS BESTÅET ✔" if all(passed)
              else f"NOGLE TESTS FEJLEDE ({sum(passed)}/{len(passed)})"))
raise SystemExit(0 if all(passed) else 1)
