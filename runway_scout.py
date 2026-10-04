#!/usr/bin/env python3
"""
RUNWAY SCOUT
============
Automated land-listing screener for private airstrip suitability.
Companion to Airstrip_Property_Analyzer.xlsx.

WHAT IT DOES (one run):
  1. Pulls active vacant-land sale listings from the RentCast API
     (licensed data, ToS permits storage and derivative use) for each
     search area defined below.
  2. Filters by price and minimum acreage.
  3. For each new candidate, samples ground elevation along several
     candidate runway headings using the USGS 3DEP Elevation Point
     Query Service (public domain, no key required).
  4. Computes: hot/cool-day density altitude, required runway length
     (same math and levers as the workbook), the longest workable
     "flat enough" run it can find, grade, wind alignment, and a score.
  5. Appends results to runway_candidates.csv. Existing rows are kept
     and re-marked, so the file accumulates history across runs.

SETUP (one time):
  pip install requests
  Get a free RentCast API key: https://app.rentcast.io  (free tier is
  fine for a monthly cadence; check current request limits/pricing)
  export RENTCAST_API_KEY="your-key-here"     (Windows: setx RENTCAST_API_KEY ...)

RUN:
  python runway_scout.py
  Schedule weekly/monthly with cron (Linux/Mac) or Task Scheduler (Windows), e.g.:
  0 7 1 * * cd /path/to/tool && python runway_scout.py   (monthly, 7am on the 1st)

HONEST LIMITATIONS (read this):
  - RentCast aggregates MLS-adjacent sources. Big rural parcels listed
    ONLY on LandWatch/land broker sites may not appear. Keep a LandWatch
    email alert running as your backstop and paste those into the
    workbook by hand.
  - The terrain scan runs transects through the LISTING COORDINATE
    (usually the parcel centroid) because listing feeds don't include
    parcel boundary geometry. The scan length is capped by a
    square-parcel diagonal estimate from acreage. A long skinny parcel
    can be better than this estimates; a weird-shaped one worse. Verify
    finalists against the county GIS parcel viewer before you get excited.
  - This is a screening tool. POH, site walk, county planner. In that order.
"""

import csv
import json
import math
import os
import sys
import time
from datetime import date

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests")

# ============================= CONFIG =============================

CONFIG = {
    # Paste your RentCast key between the quotes below, OR set the
    # RENTCAST_API_KEY environment variable. Never share a copy with your key in it.
    "api_key": "" or os.environ.get("RENTCAST_API_KEY", ""),
    "max_price": 750_000,       # skip listings above this
    "min_acres": 35,            # below this, geometry almost never works
    "out_csv": "runway_candidates.csv",
    "elev_cache_file": "elevation_cache.json",
    "transect_spacing_ft": 150, # elevation sample spacing along a heading
    "max_scan_ft": 5000,        # never scan longer than this per transect
}

# Search areas: center point + radius (miles). Regional climate/wind
# planning defaults mirror the Assumptions tab of the workbook.
AREAS = [
    {"name": "Columbia Gorge (WA side) - Goldendale/Centerville",
     "lat": 45.795, "lng": -120.85, "radius": 25,
     "hot_f": 95, "cool_f": 40, "wind_deg": 270, "wind_kt": 18},
    {"name": "Columbia Gorge (WA side) - Lyle/Klickitat",
     "lat": 45.70, "lng": -121.20, "radius": 15,
     "hot_f": 95, "cool_f": 40, "wind_deg": 270, "wind_kt": 18},
    {"name": "Central Oregon - Madras/Prineville/Redmond",
     "lat": 44.40, "lng": -121.05, "radius": 40,
     "hot_f": 92, "cool_f": 30, "wind_deg": 300, "wind_kt": 10},
    {"name": "Eastern Oregon - Christmas Valley/Fort Rock",
     "lat": 43.25, "lng": -120.75, "radius": 45,
     "hot_f": 92, "cool_f": 25, "wind_deg": 250, "wind_kt": 12},
]

# POH sea-level, ISA, max gross, over-50-ft distances. Verify vs your POH.
AIRCRAFT = {
    "C172S": {"to50": 1630, "ldg50": 1335},
    "C182T": {"to50": 1514, "ldg50": 1350},
}
DESIGN_AIRCRAFT = "C172S"

