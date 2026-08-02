"""Rebuild every input the analysis depends on, from its original public source.

    python build_data.py

Writes into data/:

    zcta_boundaries.geojson    the 17 ZIP areas, from the Census cartographic
                               boundary file
    crime_incidents_2025.csv   every Prince William County police incident
                               reported in calendar 2025, with coordinates
    indicators.csv             the six indicators per ZIP, with the crime rate
                               derived here rather than asserted

Why this file exists
--------------------
The project originally scored crime from `Crime Reports.csv`, a 16,000-row
export. That file turns out to be truncated: the county's feature service caps a
single request at exactly 16,000 records, and the service reports 23,914
incidents inside the same date window the export covers. The export therefore
holds about two thirds of its own window, and there is no reason to think the
missing third is spread evenly across ZIP codes. A rate computed from it is not
a rate.

So the incidents are pulled here directly from the service, paginated past the
cap, and assigned to ZIP areas by point-in-polygon. The service does publish a
ZipCode field, but it is null on every public record, so the spatial join is the
only route.

Coverage caveat, which the analysis has to carry
------------------------------------------------
This is the *county* police department's records system. Two of the seventeen
areas are not its jurisdiction:

    20110   City of Manassas — independent city, its own police department
    22134   MCB Quantico — military installation, federal policing

Incident counts for those ZIPs are undercounts of real crime, not measurements
of a safer place. 20111 partly overlaps Manassas Park, also independently
policed. The notebook flags all three rather than quietly ranking them.

The source also states that sexual offences are excluded for victim privacy, and
that incidents are mapped to the nearest 100 block rather than their true
location. The first makes this an undercount of violent crime everywhere; the
second is far finer than a ZIP boundary and does not threaten the join.

Source: Prince William County "Public Crime Data"
https://experience.arcgis.com/experience/cdb0743527c448b2a2ea18124e670779
"""
from __future__ import annotations

import io
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

import geopandas as gpd
import pandas as pd

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
ARCHIVE = HERE / "archive"

CRIME_SERVICE = ("https://services2.arcgis.com/0Q7l03Ls62VG0fy4/arcgis/rest/"
                 "services/Public_Crime_Reports/FeatureServer/0")
ZCTA_URL = ("https://www2.census.gov/geo/tiger/GENZ2020/shp/"
            "cb_2020_us_zcta520_500k.zip")

YEAR = 2025
PAGE = 2000          # well under the service cap, so a slow page cannot fail big

# The county police department does not police these areas.
OUTSIDE_JURISDICTION = {
    "20110": "City of Manassas — independent city, own police department",
    "22134": "MCB Quantico — military installation, federal policing",
    "20111": "partly Manassas Park — independently policed",
}


def _get(url: str, tries: int = 4) -> bytes:
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return r.read()
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("unreachable")


def fetch_boundaries(zips: list[str]) -> gpd.GeoDataFrame:
    """The 17 ZIP areas, from the Census cartographic boundary file.

    ZCTAs are the Census Bureau's areal approximation of ZIP delivery routes,
    which is the only polygon a ZIP code really has — the Postal Service does not
    publish boundaries. Close enough to assign an incident, and the reason the
    notebook talks about "ZIP areas" rather than ZIP codes.
    """
    out = DATA / "zcta_boundaries.geojson"
    print(f"boundaries → {out.name}")
    print(f"  downloading {ZCTA_URL.rsplit('/', 1)[-1]} (~64 MB, national file) …")
    blob = _get(ZCTA_URL)

    cache = DATA / "_zcta_national.zip"
    cache.write_bytes(blob)
    try:
        z = gpd.read_file(f"zip://{cache}")
    finally:
        cache.unlink(missing_ok=True)

    key = "ZCTA5CE20" if "ZCTA5CE20" in z.columns else "GEOID20"
    sub = (z[z[key].isin(zips)][[key, "geometry"]]
           .rename(columns={key: "ZIP"})
           .to_crs(4326)
           .sort_values("ZIP")
           .reset_index(drop=True))

    missing = sorted(set(zips) - set(sub.ZIP))
    if missing:
        raise SystemExit(f"  no ZCTA polygon for {missing}")

    sub.to_file(out, driver="GeoJSON")
    print(f"  {len(sub)} polygons, {out.stat().st_size / 1024:.0f} KB")
    return sub


