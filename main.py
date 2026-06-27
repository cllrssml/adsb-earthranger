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

ER_SITE   = (os.environ.get("ER_SITE") or "").rstrip("/")  # e.g. https://your-site.pamdas.org
ER_TOKEN  = os.environ.get("ER_TOKEN")
ADSB_URL  = os.environ.get("ADSB_URL", "http://localhost:8080/data/aircraft.json")

POLL_INTERVAL           = int(os.environ.get("POLL_INTERVAL",           "10"))   # seconds
STALE_POS_THRESHOLD     = int(os.environ.get("STALE_POS_THRESHOLD",     "30"))   # seconds
INACTIVE_TIMEOUT        = int(os.environ.get("INACTIVE_TIMEOUT",        "300"))  # seconds before removing from map
RECEIVER_FAIL_THRESHOLD = int(os.environ.get("RECEIVER_FAIL_THRESHOLD", "3"))    # consecutive failures before cloud

# Cloud fallback — disabled unless CLOUD_LAT and CLOUD_LON are both set.
CLOUD_SOURCE    = os.environ.get("CLOUD_SOURCE",    "airplanes.live")  # "airplanes.live" or "adsb.lol"
CLOUD_LAT       = os.environ.get("CLOUD_LAT",       "")
CLOUD_LON       = os.environ.get("CLOUD_LON",       "")
CLOUD_RADIUS_NM = int(os.environ.get("CLOUD_RADIUS_NM", "107"))

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

# Cloud fallback state
_receiver_fails = 0
_using_cloud    = False


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
    """Return {icao_hex: {"source": id, "subject": id_or_None, "subtype": str_or_None, ...}}."""
    try:
        r = session.get(
            f"{ER_SITE}/api/v1.0/sources/",
            params={"provider": PROVIDER, "page_size": 1000},
        )
        if r.status_code == 200:
            results = r.json().get("data", {}).get("results", []) or r.json().get("results", [])
            # Initialise last_seen to now so existing subjects aren't retired immediately
            # on startup before we've had a chance to observe them.
            now = time.monotonic()
            return {
                s["manufacturer_id"]: {
                    "source":    s["id"],
                    "subject":   None,
                    "subtype":   None,
                    "last_seen": now,
                    "er_active": True,
                }
                for s in results
            }
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
        print(f"   Warning: Group lookup error: {e}")
    return None


def add_to_group(group_id: str, subject_id: str, name: str):
    r = session.post(
        f"{ER_SITE}/api/v1.0/subjectgroup/{group_id}/subjects/",
        json=[{"id": subject_id}],
    )
    if r.status_code in [200, 201]:
        print(f"   Added {name} to '{SUBJECT_GROUP}' group")
    else:
        print(f"   Warning: Group add failed for {name}: {r.status_code} {r.text[:80]}")


def ensure_aircraft(hex_code: str, callsign: str, subtype: str, cache: dict, group_id: str | None):
    """Upsert source + subject + assignment + group membership. Returns source_id or None."""
    if hex_code in cache:
        entry = cache[hex_code]

        # Reactivate on map if it was previously retired.
        if not entry.get("er_active", True):
            subj_id = _resolve_subject_id(entry["source"], entry)
            if subj_id:
                r = session.patch(f"{ER_SITE}/api/v1.0/subject/{subj_id}/",
                                  json={"is_active": True})
                if r.status_code in [200, 201]:
                    name = callsign.strip() if callsign and callsign.strip() else f"ICAO-{hex_code.upper()}"
                    print(f"   Reactivated on map: {name}")
                    entry["er_active"] = True

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
        print(f"   Could not create/find source for {hex_code}: {r.text[:120]}")
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

    cache[hex_code] = {
        "source":    source_id,
        "subject":   subj_id,
        "subtype":   subtype,
        "last_seen": time.monotonic(),
        "er_active": True,
    }
    return source_id


def fetch_cloud_aircraft() -> list[dict]:
    """Fetch aircraft from the configured cloud ADS-B source."""
    try:
        if CLOUD_SOURCE == "adsb.lol":
            url = f"https://api.adsb.lol/v2/lat/{CLOUD_LAT}/lon/{CLOUD_LON}/dist/{CLOUD_RADIUS_NM}"
        else:  # airplanes.live
            url = f"https://api.airplanes.live/v2/point/{CLOUD_LAT}/{CLOUD_LON}/{CLOUD_RADIUS_NM}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json().get("ac", [])
    except Exception as e:
        print(f"   Cloud fallback ({CLOUD_SOURCE}) failed: {e}")
        return []


