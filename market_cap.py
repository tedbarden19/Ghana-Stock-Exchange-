"""Standalone daily scraper: per-company market cap from the 'Market Capitalisation'
bar chart on https://gse.com.gh/trading-and-data/

Writes / appends, with columns  Date | Share Code | Market Cap (GH¢ m):
    IndexPerCompany.csv   the source of truth (main.py rebuilds the workbook from it)
    GSE_Data.xlsx         sheet "Index per company" is refreshed from the CSV
                          (the "Data" and "Gse index" sheets are left untouched)

How it works
------------
* Opens the page in headless Chrome and reads the numbers straight out of the
  Chart.js chart (the bars are drawn in the browser, so they are not in the HTML).
* The date for the rows comes from the page itself: the latest date in the
  "Daily Market Summary" table. If that cannot be read, today's date (Accra) is
  used and a warning is logged.
* One row per company per date. Running again for the same date overwrites that
  date's rows, so re-runs, weekends and holidays never create duplicates.

Usage
-----
    python market_cap.py             # scrape and update the CSV + workbook sheet
    python market_cap.py --dry-run   # scrape and print only, write nothing
"""
import argparse
import datetime
import logging
import os
import time
from zoneinfo import ZoneInfo

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(BASE_DIR, "IndexPerCompany.csv")
XLSX_FILE = os.path.join(BASE_DIR, "GSE_Data.xlsx")
LOG_FILE = os.path.join(BASE_DIR, "market_cap.log")
SHEET = "Index per company"
URL = "https://gse.com.gh/trading-and-data/"
TZ = ZoneInfo("Africa/Accra")

DATE_COL = "Date"
CODE_COL = "Share Code"
CAP_COL = "Market Cap (GH¢ m)"
COLS = [DATE_COL, CODE_COL, CAP_COL]

# Runs in the page: returns every Chart.js chart (labels + datasets).
READ_CHARTS_JS = """
const C = window.Chart;
const out = [];
document.querySelectorAll('canvas').forEach((cv, i) => {
  let ch = null;
  if (C) {
    if (typeof C.getChart === 'function') ch = C.getChart(cv);
    if (!ch && C.instances) {
      ch = Object.values(C.instances).find(x => x && x.canvas === cv);
    }
  }
  if (ch && ch.data) {
    out.push({
      index: i,
      labels: (ch.data.labels || []).map(String),
      datasets: (ch.data.datasets || []).map(
        d => ({label: String(d.label || ''), data: d.data})
      )
    });
  }
});
return out;
"""

# Runs in the page: latest date (+ its market cap) in the "Daily Market Summary" table.
READ_LATEST_ROW_JS = """
const tx = el => ((el && (el.innerText ?? el.textContent)) || '').trim();
const parseD = s => {
  const m = /^(\\d{1,2})\\/(\\d{1,2})\\/(\\d{4})$/.exec((s || '').trim());
  return m ? new Date(+m[3], +m[2] - 1, +m[1]) : null;
};
for (const t of document.querySelectorAll('table')) {
  const heads = [...t.querySelectorAll('thead th, thead td')].map(tx);
  const di = heads.findIndex(h => h.toLowerCase() === 'date');
  const ci = heads.findIndex(h => /market cap/i.test(h));
  if (di < 0 || ci < 0) continue;
  let best = null;
  t.querySelectorAll('tbody tr').forEach(r => {
    const td = r.querySelectorAll('td');
    const d = parseD(tx(td[di]));
    if (d && (!best || d > best.d)) best = {d: d, date: tx(td[di]), cap: tx(td[ci])};
  });
  if (best) return {date: best.date, cap: best.cap};
}
return null;
"""


def log(msg, level="info"):
    print(msg)
    getattr(logging, level)(msg)


# ───────────────────────── parsing (no browser needed) ──────────────────────
def parse_charts(charts):
    """Pick the market-cap chart out of everything found on the page."""
    for ch in charts:
        for ds in ch.get("datasets", []):
            if "cap" in ds.get("label", "").lower():
                df = pd.DataFrame({CODE_COL: ch["labels"], CAP_COL: ds["data"]})
                df[CODE_COL] = (
                    df[CODE_COL].astype(str).str.replace(r"\s+", "", regex=True)
                    .str.replace("*", "", regex=False).str.upper()  # 'SCB PREF' -> 'SCBPREF'
                )
                df[CAP_COL] = pd.to_numeric(
                    df[CAP_COL].astype(str).str.replace(",", "", regex=False),
                    errors="coerce",
                )
                df = df.dropna(subset=[CAP_COL])
                if df.empty:
                    raise RuntimeError("Market-cap chart found but it has no values")
                return df.drop_duplicates(CODE_COL, keep="last").reset_index(drop=True)
    seen = [
        f"canvas#{c['index']}: " + ", ".join(d["label"] for d in c["datasets"])
        for c in charts
    ]
    raise RuntimeError(
        "No chart with a 'cap' dataset found. Charts on page: " + (" | ".join(seen) or "none")
    )


def parse_latest_row(row):
    """{'date': '02/10/2026', 'cap': '73,410.00'} -> (date, cap) or (None, None)."""
    if not row:
        return None, None
    d = pd.to_datetime(row.get("date"), format="%d/%m/%Y", errors="coerce")
    cap = pd.to_numeric(str(row.get("cap", "")).replace(",", ""), errors="coerce")
    return (d.date() if pd.notna(d) else None), (float(cap) if pd.notna(cap) else None)