LEVERS = {
    "da_factor_per_1000": 0.10,  # compounding distance increase per 1,000 ft DA
    "surface_factor": 1.15,      # grass vs paved
    "pilot_margin": 1.25,        # over-book margin
    "max_grade": 0.02,           # 2% longitudinal
    "max_roughness_ft": 3.0,     # deviation from a straight graded line,
                                 # i.e. cut/fill workable with compact equipment
    "max_crosswind_kt": 12,
}

FT_PER_DEG_LAT = 364_000.0

# ============================= PERFORMANCE MATH =============================

def density_altitude(elev_ft: float, oat_f: float) -> float:
    """DA = elevation + 120 * (OAT_C - ISA_C at elevation)."""
    oat_c = (oat_f - 32.0) * 5.0 / 9.0
    isa_c = 15.0 - 1.98 * elev_ft / 1000.0
    return elev_ft + 120.0 * (oat_c - isa_c)


def required_runway_ft(elev_ft: float, oat_f: float) -> float:
    ac = AIRCRAFT[DESIGN_AIRCRAFT]
    base = max(ac["to50"], ac["ldg50"])
    da = max(density_altitude(elev_ft, oat_f), 0.0)
    adj = (1.0 + LEVERS["da_factor_per_1000"]) ** (da / 1000.0)
    return base * adj * LEVERS["surface_factor"] * LEVERS["pilot_margin"]


def crosswind_kt(runway_deg: float, wind_deg: float, wind_kt: float) -> float:
    diff = abs(runway_deg - wind_deg) % 180.0
    diff = min(diff, 180.0 - diff)
    return wind_kt * math.sin(math.radians(diff))

# ============================= ELEVATION =============================

def load_cache(path: str) -> dict:
    if os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)
    return {}


def save_cache(path: str, cache: dict) -> None:
    with open(path, "w") as fh:
        json.dump(cache, fh)


def epqs_elevation_ft(lat: float, lng: float, cache: dict) -> float | None:
    """USGS 3DEP Elevation Point Query Service. Public domain."""
    key = f"{lat:.5f},{lng:.5f}"
    if key in cache:
        return cache[key]
    url = ("https://epqs.nationalmap.gov/v1/json"
           f"?x={lng}&y={lat}&wkid=4326&units=Feet&includeDate=false")
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 200:
                val = r.json().get("value")
                elev = float(val) if val not in (None, "None") else None
                if elev is not None and elev > -1000:
                    cache[key] = elev
                    return elev
        except (requests.RequestException, ValueError, KeyError):
            pass
        time.sleep(1.5 * (attempt + 1))
    return None


