import os
import sys
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Windows console defaults to cp1252 which can't render emoji — force UTF-8
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    # Load .env from the script's own directory so Task Scheduler / NSSM
    # deployments work regardless of working directory at launch.
    load_dotenv(Path(__file__).parent / ".env", encoding="utf-8-sig")
except ImportError:
    pass  # dotenv optional; use environment variables or a .env file via shell

ER_SITE   = os.environ.get("ER_SITE")    # e.g. https://your-site.pamdas.org
ER_TOKEN  = os.environ.get("ER_TOKEN")
ADSB_URL  = os.environ.get("ADSB_URL",  "http://localhost:8080/data/aircraft.json")
POLL_INTERVAL        = int(os.environ.get("POLL_INTERVAL",        "10"))   # seconds
STALE_POS_THRESHOLD  = int(os.environ.get("STALE_POS_THRESHOLD",  "30"))   # seconds

PROVIDER        = "adsb_receiver"
SUBJECT_GROUP   = "ADS-B"

# ADS-B category codes → ER subject subtype slugs (all exist in ER Admin → Subject Types → Aircraft).
CATEGORY_SUBTYPES = {
    "A7": "helicopter",      # Rotorcraft
    "B2": "hot_air_balloon", # Lighter than air
    "B6": "drone",           # UAV/drone with ADS-B transmitter
}
DEFAULT_SUBTYPE = "plane"  # fixed-wing: covers A1-A6, B1, B4 etc.

ADSBDB_URL = "https://api.adsbdb.com/v0/aircraft/{hex}"

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {ER_TOKEN}",
    "Content-Type": "application/json",
})

# Per-session cache so adsbdb is only queried once per unknown aircraft.
_reg_cache: dict[str, str | None] = {}


def category_to_subtype(category: str) -> str:
    return CATEGORY_SUBTYPES.get(category, DEFAULT_SUBTYPE)


def lookup_registration(hex_code: str) -> str | None:
    """Return the civil registration for an ICAO hex code via adsbdb.com, or None.

    adsbdb returns {"response": {"aircraft": {...}}} on hit,
    or {"response": "unknown aircraft"} when the hex is not in their DB.
    """
    if hex_code in _reg_cache:
        return _reg_cache[hex_code]
    reg = None
    try:
        r = requests.get(ADSBDB_URL.format(hex=hex_code), timeout=5)
        if r.status_code == 200:
            response = r.json().get("response", {})
            if isinstance(response, dict):
                reg = response.get("aircraft", {}).get("registration")
    except Exception:
        pass
    _reg_cache[hex_code] = reg
    return reg


def get_er_cache() -> dict:
    """Return {icao_hex: {"source": id, "subject": id_or_None, "subtype": str_or_None}}."""
    try:
        r = session.get(
            f"{ER_SITE}/api/v1.0/sources/",
            params={"provider": PROVIDER, "page_size": 1000},
        )
        if r.status_code == 200:
            results = r.json().get("data", {}).get("results", []) or r.json().get("results", [])
            return {s["manufacturer_id"]: {"source": s["id"], "subject": None, "subtype": None}
                    for s in results}
    except Exception as e:
        print(f"   Warning: Cache error: {e}")
    return {}


def _resolve_subject_id(source_id: str, cache_entry: dict) -> str | None:
    """Look up the subject linked to source_id and populate cache_entry."""
    if cache_entry["subject"]:
        return cache_entry["subject"]
    try:
        r = session.get(f"{ER_SITE}/api/v1.0/subjectsources/",
                        params={"source": source_id, "page_size": 1})
        if r.status_code == 200:
            results = r.json().get("data", {}).get("results", []) or r.json().get("results", [])
            if results:
                subj = results[0].get("subject", {})
                subj_id = subj.get("id") if isinstance(subj, dict) else subj
                subj_type = subj.get("subject_subtype", DEFAULT_SUBTYPE) if isinstance(subj, dict) else DEFAULT_SUBTYPE
                if subj_id:
                    cache_entry["subject"] = subj_id
                    cache_entry["subtype"] = subj_type
                    return subj_id
    except Exception:
        pass
    return None


def get_group_id(group_name: str) -> str | None:
    """Return the ID of the named subject group, or None if not found."""
    try:
        r = session.get(
            f"{ER_SITE}/api/v1.0/subjectgroups/",
            params={"flat": "true", "group_name": group_name},
        )
        if r.status_code == 200:
            for g in r.json().get("data", []):
                if g.get("name") == group_name:
                    return g["id"]
    except Exception as e:
        print(f"   ⚠️  Group lookup error: {e}")
    return None


def add_to_group(group_id: str, subject_id: str, name: str):
    r = session.post(
        f"{ER_SITE}/api/v1.0/subjectgroup/{group_id}/subjects/",
        json=[{"id": subject_id}],
    )
    if r.status_code in [200, 201]:
        print(f"   🗂️  Added {name} to '{SUBJECT_GROUP}' group")
    else:
        print(f"   ⚠️  Group add failed for {name}: {r.status_code} {r.text[:80]}")


