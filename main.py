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

# Confirmed via DevTools: rows are plain <tr class="odd"|"even"> elements
# directly inside a <tbody>, so this simple CSS selector works fine.
TABLE_ROW_SELECTOR = "table tbody tr"

# Known universe of listed share codes as of your existing Data.csv (54).
# Used only as a sanity check to flag suspiciously incomplete scrapes —
# update this number if GSE lists/delists shares over time.
EXPECTED_MIN_ROWS = 40

# ── LOCAL TESTING SWITCHES ──────────────────────────────────────────────
# Set TEST_DATE_OVERRIDE to a "DD/MM/YYYY" string (e.g. a known Friday) to
# force the scraper to query that specific date instead of today. Set it
# back to None before pushing to GitHub Actions / running in production.
TEST_DATE_OVERRIDE = None  # production: uses today's real date

# Set to True to watch the browser window while testing locally (helps you
# visually confirm "All" gets selected and the table loads fully). Must be
# False in GitHub Actions / any headless server environment.
RUN_HEADFUL_FOR_TESTING = False
# ─────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def log(msg, level="info"):
    print(msg)
    getattr(logging, level)(msg)


def wait_for_table_stable(driver, row_selector, timeout=30, stable_checks=3, poll=1):
    """
    Wait until the table's row count stops changing for `stable_checks`
    consecutive polls. This replaces a blind time.sleep() with a real signal
    that the page has finished rendering, so we don't export a half-loaded
    table on slow days.
    """
    last_count = -1
    stable_count = 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = driver.find_elements(By.CSS_SELECTOR, row_selector)
        current_count = len(rows)
        if current_count == last_count and current_count > 0:
            stable_count += 1
            if stable_count >= stable_checks:
                return current_count
        else:
            stable_count = 0
        last_count = current_count
        time.sleep(poll)
    raise TimeoutError(f"Table row count never stabilized (last seen: {last_count})")


def scrape():
    log("── SCRAPE: Starting browser...")
    # Clear previous downloads
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    chrome_options = Options()
    if not RUN_HEADFUL_FOR_TESTING:
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
        today_str = TEST_DATE_OVERRIDE if TEST_DATE_OVERRIDE else datetime.date.today().strftime("%d/%m/%Y")
        if TEST_DATE_OVERRIDE:
            log(f"── SCRAPE: TEST MODE — using override date {today_str} instead of today", "warning")
        log(f"── SCRAPE: Fetching data for {today_str}")

        driver.get("https://gse.com.gh/trading-and-data/")
        wait = WebDriverWait(driver, 30)

        # Date inputs
        from_date_input = wait.until(EC.presence_of_element_located((By.XPATH,
            "/html/body/div[1]/div/div[3]/div[1]/div/div/div/div[4]/div[2]/div/div/div/div[2]/div[1]/div/div[1]/div/span/input[1]")))
        from_date_input.clear()
        from_date_input.send_keys(today_str)

        to_date_input = wait.until(EC.presence_of_element_located((By.XPATH,
            "/html/body/div[1]/div/div[3]/div[1]/div/div/div/div[4]/div[2]/div/div/div/div[2]/div[1]/div/div[1]/div/span/input[2]")))
        to_date_input.clear()
        to_date_input.send_keys(today_str)
        to_date_input.send_keys(Keys.RETURN)

        # Wait for the table to actually reflect the new date filter,
        # instead of a blind sleep. If the table hasn't rendered anything
        # yet (e.g. it's still showing a placeholder), this will just wait
        # out its timeout below and fall through — the "All" selection and
        # final stability check will catch a genuinely broken load.
        try:
            wait_for_table_stable(driver, TABLE_ROW_SELECTOR, timeout=20)
        except TimeoutError as e:
            log(f"── SCRAPE: Table did not stabilize after date filter: {e}", "warning")

        # Select "All" entries per page
        all_selected = False
        try:
            dropdown_button = wait.until(EC.element_to_be_clickable((By.XPATH,
                "/html/body/div[1]/div/div[3]/div[1]/div/div/div/div[4]/div[2]/div/div/div/div[2]/div[2]/div[3]/label/div/button")))
            dropdown_button.click()
            time.sleep(2)
            all_option = wait.until(EC.element_to_be_clickable((By.XPATH,
                "/html/body/div[1]/div/div[3]/div[1]/div/div/div/div[4]/div[2]/div/div/div/div[2]/div[2]/div[3]/label/div/div/ul/li[7]/a")))
            all_option.click()
            all_selected = True
            log("── SCRAPE: Selected 'All' entries")
        except Exception as e:
            log(f"── SCRAPE: Could not select All: {e}", "warning")

        # Wait for the table to stabilize again after switching to "All" —
        # this is the critical wait that replaces your old time.sleep(8).
        try:
            row_count = wait_for_table_stable(driver, TABLE_ROW_SELECTOR, timeout=30)
            log(f"── SCRAPE: Table stabilized with {row_count} rows")
        except TimeoutError as e:
            log(f"── SCRAPE: Table did not stabilize after selecting All: {e}", "warning")
            row_count = len(driver.find_elements(By.CSS_SELECTOR, TABLE_ROW_SELECTOR))

        # Sanity check: flag (but don't block) a suspiciously incomplete page.
        # This won't fix a bad scrape by itself, but it guarantees the issue
        # shows up in scraper.log and in the screenshot below instead of
        # silently producing a partial CSV.
        if not all_selected:
            log("── SCRAPE WARNING: 'All' entries selection failed — export may be paginated/partial.", "warning")
        if row_count < EXPECTED_MIN_ROWS:
            log(f"── SCRAPE WARNING: Only {row_count} rows visible, expected ~{EXPECTED_MIN_ROWS}+. "
                f"Possible incomplete load.", "warning")

        # Always capture a screenshot right before export — not just on
        # failure — so you can visually confirm each day whether the full
        # table was loaded and "All" was genuinely applied before the CSV
        # was generated. Check this in the uploaded artifacts.
        try:
            driver.save_screenshot(os.path.join(BASE_DIR, "pre_export_screenshot.png"))
            log("── SCRAPE: Saved pre-export screenshot")
        except Exception as e:
            log(f"── SCRAPE: Could not save pre-export screenshot: {e}", "warning")

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
        except Exception:
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

    # Extra guard on top of the row-count check above: if the volume/value
    # columns are entirely blank across every row, GSE likely hasn't
    # published final trading figures yet even though the page returned
    # rows. Bail out here rather than writing incomplete data to Data.csv.
    volume_cols_present = [c for c in ['Total Shares Traded', 'Total Value Traded (GH¢)'] if c in df.columns]
    if volume_cols_present and all(df[c].isna().all() for c in volume_cols_present):
        log("── CLEAN: Volume/value columns entirely blank — data not yet finalized. Skipping.", "warning")
        return None

    # Row-count sanity check, mirrored from the scrape step, in case a
    # partial page slipped through (e.g. 'All' selection failed silently).
    if len(df) < EXPECTED_MIN_ROWS:
        log(f"── CLEAN WARNING: Only {len(df)} rows in cleaned data, expected ~{EXPECTED_MIN_ROWS}+. "
            f"Proceeding anyway, but check pre_export_screenshot.png.", "warning")

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
