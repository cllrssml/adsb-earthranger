"""
compare_adsb.py - ADS-B source comparison: local receiver vs three cloud sources.

Polls the local receiver, adsb.fi, airplanes.live, and OpenSky Network in every
cycle. Logs raw responses and per-cycle metrics — no EarthRanger posting, no
credentials required to start.

Usage:  python compare_adsb.py
Stop:   Ctrl+C
Output: comparison_output/comparison_log.csv
        comparison_output/raw/<source>_<date>.jsonl
"""

import csv
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RECEIVER_URL    = "http://192.168.1.39:8080/data/aircraft.json"

CFW_LAT         = -25.000000
CFW_LON         =  31.000000
RADIUS_KM       = 200           # search radius for cloud sources

POLL_INTERVAL   = 30            # seconds between cycles (safe for OpenSky anonymous)
STALE_THRESHOLD = 30            # positions older than this (seconds) are excluded
OUTPUT_DIR      = Path(__file__).parent / "comparison_output"

OPENSKY_USER    = ""            # leave blank for anonymous (400 credits/day)
OPENSKY_PASS    = ""            # add credentials here if you create an account later

# ---------------------------------------------------------------------------
# Derived constants — do not edit below this line
# ---------------------------------------------------------------------------

RADIUS_NM = int(RADIUS_KM / 1.852)   # 200 km ~ 108 nm

_dlat = RADIUS_KM / 111.32
_dlon = RADIUS_KM / (111.32 * math.cos(math.radians(abs(CFW_LAT))))
BBOX  = {
    "lamin": round(CFW_LAT - _dlat, 4),
    "lamax": round(CFW_LAT + _dlat, 4),
    "lomin": round(CFW_LON - _dlon, 4),
    "lomax": round(CFW_LON + _dlon, 4),
}

OPENSKY_URL = (
    "https://opensky-network.org/api/states/all"
    f"?lamin={BBOX['lamin']}&lomin={BBOX['lomin']}"
    f"&lamax={BBOX['lamax']}&lomax={BBOX['lomax']}"
)
ADSBFI_URL        = f"https://api.adsb.fi/v1/aircraft?lat={CFW_LAT}&lon={CFW_LON}&dist={RADIUS_NM}"
AIRPLANESLIVE_URL = f"https://api.airplanes.live/v2/point/{CFW_LAT}/{CFW_LON}/{RADIUS_NM}"

# ---------------------------------------------------------------------------
# CSV schema
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "timestamp", "cycle",
    # fresh aircraft: positioned, age < STALE_THRESHOLD, airborne
    "n_receiver", "n_adsb_fi", "n_airplanes_live", "n_opensky",
    # on-ground count per source (logged but not included in fresh)
    "gnd_receiver", "gnd_adsb_fi", "gnd_airplanes_live", "gnd_opensky",
    # how many receiver aircraft are also seen by each cloud source
    "rcvr_in_adsb_fi", "rcvr_in_airplanes_live", "rcvr_in_opensky",
    # aircraft in a cloud source that are NOT in the receiver
    "only_adsb_fi", "only_airplanes_live", "only_opensky",
    # average position age of fresh aircraft (seconds)
    "avg_age_receiver", "avg_age_adsb_fi", "avg_age_airplanes_live", "avg_age_opensky",
    # callsign completeness: % of fresh aircraft with a non-empty callsign
    "pct_cs_receiver", "pct_cs_adsb_fi", "pct_cs_airplanes_live", "pct_cs_opensky",
    # error messages (blank if ok)
    "err_receiver", "err_adsb_fi", "err_airplanes_live", "err_opensky",
]

# ---------------------------------------------------------------------------
# Fetch — each returns (raw_list, error_str)
# ---------------------------------------------------------------------------

def fetch_receiver() -> tuple:
    try:
        r = requests.get(RECEIVER_URL, timeout=5)
        r.raise_for_status()
        return r.json().get("aircraft", []), ""
    except Exception as e:
        return [], str(e)[:120]


def fetch_adsbfi() -> tuple:
    try:
        r = requests.get(ADSBFI_URL, timeout=10)
        r.raise_for_status()
        d = r.json()
        # adsb.fi returns {"ac": [...], "now": ..., "ctime": ...}
        return d.get("ac", d.get("aircraft", [])), ""
    except Exception as e:
        return [], str(e)[:120]


def fetch_airplaneslive() -> tuple:
    try:
        r = requests.get(AIRPLANESLIVE_URL, timeout=10)
        r.raise_for_status()
        d = r.json()
        return d.get("ac", d.get("aircraft", [])), ""
    except Exception as e:
        return [], str(e)[:120]


