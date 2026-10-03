import argparse
import datetime
import logging
import os
import re
import shutil
import time
from zoneinfo import ZoneInfo

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
MAIN_DATA_FILE = os.path.join(BASE_DIR, "Data.csv")
LOG_FILE = os.path.join(BASE_DIR, "scraper.log")
TZ = ZoneInfo("Africa/Accra")

DATE_COL = "Daily Date"
CODE_COL = "Share Code"

# Final column names/order (currency symbol removed on purpose; all prices are GH¢).
FINAL_COLS = [
    DATE_COL, CODE_COL,
    "Year High", "Year Low", "Previous Closing Price - VWAP",
    "Opening Price", "Last Transaction Price", "Closing Price - VWAP",
    "Price Change", "Closing Bid Price", "Closing Offer Price",
    "Total Shares Traded", "Total Value Traded",
]
NUMERIC_COLS = FINAL_COLS[2:]
# Blank here really means "nothing happened", so 0 is the honest value.
FILL_ZERO_COLS = ["Closing Bid Price", "Closing Offer Price",
                  "Total Shares Traded", "Total Value Traded"]

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8",
)


def log(msg, level="info"):
    print(msg)
    getattr(logging, level)(msg)


# ───────────────────────── cleaning helpers ─────────────────────────
def normalize_header(name):
    """'Year Low (GH¢)' / 'Year Low (GHÂ¢)' -> 'Year Low'."""
    name = str(name).replace("\ufeff", "").strip()
    name = re.sub(r"\s*\(\s*GH[^)]*\)", "", name)
    return re.sub(r"\s+", " ", name).strip()


def parse_mixed_dates(s):
    """Parse a column holding a mix of YYYY-MM-DD and M/D/YYYY text."""
    s = s.astype(str).str.strip()
    is_iso = s.str.match(r"^\d{4}-\d{2}-\d{2}")
    iso = pd.to_datetime(s.where(is_iso).str[:10], format="%Y-%m-%d", errors="coerce")
    us = pd.to_datetime(s.where(~is_iso), format="%m/%d/%Y", errors="coerce")
    return iso.fillna(us)


def format_dates(s):
    """Datetime -> M/D/YYYY text (no zero padding, same as the historical rows)."""
    return (s.dt.month.astype(str) + "/" + s.dt.day.astype(str) + "/" + s.dt.year.astype(str))


def standardize(df):
    """Common cleaning for both the downloaded file and the existing Data.csv."""
    df = df.loc[:, ~df.columns.astype(str).str.match(r"^Unnamed")].copy()
    df.columns = [normalize_header(c) for c in df.columns]

    missing = [c for c in FINAL_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"Expected columns missing from data: {missing}. "
                         f"Found: {list(df.columns)}")
    df = df[FINAL_COLS]

    df[CODE_COL] = df[CODE_COL].astype(str).str.replace("*", "", regex=False).str.strip()
    df = df[df[CODE_COL].ne("") & df[CODE_COL].str.lower().ne("nan")]

    for col in NUMERIC_COLS:
        if df[col].dtype == object or str(df[col].dtype).startswith("str"):
            df[col] = df[col].astype(str).str.replace(",", "", regex=False).str.strip()
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df[FILL_ZERO_COLS] = df[FILL_ZERO_COLS].fillna(0)
    return df


def finalize(df):
    """Dedupe + sort (stable, so share order within a day is preserved)."""
    df = df.copy()
    df[DATE_COL] = pd.to_datetime(df[DATE_COL], errors="coerce")
    df = df.dropna(subset=[DATE_COL])
    df = df.drop_duplicates(subset=[DATE_COL, CODE_COL], keep="last")
    return df.sort_values(DATE_COL, kind="mergesort").reset_index(drop=True)


def load_existing():
    if not os.path.exists(MAIN_DATA_FILE):
        return pd.DataFrame(columns=FINAL_COLS)
    raw = pd.read_csv(MAIN_DATA_FILE, encoding="utf-8-sig", dtype=str)
    raw.columns = [normalize_header(c) for c in raw.columns]
    raw[DATE_COL] = parse_mixed_dates(raw[DATE_COL])
    bad = raw[DATE_COL].isna().sum()
    if bad:
        log(f"── LOAD: dropping {bad} rows in Data.csv with unreadable dates", "warning")
    return finalize(standardize(raw))


