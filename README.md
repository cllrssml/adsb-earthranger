# ADS-B → EarthRanger Integration

Polls a local [dump1090](https://github.com/flightaware/dump1090) / [readsb](https://github.com/wiedehopf/readsb) ADS-B receiver and streams live aircraft positions into [EarthRanger](https://www.earthranger.com/) as tracked subjects.

Designed for conservation operations centres that run an ADS-B receiver on-site and want real-time airspace awareness alongside their wildlife and ranger tracking. Includes automatic cloud fallback (airplanes.live or adsb.lol) if the local receiver goes offline, and automatically removes aircraft from the map after they leave range.

---

## How it works

```
[ADS-B Receiver]  ──local LAN──▶  [adsb-earthranger.py]  ──internet──▶  [EarthRanger]
  dump1090/readsb                    runs on ops room PC                  Subjects + Tracks
  :8080/data/aircraft.json
            ↕ fallback
  [airplanes.live / adsb.lol]   (cloud source, used when receiver is unreachable)
```

Every 10 seconds the script:

1. Fetches the live aircraft JSON from the receiver (or cloud fallback if the receiver is down)
2. Filters out aircraft with no position fix or a stale fix (> `STALE_POS_THRESHOLD` seconds)
3. For each **new** aircraft: resolves a display name, then auto-registers a Source, Subject, and SubjectSource link in EarthRanger and adds it to the **ADS-B** subject group
4. For all visible aircraft: posts an Observation with position, altitude, ground speed, heading, climb rate, squawk, and ADS-B category
5. **Auto-corrects aircraft type**: if an aircraft's ADS-B transponder category changes (e.g. we learn it is a helicopter after initially registering it as a plane), the EarthRanger subject subtype and map icon are updated automatically
6. **Removes from map**: any aircraft not seen for `INACTIVE_TIMEOUT` seconds is patched `is_active: false` in EarthRanger so it disappears from the live map
7. **Reactivates**: if a removed aircraft reappears it is patched back to `is_active: true` immediately

### Aircraft naming

Aircraft are identified by their ICAO 24-bit hex address. The display name is resolved in this order:

1. **ADS-B callsign** — used when the aircraft broadcasts one (e.g. `QTR1375`)
2. **adsbdb.com lookup** — if no callsign is broadcast, the ICAO hex is queried against the [adsbdb.com](https://www.adsbdb.com) public aircraft database to retrieve the civil registration (e.g. `ZS-SXD`). Results are cached per session so each aircraft is looked up at most once.
3. **ICAO hex fallback** — if adsbdb has no record, the subject is named `ICAO-<HEX>` (e.g. `ICAO-00B1F2`)

### Startup

On startup the script loads all existing ADS-B sources and their linked EarthRanger subjects. This avoids re-registering aircraft already in ER and allows map retirement to work immediately for aircraft tracked in a previous session. On a large ER instance this may take 10–20 seconds as it pages through all subject-source records.

---

## Prerequisites

- Python 3.10+
- A local ADS-B receiver running dump1090, readsb, or compatible software (exposes `GET /data/aircraft.json`)
- An EarthRanger account with a bearer token
- The receiver must be reachable from the machine running this script (same LAN or VPN)

### EarthRanger setup (one-time)

Before the first run, two things must exist in your ER instance:

1. **Subject group** named `ADS-B`
   → ER Admin → Subject Groups → Add Subject Group

2. **Subject subtypes** under the `Aircraft` subject type (most are present by default):
   `plane`, `helicopter`, `drone`, `hot_air_balloon`
   → ER Admin → Subject Sub Types → filter by Aircraft

The `adsb_receiver` source provider is created automatically on first run.

---

## Quick install (Windows)

Open PowerShell **as Administrator** and run:

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force
Invoke-WebRequest https://raw.githubusercontent.com/cllrssml/adsb-earthranger/main/install.ps1 -OutFile install.ps1 -UseBasicParsing
.\install.ps1
```

The installer will:
- Install Python 3.12 automatically if not already present (via winget)
- Download the latest release from GitHub (no Git required)
- Prompt for your EarthRanger URL, token, and receiver IP
- Register a Task Scheduler task that starts at boot and auto-restarts on failure

When prompted for `ER_SITE`, enter the URL **without** a trailing slash:
```
https://your-site.pamdas.org        ✅
https://your-site.pamdas.org/       ❌
```

Monitor the output afterwards with:

```powershell
Get-Content C:\adsb-earthranger\output.log -Wait -Tail 20 -Encoding UTF8
```

### Updating

Re-run the installer at any time to pull the latest version. Your `.env` is preserved automatically.

### Uninstalling

```powershell
Stop-ScheduledTask -TaskName adsb-earthranger -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName adsb-earthranger -Confirm:$false
Remove-Item "C:\adsb-earthranger" -Recurse -Force
```

---

## Manual installation

```bash
git clone https://github.com/cllrssml/adsb-earthranger.git
cd adsb-earthranger
pip install -r requirements.txt
```

---

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `ER_SITE` | ✅ | — | EarthRanger base URL, e.g. `https://your-site.pamdas.org` |
| `ER_TOKEN` | ✅ | — | EarthRanger bearer token |
| `ADSB_URL` | | `http://localhost:8080/data/aircraft.json` | ADS-B receiver JSON endpoint |
| `POLL_INTERVAL` | | `10` | Seconds between receiver polls |
| `STALE_POS_THRESHOLD` | | `30` | Max age (seconds) of a position fix before it's skipped |
| `INACTIVE_TIMEOUT` | | `300` | Seconds without a sighting before a subject is removed from the map |
| `RECEIVER_FAIL_THRESHOLD` | | `3` | Consecutive receiver failures before switching to cloud fallback |
| `CLOUD_SOURCE` | | `airplanes.live` | Cloud fallback source: `airplanes.live` or `adsb.lol` |
| `CLOUD_LAT` | | — | Latitude of centre point for cloud queries (required to enable fallback) |
| `CLOUD_LON` | | — | Longitude of centre point for cloud queries (required to enable fallback) |
| `CLOUD_RADIUS_NM` | | `107` | Radius in nautical miles for cloud queries |

### Cloud fallback

When `CLOUD_LAT` and `CLOUD_LON` are set, the script will automatically switch to the configured cloud ADS-B source after `RECEIVER_FAIL_THRESHOLD` consecutive receiver failures, and switch back when the receiver recovers. To enable, add to your `.env`:

```
CLOUD_LAT=<your-latitude>
CLOUD_LON=<your-longitude>
CLOUD_RADIUS_NM=107
CLOUD_SOURCE=airplanes.live
```

Both [airplanes.live](https://airplanes.live) and [adsb.lol](https://adsb.lol) provide free public ADS-B feeds. Coverage varies by region — run the included `compare_adsb.py` to test which source has better coverage for your location before committing to one.

---

## Running

```bash
python main.py
```

Example output:

```
ADS-B -> EarthRanger  starting up
   Receiver : http://192.168.x.x:8080/data/aircraft.json
   ER site  : https://your-site.pamdas.org
   Poll      : every 10s  |  stale threshold: 30s
   Inactive timeout: 300s (subjects removed from map after this long unseen)
   Cloud fallback: airplanes.live  lat=-25.0 lon=31.0 r=107nm  (after 3 receiver failures)
   Subjectsources: 4500 scanned (4500 total), 12 matched ADS-B aircraft
   12 aircraft already known in ER
   Subject group 'ADS-B' found (xxxxxxxx-...)

08:12:04
   Registering: ABC123 (aabbcc)  subtype=plane        ← has callsign
   Added ABC123 to 'ADS-B' group
   Registering: ZS-XYZ (001234)  subtype=plane        ← no callsign; name from adsbdb.com
   Added ZS-XYZ to 'ADS-B' group
   Registering: ICAO-00B1F2 (00b1f2)  subtype=plane   ← no callsign, not in adsbdb
   Added ICAO-00B1F2 to 'ADS-B' group
   3 observation(s) posted
08:12:14
   Updated ABC123: subtype plane -> helicopter         ← ADS-B category updated; icon changes
   3 observation(s) posted
08:12:25
   3 observation(s) posted
08:17:40
   Retire ZS-XYZ (001234): HTTP 200, is_active=False (was unseen 312s)
   3 observation(s) posted
08:19:10
   Receiver unreachable (3 fails) -- switching to airplanes.live
   2 observation(s) posted
08:25:30
   Receiver restored -- switching back from airplanes.live
   3 observation(s) posted
08:31:05
   Reactivated on map: ZS-XYZ                         ← aircraft reappeared
   3 observation(s) posted
```

---

## Deploying on Windows (ops room PC)

Use the **Quick install** script above — it handles everything automatically.

To update to a newer version, re-run the installer. Your `.env` is preserved automatically.

### Option B — NSSM (alternative to Task Scheduler)

[NSSM](https://nssm.cc) wraps the script as a proper Windows service (survives logoff):

```cmd
nssm install adsb-earthranger "C:\Python312\python.exe" "C:\adsb-earthranger\main.py"
nssm set adsb-earthranger AppDirectory "C:\adsb-earthranger"
nssm set adsb-earthranger AppRestartDelay 5000
nssm start adsb-earthranger
```

### Option C — Linux / WSL (systemd)

A systemd unit file is included. Copy it to your user services directory:

```bash
cp adsb-earthranger.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now adsb-earthranger
journalctl --user -u adsb-earthranger -f   # follow logs
```

---

## ADS-B category mapping

EarthRanger subject subtypes are assigned from the ADS-B `category` field broadcast by each aircraft's transponder:

| ADS-B Category | Aircraft type | ER Subject Subtype |
|---|---|---|
| A1 – A6 (default) | Powered fixed-wing (all sizes) | `plane` |
| A7 | Rotorcraft | `helicopter` |
| B2 | Lighter than air | `hot_air_balloon` |
| B6 | UAV / drone | `drone` |

To customise the mapping, edit `CATEGORY_SUBTYPES` in `main.py`.

---

## EarthRanger data model

| ER concept | Maps to |
|---|---|
| **Source** | ADS-B transponder (keyed on ICAO hex, provider `adsb_receiver`) |
| **Subject** | The aircraft (named by callsign, typed by category) |
| **SubjectSource** | Permanent link between transponder and aircraft |
| **Observation** | Each position fix (lat/lon + altitude, speed, heading, climb rate, squawk) |
| **Subject Group** | All ADS-B traffic grouped under `ADS-B` |

---

## License

MIT
