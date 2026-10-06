from __future__ import annotations

import argparse
import datetime
import logging
import os
import re
import shutil
import time
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
MAIN_DATA_FILE = os.path.join(BASE_DIR, "Data.csv")
INDEX_FILE = os.path.join(BASE_DIR, "Index.csv")
XLSX_FILE = os.path.join(BASE_DIR, "GSE_Data.xlsx")
LOG_FILE = os.path.join(BASE_DIR, "scraper.log")
TZ = ZoneInfo("Africa/Accra")

# ── Shares table ──────────────────────────────────────────────────────────────
DATE_COL = "Daily Date"
CODE_COL = "Share Code"

FINAL_COLS = [
    DATE_COL, CODE_COL,
    "Year High", "Year Low", "Previous Closing Price - VWAP",
    "Opening Price", "Last Transaction Price", "Closing Price - VWAP",
    "Price Change", "Closing Bid Price", "Closing Offer Price",
    "Total Shares Traded", "Total Value Traded",
]
NUMERIC_COLS = FINAL_COLS[2:]
FILL_ZERO_COLS = [
    "Closing Bid Price", "Closing Offer Price",
    "Total Shares Traded", "Total Value Traded",
]

# ── Index table ───────────────────────────────────────────────────────────────
INDEX_COLS = ["Date", "Volume", "GSE-CI", "Market Cap (GH¢ m)", "GSE-FSI"]
INDEX_PAGE_URL = "https://gse.com.gh/trading-and-data/"
INDEX_AJAX_URL = "https://gse.com.gh/wp-admin/admin-ajax.php"
INDEX_HEADER_MARKER = "Financial Stock Index"
INDEX_PAGE_SIZE = 100
INDEX_DELAY = 1.0

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8",
)


def log(msg, level="info"):
    print(msg)
    getattr(logging, level)(msg)


# ───────────────────────── shared date helpers ───────────────────────────────
def parse_mixed_dates(s):
    """Parse a column holding a mix of YYYY-MM-DD and M/D/YYYY text."""
    s = s.astype(str).str.strip()
    is_iso = s.str.match(r"^\d{4}-\d{2}-\d{2}")
    iso = pd.to_datetime(s.where(is_iso).str[:10], format="%Y-%m-%d", errors="coerce")
    us = pd.to_datetime(s.where(~is_iso), format="%m/%d/%Y", errors="coerce")
    return iso.fillna(us)


def format_dates_mdy(s):
    """Datetime -> M/D/YYYY text (no zero padding)."""
    return (
        s.dt.month.astype(str) + "/"
        + s.dt.day.astype(str) + "/"
        + s.dt.year.astype(str)
    )


def trading_days(start, end):
    """Mon–Fri dates from start to end inclusive."""
    n = (end - start).days + 1
    return [
        start + datetime.timedelta(days=i)
        for i in range(n)
        if (start + datetime.timedelta(days=i)).weekday() < 5
    ]


# ───────────────────────── shares cleaning ───────────────────────────────────
def normalize_header(name):
    """'Year Low (GH¢)' / 'Year Low (GHÂ¢)' -> 'Year Low'."""
    name = str(name).replace("\ufeff", "").strip()
    name = re.sub(r"\s*\(\s*GH[^)]*\)", "", name)
    return re.sub(r"\s+", " ", name).strip()


def standardize(df):
    df = df.loc[:, ~df.columns.astype(str).str.match(r"^Unnamed")].copy()
    df.columns = [normalize_header(c) for c in df.columns]

    missing = [c for c in FINAL_COLS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Expected columns missing from data: {missing}. "
            f"Found: {list(df.columns)}"
        )
    df = df[FINAL_COLS]

    df[CODE_COL] = (
        df[CODE_COL].astype(str).str.replace("*", "", regex=False).str.strip()
    )
    df = df[df[CODE_COL].ne("") & df[CODE_COL].str.lower().ne("nan")]

    for col in NUMERIC_COLS:
        if df[col].dtype == object or str(df[col].dtype).startswith("str"):
            df[col] = df[col].astype(str).str.replace(",", "", regex=False).str.strip()
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df[FILL_ZERO_COLS] = df[FILL_ZERO_COLS].fillna(0)
    return df