def save(df):
    out = df.copy()
    out["Total Shares Traded"] = out["Total Shares Traded"].round().astype("Int64")
    out[DATE_COL] = format_dates(out[DATE_COL])
    tmp = MAIN_DATA_FILE + ".tmp"
    out.to_csv(tmp, index=False, encoding="utf-8")
    os.replace(tmp, MAIN_DATA_FILE)


def clean_download(filepath):
    log("── CLEAN: Cleaning downloaded file...")
    raw = pd.read_csv(filepath, encoding="utf-8-sig", dtype=str)
    if raw.empty:
        log("── CLEAN: Downloaded file has no rows.")
        return pd.DataFrame(columns=FINAL_COLS)
    raw.columns = [str(c).strip() for c in raw.columns]
    date_src = next((c for c in raw.columns if normalize_header(c) == DATE_COL), None)
    if date_src is None:
        raise ValueError(f"No '{DATE_COL}' column in download. Found: {list(raw.columns)}")
    raw[date_src] = pd.to_datetime(raw[date_src], dayfirst=True, errors="coerce")
    raw = raw.dropna(subset=[date_src])
    df = standardize(raw)
    log(f"── CLEAN: {len(df)} rows cleaned")
    return df


# ───────────────────────── scraping ─────────────────────────
PAGE_SIZE = "All"   # rows-per-page option to select on the site ("All" or e.g. "100")
# Truncation guard only makes sense for a numeric page size.
MAX_ROWS = int(PAGE_SIZE) if PAGE_SIZE.isdigit() else None
URL = "https://gse.com.gh/trading-and-data/"
BASE_XPATH = ("/html/body/div[1]/div/div[3]/div[1]/div/div/div/div[4]/div[2]"
              "/div/div/div/div[2]")


def trading_days(start, end):
    """Mon-Fri dates from start to end inclusive (GSE does not publish at weekends)."""
    n = (end - start).days + 1
    return [start + datetime.timedelta(days=i) for i in range(n)
            if (start + datetime.timedelta(days=i)).weekday() < 5]


def _new_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    opts = Options()
    for a in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
              "--disable-gpu", "--window-size=1920,1080"):
        opts.add_argument(a)
    opts.add_experimental_option("prefs", {
        "download.default_directory": DOWNLOAD_DIR,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
    })
    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd("Page.setDownloadBehavior",
                           {"behavior": "allow", "downloadPath": DOWNLOAD_DIR})
    return driver