# ───────────────────────── browser ──────────────────────────────────────────
def fetch_company_caps(wait_seconds=45):
    """Returns (DataFrame[Share Code, Market Cap], as_of_date or None, table_total or None)."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    opts = Options()
    for a in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
              "--disable-gpu", "--window-size=1920,1080"):
        opts.add_argument(a)
    driver = webdriver.Chrome(options=opts)
    try:
        log("── CAP: Opening trading-and-data page...")
        driver.get(URL)
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "canvas"))
        )

        df, last_err = None, None
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            try:
                df = parse_charts(driver.execute_script(READ_CHARTS_JS))
                break
            except RuntimeError as e:
                last_err = e
                time.sleep(3)
        if df is None:
            raise last_err
        log(f"── CAP: read {len(df)} companies from the chart")

        as_of, table_cap = None, None
        end = time.time() + 20
        while time.time() < end and as_of is None:
            as_of, table_cap = parse_latest_row(driver.execute_script(READ_LATEST_ROW_JS))
            if as_of is None:
                time.sleep(2)
        return df, as_of, table_cap
    except Exception:
        try:
            driver.save_screenshot(os.path.join(BASE_DIR, "cap_error_screenshot.png"))
        except Exception:
            pass
        raise
    finally:
        driver.quit()


# ───────────────────────── storage (csv + workbook sheet) ───────────────────
def load_existing(path=None):
    path = path or CSV_FILE
    if not os.path.exists(path):
        return pd.DataFrame(columns=COLS)
    df = pd.read_csv(path, encoding="utf-8")
    df = df[[c for c in COLS if c in df.columns]]
    df[DATE_COL] = pd.to_datetime(df[DATE_COL], errors="coerce")
    df[CAP_COL] = pd.to_numeric(df[CAP_COL], errors="coerce")
    return df.dropna(subset=[DATE_COL])


def merge(existing, fresh, as_of):
    fresh = fresh.copy()
    fresh[DATE_COL] = pd.Timestamp(as_of)
    parts = [d for d in (existing, fresh[COLS]) if len(d)]
    out = pd.concat(parts, ignore_index=True)
    out[DATE_COL] = pd.to_datetime(out[DATE_COL])
    out = out.drop_duplicates([DATE_COL, CODE_COL], keep="last")  # same date -> overwrite
    return out.sort_values([DATE_COL, CODE_COL],
                           kind="mergesort").reset_index(drop=True)


def save_csv(df, path=None):
    path = path or CSV_FILE
    out = df.copy()
    out[DATE_COL] = pd.to_datetime(out[DATE_COL]).dt.strftime("%Y-%m-%d")
    tmp = path + ".tmp"
    out.to_csv(tmp, index=False, encoding="utf-8")
    os.replace(tmp, path)


def update_workbook(df, path=None):
    """Replace ONLY the 'Index per company' sheet in GSE_Data.xlsx (created if missing)."""
    from openpyxl import load_workbook

    path = path or XLSX_FILE
    if not os.path.exists(path):
        log(f"── CAP: {os.path.basename(path)} not found; CSV saved, the sheet will be "
            "added the next time main.py builds the workbook", "warning")
        return False

    wb = load_workbook(path)
    pos = wb.sheetnames.index(SHEET) if SHEET in wb.sheetnames else len(wb.sheetnames)
    if SHEET in wb.sheetnames:
        del wb[SHEET]
    ws = wb.create_sheet(SHEET, pos)
    ws.append(COLS)
    for d, code, cap in zip(pd.to_datetime(df[DATE_COL]).dt.date, df[CODE_COL], df[CAP_COL]):
        ws.append([d, code, float(cap)])
    for cell in ws["A"][1:]:
        cell.number_format = "YYYY-MM-DD"

    tmp = path + ".tmp.xlsx"
    wb.save(tmp)
    os.replace(tmp, path)
    return True


# ───────────────────────── main ─────────────────────────────────────────────
def run(dry_run=False):
    fresh, as_of, table_cap = fetch_company_caps()

    if as_of is None:
        as_of = datetime.datetime.now(TZ).date()
        log(f"── CAP: could not read the date from the page; using today ({as_of})", "warning")
    else:
        log(f"── CAP: page's latest trading date is {as_of}")

    total = float(fresh[CAP_COL].sum())
    if table_cap:
        diff = (total - table_cap) / table_cap * 100
        log(f"── CAP: chart total {total:,.0f} vs market summary {table_cap:,.0f} ({diff:+.1f}%)",
            "info" if abs(diff) < 5 else "warning")

    if dry_run:
        print(fresh.assign(**{DATE_COL: as_of})[COLS].to_string(index=False))
        print(f"\n{len(fresh)} companies, total {total:,.1f} (GH¢ million). Nothing written (dry run).")
        return

    combined = merge(load_existing(), fresh, as_of)
    save_csv(combined)
    log(f"── CAP: {os.path.basename(CSV_FILE)} now {len(combined)} rows, "
        f"{combined[DATE_COL].nunique()} date(s), latest {combined[DATE_COL].max().date()}")
    if update_workbook(combined):
        log(f"── CAP: sheet '{SHEET}' updated in {os.path.basename(XLSX_FILE)}")


if __name__ == "__main__":
    logging.basicConfig(
        filename=LOG_FILE, level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s", encoding="utf-8",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the table, write nothing")
    args = ap.parse_args()
    log(f"Market cap scrape started at {datetime.datetime.now(TZ)}")
    try:
        run(dry_run=args.dry_run)
        log("✓ Finished")
    except Exception as e:
        log(f"✗ Fatal error: {e}", "error")
        raise
