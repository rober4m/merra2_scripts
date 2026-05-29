#!/usr/bin/env python3
"""
MERRA-2 Parallel Downloader — run merra2_download.py for multiple locations simultaneously.

Usage:
    # From a CSV file with columns: lat,lon  (or lat,lon,name)
    python merra2_parallel.py --locations sites.csv

    # Inline list
    python merra2_parallel.py --locations "40.54,5.25 48.85,2.35 51.51,-0.13"

    # Control parallelism (default: 4, max recommended: 12)
    python merra2_parallel.py --locations sites.csv --workers 12

CSV format (sites.csv):
    lat,lon,name          ← name column is optional but recommended
    40.54,5.25,Madrid
    48.85,2.35,Paris
    51.51,-0.13,London
"""

import argparse
import csv
import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

SCRIPT = Path(__file__).parent / "merra2_download.py"


# ── location parsing ───────────────────────────────────────────────────────────

def parse_locations(source: str) -> list[dict]:
    """
    Accept either:
      - path to a CSV file  (lat,lon[,name])
      - inline string       "lat1,lon1 lat2,lon2 ..."
    Returns list of dicts with keys: lat, lon, name
    """
    locations = []

    path = Path(source)
    if path.exists():
        with path.open() as fh:
            # sniff for header
            sample = fh.read(1024)
            fh.seek(0)
            has_header = csv.Sniffer().has_header(sample)
            reader = csv.DictReader(fh) if has_header else csv.reader(fh)

            for i, row in enumerate(reader):
                if isinstance(row, dict):
                    lat = float(row.get("lat") or row.get("latitude") or list(row.values())[0])
                    lon = float(row.get("lon") or row.get("longitude") or list(row.values())[1])
                    name = row.get("name") or row.get("site") or f"site_{i+1:03d}"
                else:
                    lat, lon = float(row[0]), float(row[1])
                    name = row[2].strip() if len(row) > 2 else f"site_{i+1:03d}"
                locations.append({"lat": lat, "lon": lon, "name": name.strip()})
    else:
        # inline "lat,lon lat,lon ..."
        for i, token in enumerate(source.split()):
            parts = token.split(",")
            if len(parts) < 2:
                log.warning("Skipping malformed token: %s", token)
                continue
            lat, lon = float(parts[0]), float(parts[1])
            name = parts[2] if len(parts) > 2 else f"site_{i+1:03d}"
            locations.append({"lat": lat, "lon": lon, "name": name})

    return locations


# ── worker ────────────────────────────────────────────────────────────────────

def download_one(loc: dict, output_root: Path, extra_args: list[str]) -> dict:
    """Spawn merra2_download.py as a subprocess for one location."""
    name = loc["name"]
    lat  = loc["lat"]
    lon  = loc["lon"]

    site_dir = output_root / name.replace(" ", "_")
    site_dir.mkdir(parents=True, exist_ok=True)

    log_path = site_dir / "download.log"

    cmd = [
        sys.executable, str(SCRIPT),
        "-lat", str(lat),
        "-lon", str(lon),
        "-o", str(site_dir),
    ]
    cmd.extend(extra_args)

    started = time.time()
    log.info("▶  Starting  %s  (lat=%.4f, lon=%.4f)", name, lat, lon)

    result = {
        "name": name, "lat": lat, "lon": lon,
        "output_dir": site_dir,
        "success": False,
        "elapsed": 0.0,
        "error": None,
    }

    try:
        with log_path.open("w") as log_fh:
            proc = subprocess.run(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                # Pass through credentials from environment
                env={**os.environ},
            )
        result["success"] = proc.returncode == 0
        if not result["success"]:
            result["error"] = f"Exit code {proc.returncode}. See {log_path}"
    except Exception as exc:
        result["error"] = str(exc)

    result["elapsed"] = time.time() - started
    status = "✓" if result["success"] else "✗"
    log.info("%s  Finished  %s  in %.1f min", status, name, result["elapsed"] / 60)
    return result


# ── summary ───────────────────────────────────────────────────────────────────

def print_summary(results: list[dict], total_elapsed: float):
    ok  = [r for r in results if r["success"]]
    bad = [r for r in results if not r["success"]]

    print("\n" + "=" * 60)
    print(f"  MERRA-2 parallel summary")
    print(f"  Wall-clock time : {total_elapsed/60:.1f} min")
    print(f"  Succeeded       : {len(ok)}/{len(results)}")
    print("=" * 60)

    if ok:
        print("\n  ✓ Completed:")
        for r in ok:
            print(f"    {r['name']:20s}  lat={r['lat']:.4f}  lon={r['lon']:.4f}"
                  f"  → {r['output_dir']}")

    if bad:
        print("\n  ✗ Failed:")
        for r in bad:
            print(f"    {r['name']:20s}  {r['error']}")
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Run merra2_download.py in parallel for multiple locations.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--locations", "-l", required=True,
        help=(
            'Path to CSV file (lat,lon[,name]) OR inline string '
            '"lat1,lon1[,name1] lat2,lon2[,name2] ..."'
        ),
    )
    p.add_argument(
        "--workers", "-w", type=int, default=7,
        help="Number of parallel downloads (recommended: 4–12)",
    )
    p.add_argument(
        "--output-dir", "-o", type=Path, default=Path("merra2_output"),
        help="Root output directory; a sub-folder is created per site",
    )
    # Credentials can also be passed as args (forwarded to child scripts)
    p.add_argument("--user", help="NASA Earthdata username (or set EARTHDATA_USER)")
    p.add_argument("--password", help="NASA Earthdata password (or set EARTHDATA_PASS)")
    return p.parse_args()


def main():
    args = parse_args()

    if not SCRIPT.exists():
        print(f"ERROR: merra2_download.py not found at {SCRIPT}")
        print("Place merra2_parallel.py in the same directory as merra2_download.py.")
        sys.exit(1)

    # Inject credentials into environment so child processes inherit them
    if args.user:
        os.environ["EARTHDATA_USER"] = args.user
    if args.password:
        os.environ["EARTHDATA_PASS"] = args.password

    locations = parse_locations(args.locations)
    if not locations:
        print("ERROR: No valid locations found.")
        sys.exit(1)

    workers = min(args.workers, len(locations), 12)
    log.info("Loaded %d location(s), running with %d worker(s)", len(locations), workers)

    # Warn if too many workers — NASA GES DISC rate-limits aggressive clients
    if workers > 8:
        log.warning(
            "Using %d workers. NASA GES DISC may throttle > 8 simultaneous connections. "
            "Reduce with --workers if you see many 429/503 errors.", workers
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    wall_start = time.time()
    results = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(download_one, loc, args.output_dir, []): loc
            for loc in locations
        }
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as exc:
                loc = futures[fut]
                log.error("Unhandled error for %s: %s", loc["name"], exc)
                results.append({
                    "name": loc["name"], "lat": loc["lat"], "lon": loc["lon"],
                    "success": False, "elapsed": 0, "error": str(exc),
                    "output_dir": args.output_dir / loc["name"],
                })

    print_summary(results, time.time() - wall_start)

    # Exit non-zero if any site failed
    if any(not r["success"] for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
