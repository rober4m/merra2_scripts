#!/usr/bin/env python3
"""
MERRA-2 Hourly Data Downloader (OPeNDAP edition)
Streams hourly data for a single lat/lon point from 1980 to present using
the GES DISC OPeNDAP server, accessed directly through xarray's netCDF4
backend (libcurl/libnetcdf handles the OPeNDAP protocol).

Usage:
    python merra2_download.py -lat 40.54 -lon 5.25
    python merra2_download.py --locations city_coordinates.csv -o merra2_output

Requirements:
    pip install xarray netCDF4 numpy pandas

Authentication:
    You need a NASA Earthdata account. Credentials are read from ~/.netrc
    (libcurl picks them up automatically). Your ~/.netrc should contain:

        machine urs.earthdata.nasa.gov login YOUR_USER password YOUR_PASS

    and have permissions 600. A ~/.dodsrc pointing at a cookie jar is also
    recommended for OPeNDAP session reuse.
"""

import argparse
import csv
import sys
import logging
import calendar
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

# ── required heavy deps ───────────────────────────────────────────────────────
try:
    import numpy as np  # noqa: F401
    import pandas as pd
    import xarray as xr
except ImportError:
    print("Missing dependencies. Install with:")
    print("  pip install xarray netCDF4 numpy pandas")
    sys.exit(1)

# ── logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── MERRA-2 dataset configuration ─────────────────────────────────────────────
# Each MERRA-2 collection and its variables
# inst1_2d_asm_Nx  : single-level hourly instantaneous (surface/near-surface)
# tavg1_2d_slv_Nx  : single-level hourly time-averaged
COLLECTIONS = {
    "M2I1NXASM.5.12.4": {
        "shortname": "inst1_2d_asm_Nx",
        "variables": ["T2M", "QV2M", "U10M", "V10M", "PS", "TQV"],
        "label": "Instantaneous surface/near-surface (T, humidity, wind, pressure)",
    },
    "M2T1NXSLV.5.12.4": {
        "shortname": "tavg1_2d_slv_Nx",
        "variables": ["T2MMAX", "T2MMIN", "PRECTOT", "SWGDN", "LWGDN"],
        "label": "Time-averaged single-level (precip, radiation, T extremes)",
    },
}

# OPeNDAP URL template — same path structure as the HTTPS file URL but under /opendap/
# Files are organised as: COLLECTION/YYYY/MM/filename
OPENDAP_URL_TMPL = (
    "https://goldsmr4.gesdisc.eosdis.nasa.gov/opendap/MERRA2/{collection}/{year}/{month:02d}/"
    "MERRA2_{stream}.{shortname}.{date8}.nc4"
)

# Stream prefix changes with date (reprocessing versions)
def _stream(year: int) -> str:
    """Return the MERRA-2 stream identifier for a given year."""
    if year < 1992:
        return "100"
    elif year < 2001:
        return "200"
    elif year < 2011:
        return "300"
    else:
        return "400"


# ── nearest grid point ─────────────────────────────────────────────────────────
# MERRA-2 grid: 0.625° lon × 0.5° lat
LON_RES = 0.625
LAT_RES = 0.500
LON_ORIGIN = -180.0   # first longitude
LAT_ORIGIN = -90.0    # first latitude

def nearest_index(lat: float, lon: float) -> tuple[int, int]:
    """Return (lat_idx, lon_idx) of the nearest MERRA-2 grid cell."""
    lat_i = round((lat - LAT_ORIGIN) / LAT_RES)
    lon_i = round((lon - LON_ORIGIN) / LON_RES)
    lat_i = max(0, min(lat_i, 360))   # 361 lats (0..360)
    lon_i = max(0, min(lon_i, 575))   # 576 lons (0..575)
    return lat_i, lon_i


def actual_coords(lat_i: int, lon_i: int) -> tuple[float, float]:
    lat = LAT_ORIGIN + lat_i * LAT_RES
    lon = LON_ORIGIN + lon_i * LON_RES
    return lat, lon


# ── URL builder ────────────────────────────────────────────────────────────────
def _build_url(collection: str, shortname: str, year: int, month: int, day: int) -> str:
    """Build a MERRA-2 OPeNDAP .nc4 base URL (no subsetting suffix)."""
    d = date(year, month, day)
    date8 = d.strftime("%Y%m%d")
    stream = _stream(year)
    return OPENDAP_URL_TMPL.format(
        collection=collection,
        year=year,
        month=month,
        stream=stream,
        shortname=shortname,
        date8=date8,
    )


# ── per-day fetcher ───────────────────────────────────────────────────────────
def _fetch_day(year: int, month: int, day: int, lat_i: int, lon_i: int) -> pd.DataFrame | None:
    day_frames = []
    for collection, meta in COLLECTIONS.items():
        url = _build_url(collection, meta["shortname"], year, month, day)
        variables = meta["variables"]
        try:
            ds = xr.open_dataset(url, engine="pydap")
            df = ds[variables].isel(lat=lat_i, lon=lon_i).to_dataframe().reset_index()
            df = df.rename(columns={"time": "datetime"})
            df = df.drop(columns=[c for c in ("lat", "lon") if c in df.columns])
            day_frames.append(df)
            ds.close()
        except Exception as exc:
            log.warning("Failed %04d-%02d-%02d %s: %s", year, month, day, collection, exc)
    if not day_frames:
        return None
    merged = day_frames[0]
    for extra in day_frames[1:]:
        merged = merged.merge(extra, on="datetime", how="outer")
    return merged