def finalize(df):
    df = df.copy()
    df[DATE_COL] = pd.to_datetime(df[DATE_COL], errors="coerce")
    df = df.dropna(subset=[DATE_COL])
    df = df.drop_duplicates(subset=[DATE_COL, CODE_COL], keep="last")
    return df.sort_values(DATE_COL, kind="mergesort").reset_index(drop=True)


def load_existing_shares():
    if not os.path.exists(MAIN_DATA_FILE):
        return pd.DataFrame(columns=FINAL_COLS)
    raw = pd.read_csv(MAIN_DATA_FILE, encoding="utf-8-sig", dtype=str)
    raw.columns = [normalize_header(c) for c in raw.columns]
    raw[DATE_COL] = parse_mixed_dates(raw[DATE_COL])
    bad = raw[DATE_COL].isna().sum()
    if bad:
        log(f"── LOAD: dropping {bad} rows in Data.csv with unreadable dates", "warning")
    return finalize(standardize(raw))


def save_shares(df):
    out = df.copy()
    out["Total Shares Traded"] = out["Total Shares Traded"].round().astype("Int64")
    out[DATE_COL] = format_dates_mdy(out[DATE_COL])
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
    date_src = next(
        (c for c in raw.columns if normalize_header(c) == DATE_COL), None
    )
    if date_src is None:
        raise ValueError(
            f"No '{DATE_COL}' column in download. Found: {list(raw.columns)}"
        )
    raw[date_src] = pd.to_datetime(raw[date_src], dayfirst=True, errors="coerce")
    raw = raw.dropna(subset=[date_src])
    df = standardize(raw)
    log(f"── CLEAN: {len(df)} rows cleaned")
    return df


# ───────────────────────── shares scraping (Selenium) ────────────────────────
PAGE_SIZE = "All"
MAX_ROWS = int(PAGE_SIZE) if PAGE_SIZE.isdigit() else None
URL = "https://gse.com.gh/trading-and-data/"
BASE_XPATH = (
    "/html/body/div[1]/div/div[3]/div[1]/div/div/div/div[4]/div[2]"
    "/div/div/div/div[2]"
)


def _new_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    for a in (
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--window-size=1920,1080",
    ):
        opts.add_argument(a)
    opts.add_experimental_option(
        "prefs",
        {
            "download.default_directory": DOWNLOAD_DIR,
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
        },
    )
    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd(
        "Page.setDownloadBehavior",
        {"behavior": "allow", "downloadPath": DOWNLOAD_DIR},
    )
    return driver


def _download_day(driver, day):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    day_str = day.strftime("%d/%m/%Y")
    log(f"── SCRAPE: Fetching shares for {day_str}")
    driver.get(URL)
    wait = WebDriverWait(driver, 30)

    from_in = wait.until(
        EC.presence_of_element_located(
            (By.XPATH, BASE_XPATH + "/div[1]/div/div[1]/div/span/input[1]")
        )
    )
    from_in.clear()
    from_in.send_keys(day_str)
    to_in = wait.until(
        EC.presence_of_element_located(
            (By.XPATH, BASE_XPATH + "/div[1]/div/div[1]/div/span/input[2]")
        )
    )
    to_in.clear()
    to_in.send_keys(day_str)
    to_in.send_keys(Keys.RETURN)
    time.sleep(10)

    try:
        wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, BASE_XPATH + "/div[2]/div[3]/label/div/button")
            )
        ).click()
        time.sleep(2)
        items = BASE_XPATH + "/div[2]/div[3]/label/div/div/ul"
        by_text = (
            items
            + "/li/a[translate(normalize-space(.), "
            "'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')="
            f"'{PAGE_SIZE.upper()}']"
        )
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
        raise RuntimeError(f"Could not select '{PAGE_SIZE}' entries: {e}") from e

    before = set(os.listdir(DOWNLOAD_DIR))
    wait.until(
        EC.element_to_be_clickable(
            (By.XPATH, BASE_XPATH + "/div[2]/div[1]/button[3]")
        )
    ).click()
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