def fetch_incidents() -> pd.DataFrame:
    """Every incident reported in `YEAR`, paginated past the service's cap."""
    out = DATA / f"crime_incidents_{YEAR}.csv"
    print(f"incidents → {out.name}")

    where = (f"OccurredOn >= timestamp '{YEAR}-01-01 00:00:00' "
             f"AND OccurredOn < timestamp '{YEAR + 1}-01-01 00:00:00'")
    base = {"where": where, "outFields": "CaseNo,OccurredOn,CrimeCategory,IBRCode,"
                                         "BlockAddress,Disposition",
            "returnGeometry": "true", "outSR": "4326", "f": "geojson"}

    expected = json.loads(_get(
        f"{CRIME_SERVICE}/query?"
        + urllib.parse.urlencode({"where": where, "returnCountOnly": "true",
                                  "f": "json"})))["count"]
    print(f"  service reports {expected:,} incidents in {YEAR}")

    rows, offset = [], 0
    while True:
        q = dict(base, resultOffset=str(offset), resultRecordCount=str(PAGE))
        fc = json.loads(_get(f"{CRIME_SERVICE}/query?" + urllib.parse.urlencode(q)))
        feats = fc.get("features", [])
        if not feats:
            break
        for f in feats:
            g = f.get("geometry") or {}
            c = g.get("coordinates") or [None, None]
            rows.append({**f["properties"], "lon": c[0], "lat": c[1]})
        offset += len(feats)
        print(f"\r  fetched {offset:,} / {expected:,}", end="", flush=True)
        if len(feats) < PAGE:
            break
    print()

    d = pd.DataFrame(rows)
    d["OccurredOn"] = pd.to_datetime(d["OccurredOn"], unit="ms", errors="coerce")
    # The cap is the whole reason this script exists — verify we cleared it.
    if len(d) < expected:
        raise SystemExit(f"  got {len(d):,}, expected {expected:,} — pagination broke")
    d.to_csv(out, index=False)
    print(f"  {len(d):,} incidents, {out.stat().st_size / 1024 / 1024:.1f} MB")
    return d


def build_indicators(shapes: gpd.GeoDataFrame, crime: pd.DataFrame) -> pd.DataFrame:
    """The six indicators per ZIP, with crime derived from the incidents."""
    out = DATA / "indicators.csv"
    print(f"indicators → {out.name}")

    raw = pd.read_csv(ARCHIVE / "indicators_original.csv")
    raw.columns = raw.columns.str.replace(r'[\n\r"]', "", regex=True).str.strip()
    raw = raw.rename(columns={
        "ZIP Code": "ZIP",
        "Median Income": "Income",
        "Population": "Population",
        "Land Area (sq mi)": "LandArea",
        "Population Density (people per square mile)": "Density",
        "Education Level Rate(Bachelor's degreeor higher)": "Education",
        "Housing Median Prices(Dollars)": "Housing",
        "Unemployment Rate": "Unemployment",
        "Crime Rate(/1,000)": "CrimeRateOriginal",
    })
    raw = raw[[c for c in raw.columns if not c.startswith("Unnamed")
               and c in {"ZIP", "Income", "Population", "LandArea", "Density",
                         "Education", "Housing", "Unemployment",
                         "CrimeRateOriginal"}]]

    raw["ZIP"] = raw["ZIP"].astype(str).str.zfill(5)
    for c in raw.columns.drop("ZIP"):
        raw[c] = (raw[c].astype(str).str.replace(",", "", regex=False)
                  .str.replace("%", "", regex=False)
                  .str.replace("$", "", regex=False).str.strip().astype(float))
    raw["Education"] /= 100
    raw["Unemployment"] /= 100

    pts = gpd.GeoDataFrame(
        crime.dropna(subset=["lon", "lat"]),
        geometry=gpd.points_from_xy(crime.dropna(subset=["lon", "lat"]).lon,
                                    crime.dropna(subset=["lon", "lat"]).lat),
        crs=4326)
    joined = gpd.sjoin(pts, shapes, how="inner", predicate="within")
    counts = joined.groupby("ZIP").size().rename("Incidents")
    inside = len(joined)
    print(f"  {inside:,} of {len(pts):,} incidents fall inside the 17 areas "
          f"({inside / len(pts):.1%}); the rest are elsewhere in the county")

    d = raw.merge(counts, on="ZIP", how="left")
    d["Incidents"] = d["Incidents"].fillna(0).astype(int)
    d["CrimeRate"] = d.Incidents / d.Population * 1000
    d["OutsideJurisdiction"] = d.ZIP.isin(OUTSIDE_JURISDICTION)
    d["JurisdictionNote"] = d.ZIP.map(OUTSIDE_JURISDICTION).fillna("")

    d.to_csv(out, index=False)
    r = d.CrimeRate.corr(d.CrimeRateOriginal, method="spearman")
    print(f"  rank correlation between derived and original crime rate: {r:+.3f}")
    print(f"  wrote {len(d)} rows")
    return d


def main() -> None:
    DATA.mkdir(exist_ok=True)
    raw = pd.read_csv(ARCHIVE / "indicators_original.csv")
    raw.columns = raw.columns.str.replace(r'[\n\r"]', "", regex=True).str.strip()
    zips = raw["ZIP Code"].astype(str).str.zfill(5).tolist()

    shapes = fetch_boundaries(zips)
    crime = fetch_incidents()
    build_indicators(shapes, crime)
    print("\ndone — the notebook reads only from data/")


if __name__ == "__main__":
    main()
