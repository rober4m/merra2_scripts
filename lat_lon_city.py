import pandas as pd
import argparse
import time
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderRateLimited, GeocoderTimedOut, GeocoderServiceError

parser = argparse.ArgumentParser()
parser.add_argument("input_csv", help="CSV file with 'city' and 'country' columns")
parser.add_argument("-o", "--output", required=True, help="Output CSV file")
args = parser.parse_args()

geolocator = Nominatim(user_agent="city_geocoder/1.0 (rober.mamani@gmail.com)", timeout=10)

def geocode(city, country, max_retries=6):
    query = f"{city}, {country}"
    for attempt in range(max_retries):
        try:
            time.sleep(1.5)
            location = geolocator.geocode(query)
            if location:
                print(f"✓ {city}, {country}")
                return round(location.latitude, 5), round(location.longitude, 5)
            print(f"✗ {city}, {country}: not found")
            return None, None
        except GeocoderRateLimited:
            wait = 30 * (2 ** attempt)
            print(f"  rate limited on '{city}, {country}', retrying in {wait}s...")
            time.sleep(wait)
        except (GeocoderTimedOut, GeocoderServiceError) as e:
            wait = 10 * (2 ** attempt)
            print(f"  error on '{city}, {country}' ({e}), retrying in {wait}s...")
            time.sleep(wait)
    print(f"✗ {city}, {country}: failed after {max_retries} retries")
    return None, None

df = pd.read_csv(args.input_csv)
df[["lat", "lon"]] = df.apply(lambda row: pd.Series(geocode(row["city"], row["country"])), axis=1)
df[["city", "country", "lat", "lon"]].to_csv(args.output, index=False)