def fetch_opensky() -> tuple:
    try:
        auth = (OPENSKY_USER, OPENSKY_PASS) if OPENSKY_USER else None
        r = requests.get(OPENSKY_URL, auth=auth, timeout=20)
        r.raise_for_status()
        d = r.json()
        # OpenSky time field = UNIX epoch when the snapshot was taken
        api_now = float(d.get("time") or time.time())
        aircraft = []
        for s in (d.get("states") or []):
            # OpenSky state vector indices:
            # 0=icao24  1=callsign  2=origin_country  3=time_position
            # 4=last_contact  5=longitude  6=latitude  7=baro_altitude(m)
            # 8=on_ground  9=velocity(m/s)  10=true_track  11=vertical_rate(m/s)
            # 12=sensors  13=geo_altitude  14=squawk  15=spi  16=position_source
            if len(s) < 9 or s[5] is None or s[6] is None:
                continue
            # Normalise to a dict that looks like dump1090 format as much as possible
            aircraft.append({
                "hex":        (s[0] or "").lower().strip(),
                "lat":        s[6],
                "lon":        s[5],
                "_pos_epoch": s[3] or s[4],   # time_position preferred; fall back to last_contact
                "_api_now":   api_now,
                "flight":     (s[1] or "").strip(),
                "alt_baro":   round(s[7] * 3.28084) if s[7] is not None else None,  # metres → feet
                "gnd":        bool(s[8]),
                "gs":         round(s[9] * 1.94384) if s[9] is not None else None,  # m/s → knots
                "track":      s[10],
                "squawk":     s[14] if len(s) > 14 else None,
            })
        return aircraft, ""
    except Exception as e:
        return [], str(e)[:120]


# ---------------------------------------------------------------------------
# Analysis — normalise and filter one source's aircraft list
# ---------------------------------------------------------------------------

def _pos_age(a: dict) -> float:
    """Return position age in seconds, or a large number if unknown."""
    sp = a.get("seen_pos")
    if sp is not None:
        return float(sp)
    epoch = a.get("_pos_epoch")
    now   = a.get("_api_now")
    if epoch and now:
        return max(0.0, float(now) - float(epoch))
    return 9999.0


def _on_ground(a: dict) -> bool:
    gnd = a.get("gnd") or a.get("ground")
    if gnd is not None:
        return bool(gnd)
    # dump1090 encodes on-ground as the string "ground" in altitude field
    alt = a.get("alt_baro") or a.get("altitude")
    return isinstance(alt, str) and alt.lower() == "ground"


def _in_bbox(a: dict) -> bool:
    lat, lon = a.get("lat"), a.get("lon")
    if lat is None or lon is None:
        return False
    return BBOX["lamin"] <= lat <= BBOX["lamax"] and BBOX["lomin"] <= lon <= BBOX["lomax"]