def ensure_aircraft(hex_code: str, callsign: str, subtype: str, cache: dict, group_id: str | None):
    """Upsert source + subject + assignment + group membership. Returns source_id or None."""
    if hex_code in cache:
        entry = cache[hex_code]
        # If we now see a specific (non-default) subtype that differs from what is registered,
        # patch the ER subject so the icon updates (e.g. plane -> helicopter when A7 arrives).
        if subtype != DEFAULT_SUBTYPE and entry["subtype"] != subtype:
            subj_id = _resolve_subject_id(entry["source"], entry)
            if subj_id:
                r = session.patch(f"{ER_SITE}/api/v1.0/subject/{subj_id}/",
                                  json={"subject_subtype": subtype})
                if r.status_code in [200, 201]:
                    name = callsign.strip() if callsign and callsign.strip() else f"ICAO-{hex_code.upper()}"
                    print(f"   Updated {name}: subtype {entry['subtype'] or DEFAULT_SUBTYPE} -> {subtype}")
                    entry["subtype"] = subtype
        return entry["source"]

    cs = callsign.strip() if callsign else ""
    if cs:
        name = cs
    else:
        name = lookup_registration(hex_code) or f"ICAO-{hex_code.upper()}"
    print(f"   Registering: {name} ({hex_code})  subtype={subtype}")

    # 1. Create source (the ADS-B transponder)
    r = session.post(f"{ER_SITE}/api/v1.0/sources/", json={
        "manufacturer_id": hex_code,
        "source_type":     "tracking-device",
        "model_name":      "ADS-B Transponder",
        "provider":        PROVIDER,
        "additional":      {},
    })
    source_id = None
    if r.status_code in [200, 201]:
        source_id = r.json().get("data", r.json()).get("id")
    elif "already exists" in r.text:
        l = session.get(f"{ER_SITE}/api/v1.0/sources/", params={"provider": PROVIDER, "manufacturer_id": hex_code})
        if l.status_code == 200:
            res = l.json().get("data", {}).get("results", []) or l.json().get("results", [])
            if res:
                source_id = res[0]["id"]

    if not source_id:
        print(f"   ❌  Could not create/find source for {hex_code}: {r.text[:120]}")
        return None

    # 2. Create subject (subject_type is readOnly — ER derives it from subtype)
    r2 = session.post(f"{ER_SITE}/api/v1.0/subjects/", json={
        "name":            name,
        "subject_subtype": subtype,
        "is_active":       True,
    })
    subj_id = None
    if r2.status_code in [200, 201]:
        subj_id = r2.json().get("data", r2.json()).get("id")

        # 3. Link source → subject
        session.post(f"{ER_SITE}/api/v1.0/subject/{subj_id}/sources/", json={
            "source":         source_id,
            "assigned_range": {"lower": "2000-01-01T00:00:00Z", "upper": "2099-01-01T00:00:00Z"},
            "additional":     {},
        })

        # 4. Add to ADS-B subject group
        if group_id:
            add_to_group(group_id, subj_id, name)
    else:
        print(f"   Warning: Subject creation issue for {name}: {r2.text[:120]}")

    cache[hex_code] = {"source": source_id, "subject": subj_id, "subtype": subtype}
    return source_id


def run_sync(cache: dict, group_id: str | None) -> int:
    try:
        r = requests.get(ADSB_URL, timeout=5)
        r.raise_for_status()
    except Exception as e:
        print(f"   ⚠️  ADS-B fetch failed: {e}")
        return 0

    all_aircraft = r.json().get("aircraft", [])
    positioned = [
        a for a in all_aircraft
        if "lat" in a and "lon" in a and a.get("seen_pos", 999) < STALE_POS_THRESHOLD
    ]

    if not positioned:
        print(f"   💤  No positioned aircraft  ({len(all_aircraft)} in range, none with fresh fix)")
        return 0

    count = 0
    for a in positioned:
        hex_code = a["hex"]
        source_id = ensure_aircraft(
            hex_code, a.get("flight", ""),
            category_to_subtype(a.get("category", "")),
            cache, group_id,
        )
        if not source_id:
            continue

        recorded_at = (datetime.now(timezone.utc) - timedelta(seconds=a.get("seen_pos", 0))).isoformat()

        additional = {
            "category": a.get("category", ""),
            "squawk":   a.get("squawk",   ""),
        }
        for src_key, dst_key in [
            ("alt_baro",  "altitude_ft"),
            ("gs",        "ground_speed_kts"),
            ("track",     "track_deg"),
            ("baro_rate", "climb_rate_fpm"),
        ]:
            if src_key in a:
                additional[dst_key] = a[src_key]

        p = session.post(f"{ER_SITE}/api/v1.0/observations/", json={
            "source":      source_id,
            "location":    {"latitude": a["lat"], "longitude": a["lon"]},
            "recorded_at": recorded_at,
            "additional":  additional,
        })
        if p.status_code in [200, 201]:
            count += 1
        else:
            print(f"   ⚠️  Observation failed for {hex_code}: {p.status_code} {p.text[:80]}")

    return count


def main():
    print("🛫 ADS-B → EarthRanger  starting up")
    print(f"   Receiver : {ADSB_URL}")
    print(f"   ER site  : {ER_SITE}")
    print(f"   Poll      : every {POLL_INTERVAL}s  |  stale threshold: {STALE_POS_THRESHOLD}s")

    cache = get_er_cache()
    print(f"   📋  {len(cache)} aircraft already known in ER")

    group_id = get_group_id(SUBJECT_GROUP)
    if group_id:
        print(f"   🗂️  Subject group '{SUBJECT_GROUP}' found ({group_id})")
    else:
        print(f"   ⚠️  Subject group '{SUBJECT_GROUP}' not found — create it in ER Admin → Subject Groups")
    print()

    while True:
        print(f"⏱️  {datetime.now().strftime('%H:%M:%S')}")
        count = run_sync(cache, group_id)
        if count > 0:
            print(f"   ✅  {count} observation(s) posted")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
