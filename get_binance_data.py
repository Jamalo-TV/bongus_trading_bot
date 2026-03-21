import os
import io
import zipfile
import requests
import pandas as pd
from datetime import datetime
from dateutil.relativedelta import relativedelta

# --- CONFIGURATION ---
SYMBOL = "BTCUSDT"
START_MONTH = "2023-01"
END_MONTH = "2024-12"
DATA_DIR = "data"

# Binance Vision Base URLs
URLS = {
    "spot": f"https://data.binance.vision/data/spot/monthly/klines/{SYMBOL}/1m/{SYMBOL}-1m-{{}}.zip",
    "perp": f"https://data.binance.vision/data/futures/um/monthly/klines/{SYMBOL}/1m/{SYMBOL}-1m-{{}}.zip",
    "funding": f"https://data.binance.vision/data/futures/um/monthly/fundingRate/{SYMBOL}/{SYMBOL}-fundingRate-{{}}.zip"
}

# Standard Kline columns for Binance CSVs
KLINE_COLS = [
    'timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 
    'quote_volume', 'count', 'taker_buy_volume', 'taker_buy_quote_volume', 'ignore'
]

def get_months(start, end):
    """Generate a list of YYYY-MM strings between start and end dates."""
    start_date = datetime.strptime(start, "%Y-%m")
    end_date = datetime.strptime(end, "%Y-%m")
    months = []
    current_date = start_date
    while current_date <= end_date:
        months.append(current_date.strftime("%Y-%m"))
        current_date += relativedelta(months=1)
    return months

def download_and_extract(url):
    """Download a zip file from a URL and return the raw CSV string."""
    response = requests.get(url)
    if response.status_code == 200:
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            # Assuming there is only one CSV inside the zip
            csv_filename = z.namelist()[0]
            with z.open(csv_filename) as f:
                return pd.read_csv(f, header=None) # Read raw without headers first
    else:
        print(f"  [!] Failed to download: {url} (Status: {response.status_code})")
        return None

def process_klines(raw_df):
    """Format Kline (Spot/Perp) data."""
    # Handle files with a header string "open_time" in the first row
    if isinstance(raw_df.iloc[0, 0], str) and raw_df.iloc[0, 0] == 'open_time':
        raw_df = raw_df[1:].reset_index(drop=True)

    raw_df.columns = KLINE_COLS
    
    # Cast columns to numeric. If the file had a header, pandas read these as strings!
    for col in ['timestamp', 'open', 'high', 'low', 'close', 'volume']:
        raw_df[col] = pd.to_numeric(raw_df[col])
        
    # Convert millisecond timestamp to datetime
    raw_df['timestamp'] = pd.to_datetime(raw_df['timestamp'], unit='ms')
    # Keep only the essential columns to save space
    return raw_df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]

def process_funding(raw_df):
    """Format Funding Rate data."""
    # Binance funding CSVs sometimes have headers, sometimes they don't.
    # We check if the first row contains text ('calc_time') to handle both.
    if isinstance(raw_df.iloc[0, 0], str) and raw_df.iloc[0, 0] == 'calc_time':
        raw_df = raw_df[1:].reset_index(drop=True)
    
    raw_df = raw_df.iloc[:, :3] # Take only first 3 columns
    
    # Binance Vision Funding format: [calc_time, funding_interval_hours, last_funding_rate]
    raw_df.columns = ['calc_time', 'funding_interval_hours', 'funding_rate']
    
    # Convert millisecond timestamp to datetime
    raw_df['calc_time'] = pd.to_numeric(raw_df['calc_time'])
    raw_df['timestamp'] = pd.to_datetime(raw_df['calc_time'], unit='ms')
    raw_df['funding_rate'] = pd.to_numeric(raw_df['funding_rate'])
    
    return raw_df[['timestamp', 'funding_rate']]

def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    months = get_months(START_MONTH, END_MONTH)
    
    spot_dfs = []
    perp_dfs = []
    funding_dfs = []
    
    print(f"Starting download for {SYMBOL} from {START_MONTH} to {END_MONTH}...")
    
    for month in months:
        print(f"\nProcessing {month}...")
        
        # 1. Spot Klines
        print("  Downloading Spot 1m Klines...")
        spot_url = URLS["spot"].format(month)
        raw_spot = download_and_extract(spot_url)
        if raw_spot is not None:
            spot_dfs.append(process_klines(raw_spot))
            
        # 2. Perp Klines
        print("  Downloading Perp 1m Klines...")
        perp_url = URLS["perp"].format(month)
        raw_perp = download_and_extract(perp_url)
        if raw_perp is not None:
            perp_dfs.append(process_klines(raw_perp))
            
        # 3. Funding Rates
        print("  Downloading Funding Rates...")
        funding_url = URLS["funding"].format(month)
        raw_funding = download_and_extract(funding_url)
        if raw_funding is not None:
            funding_dfs.append(process_funding(raw_funding))

    print("\nStitching datasets together and saving to Parquet...")
    
    # Concatenate, sort by timestamp, and save SPOT
    if spot_dfs:
        spot_df = pd.concat(spot_dfs, ignore_index=True)
        spot_df.sort_values('timestamp', inplace=True)
        spot_df.to_parquet(os.path.join(DATA_DIR, "spot_1m.parquet"), index=False)
        print(f"✅ Saved spot_1m.parquet ({len(spot_df)} rows)")

    # Concatenate, sort by timestamp, and save PERP
    if perp_dfs:
        perp_df = pd.concat(perp_dfs, ignore_index=True)
        perp_df.sort_values('timestamp', inplace=True)
        perp_df.to_parquet(os.path.join(DATA_DIR, "perp_1m.parquet"), index=False)
        print(f"✅ Saved perp_1m.parquet ({len(perp_df)} rows)")

    # Concatenate, sort by timestamp, and save FUNDING
    if funding_dfs:
        funding_df = pd.concat(funding_dfs, ignore_index=True)
        funding_df.sort_values('timestamp', inplace=True)
        funding_df.to_parquet(os.path.join(DATA_DIR, "funding_rates.parquet"), index=False)
        print(f"✅ Saved funding_rates.parquet ({len(funding_df)} rows)")

    print("\nAll done! Your data is ready for feature engineering.")

if __name__ == "__main__":
    main()