# ── main download loop ─────────────────────────────────────────────────────────
def run(lat: float, lon: float, output_dir: Path):
    lat_i, lon_i = nearest_index(lat, lon)
    grid_lat, grid_lon = actual_coords(lat_i, lon_i)
    log.info("Requested point : lat=%.4f, lon=%.4f", lat, lon)
    log.info("Nearest MERRA-2 : lat=%.3f (idx %d), lon=%.3f (idx %d)",
             grid_lat, lat_i, grid_lon, lon_i)

    start_year = 1980
    today = date.today()
    output_dir.mkdir(parents=True, exist_ok=True)

    all_frames: list[pd.DataFrame] = []
    month_frames: list[pd.DataFrame] = []  # kept outside try so interrupt handler can access it
    current_year, current_month = start_year, 1

    # Iterate over every month from 1980-01 to today
    try:
        for year in range(start_year, today.year + 1):
            for month in range(1, 13):
                if year == today.year and month > today.month:
                    break

                current_year, current_month = year, month
                month_csv = output_dir / f"merra2_{year}{month:02d}_lat{lat:.4f}_lon{lon:.4f}.csv"
                if month_csv.exists():
                    log.info("  → %04d-%02d already complete, loading from cache", year, month)
                    all_frames.append(pd.read_csv(month_csv, parse_dates=["datetime"]))
                    continue

                last_day = calendar.monthrange(year, month)[1]
                end_day = last_day if not (year == today.year and month == today.month) else today.day

                log.info("Processing %04d-%02d …", year, month)
                month_frames = []

                with ThreadPoolExecutor(max_workers=8) as pool:
                    futures = {
                        pool.submit(_fetch_day, year, month, day, lat_i, lon_i): day
                        for day in range(1, end_day + 1)
                    }
                    for future in as_completed(futures):
                        day = futures[future]
                        try:
                            df = future.result()
                            if df is not None:
                                month_frames.append(df)
                        except Exception as exc:
                            log.warning("Day %04d-%02d-%02d failed: %s", year, month, day, exc)

                if month_frames:
                    month_df = pd.concat(month_frames, ignore_index=True)
                    month_df.sort_values("datetime", inplace=True)
                    month_df.reset_index(drop=True, inplace=True)
                    month_df.to_csv(month_csv, index=False)
                    log.info("  → saved %s (%d rows)", month_csv.name, len(month_df))
                    all_frames.append(month_df)
                    month_frames = []

    except KeyboardInterrupt:
        log.warning("Interrupted — saving partial results collected so far.")
        if month_frames:
            log.info("  → flushing %d partial days from %04d-%02d into output",
                     len(month_frames), current_year, current_month)
            all_frames.append(pd.concat(month_frames, ignore_index=True))
        if not all_frames:
            log.error("No data was downloaded. Check credentials and network.")
            sys.exit(1)

    if not all_frames:
        log.error("No data was downloaded. Check credentials and network.")
        sys.exit(1)

    # Combine everything into a single file
    full_df = pd.concat(all_frames, ignore_index=True)
    full_df.sort_values("datetime", inplace=True)
    full_df.reset_index(drop=True, inplace=True)

    out_csv = output_dir / f"merra2_hourly_lat{lat:.4f}_lon{lon:.4f}_1980_{today.year}.csv"
    full_df.to_csv(out_csv, index=False)
    log.info("=" * 60)
    log.info("Complete dataset saved → %s", out_csv)
    log.info("Total records: %d (expected ~%d hourly steps)",
             len(full_df),
             int((today - date(1980, 1, 1)).days * 24))

    # Quick summary stats
    numeric_cols = full_df.select_dtypes(include="number").columns.tolist()
    if numeric_cols:
        log.info("\nSummary statistics:")
        print(full_df[numeric_cols].describe().to_string())

    return out_csv


# ── CSV location parser ────────────────────────────────────────────────────────
def _parse_locations_csv(path: Path) -> list[dict]:
    locs = []
    with path.open() as fh:
        reader = csv.DictReader(fh)
        for i, row in enumerate(reader):
            lat = float(row.get("lat") or row.get("latitude") or "")
            lon = float(row.get("lon") or row.get("longitude") or "")
            name = (row.get("name") or row.get("site") or f"site_{i+1:03d}").strip()
            locs.append({"lat": lat, "lon": lon, "name": name})
    return locs


# ── CLI ────────────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(
        description="Download MERRA-2 hourly data via OPeNDAP (1980–present).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    loc = p.add_mutually_exclusive_group(required=True)
    loc.add_argument("-lat", type=float, help="Latitude (-90 to 90) — use with -lon")
    loc.add_argument(
        "--locations", type=Path, metavar="CSV",
        help="CSV file with columns: lat, lon[, name] — downloads all rows",
    )
    p.add_argument("-lon", type=float, help="Longitude (-180 to 180) — required with -lat")
    p.add_argument(
        "-o", "--output-dir",
        type=Path,
        default=Path("merra2_output"),
        help="Directory for output CSV files",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.locations:
        locs = _parse_locations_csv(args.locations)
        if not locs:
            print("ERROR: No valid locations found in CSV.")
            sys.exit(1)
        log.info("Loaded %d location(s) from %s", len(locs), args.locations)
        for loc in locs:
            log.info("--- %s (lat=%.4f, lon=%.4f) ---", loc["name"], loc["lat"], loc["lon"])
            run(
                lat=loc["lat"],
                lon=loc["lon"],
                output_dir=args.output_dir / loc["name"].replace(" ", "_"),
            )
    else:
        if args.lon is None:
            print("ERROR: -lon is required when using -lat")
            sys.exit(1)
        if not (-90 <= args.lat <= 90):
            print("ERROR: latitude must be between -90 and 90")
            sys.exit(1)
        if not (-180 <= args.lon <= 180):
            print("ERROR: longitude must be between -180 and 180")
            sys.exit(1)
        run(lat=args.lat, lon=args.lon, output_dir=args.output_dir)