def fetch_aircraft() -> list[dict]:
    """Try the local receiver; fall back to the cloud source after repeated failures."""
    global _receiver_fails, _using_cloud
    cloud_available = bool(CLOUD_LAT and CLOUD_LON)

    try:
        r = requests.get(ADSB_URL, timeout=5)
        r.raise_for_status()
        ac = r.json().get("aircraft", [])
        if _using_cloud:
            print(f"   Receiver restored — switching back from {CLOUD_SOURCE}")
            _using_cloud = False
        _receiver_fails = 0
        return ac
    except Exception as e:
        _receiver_fails += 1
        if cloud_available and _receiver_fails >= RECEIVER_FAIL_THRESHOLD:
            if not _using_cloud:
                print(f"   Receiver unreachable ({_receiver_fails} fails) — switching to {CLOUD_SOURCE}")
                _using_cloud = True
            return fetch_cloud_aircraft()
        print(f"   ADS-B fetch failed ({_receiver_fails}/{RECEIVER_FAIL_THRESHOLD}): {e}")
        return []


def retire_stale_subjects(cache: dict, now: float):
    """Patch is_active=False in ER for aircraft not seen for INACTIVE_TIMEOUT seconds."""
    for hex_code, entry in cache.items():
        if not entry.get("er_active", True):
            continue  # already inactive
        last_seen = entry.get("last_seen")
        if last_seen is None:
            continue
        elapsed = now - last_seen
        if elapsed < INACTIVE_TIMEOUT:
            continue
        subj_id = _resolve_subject_id(entry["source"], entry)
        if not subj_id:
            continue
        r = session.patch(f"{ER_SITE}/api/v1.0/subject/{subj_id}/",
                          json={"is_active": False})
        if r.status_code in [200, 201]:
            print(f"   Removed from map: ICAO-{hex_code.upper()} (not seen for {int(elapsed)}s)")
            entry["er_active"] = False


def run_sync(cache: dict, group_id: str | None) -> int:
    now = time.monotonic()
    all_aircraft = fetch_aircraft()

    positioned = [
        a for a in all_aircraft
        if "lat" in a and "lon" in a and a.get("seen_pos", 999) < STALE_POS_THRESHOLD
    ]

    if not positioned:
        source_label = CLOUD_SOURCE if _using_cloud else "receiver"
        print(f"   No positioned aircraft  ({len(all_aircraft)} from {source_label}, none with fresh fix)")
        retire_stale_subjects(cache, now)
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

        # Mark seen and ensure active state is correct.
        cache[hex_code]["last_seen"] = now
        cache[hex_code]["er_active"] = True

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
            print(f"   Observation failed for {hex_code}: {p.status_code} {p.text[:80]}")

    retire_stale_subjects(cache, now)
    return count


def main():
    print("ADS-B -> EarthRanger  starting up")
    print(f"   Receiver : {ADSB_URL}")
    print(f"   ER site  : {ER_SITE}")
    print(f"   Poll      : every {POLL_INTERVAL}s  |  stale threshold: {STALE_POS_THRESHOLD}s")
    print(f"   Inactive timeout: {INACTIVE_TIMEOUT}s (subjects removed from map after this long unseen)")
    if CLOUD_LAT and CLOUD_LON:
        print(f"   Cloud fallback: {CLOUD_SOURCE}  lat={CLOUD_LAT} lon={CLOUD_LON} r={CLOUD_RADIUS_NM}nm"
              f"  (after {RECEIVER_FAIL_THRESHOLD} receiver failures)")
    else:
        print(f"   Cloud fallback: disabled (set CLOUD_LAT + CLOUD_LON to enable)")
    print()

    cache = get_er_cache()
    print(f"   {len(cache)} aircraft already known in ER")

    group_id = get_group_id(SUBJECT_GROUP)
    if group_id:
        print(f"   Subject group '{SUBJECT_GROUP}' found ({group_id})")
    else:
        print(f"   Warning: Subject group '{SUBJECT_GROUP}' not found -- create it in ER Admin -> Subject Groups")
    print()

    while True:
        print(f"{datetime.now().strftime('%H:%M:%S')}")
        count = run_sync(cache, group_id)
        if count > 0:
            print(f"   {count} observation(s) posted")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
