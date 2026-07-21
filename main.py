import datetime
import logging
import os
import time
import pandas as pd
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.keys import Keys
import shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
MAIN_DATA_FILE = os.path.join(BASE_DIR, "Data.csv")
LOG_FILE = os.path.join(BASE_DIR, "scraper.log")

# Canonical date format used throughout Data.csv. Keeping this as a single
# constant means every write path (first write, append) stays consistent,
# which is what Power Query / Power BI needs to reliably type the column.
DATE_FORMAT = "%m/%d/%Y"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def log(msg, level="info"):
    print(msg)
    getattr(logging, level)(msg)

def scrape():
    log("── SCRAPE: Starting browser...")
    # Clear previous downloads
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")

    prefs = {
        "download.default_directory": DOWNLOAD_DIR,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True
    }
    chrome_options.add_experimental_option("prefs", prefs)

    driver = webdriver.Chrome(options=chrome_options)
    driver.execute_cdp_cmd("Page.setDownloadBehavior", {
        "behavior": "allow",
        "downloadPath": DOWNLOAD_DIR
    })

    try:
        today_str = datetime.date.today().strftime("%d/%m/%Y")
        log(f"── SCRAPE: Fetching data for {today_str}")

        driver.get("https://gse.com.gh/trading-and-data/")
        wait = WebDriverWait(driver, 30)

        # Date inputs (more robust selectors)
        from_date_input = wait.until(EC.presence_of_element_located((By.XPATH,
            "/html/body/div[1]/div/div[3]/div[1]/div/div/div/div[4]/div[2]/div/div/div/div[2]/div[1]/div/div[1]/div/span/input[1]")))
        from_date_input.clear()
        from_date_input.send_keys(today_str)

        to_date_input = wait.until(EC.presence_of_element_located((By.XPATH,
            "/html/body/div[1]/div/div[3]/div[1]/div/div/div/div[4]/div[2]/div/div/div/div[2]/div[1]/div/div[1]/div/span/input[2]")))
        to_date_input.clear()
        to_date_input.send_keys(today_str)
        to_date_input.send_keys(Keys.RETURN)

        time.sleep(10)

        # Select "All"
        try:
            dropdown_button = wait.until(EC.element_to_be_clickable((By.XPATH,
                "/html/body/div[1]/div/div[3]/div[1]/div/div/div/div[4]/div[2]/div/div/div/div[2]/div[2]/div[3]/label/div/button")))
            dropdown_button.click()
            time.sleep(2)
            all_option = wait.until(EC.element_to_be_clickable((By.XPATH,
                "/html/body/div[1]/div/div[3]/div[1]/div/div/div/div[4]/div[2]/div/div/div/div[2]/div[2]/div[3]/label/div/div/ul/li[7]/a")))
            all_option.click()
            time.sleep(8)
            log("── SCRAPE: Selected 'All' entries")
        except Exception as e:
            log(f"── SCRAPE: Could not select All: {e}", "warning")

        # Download CSV
        csv_button = wait.until(EC.element_to_be_clickable((By.XPATH,
            "/html/body/div[1]/div/div[3]/div[1]/div/div/div/div[4]/div[2]/div/div/div/div[2]/div[2]/div[1]/button[3]")))
        csv_button.click()
        log("── SCRAPE: Download initiated...")

        latest_file = _wait_for_download(timeout=45)
        return latest_file

    except Exception as e:
        log(f"── SCRAPE ERROR: {e}", "error")
        try:
            driver.save_screenshot(os.path.join(BASE_DIR, "error_screenshot.png"))
        except:
            pass
        raise
    finally:
        driver.quit()
        log("── SCRAPE: Browser closed")


def _wait_for_download(timeout=45, poll=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        files = os.listdir(DOWNLOAD_DIR)
        csv_files = [os.path.join(DOWNLOAD_DIR, f) for f in files if f.endswith(".csv")]
        if csv_files:
            latest = max(csv_files, key=os.path.getmtime)
            if os.path.getsize(latest) > 200:  # meaningful file
                log(f"── SCRAPE: Download confirmed: {os.path.basename(latest)}")
                return latest
        time.sleep(poll)
    raise TimeoutError("No completed CSV download detected")


# === CLEANING ===
NUMERIC_COLS = [
    'Year High (GH¢)', 'Year Low (GH¢)', 'Previous Closing Price - VWAP (GH¢)',
    'Opening Price (GH¢)', 'Last Transaction Price (GH¢)', 'Closing Price - VWAP (GH¢)',
    'Price Change (GH¢)', 'Closing Bid Price (GH¢)', 'Closing Offer Price (GH¢)',
    'Total Shares Traded', 'Total Value Traded (GH¢)'
]

def clean(filepath):
    log("── CLEAN: Cleaning data...")
    df = pd.read_csv(filepath, encoding='utf-8-sig')

    if df.empty or len(df) < 5:
        log("── CLEAN: No meaningful data for today — GSE has not published yet.")
        return None

    df = df.loc[:, ~df.columns.str.match(r"^Unnamed")]
    df.columns = df.columns.str.strip()

    df['Daily Date'] = pd.to_datetime(df['Daily Date'], dayfirst=True, errors='coerce')
    df = df.dropna(subset=['Daily Date'])

    if df.empty:
        log("── CLEAN: No valid rows after date parsing.")
        return None

    df['Share Code'] = df['Share Code'].astype(str).str.replace('*', '', regex=False).str.strip()

    for col in NUMERIC_COLS:
        if col in df.columns:
            if df[col].dtype == object:
                df[col] = df[col].astype(str).str.replace(',', '', regex=False)
            df[col] = pd.to_numeric(df[col], errors='coerce')

    cols = ['Closing Bid Price (GH¢)', 'Closing Offer Price (GH¢)']
    for c in cols:
        if c in df.columns:
            df[c] = df[c].fillna(0)

    log(f"── CLEAN: {len(df)} rows cleaned successfully")
    return df


def append_to_main(df):
    if df is None or df.empty:
        return

    log("── APPEND: Appending to main dataset...")
    write_header = not os.path.exists(MAIN_DATA_FILE)

    if not write_header:
        existing = pd.read_csv(MAIN_DATA_FILE)
        existing['Daily Date'] = pd.to_datetime(existing['Daily Date'], errors='coerce')
        df = df[~df['Daily Date'].isin(existing['Daily Date'])]

    if df.empty:
        log("── APPEND: No new rows to add (all dates already present).")
        return

    # Lock the date column to a single consistent text format before writing.
    # Without this, pandas writes datetime columns out as ISO (YYYY-MM-DD),
    # which silently mismatches the M/D/YYYY format already in Data.csv and
    # breaks Power Query's date type detection on the newest rows.
    df = df.copy()
    df['Daily Date'] = df['Daily Date'].dt.strftime(DATE_FORMAT)

    df.to_csv(MAIN_DATA_FILE, mode='a', header=write_header, index=False)
    log(f"── APPEND: {len(df)} new rows added")


# === MAIN ===
if __name__ == "__main__":
    log("══════════════════════════════════════")
    log(f"GSE Scraper started at {datetime.datetime.now()}")
    log("══════════════════════════════════════")

    try:
        latest_file = scrape()
        cleaned_df = clean(latest_file)
        append_to_main(cleaned_df)
        log("✓ All steps completed successfully")
    except Exception as e:
        log(f"✗ Fatal error: {e}", "error")
        raise