def analyse(aircraft: list) -> dict:
    """
    Filter to fresh, airborne, in-bbox aircraft.
    Returns: fresh_hexes set, counts, avg position age, callsign %.
    """
    fresh = []
    on_ground_count = 0
    for a in aircraft:
        if not _in_bbox(a):
            continue
        if a.get("lat") is None or a.get("lon") is None:
            continue
        if _pos_age(a) >= STALE_THRESHOLD:
            continue
        if _on_ground(a):
            on_ground_count += 1
            continue
        fresh.append(a)

    hexes = {a["hex"].lower() for a in fresh if a.get("hex")}
    ages  = [_pos_age(a) for a in fresh]
    n     = len(fresh)
    cs_n  = sum(1 for a in fresh if (a.get("flight") or "").strip())

    return {
        "fresh_hexes": hexes,
        "n":           n,
        "gnd":         on_ground_count,
        "avg_age":     round(sum(ages) / n, 1) if ages else 0.0,
        "pct_cs":      round(100 * cs_n / n) if n else 0,
    }


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def open_output() -> tuple:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "raw").mkdir(exist_ok=True)
    csv_path = OUTPUT_DIR / "comparison_log.csv"
    is_new = not csv_path.exists() or csv_path.stat().st_size == 0
    fh = open(csv_path, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
    if is_new:
        writer.writeheader()
    return fh, writer


def log_raw(source: str, aircraft: list, ts: str):
    date = ts[:10]
    path = OUTPUT_DIR / "raw" / f"{source}_{date}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "ac": aircraft}, default=str) + "\n")


def print_cycle(cycle: int, elapsed_min: float, ts: str, r: dict, err: dict):
    sources = [
        ("receiver",       "Receiver      "),
        ("adsb_fi",        "adsb.fi        "),
        ("airplanes_live", "airplanes.live "),
        ("opensky",        "OpenSky        "),
    ]
    rcvr_hex = r["receiver"]["fresh_hexes"]
    rcvr_n   = r["receiver"]["n"]

    print(f"\n  {ts}  (cycle {cycle}, {elapsed_min:.0f}m elapsed)")
    print(f"  {'Source':<17} {'Fresh':>6} {'OnGnd':>6} {'AvgAge':>9} {'CS%':>5}")
    print("  " + "-" * 46)
    for key, label in sources:
        e = err[key]
        if e:
            print(f"  {label} {'ERR':>6}   {e[:38]}")
        else:
            d = r[key]
            print(f"  {label} {d['n']:>6} {d['gnd']:>6} {d['avg_age']:>8.1f}s {d['pct_cs']:>4}%")
    print("  " + "-" * 46)

    if rcvr_n:
        for key, label in sources[1:]:
            matched = len(rcvr_hex & r[key]["fresh_hexes"])
            pct     = round(100 * matched / rcvr_n)
            label   = label.strip()
            print(f"  Coverage vs receiver  {label}: {matched}/{rcvr_n} ({pct}%)")
    else:
        print("  (no fresh receiver aircraft this cycle)")

    print(f"  -> {OUTPUT_DIR / 'comparison_log.csv'}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("ADS-B Source Comparison")
    print(f"  Receiver      : {RECEIVER_URL}")
    print(f"  adsb.fi       : {ADSBFI_URL}")
    print(f"  airplanes.live: {AIRPLANESLIVE_URL}")
    print(f"  OpenSky       : {OPENSKY_URL}")
    print(f"  Bbox          : lat [{BBOX['lamin']}, {BBOX['lamax']}]  "
          f"lon [{BBOX['lomin']}, {BBOX['lomax']}]")
    print(f"  Poll {POLL_INTERVAL}s  |  stale >{STALE_THRESHOLD}s excluded  |  "
          f"OpenSky anonymous: do not reduce poll below 10s")
    print(f"  Output        : {OUTPUT_DIR}")
    print()

    fh, writer = open_output()
    start = time.time()
    cycle = 0

    try:
        while True:
            t0 = time.time()
            cycle += 1
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            ac_recv, err_recv = fetch_receiver()
            ac_adsb, err_adsb = fetch_adsbfi()
            ac_air,  err_air  = fetch_airplaneslive()
            ac_osky, err_osky = fetch_opensky()

            for src, ac in [("receiver",       ac_recv),
                            ("adsb_fi",        ac_adsb),
                            ("airplanes_live", ac_air),
                            ("opensky",        ac_osky)]:
                log_raw(src, ac, ts)

            r = {
                "receiver":       analyse(ac_recv),
                "adsb_fi":        analyse(ac_adsb),
                "airplanes_live": analyse(ac_air),
                "opensky":        analyse(ac_osky),
            }
            err = {
                "receiver":       err_recv,
                "adsb_fi":        err_adsb,
                "airplanes_live": err_air,
                "opensky":        err_osky,
            }

            rcvr = r["receiver"]["fresh_hexes"]
            writer.writerow({
                "timestamp":              ts,
                "cycle":                  cycle,
                "n_receiver":             r["receiver"]["n"],
                "n_adsb_fi":              r["adsb_fi"]["n"],
                "n_airplanes_live":       r["airplanes_live"]["n"],
                "n_opensky":              r["opensky"]["n"],
                "gnd_receiver":           r["receiver"]["gnd"],
                "gnd_adsb_fi":            r["adsb_fi"]["gnd"],
                "gnd_airplanes_live":     r["airplanes_live"]["gnd"],
                "gnd_opensky":            r["opensky"]["gnd"],
                "rcvr_in_adsb_fi":        len(rcvr & r["adsb_fi"]["fresh_hexes"]),
                "rcvr_in_airplanes_live": len(rcvr & r["airplanes_live"]["fresh_hexes"]),
                "rcvr_in_opensky":        len(rcvr & r["opensky"]["fresh_hexes"]),
                "only_adsb_fi":           len(r["adsb_fi"]["fresh_hexes"] - rcvr),
                "only_airplanes_live":    len(r["airplanes_live"]["fresh_hexes"] - rcvr),
                "only_opensky":           len(r["opensky"]["fresh_hexes"] - rcvr),
                "avg_age_receiver":       r["receiver"]["avg_age"],
                "avg_age_adsb_fi":        r["adsb_fi"]["avg_age"],
                "avg_age_airplanes_live": r["airplanes_live"]["avg_age"],
                "avg_age_opensky":        r["opensky"]["avg_age"],
                "pct_cs_receiver":        r["receiver"]["pct_cs"],
                "pct_cs_adsb_fi":         r["adsb_fi"]["pct_cs"],
                "pct_cs_airplanes_live":  r["airplanes_live"]["pct_cs"],
                "pct_cs_opensky":         r["opensky"]["pct_cs"],
                "err_receiver":           err_recv,
                "err_adsb_fi":            err_adsb,
                "err_airplanes_live":     err_air,
                "err_opensky":            err_osky,
            })
            fh.flush()

            print_cycle(cycle, (time.time() - start) / 60, ts, r, err)

            time.sleep(max(0.0, POLL_INTERVAL - (time.time() - t0)))

    except KeyboardInterrupt:
        print("\n\nStopped.")
    finally:
        fh.close()
        print(f"Results saved to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