def scrape_shares(days):
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


# ───────────────────────── index scraping (AJAX – no browser) ────────────────
def _index_session():
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": INDEX_PAGE_URL,
            "Origin": "https://gse.com.gh",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
    )
    return s


def _discover_index_table(session):
    resp = session.get(INDEX_PAGE_URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    for table in soup.find_all("table"):
        cols = [th.get_text(strip=True) for th in table.find_all("th")]
        if any(INDEX_HEADER_MARKER in c for c in cols):
            table_id = table.get("data-wpdatatable_id") or re.sub(
                r"\D", "", table.get("id", "")
            )
            nonce_tag = soup.find("input", id=re.compile(rf"wdtNonce.*{table_id}"))
            if nonce_tag is None:
                nonce_tag = soup.find("input", id=re.compile(r"^wdtNonce"))
            if not table_id or nonce_tag is None:
                raise RuntimeError("Found index table but not id/nonce")
            return table_id, nonce_tag["value"], cols
    raise RuntimeError("Index (Daily Market Summary) table not found on page")


def _fetch_index_page(session, table_id, nonce, n_cols, order_col, start, draw):
    payload = {
        "draw": draw,
        "start": start,
        "length": INDEX_PAGE_SIZE,
        "order[0][column]": order_col,
        "order[0][dir]": "desc",
        "search[value]": "",
        "search[regex]": "false",
        "wdtNonce": nonce,
    }
    for i in range(n_cols):
        payload[f"columns[{i}][data]"] = i
        payload[f"columns[{i}][name]"] = ""
        payload[f"columns[{i}][searchable]"] = "true"
        payload[f"columns[{i}][orderable]"] = "true"
        payload[f"columns[{i}][search][value]"] = ""
        payload[f"columns[{i}][search][regex]"] = "false"

    for attempt in range(1, 4):
        try:
            r = session.post(
                INDEX_AJAX_URL,
                params={"action": "get_wdtable", "table_id": table_id},
                data=payload,
                timeout=30,
            )
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            log(f"── INDEX: attempt {attempt} failed at start={start}: {e}", "warning")
            time.sleep(3 * attempt)
    raise RuntimeError(f"Index fetch gave up at start={start}")


def scrape_index():
    """
    Pull the full Daily Market Summary table via AJAX.
    Returns a DataFrame with columns INDEX_COLS and Date as datetime.
    """
    log("── INDEX: Discovering table …")
    session = _index_session()
    table_id, nonce, cols = _discover_index_table(session)
    order_col = next(i for i, c in enumerate(cols) if c.strip() == "Date")
    n_cols = len(cols)
    log(f"── INDEX: table_id={table_id}  columns={cols}")

    rows, start, draw = [], 0, 1
    while True:
        js = _fetch_index_page(
            session, table_id, nonce, n_cols, order_col, start, draw
        )
        batch = js.get("data", [])
        total = int(js.get("recordsFiltered", js.get("recordsTotal", 0)))
        rows.extend(batch)
        log(f"── INDEX: fetched {len(rows)} / {total}")
        start += len(batch)
        draw += 1
        if not batch or start >= total:
            break
        time.sleep(INDEX_DELAY)

    # Site order: [wdt_ID, Day, Date, Volume, GSE-CI, Market Cap, GSE-FSI]
    # We keep: Date, Volume, GSE-CI, Market Cap, GSE-FSI
    raw = pd.DataFrame(
        [r[-5:] for r in rows],  # last 5 = Date + 4 metrics
        columns=["Date", "Volume", "GSE-CI", "Market Cap (GH¢ m)", "GSE-FSI"],
    )
    raw["Date"] = pd.to_datetime(raw["Date"], format="%d/%m/%Y", errors="coerce")
    for c in ["Volume", "GSE-CI", "Market Cap (GH¢ m)", "GSE-FSI"]:
        raw[c] = pd.to_numeric(
            raw[c].astype(str).str.replace(",", "", regex=False), errors="coerce"
        )
    raw = raw.dropna(subset=["Date"]).drop_duplicates(subset=["Date"], keep="last")
    raw = raw.sort_values("Date").reset_index(drop=True)
    log(f"── INDEX: {len(raw)} clean rows "
        f"({raw['Date'].min().date()} → {raw['Date'].max().date()})")
    return raw[INDEX_COLS]


def load_existing_index():
    if not os.path.exists(INDEX_FILE):
        return pd.DataFrame(columns=INDEX_COLS)
    raw = pd.read_csv(INDEX_FILE, encoding="utf-8-sig", dtype=str)
    raw.columns = [c.strip() for c in raw.columns]
    # tolerate old column names
    rename = {}
    for c in raw.columns:
        cl = c.lower()
        if "volume" in cl:
            rename[c] = "Volume"
        elif "gse-ci" in cl or "composite" in cl:
            rename[c] = "GSE-CI"
        elif "cap" in cl:
            rename[c] = "Market Cap (GH¢ m)"
        elif "fsi" in cl or "financial" in cl:
            rename[c] = "GSE-FSI"
        elif cl == "date":
            rename[c] = "Date"
    raw = raw.rename(columns=rename)
    raw["Date"] = parse_mixed_dates(raw["Date"])
    for c in ["Volume", "GSE-CI", "Market Cap (GH¢ m)", "GSE-FSI"]:
        if c in raw.columns:
            raw[c] = pd.to_numeric(
                raw[c].astype(str).str.replace(",", "", regex=False), errors="coerce"
            )
    raw = raw.dropna(subset=["Date"]).drop_duplicates(subset=["Date"], keep="last")
    return raw[INDEX_COLS].sort_values("Date").reset_index(drop=True)


def save_index(df):
    out = df.copy()
    out["Date"] = format_dates_mdy(out["Date"])
    # Volume as integer when possible
    out["Volume"] = pd.to_numeric(out["Volume"], errors="coerce").round().astype("Int64")
    tmp = INDEX_FILE + ".tmp"
    out.to_csv(tmp, index=False, encoding="utf-8")
    os.replace(tmp, INDEX_FILE)


def update_index(since=None):
    """
    Fetch full index history from the site, merge with existing Index.csv,
    keep rows from `since` onward (default: keep everything the site has).
    """
    fresh = scrape_index()
    existing = load_existing_index()

    if since is not None:
        since_ts = pd.Timestamp(since)
        fresh = fresh[fresh["Date"] >= since_ts]
        existing = existing[existing["Date"] < since_ts]

    combined = (
        pd.concat([existing, fresh], ignore_index=True)
        .drop_duplicates(subset=["Date"], keep="last")
        .sort_values("Date")
        .reset_index(drop=True)
    )
    save_index(combined)
    log(
        f"── INDEX: saved {len(combined)} rows "
        f"({combined['Date'].min().date()} → {combined['Date'].max().date()})"
    )
    return combined


def export_xlsx():
    """
    Write GSE_Data.xlsx with two sheets matching the target workbook:
      - "Data"      : shares (from Data.csv)
      - "Gse index" : market summary (from Index.csv)
    Date columns are pure Excel *dates* (no time component).
    """
    from openpyxl.styles import numbers

    shares = load_existing_shares()
    index = load_existing_index()

    shares_out = shares.copy()
    if len(shares_out):
        # Convert to Python date objects so Excel stores date-only cells
        shares_out[DATE_COL] = (
            pd.to_datetime(shares_out[DATE_COL], errors="coerce").dt.date
        )

    index_out = index.copy()
    if len(index_out):
        index_out["Date"] = (
            pd.to_datetime(index_out["Date"], errors="coerce").dt.date
        )
        index_out["Volume"] = pd.to_numeric(index_out["Volume"], errors="coerce")

    tmp = XLSX_FILE + ".tmp.xlsx"
    with pd.ExcelWriter(tmp, engine="openpyxl") as writer:
        shares_out.to_excel(writer, sheet_name="Data", index=False)
        index_out.to_excel(writer, sheet_name="Gse index", index=False)

        # Explicit date format on the date columns (column A on both sheets)
        for sheet_name in ("Data", "Gse index"):
            ws = writer.sheets[sheet_name]
            for cell in ws["A"]:
                if cell.row == 1:
                    continue  # header
                cell.number_format = "YYYY-MM-DD"

    os.replace(tmp, XLSX_FILE)
    log(
        f"── XLSX: wrote {XLSX_FILE} "
        f"(Data: {len(shares_out)} rows, Gse index: {len(index_out)} rows)"
    )


# ───────────────────────── main ──────────────────────────────────────────────
def run(since=None, clean_only=False, index_only=False):
    # ── Index first (fast, no browser) ───────────────────────────────────────
    if not clean_only:
        try:
            update_index(since=since)
        except Exception as e:
            log(f"── INDEX ERROR: {e}", "error")
            if index_only:
                raise

    if index_only:
        export_xlsx()
        return

    # ── Shares ───────────────────────────────────────────────────────────────
    existing = load_existing_shares()
    log(
        f"── LOAD: {len(existing)} existing share rows"
        + (
            f", last date {existing[DATE_COL].max():%Y-%m-%d}"
            if len(existing)
            else ""
        )
    )

    if clean_only:
        save_shares(existing)
        if os.path.exists(INDEX_FILE):
            idx = load_existing_index()
            save_index(idx)
            log(f"── CLEAN-ONLY: Index.csv rewritten with {len(idx)} rows")
        log(f"── CLEAN-ONLY: Data.csv rewritten with {len(existing)} rows")
        export_xlsx()
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
        log("── Shares: nothing to fetch (up to date, or only weekend days).")
        save_shares(existing)
        export_xlsx()
        return

    results, error = scrape_shares(days)

    frames = []
    for day, path in results:
        df = clean_download(path)
        df = df[df[DATE_COL] == pd.Timestamp(day)]
        if MAX_ROWS and len(df) >= MAX_ROWS:
            error = RuntimeError(
                f"{day}: {len(df)} rows >= {MAX_ROWS}; page size may be truncating. "
                "Stopping before this day."
            )
            log(f"── {error}", "error")
            break
        if df.empty:
            log(f"── {day}: no share data published (holiday, or not uploaded yet).")
            continue
        frames.append(df)

    new = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(columns=FINAL_COLS)
    )
    if new.empty:
        log("── No new share data. Will retry on the next run.")
        save_shares(existing)
    else:
        combined = finalize(pd.concat([existing, new], ignore_index=True))
        save_shares(combined)
        got = sorted(new[DATE_COL].dt.strftime("%Y-%m-%d").unique())
        log(
            f"── APPEND: {len(combined) - len(existing)} new share rows for "
            f"{len(got)} day(s): {', '.join(got)}"
        )

    # Always refresh the combined workbook after shares + index are up to date
    export_xlsx()

    if error:
        raise error


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="Backfill from this date (YYYY-MM-DD)")
    ap.add_argument(
        "--clean-only", action="store_true", help="Clean CSVs, no scraping"
    )
    ap.add_argument(
        "--index-only",
        action="store_true",
        help="Only refresh Index.csv (no Selenium / shares)",
    )
    args = ap.parse_args()
    since = datetime.date.fromisoformat(args.since) if args.since else None

    log("══════════════════════════════════════")
    log(f"GSE Scraper started at {datetime.datetime.now(TZ)}")
    log("══════════════════════════════════════")
    try:
        run(
            since=since,
            clean_only=args.clean_only,
            index_only=args.index_only,
        )
        log("✓ Finished")
    except Exception as e:
        log(f"✗ Fatal error: {e}", "error")
        raise
