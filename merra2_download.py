#!/usr/bin/env python3
"""
MERRA-2 Hourly Data Downloader (OPeNDAP edition)
Streams hourly data for a single lat/lon point from 1980 to present using
GES DISC OPeNDAP server-side subsetting — no full .nc4 files are downloaded.

Usage:
    python merra2_download.py -lat 40.54 -lon 5.25

Requirements:
    pip install requests xarray pydap numpy pandas tqdm

Authentication:
    You need a NASA Earthdata account. Set credentials via environment variables:
        export EARTHDATA_USER=your_username
        export EARTHDATA_PASS=your_password
    Or the script will read them from ./merra2_credential.json.
"""

import argparse
import os
import sys
import getpass
import json
import logging
import calendar
from datetime import date, datetime
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

# ── required heavy deps ───────────────────────────────────────────────────────
try:
    import numpy as np
    import pandas as pd
    import xarray as xr
except ImportError:
    print("Missing dependencies. Install with:")
    print("  pip install requests xarray pydap numpy pandas tqdm")
    sys.exit(1)

# pydap is required for OPeNDAP access through xarray
try:
    from pydap.client import open_url  # noqa: F401  (ensures pydap is importable)
    import pydap  # noqa: F401
except ImportError:
    print("Missing dependency 'pydap'. Install with:")
    print("  pip install pydap")
    sys.exit(1)

# pydap.cas.urs gives a session that handles the Earthdata Login redirect chain.
# Fall back to plain HTTPBasicAuth if it's not available in this pydap build.
try:
    from pydap.cas.urs import setup_session as _urs_setup_session
    _HAS_PYDAP_CAS = True
except ImportError:
    _urs_setup_session = None
    _HAS_PYDAP_CAS = False

# tqdm is optional — only used for progress display
try:
    from tqdm import tqdm  # noqa: F401
except ImportError:
    tqdm = None  # noqa: F401

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


# ── credential helpers ─────────────────────────────────────────────────────────
#CRED_FILE = Path.home() / ".merra2_credentials.json"
CRED_FILE = Path() / "merra2_credential.json"

print(f'The credential should be here: {CRED_FILE.resolve()}')

def load_credentials() -> tuple[str, str]:
    """Load NASA Earthdata credentials from env vars, cache file, or prompt."""
    user = os.environ.get("EARTHDATA_USER", "")
    pwd = os.environ.get("EARTHDATA_PASS", "")

    if user and pwd:
        return user, pwd

    if CRED_FILE.exists():
        data = json.loads(CRED_FILE.read_text())
        print('Credentials found')
        return data.get("user", ""), data.get("pass", "")
    print('Local credential not found')

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


def make_session(user: str, pwd: str, check_url: str | None = None) -> requests.Session:
    """Build an authenticated session for NASA Earthdata OPeNDAP requests.

    Prefers pydap.cas.urs.setup_session (which understands the Earthdata
    Login redirect dance + cookie handling).  Falls back to a plain
    requests.Session with HTTPBasicAuth if pydap.cas is unavailable.
    """
    if _HAS_PYDAP_CAS and check_url:
        try:
            session = _urs_setup_session(user, pwd, check_url=check_url)
            return session
        except Exception as exc:
            log.warning("pydap.cas.urs.setup_session failed (%s); "
                        "falling back to HTTPBasicAuth session.", exc)

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
def daily_opendap_url(
    collection: str,
    shortname: str,
    year: int,
    month: int,
    day: int,
    variables: list[str],
    lat_i: int,
    lon_i: int,
) -> str:
    """Build a MERRA-2 OPeNDAP URL with a server-side subsetting suffix.

    The suffix restricts the response to:
      - the hourly time steps [0:23] for each requested variable
      - the single (lat_i, lon_i) grid cell
      - the time/lat/lon coordinate arrays
    """
    d = date(year, month, day)
    date8 = d.strftime("%Y%m%d")
    stream = _stream(year)
    base = OPENDAP_URL_TMPL.format(
        collection=collection,
        year=year,
        month=month,
        stream=stream,
        shortname=shortname,
        date8=date8,
    )

    var_subset = ",".join(
        f"{v}[0:23][{lat_i}:{lat_i}][{lon_i}:{lon_i}]" for v in variables
    )
    coord_subset = f"time[0:23],lat[{lat_i}:{lat_i}],lon[{lon_i}:{lon_i}]"
    return f"{base}?{var_subset},{coord_subset}"


