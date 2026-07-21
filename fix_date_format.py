"""
Run this ONCE against your existing Data.csv in the repo to normalize
every row to a single date format (M/D/YYYY). pandas can read mixed
formats fine, so this just re-parses and rewrites the whole column
consistently. After running this, main.py's updated append_to_main()
will keep all future rows in the same format automatically.
"""
import pandas as pd

DATA_FILE = "Data.csv"
DATE_FORMAT = "%m/%d/%Y"

df = pd.read_csv(DATA_FILE)
df['Daily Date'] = pd.to_datetime(df['Daily Date'])
df['Daily Date'] = df['Daily Date'].dt.strftime(DATE_FORMAT)
df.to_csv(DATA_FILE, index=False)

print(f"Normalized {len(df)} rows to {DATE_FORMAT} format in {DATA_FILE}")
