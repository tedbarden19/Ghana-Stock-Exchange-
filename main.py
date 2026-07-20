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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
MAIN_DATA_FILE = os.path.join(BASE_DIR, "Data.csv")
LOG_FILE = os.path.join(BASE_DIR, "scraper.log")

logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")

def log(msg, level="info"):
    print(msg)
    getattr(logging, level)(msg)

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
        "behavior": "allow", "downloadPath": DOWNLOAD_DIR
    })
    
    try:
        today_str = datetime.date.today().strftime("%d/%m/%Y")
        log(f"── SCRAPE: Fetching data for {today_str}")
        
        driver.get("https://gse.com.gh/trading-and-data/")
        wait = WebDriverWait(driver, 30)
        
        # More robust date inputs (try multiple possible selectors if needed)
        from_date_input = wait.until(EC.presence_of_element_located((By.XPATH,
            "//input[contains(@placeholder, 'From') or @name='from'] | //span/input[1]")))
        from_date_input.clear()
        from_date_input.send_keys(today_str)
        
        to_date_input = wait.until(EC.presence_of_element_located((By.XPATH,
            "//input[contains(@placeholder, 'To') or @name='to'] | //span/input[2]")))
        to_date_input.clear()
        to_date_input.send_keys(today_str)
        to_date_input.send_keys(Keys.RETURN)
        
        time.sleep(8)  # Give table time to load
        
        # Select "All" entries
        try:
            dropdown = wait.until(EC.element_to_be_clickable((By.XPATH,
                "//button[contains(., 'entries') or contains(@class, 'dropdown')]")))
            dropdown.click()
            all_option = wait.until(EC.element_to_be_clickable((By.XPATH,
                "//a[contains(text(), 'All')] | //li[contains(@class, 'all')]")))
            all_option.click()
            time.sleep(8)
            log("── SCRAPE: Selected 'All' entries")
        except Exception as e:
            log(f"── SCRAPE: Could not select All (non-fatal): {e}", "warning")
        
        # Download CSV
        csv_button = wait.until(EC.element_to_be_clickable((By.XPATH,
            "//button[contains(., 'CSV') or contains(@class, 'csv')]")))
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
        files = [f for f in os.listdir(DOWNLOAD_DIR) if f.endswith(".csv")]
        if files:
            latest = max([os.path.join(DOWNLOAD_DIR, f) for f in files], key=os.path.getmtime)
            # Check file is complete (no .crdownload and > 100 bytes)
            if not any(f.endswith(".crdownload") for f in os.listdir(DOWNLOAD_DIR)) and os.path.getsize(latest) > 100:
                log(f"── SCRAPE: Download ready: {os.path.basename(latest)}")
                return latest
        time.sleep(poll)
    raise TimeoutError("Download timeout")

def clean(filepath):
    log("── CLEAN: Cleaning data...")
    df = pd.read_csv(filepath, encoding='utf-8-sig')  # Handles BOM if present
    
    if df.empty or len(df) < 5:  # More tolerant threshold
        log("── CLEAN: No (or very little) data for today. Stopping.")
        return None
    
    df = df.loc[:, ~df.columns.str.match(r"^Unnamed")]
    df.columns = df.columns.str.strip()
    
    df['Daily Date'] = pd.to_datetime(df['Daily Date'], dayfirst=True, errors='coerce')
    df = df.dropna(subset=['Daily Date'])  # Drop bad rows
    
    if df.empty:
        log("── CLEAN: No valid rows after parsing dates.")
        return None
    
    # Rest of your cleaning...
    df['Share Code'] = df['Share Code'].astype(str).str.replace('*', '', regex=False).str.strip()
    
    NUMERIC_COLS = [...]  # your list
    for col in NUMERIC_COLS:
        if col in df.columns:
            if df[col].dtype == object:
                df[col] = df[col].astype(str).str.replace(',', '', regex=False)
            df[col] = pd.to_numeric(df[col], errors='coerce')
    
    log(f"── CLEAN: {len(df)} rows cleaned")
    return df

# append_to_main remains mostly the same (add duplicate check by date + Share Code for safety)

if __name__ == "__main__":
    # ... your main runner with try/except