def transect(lat: float, lng: float, heading_deg: float,
             half_len_ft: float, spacing_ft: float) -> list[tuple[float, float]]:
    """Points along heading through (lat,lng), from -half_len to +half_len."""
    ft_per_deg_lng = FT_PER_DEG_LAT * math.cos(math.radians(lat))
    h = math.radians(heading_deg)
    n = int(half_len_ft // spacing_ft)
    pts = []
    for i in range(-n, n + 1):
        d = i * spacing_ft
        dlat = (d * math.cos(h)) / FT_PER_DEG_LAT
        dlng = (d * math.sin(h)) / ft_per_deg_lng
        pts.append((lat + dlat, lng + dlng))
    return pts


def best_run(elevs: list[float | None], spacing_ft: float) -> tuple[float, float, float]:
    """
    Longest contiguous window that could be graded into a runway:
    end-to-end grade <= max_grade AND max deviation from the window's
    best-fit line <= max_roughness_ft (proxy for cut/fill volume).
    Returns (run_ft, grade, roughness_ft) of the best window found.
    """
    pts = [(i * spacing_ft, e) for i, e in enumerate(elevs) if e is not None]
    best = (0.0, 0.0, 0.0)
    n = len(pts)
    for i in range(n):
        for j in range(n - 1, i, -1):
            run = pts[j][0] - pts[i][0]
            if run <= best[0]:
                break
            window = pts[i:j + 1]
            # contiguity check: no gaps bigger than 2 spacings
            gaps = [window[k + 1][0] - window[k][0] for k in range(len(window) - 1)]
            if gaps and max(gaps) > 2.05 * spacing_ft:
                continue
            grade = abs(window[-1][1] - window[0][1]) / run
            if grade > LEVERS["max_grade"]:
                continue
            # roughness vs best-fit line
            xs = [p[0] for p in window]
            ys = [p[1] for p in window]
            m = len(xs)
            xbar, ybar = sum(xs) / m, sum(ys) / m
            denom = sum((x - xbar) ** 2 for x in xs) or 1.0
            slope = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / denom
            rough = max(abs(y - (ybar + slope * (x - xbar))) for x, y in zip(xs, ys))
            if rough > LEVERS["max_roughness_ft"]:
                continue
            if run > best[0]:
                best = (run, grade, rough)
    return best


def scan_property(lat: float, lng: float, acres: float, area: dict,
                  cache: dict) -> dict:
    """Scan transects through the listing point on wind-aligned headings."""
    diag_ft = math.sqrt(2.0 * acres * 43_560.0)          # square-parcel diagonal
    half = min(diag_ft / 2.0, CONFIG["max_scan_ft"] / 2.0)
    spacing = CONFIG["transect_spacing_ft"]
    headings = [area["wind_deg"] % 180,
                (area["wind_deg"] - 30) % 180,
                (area["wind_deg"] + 30) % 180]
    result = {"best_run_ft": 0.0, "best_heading": None,
              "grade": None, "roughness_ft": None, "center_elev_ft": None}
    for hd in headings:
        pts = transect(lat, lng, hd, half, spacing)
        elevs = [epqs_elevation_ft(a, b, cache) for a, b in pts]
        mid = elevs[len(elevs) // 2]
        if mid is not None and result["center_elev_ft"] is None:
            result["center_elev_ft"] = mid
        run, grade, rough = best_run(elevs, spacing)
        if run > result["best_run_ft"]:
            result.update(best_run_ft=run, best_heading=hd,
                          grade=grade, roughness_ft=rough)
        time.sleep(0.1)
    return result

# ============================= LISTINGS =============================

def fetch_listings(area: dict) -> list[dict]:
    """RentCast /listings/sale, propertyType=Land, paginated."""
    out, offset = [], 0
    headers = {"X-Api-Key": CONFIG["api_key"], "Accept": "application/json"}
    while True:
        params = {
            "latitude": area["lat"], "longitude": area["lng"],
            "radius": area["radius"], "propertyType": "Land",
            "status": "Active", "limit": 500, "offset": offset,
        }
        try:
            r = requests.get("https://api.rentcast.io/v1/listings/sale",
                             headers=headers, params=params, timeout=30)
        except requests.RequestException as exc:
            print(f"  ! network error: {exc}")
            break
        if r.status_code == 401:
            sys.exit("RentCast rejected the API key. Set RENTCAST_API_KEY.")
        if r.status_code != 200:
            print(f"  ! RentCast HTTP {r.status_code}: {r.text[:200]}")
            break
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if len(batch) < 500:
            break
        offset += 500
    return out


def listing_acres(rec: dict) -> float | None:
    lot = rec.get("lotSize")
    if lot in (None, 0):
        return None
    return float(lot) / 43_560.0

# ============================= OUTPUT =============================

FIELDS = ["id", "first_seen", "last_seen", "status", "address", "county",
          "region", "price", "acres", "price_per_acre", "lat", "lng",
          "elev_ft", "da_hot_ft", "da_cool_ft", "req_hot_ft", "req_cool_ft",
          "best_run_ft", "best_heading_deg", "grade_pct", "roughness_ft",
          "crosswind_kt", "fit", "score", "map_link"]


def score_row(row: dict) -> int:
    if row["fit"] == "TOO SHORT":
        return 0
    s = 40 if row["fit"] == "ALL-SEASON" else 25
    xw = row["crosswind_kt"] or 0
    s += max(0, round(20 * (1 - xw / LEVERS["max_crosswind_kt"])))
    s += 15 if (row["grade_pct"] or 99) <= LEVERS["max_grade"] * 100 else 0
    s += 10 if (row["roughness_ft"] or 99) <= LEVERS["max_roughness_ft"] else 0
    s += 15  # approach obstacles unknown from a desk; verify on the GIS/site walk
    return s


def main() -> None:
    if not CONFIG["api_key"]:
        sys.exit("Set RENTCAST_API_KEY first (free key at app.rentcast.io).")

    cache = load_cache(CONFIG["elev_cache_file"])
    existing: dict[str, dict] = {}
    if os.path.exists(CONFIG["out_csv"]):
        with open(CONFIG["out_csv"], newline="") as fh:
            existing = {r["id"]: r for r in csv.DictReader(fh)}

    today = date.today().isoformat()
    seen_ids = set()

    for area in AREAS:
        print(f"\n=== {area['name']} ===")
        listings = fetch_listings(area)
        print(f"  {len(listings)} active land listings returned")
        for rec in listings:
            rid = str(rec.get("id") or rec.get("formattedAddress", ""))
            if not rid or rid in seen_ids:
                continue
            seen_ids.add(rid)
            acres = listing_acres(rec)
            price = rec.get("price")
            lat, lng = rec.get("latitude"), rec.get("longitude")
            if not all([acres, price, lat, lng]):
                continue
            if acres < CONFIG["min_acres"] or price > CONFIG["max_price"]:
                continue

            if rid in existing:                      # already analyzed
                existing[rid]["last_seen"] = today
                existing[rid]["status"] = "ACTIVE"
                continue

            print(f"  scanning: {rec.get('formattedAddress','?')} "
                  f"({acres:.0f} ac, ${price:,.0f})")
            scan = scan_property(lat, lng, acres, area, cache)
            elev = scan["center_elev_ft"]
            if elev is None:
                print("    ! no elevation data, skipped")
                continue

            req_hot = required_runway_ft(elev, area["hot_f"])
            req_cool = required_runway_ft(elev, area["cool_f"])
            run = scan["best_run_ft"]
            fit = ("ALL-SEASON" if run >= req_hot else
                   "COOL-DAY ONLY" if run >= req_cool else "TOO SHORT")
            xw = (crosswind_kt(scan["best_heading"], area["wind_deg"],
                               area["wind_kt"])
                  if scan["best_heading"] is not None else None)

            row = {
                "id": rid, "first_seen": today, "last_seen": today,
                "status": "NEW",
                "address": rec.get("formattedAddress", ""),
                "county": rec.get("county", ""),
                "region": area["name"], "price": round(price),
                "acres": round(acres, 1),
                "price_per_acre": round(price / acres),
                "lat": lat, "lng": lng,
                "elev_ft": round(elev),
                "da_hot_ft": round(density_altitude(elev, area["hot_f"])),
                "da_cool_ft": round(density_altitude(elev, area["cool_f"])),
                "req_hot_ft": round(req_hot), "req_cool_ft": round(req_cool),
                "best_run_ft": round(run),
                "best_heading_deg": scan["best_heading"],
                "grade_pct": (round(scan["grade"] * 100, 2)
                              if scan["grade"] is not None else None),
                "roughness_ft": (round(scan["roughness_ft"], 1)
                                 if scan["roughness_ft"] is not None else None),
                "crosswind_kt": round(xw, 1) if xw is not None else None,
                "fit": fit,
                "map_link": f"https://www.google.com/maps?q={lat},{lng}",
            }
            row["score"] = score_row(row)
            existing[rid] = {k: ("" if v is None else v) for k, v in row.items()}
            save_cache(CONFIG["elev_cache_file"], cache)

    # anything not seen this run is off-market
    for rid, row in existing.items():
        if rid not in seen_ids and row.get("status") != "OFF-MARKET":
            row["status"] = "OFF-MARKET"

    rows = sorted(existing.values(),
                  key=lambda r: int(r.get("score") or 0), reverse=True)
    with open(CONFIG["out_csv"], "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    save_cache(CONFIG["elev_cache_file"], cache)

    fresh = [r for r in rows if r["status"] == "NEW"]
    print(f"\nDone. {len(rows)} tracked, {len(fresh)} new this run "
          f"-> {CONFIG['out_csv']}")
    for r in fresh[:10]:
        print(f"  [{r['score']:>3}] {r['fit']:<14} {r['address']} "
              f"({r['acres']} ac, ${int(r['price']):,}) {r['map_link']}")


if __name__ == "__main__":
    main()