def _download_day(driver, day):
    """Download one trading day. Returns the path of the CSV."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    day_str = day.strftime("%d/%m/%Y")
    log(f"── SCRAPE: Fetching {day_str}")
    driver.get(URL)  # fresh page state for every day
    wait = WebDriverWait(driver, 30)

    from_in = wait.until(EC.presence_of_element_located(
        (By.XPATH, BASE_XPATH + "/div[1]/div/div[1]/div/span/input[1]")))
    from_in.clear()
    from_in.send_keys(day_str)
    to_in = wait.until(EC.presence_of_element_located(
        (By.XPATH, BASE_XPATH + "/div[1]/div/div[1]/div/span/input[2]")))
    to_in.clear()
    to_in.send_keys(day_str)
    to_in.send_keys(Keys.RETURN)
    time.sleep(10)

    # Pick the page size by its visible text (case-insensitive), falling back to
    # the list position that worked in the original script for "All".
    try:
        wait.until(EC.element_to_be_clickable(
            (By.XPATH, BASE_XPATH + "/div[2]/div[3]/label/div/button"))).click()
        time.sleep(2)
        items = BASE_XPATH + "/div[2]/div[3]/label/div/div/ul"
        by_text = (items + "/li/a[translate(normalize-space(.), "
                   "'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')="
                   f"'{PAGE_SIZE.upper()}']")
        candidates = [by_text]
        if PAGE_SIZE.upper() == "ALL":
            candidates.append(items + "/li[7]/a")
        last_err = None
        for xp in candidates:
            try:
                wait.until(EC.element_to_be_clickable((By.XPATH, xp))).click()
                last_err = None
                break
            except Exception as e:
                last_err = e
        if last_err:
            raise last_err
        time.sleep(8)
        log(f"── SCRAPE: Selected '{PAGE_SIZE}' entries per page")
    except Exception as e:
        # Fail loudly: a truncated download would never be backfilled.
        raise RuntimeError(f"Could not select '{PAGE_SIZE}' entries: {e}") from e

    before = set(os.listdir(DOWNLOAD_DIR))
    wait.until(EC.element_to_be_clickable(
        (By.XPATH, BASE_XPATH + "/div[2]/div[1]/button[3]"))).click()
    log("── SCRAPE: Download initiated...")
    path = _wait_for_download(before, timeout=60)
    final = os.path.join(DOWNLOAD_DIR, f"{day:%Y-%m-%d}.csv")
    os.replace(path, final)
    return final


def _wait_for_download(before, timeout=60, poll=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        files = set(os.listdir(DOWNLOAD_DIR))
        if not any(f.endswith(".crdownload") for f in files):
            new = [f for f in files - before if f.endswith(".csv")]
            if new:
                path = os.path.join(DOWNLOAD_DIR, new[0])
                log(f"── SCRAPE: Download confirmed ({os.path.getsize(path)} bytes)")
                return path
        time.sleep(poll)
    raise TimeoutError("No completed CSV download detected")


def scrape(days):
    """Download each day in order. Returns (results, error).

    Stops at the first failing day so we never save later days while leaving a
    gap behind them (the next run would start after the gap and skip it).
    """
    log(f"── SCRAPE: Starting browser for {len(days)} trading day(s)...")
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    driver = _new_driver()
    results, error = [], None
    try:
        for day in days:
            results.append((day, _download_day(driver, day)))
    except Exception as e:
        error = e
        log(f"── SCRAPE ERROR: {e}", "error")
        try:
            driver.save_screenshot(os.path.join(BASE_DIR, "error_screenshot.png"))
        except Exception:
            pass
    finally:
        driver.quit()
        log("── SCRAPE: Browser closed")
    return results, error


# ───────────────────────── main ─────────────────────────
def run(since=None, clean_only=False):
    existing = load_existing()
    log(f"── LOAD: {len(existing)} existing rows"
        + (f", last date {existing[DATE_COL].max():%Y-%m-%d}" if len(existing) else ""))

    if clean_only:
        save(existing)
        log(f"── CLEAN-ONLY: Data.csv rewritten with {len(existing)} rows")
        return

    today = datetime.datetime.now(TZ).date()
    if since:
        start = since
    elif len(existing):
        start = existing[DATE_COL].max().date() + datetime.timedelta(days=1)
    else:
        start = today

    days = trading_days(start, today) if start <= today else []
    if not days:
        log("── Nothing to fetch (up to date, or only weekend days in range).")
        save(existing)
        return

    results, error = scrape(days)

    frames = []
    for day, path in results:
        df = clean_download(path)
        df = df[df[DATE_COL] == pd.Timestamp(day)]
        if MAX_ROWS and len(df) >= MAX_ROWS:
            error = RuntimeError(f"{day}: {len(df)} rows >= {MAX_ROWS}; page size "
                                 f"may be truncating. Stopping before this day.")
            log(f"── {error}", "error")
            break
        if df.empty:
            log(f"── {day}: no data published (holiday, or not uploaded yet).")
            continue
        frames.append(df)

    new = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=FINAL_COLS)
    if new.empty:
        log("── No new trading data. Will retry on the next run.")
        save(existing)
    else:
        combined = finalize(pd.concat([existing, new], ignore_index=True))
        save(combined)
        got = sorted(new[DATE_COL].dt.strftime("%Y-%m-%d").unique())
        log(f"── APPEND: {len(combined) - len(existing)} new rows for "
            f"{len(got)} day(s): {', '.join(got)}")

    if error:
        raise error  # run turns red, but days gathered before the failure are saved


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="Backfill from this date (YYYY-MM-DD)")
    ap.add_argument("--clean-only", action="store_true", help="Clean Data.csv, no scraping")
    args = ap.parse_args()
    since = datetime.date.fromisoformat(args.since) if args.since else None

    log("══════════════════════════════════════")
    log(f"GSE Scraper started at {datetime.datetime.now(TZ)}")
    log("══════════════════════════════════════")
    try:
        run(since=since, clean_only=args.clean_only)
        log("✓ Finished")
    except Exception as e:
        log(f"✗ Fatal error: {e}", "error")
        raise
