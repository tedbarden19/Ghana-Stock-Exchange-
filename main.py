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


BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR   = os.path.join(BASE_DIR, "downloads")
MAIN_DATA_FILE = os.path.join(BASE_DIR, "Data.csv")
LOG_FILE       = os.path.join(BASE_DIR, "scraper.log")


logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def log(msg, level="info"):
    print(msg)
    getattr(logging, level)(msg)

#Web Scraping
def scrape():
    log("── SCRAPE: Starting browser...")
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
        current_date = datetime.date.today().strftime("%d/%m/%Y")
        log(f"── SCRAPE: Fetching data for {current_date}")

        driver.get("https://gse.com.gh/trading-and-data/")
        wait = WebDriverWait(driver, 20)

        from_date_input = wait.until(EC.presence_of_element_located((By.XPATH,
            "/html/body/div[1]/div/div[3]/div[1]/div/div/div/div[4]/div[2]/div/div/div/div[2]/div[1]/div/div[1]/div/span/input[1]")))
        from_date_input.clear()
        from_date_input.send_keys(current_date)

        to_date_input = wait.until(EC.presence_of_element_located((By.XPATH,
            "/html/body/div[1]/div/div[3]/div[1]/div/div/div/div[4]/div[2]/div/div/div/div[2]/div[1]/div/div[1]/div/span/input[2]")))
        to_date_input.clear()
        to_date_input.send_keys(current_date)
        to_date_input.send_keys(Keys.RETURN)
        time.sleep(10)

        try:
            dropdown_button = wait.until(EC.element_to_be_clickable((By.XPATH,
                "/html/body/div[1]/div/div[3]/div[1]/div/div/div/div[4]/div[2]/div/div/div/div[2]/div[2]/div[3]/label/div/button")))
            dropdown_button.click()
            time.sleep(1)

            all_option = wait.until(EC.element_to_be_clickable((By.XPATH,
                "/html/body/div[1]/div/div[3]/div[1]/div/div/div/div[4]/div[2]/div/div/div/div[2]/div[2]/div[3]/label/div/div/ul/li[7]/a")))
            all_option.click()
            time.sleep(10)
            log("── SCRAPE: Selected 'All' entries")
        except Exception as e:
            log(f"── SCRAPE: Could not select 'All' option: {e}", "warning")

        csv_button = wait.until(EC.element_to_be_clickable((By.XPATH,
            "/html/body/div[1]/div/div[3]/div[1]/div/div/div/div[4]/div[2]/div/div/div/div[2]/div[2]/div[1]/button[3]")))
        csv_button.click()
        log("── SCRAPE: Download initiated, waiting for file...")

      
        _wait_for_download(timeout=30)

    except Exception as e:
        log(f"── SCRAPE ERROR: {e}", "error")
        try:
            driver.save_screenshot(os.path.join(BASE_DIR, "error_screenshot.png"))
        except Exception:
            pass
        raise
    finally:
        driver.quit()
        log("── SCRAPE: Browser closed")


def _wait_for_download(timeout=30, poll=1):
    """Wait until a .csv file appears in DOWNLOAD_DIR and is fully written
    (no lingering .crdownload part-file)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        files = os.listdir(DOWNLOAD_DIR)
        has_partial = any(f.endswith(".crdownload") for f in files)
        csvs = [f for f in files if f.endswith(".csv")]
        if csvs and not has_partial:
            log(f"── SCRAPE: Download confirmed: {csvs[-1]}")
            return
        time.sleep(poll)
    raise TimeoutError(
        f"No completed CSV download detected in {DOWNLOAD_DIR} after {timeout}s. "
        "Check error_screenshot.png / scraper.log — the page likely didn't load "
        "as expected (selectors may have changed, or download was blocked)."
    )


#  FIND the most recently downloaded CSV

def get_latest_download():
    csv_files = [
        os.path.join(DOWNLOAD_DIR, f)
        for f in os.listdir(DOWNLOAD_DIR)
        if f.endswith(".csv")
    ]
    if not csv_files:
        raise FileNotFoundError(f"No CSV files found in {DOWNLOAD_DIR}")
    latest = max(csv_files, key=os.path.getmtime)
    log(f"── CLEAN: Found downloaded file: {latest}")
    return latest



#  CLEAN the downloaded data



NUMERIC_COLS = [
    'Year High (GH¢)', 'Year Low (GH¢)', 'Previous Closing Price - VWAP (GH¢)',
    'Opening Price (GH¢)', 'Last Transaction Price (GH¢)', 'Closing Price - VWAP (GH¢)',
    'Price Change (GH¢)', 'Closing Bid Price (GH¢)', 'Closing Offer Price (GH¢)',
    'Total Shares Traded', 'Total Value Traded (GH¢)'
]


def clean(filepath):
    log("── CLEAN: Cleaning data...")
    df = pd.read_csv(filepath)

    # If the file is empty, stop the process
    if df.empty:
        log("── CLEAN: No data for today — the GSE has not published records yet. Stopping.")
        return None

    df = df.loc[:, ~df.columns.str.match(r"^Unnamed")]
    df.columns = df.columns.str.strip()

   
    df['Daily Date'] = pd.to_datetime(df['Daily Date'], format="%d/%m/%Y")

    df['Share Code'] = df['Share Code'].astype(str).str.replace('*', '', regex=False).str.strip()

    for col in NUMERIC_COLS:
        if df[col].dtype == object:
            df[col] = df[col].astype(str).str.replace(',', '', regex=False)
        df[col] = pd.to_numeric(df[col], errors='coerce')

    cols = ['Closing Bid Price (GH¢)', 'Closing Offer Price (GH¢)']
    df[cols] = df[cols].fillna(0)

    log(f"── CLEAN: {len(df)} rows cleaned")
    return df


# APPEND new rows to main data file

def append_to_main(df):
    log("── APPEND: Appending to main dataset...")

    # Write header only if Data.csv doesn't exist yet (first ever run)
    write_header = not os.path.exists(MAIN_DATA_FILE)

    # Avoid duplicate rows if the workflow runs more than once for the same day
    if not write_header:
        try:
            existing = pd.read_csv(MAIN_DATA_FILE)
            # Data.csv is always written in ISO format (see append below),
            # so pin the format here too instead of letting pandas guess.
            existing['Daily Date'] = pd.to_datetime(existing['Daily Date'], format="%Y-%m-%d", errors='coerce')
            before = len(df)
            df = df[~df['Daily Date'].isin(existing['Daily Date'])]
            skipped = before - len(df)
            if skipped:
                log(f"── APPEND: Skipped {skipped} row(s) already present for today's date")
        except Exception as e:
            log(f"── APPEND: Could not check for duplicates ({e}), appending as-is", "warning")

    if df.empty:
        log("── APPEND: Nothing new to append")
        return

    df.to_csv(MAIN_DATA_FILE, mode='a', header=write_header, index=False)
    log(f"── APPEND: {len(df)} new rows added to {MAIN_DATA_FILE}")



# MAIN RUNNER

if __name__ == "__main__":
    log("══════════════════════════════════════")
    log(f"  GSE Scraper started at {datetime.datetime.now()}")
    log("══════════════════════════════════════")

    try:
        scrape()
        latest_file = get_latest_download()
        cleaned_df  = clean(latest_file)

        if cleaned_df is None:
            log("✗ Process stopped — no data available for today.")
        else:
            append_to_main(cleaned_df)
            log("✓ All steps completed successfully")

    except Exception as e:
        log(f"✗ Fatal error: {e}", "error")
        raise