# ── OPeNDAP point reader ──────────────────────────────────────────────────────
def read_opendap_point(
    session: requests.Session,
    url: str,
    lat_i: int,
    lon_i: int,
    variables: list[str],
) -> pd.DataFrame:
    """Open a MERRA-2 OPeNDAP URL and return hourly data for one grid cell.

    Uses xarray + pydap with the supplied authenticated session. Only the
    bytes for the requested variables and the single (lat_i, lon_i) cell
    are transferred from the server.
    """
    # Prefer PydapDataStore.open when available — it accepts the requests.Session
    # directly so the Earthdata cookies/auth carry through.
    try:
        from xarray.backends import PydapDataStore
        store = PydapDataStore.open(url, session=session)
        ds = xr.open_dataset(store)
    except Exception:
        # Older / newer xarray versions: pass session via engine kwargs.
        ds = xr.open_dataset(url, engine="pydap", session=session)

    with ds:
        # Materialize a single grid point. The server has already subset to a
        # 1×1 spatial slab; isel just collapses those length-1 dimensions.
        try:
            point = ds.isel(lat=0, lon=0)
        except Exception:
            # Fallback in case the dataset still carries the full grid (e.g.
            # a server that ignored the projection clause).
            point = ds.isel(lat=lat_i, lon=lon_i)

        # Time axis → real datetimes via xarray's decoded coordinate
        times = pd.to_datetime(point["time"].values)
        rows: dict[str, list] = {"datetime": list(times)}

        for vname in variables:
            if vname in point.variables:
                arr = np.asarray(point[vname].values, dtype=float)
                # OPeNDAP fill values are typically already masked by xarray's
                # decode_cf; coerce any remaining sentinel/NaN handling.
                rows[vname] = arr.tolist()
            else:
                log.debug("Variable %s not in OPeNDAP response for %s", vname, url)

    return pd.DataFrame(rows)


# ── main download loop ─────────────────────────────────────────────────────────
def run(lat: float, lon: float, output_dir: Path):
    lat_i, lon_i = nearest_index(lat, lon)
    grid_lat, grid_lon = actual_coords(lat_i, lon_i)
    log.info("Requested point : lat=%.4f, lon=%.4f", lat, lon)
    log.info("Nearest MERRA-2 : lat=%.3f (idx %d), lon=%.3f (idx %d)",
             grid_lat, lat_i, grid_lon, lon_i)

    user, pwd = load_credentials()

    # Build a check_url against the first collection / first available date so
    # pydap.cas.urs.setup_session can complete the Earthdata Login handshake.
    start_year = 1980
    first_collection = next(iter(COLLECTIONS))
    first_meta = COLLECTIONS[first_collection]
    first_url = daily_opendap_url(
        first_collection,
        first_meta["shortname"],
        start_year, 1, 1,
        first_meta["variables"],
        lat_i, lon_i,
    )
    session = make_session(user, pwd, check_url=first_url)

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

                for day in range(1, end_day + 1):
                    day_frames: list[pd.DataFrame] = []

                    for collection, meta in COLLECTIONS.items():
                        shortname = meta["shortname"]
                        variables = meta["variables"]
                        url = daily_opendap_url(
                            collection, shortname, year, month, day,
                            variables, lat_i, lon_i,
                        )

                        try:
                            df = read_opendap_point(session, url, lat_i, lon_i, variables)
                            day_frames.append(df)
                        except Exception as exc:
                            log.warning("OPeNDAP read failed for %s: %s", url, exc)
                            continue

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
        description="Download MERRA-2 hourly data for a single lat/lon point (1980–present) via OPeNDAP.",
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
    )
