# MERRA-2 Parallel Downloader

Tools for geocoding city names and downloading MERRA-2 hourly meteorological data for multiple locations in parallel.

---

## Scripts

### `lan_lon_city.py` — Geocode city names to coordinates

Reads a CSV of city names and writes a new CSV with latitude and longitude columns added, using the [Nominatim](https://nominatim.org/) geocoding service (OpenStreetMap).

**Usage**

```bash
python lan_lon_city.py city_names.csv -o city_coordinates.csv
```

**Arguments**

| Argument | Description |
|---|---|
| `city_names.csv` | Input CSV file. Must contain a `name` column with city names. |
| `-o / --output` | Output CSV file path (required). |

**Input format** (`city_names.csv`)

```
name
Antwerp
Brussels
Paris
Madrid
```

**Output format** (`city_coordinates.csv`)

```
lat,lon,name
51.22111,4.39971,Antwerp
50.84674,4.35249,Brussels
48.8535,2.34839,Paris
40.41678,-3.70351,Madrid
```

Cities that cannot be found are written with empty `lat`/`lon` values. The script retries automatically on rate-limit or timeout errors (exponential back-off, up to 6 attempts per city).

**Dependencies**

```bash
pip install pandas geopy
```

---

### `merra2_parallel.py` — Download MERRA-2 data for multiple sites [Default number = 5]

Runs `merra2_download.py` in parallel for every location in a CSV file (or an inline list), downloading hourly MERRA-2 data from NASA GES DISC. Each site gets its own sub-folder and a `download.log` file.

**Usage**

```bash
# From a coordinates CSV (output of lan_lon_city.py)
python merra2_parallel.py --locations city_coordinates.csv -o merra2_output

# With parallel workers (default: 1; recommended: 4–12)
python merra2_parallel.py --locations city_coordinates.csv -o merra2_output --workers 5

# Inline locations (no CSV needed)
python merra2_parallel.py --locations "40.54,-3.70 48.85,2.35 51.51,-0.13" -o merra2_output

# Keep raw .nc4 files after extraction
python merra2_parallel.py --locations city_coordinates.csv -o merra2_output --keep-nc4
```

**Arguments**

| Argument | Default | Description |
|---|---|---|
| `-l / --locations` | *(required)* | Path to CSV file (`lat,lon[,name]`) **or** inline string `"lat1,lon1 lat2,lon2 ..."` |
| `-o / --output-dir` | `merra2_output` | Root output directory. A sub-folder is created per site. |
| `-w / --workers` | `5` | Number of parallel downloads. Recommended: 4–12. Hard cap: 12. |
| `--keep-nc4` | off | Keep raw `.nc4` files after data extraction. |
| `--user` | env | NASA Earthdata username. Alternatively set `EARTHDATA_USER`. |
| `--password` | env | NASA Earthdata password. Alternatively set `EARTHDATA_PASS`. |

**CSV format accepted by `--locations`**

The output of `lan_lon_city.py` works directly. Column names `lat`/`latitude` and `lon`/`longitude` are all accepted. A `name` column is optional but strongly recommended (used as the sub-folder name).

```
lat,lon,name
51.22111,4.39971,Antwerp
48.8535,2.34839,Paris
```

**Output structure**

```
merra2_output/
├── Antwerp/
│   ├── download.log
│   └── *.csv          ← extracted hourly data
├── Paris/
│   ├── download.log
│   └── *.csv
└── ...
```

**Authentication**

A free [NASA Earthdata](https://urs.earthdata.nasa.gov/) account is required. Export credentials before running:

```bash
export EARTHDATA_USER=your_username
export EARTHDATA_PASS=your_password
```

Or pass them directly with `--user` and `--password`. If neither is provided, `merra2_download.py` will prompt interactively on the first run and cache the credentials locally.

> **Note:** NASA GES DISC may throttle clients with more than 8 simultaneous connections. If you see many `429` or `503` errors, reduce `--workers`.

**Dependencies**

```bash
pip install requests tqdm netCDF4 numpy pandas
```

---

## End-to-end workflow

```bash
# 1. Geocode your cities
python lan_lon_city.py city_names.csv -o city_coordinates.csv

# 2. Download MERRA-2 data for all cities in parallel
export EARTHDATA_USER=your_username
export EARTHDATA_PASS=your_password
python merra2_parallel.py --locations city_coordinates.csv -o merra2_output --workers 6
```
