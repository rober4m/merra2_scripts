# MERRA-2 Parallel Downloader

Tools for geocoding city names and streaming MERRA-2 hourly meteorological data for multiple locations in parallel via OPeNDAP — no full `.nc4` files are downloaded.

---
**Dependencies**

```bash
pip install requests xarray pydap numpy pandas tqdm geopy
```

## Scripts

### `lat_lon_city.py` — Geocode city names to coordinates

Reads a CSV of city names and countries and writes a new CSV with latitude and longitude columns added, using the [Nominatim](https://nominatim.org/) geocoding service (OpenStreetMap). Including the country eliminates ambiguity for cities that exist in multiple countries.

**Usage**

```bash
python lat_lon_city.py city_names.csv -o city_coordinates.csv
```

**Arguments**

| Argument | Description |
|---|---|
| `city_names.csv` | Input CSV file. Must contain `city` and `country` columns. |
| `-o / --output` | Output CSV file path (required). |

**Input format** (`city_names.csv`)

```
city,country
Antwerp,Belgium
Brussels,Belgium
Paris,France
Madrid,Spain
```

**Output format** (`city_coordinates.csv`)

```
city,country,lat,lon
Antwerp,Belgium,51.22111,4.39971
Brussels,Belgium,50.84674,4.35249
Paris,France,48.8535,2.34839
Madrid,Spain,40.41678,-3.70351
```

Cities that cannot be found are written with empty `lat`/`lon` values. The script retries automatically on rate-limit or timeout errors (exponential back-off, up to 6 attempts per city).

---

### `merra2_parallel.py` — Stream MERRA-2 data for multiple sites

Runs `merra2_download.py` in parallel (default=7) for every location in a CSV file (or an inline list), streaming hourly MERRA-2 data from NASA GES DISC via OPeNDAP using the DAP4 protocol. Each site gets its own sub-folder and a `download.log` file.

**Usage**

```bash
# From a coordinates CSV (output of lat_lon_city.py)
python merra2_parallel.py --locations city_coordinates.csv -o merra2_output

# With parallel workers (default: 7; recommended: 4–12)
python merra2_parallel.py --locations city_coordinates.csv -o merra2_output --workers 5

# Inline locations (no CSV needed)
python merra2_parallel.py --locations "40.54,-3.70 48.85,2.35 51.51,-0.13" -o merra2_output
```

**Arguments**

| Argument | Default | Description |
|---|---|---|
| `-l / --locations` | *(required)* | Path to CSV file (`lat,lon[,name]`) **or** inline string `"lat1,lon1 lat2,lon2 ..."` |
| `-o / --output-dir` | `merra2_output` | Root output directory. A sub-folder is created per site. |
| `-w / --workers` | `7` | Number of parallel workers. Recommended: 4–12. Hard cap: 12. |

**CSV format accepted by `--locations`**

The output of `lat_lon_city.py` works directly. Column names `lat`/`latitude` and `lon`/`longitude` are all accepted. A `name` or `city` column is optional but strongly recommended (used as the sub-folder name).

```
city,country,lat,lon
Antwerp,Belgium,51.22111,4.39971
Paris,France,48.8535,2.34839
```

**Output structure**

```
merra2_output/
├── Antwerp/
│   ├── download.log
│   └── *.csv          ← hourly data streamed from OPeNDAP
├── Paris/
│   ├── download.log
│   └── *.csv
└── ...
```

**Downloaded variables**

| Variable | Collection | Description |
|---|---|---|
| `T2M` | inst1 & tavg1 | 2 m air temperature (K) |
| `QV2M` | inst1 | 2 m specific humidity (kg kg⁻¹) |
| `U10M` / `V10M` | inst1 | 10 m eastward / northward wind (m s⁻¹) |
| `U50M` / `V50M` | inst1 | 50 m eastward / northward wind (m s⁻¹) |
| `PS` | inst1 | Surface pressure (Pa) |
| `TQV` | inst1 | Total precipitable water vapour (kg m⁻²) |
| `PRECTOT` | tavg1 | Total precipitation (kg m⁻² s⁻¹) |
| `SWGDN` | tavg1 | Downwelling shortwave radiation at surface (W m⁻²) |
| `LWGDN` | tavg1 | Downwelling longwave radiation at surface (W m⁻²) |

Data are drawn from two MERRA-2 collections merged on the `datetime` column:
- **`M2I1NXASM`** — hourly instantaneous surface/near-surface fields
- **`M2T1NXSLV`** — hourly time-averaged single-level fields

**Authentication**

A free [NASA Earthdata](https://urs.earthdata.nasa.gov/) account is required. Save credentials in `merra2_credential.json` before running:

```json
{"user": "your_username", "pass": "your_password"}
```

Or export them as environment variables:

```bash
export EARTHDATA_USER=your_username
export EARTHDATA_PASS=your_password
```

Or pass them directly with `--user` and `--password`.

> **Note:** NASA GES DISC may throttle clients with more than 8 simultaneous connections. If you see many `429` or `503` errors, reduce `--workers`.


## End-to-end workflow

```bash
# 1. Geocode your cities (input must have 'city' and 'country' columns)
python lat_lon_city.py city_names.csv -o city_coordinates.csv

# 2. Stream MERRA-2 data for all cities in parallel via OPeNDAP
python merra2_parallel.py --locations city_coordinates.csv -o merra2_output --workers 6
```
