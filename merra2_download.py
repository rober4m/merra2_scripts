#!/usr/bin/env python3
"""
MERRA-2 Hourly Data Downloader
Downloads hourly data for a single lat/lon point from 1980 to present.

Usage:
    python merra2_download.py -lat 40.54 -lon 5.25

Requirements:
    pip install requests tqdm netCDF4 numpy pandas

Authentication:
    You need a NASA Earthdata account. Set credentials via environment variables:
        export EARTHDATA_USER=your_username
        export EARTHDATA_PASS=your_password
    Or the script will prompt you interactively on first run and cache them.
"""

import argparse
import os
import sys
import time
import getpass
import json
import logging
import calendar
from datetime import date, datetime
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

# ── optional heavy deps (imported lazily after install check) ──────────────────
try:
    import numpy as np
    import pandas as pd
    import netCDF4 as nc
    from tqdm import tqdm
except ImportError:
    print("Missing dependencies. Install with:")
    print("  pip install requests tqdm netCDF4 numpy pandas")
    sys.exit(1)

# ── logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── MERRA-2 dataset configuration ─────────────────────────────────────────────
# NASA GES DISC OPeNDAP base URL
GESDISC_BASE = "https://goldsmr4.gesdisc.eosdis.nasa.gov/dods"

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

# OPeNDAP / HTTPS file URL templates
# Files are organised as: COLLECTION/YYYY/DDD/filename
FILE_URL_TMPL = (
    "https://goldsmr4.gesdisc.eosdis.nasa.gov/data/MERRA2/{collection}/{year}/{month:02d}/"
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


# ── credential helpers ─────────────────────────────────────────────────────────
CRED_FILE = Path.home() / ".merra2_credentials.json"


def load_credentials() -> tuple[str, str]:
    """Load NASA Earthdata credentials from env vars, cache file, or prompt."""
    user = os.environ.get("EARTHDATA_USER", "")
    pwd = os.environ.get("EARTHDATA_PASS", "")

    if user and pwd:
        return user, pwd

    if CRED_FILE.exists():
        data = json.loads(CRED_FILE.read_text())
        return data.get("user", ""), data.get("pass", "")

    print("\nNASA Earthdata credentials required.")
    print("Register free at: https://urs.earthdata.nasa.gov/\n")
    user = input("Earthdata username: ").strip()
    pwd = getpass.getpass("Earthdata password: ")

    save = input("Save credentials to ~/.merra2_credentials.json? [y/N]: ").strip().lower()
    if save == "y":
        CRED_FILE.write_text(json.dumps({"user": user, "pass": pwd}))
        CRED_FILE.chmod(0o600)
        log.info("Credentials saved to %s", CRED_FILE)

    return user, pwd


# ── session with authentication ────────────────────────────────────────────────
class _EarthdataSession(requests.Session):
    """requests.Session that keeps auth alive when redirected to Earthdata Login.

    By default requests strips the Authorization header on cross-domain
    redirects, so credentials never reach urs.earthdata.nasa.gov.  This
    override preserves auth specifically for that host.
    """
    _AUTH_HOST = "urs.earthdata.nasa.gov"

    def rebuild_auth(self, prepared_request, response):
        headers = prepared_request.headers
        if "Authorization" in headers:
            orig = requests.utils.urlparse(response.request.url).hostname
            dest = requests.utils.urlparse(prepared_request.url).hostname
            if orig != dest and dest != self._AUTH_HOST and orig != self._AUTH_HOST:
                del headers["Authorization"]


def make_session(user: str, pwd: str) -> requests.Session:
    session = _EarthdataSession()
    session.auth = HTTPBasicAuth(user, pwd)
    return session


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


# ── URL builders ───────────────────────────────────────────────────────────────
def daily_url(collection: str, shortname: str, year: int, month: int, day: int) -> str:
    d = date(year, month, day)
    date8 = d.strftime("%Y%m%d")
    stream = _stream(year)
    return FILE_URL_TMPL.format(
        collection=collection,
        year=year,
        month=month,
        stream=stream,
        shortname=shortname,
        date8=date8,
    )


# ── download helpers ───────────────────────────────────────────────────────────
CHUNK = 1024 * 256   # 256 KB

def download_file(
    session: requests.Session,
    url: str,
    dest: Path,
    retries: int = 3,
) -> bool:
    """Download *url* to *dest*. Returns True on success."""
    tmp = dest.with_suffix(".tmp")
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, stream=True, timeout=120)
            if resp.status_code == 404:
                log.warning("File not found (404): %s", url)
                return False
            resp.raise_for_status()

            dest.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("wb") as fh:
                for chunk in resp.iter_content(CHUNK):
                    fh.write(chunk)
            tmp.rename(dest)
            return True

        except KeyboardInterrupt:
            if tmp.exists():
                tmp.unlink()
            raise
        except (requests.RequestException, Exception) as exc:
            log.warning("Attempt %d/%d failed for %s: %s", attempt, retries, url, exc)
            if tmp.exists():
                tmp.unlink()
            if attempt < retries:
                time.sleep(5 * attempt)

    return False


# ── extract point data from NetCDF4 ───────────────────────────────────────────
def extract_point(
    filepath: Path,
    lat_i: int,
    lon_i: int,
    variables: list[str],
) -> pd.DataFrame:
    """Read a MERRA-2 daily NetCDF4 file and extract hourly data for one grid cell."""
    with nc.Dataset(filepath) as ds:
        # Time axis → real datetimes
        time_var = ds.variables["time"]
        times = nc.num2date(
            time_var[:],
            units=time_var.units,
            calendar=getattr(time_var, "calendar", "standard"),
            only_use_cftime_datetimes=False,
            only_use_python_datetimes=True,
        )

        rows = {"datetime": list(times)}
        for vname in variables:
            if vname in ds.variables:
                var = ds.variables[vname]
                data = var[:, lat_i, lon_i]  # netCDF4 auto-applies scale/offset and masking
                arr = np.ma.filled(np.ma.array(data, dtype=float), np.nan)
                rows[vname] = arr.tolist()
            else:
                log.debug("Variable %s not in file %s", vname, filepath.name)

    return pd.DataFrame(rows)


# ── main download loop ─────────────────────────────────────────────────────────
def run(lat: float, lon: float, output_dir: Path, keep_nc4: bool = False):
    lat_i, lon_i = nearest_index(lat, lon)
    grid_lat, grid_lon = actual_coords(lat_i, lon_i)
    log.info("Requested point : lat=%.4f, lon=%.4f", lat, lon)
    log.info("Nearest MERRA-2 : lat=%.3f (idx %d), lon=%.3f (idx %d)",
             grid_lat, lat_i, grid_lon, lon_i)

    user, pwd = load_credentials()
    session = make_session(user, pwd)

    start_year = 1980
    today = date.today()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Remove stale .tmp files left by previous interrupted runs
    for stale in output_dir.rglob("*.tmp"):
        log.info("Removing stale temp file: %s", stale)
        stale.unlink()

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

                for day in range(1, end_day + 1):
                    day_frames: list[pd.DataFrame] = []

                    for collection, meta in COLLECTIONS.items():
                        shortname = meta["shortname"]
                        variables = meta["variables"]
                        url = daily_url(collection, shortname, year, month, day)

                        nc4_path = output_dir / "nc4" / f"{shortname}_{year}{month:02d}{day:02d}.nc4"

                        if not nc4_path.exists():
                            ok = download_file(session, url, nc4_path)
                            if not ok:
                                continue

                        try:
                            df = extract_point(nc4_path, lat_i, lon_i, variables)
                            day_frames.append(df)
                        except Exception as exc:
                            log.warning("Could not parse %s: %s", nc4_path, exc)

                        if not keep_nc4 and nc4_path.exists():
                            nc4_path.unlink()

                    if day_frames:
                        # Merge all collections for this day on datetime
                        day_df = day_frames[0]
                        for extra in day_frames[1:]:
                            day_df = day_df.merge(extra, on="datetime", how="outer")
                        month_frames.append(day_df)

                if month_frames:
                    month_df = pd.concat(month_frames, ignore_index=True)
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


# ── CLI ────────────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(
        description="Download MERRA-2 hourly data for a single lat/lon point (1980–present).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-lat", type=float, required=True, help="Latitude  (-90 to 90)")
    p.add_argument("-lon", type=float, required=True, help="Longitude (-180 to 180)")
    p.add_argument(
        "-o", "--output-dir",
        type=Path,
        default=Path("merra2_output"),
        help="Directory for output CSV files",
    )
    p.add_argument(
        "--keep-nc4",
        action="store_true",
        default=False,
        help="Keep downloaded .nc4 files after extraction (large!)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if not (-90 <= args.lat <= 90):
        print("ERROR: latitude must be between -90 and 90")
        sys.exit(1)
    if not (-180 <= args.lon <= 180):
        print("ERROR: longitude must be between -180 and 180")
        sys.exit(1)

    run(
        lat=args.lat,
        lon=args.lon,
        output_dir=args.output_dir,
        keep_nc4=args.keep_nc4,
    )
