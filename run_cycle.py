"""Entry point for one cycle (GitHub Actions or any scheduler).

SAFETY
  * Dry-run by default. Live mode needs BOTH DRY_RUN=0 and
    LIVE_CONFIRM=I-ACCEPT-LIVE-CONVERSIONS. The workflow in this repo
    hard-codes DRY_RUN=1.
  * Logs are PUBLIC on a public repo: only masked phones, state names and
    field NAMES are printed. Never names, addresses or field values.
  * Login/CAPTCHA/OTP -> exit code 3 (safe stop, never bypass).
"""
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright

from store import Store
from pipeline import process_cycle
from easyorders_pw import EasyOrdersDriver, RealOrders, ChallengeDetected

LIVE = (os.environ.get("DRY_RUN", "1") == "0"
        and os.environ.get("LIVE_CONFIRM") == "I-ACCEPT-LIVE-CONVERSIONS")
MAX_ORDERS = int(os.environ.get("MAX_ORDERS", "15"))   # newest first
ONLY_KEY = os.environ.get("ONLY_KEY", "").strip()      # live mode: ONE order (key prefix)
STAGE = os.environ.get("STAGE", "save")                # save = stop after verified save


class Limited:
    """Wrap a driver so a cycle only looks at the newest N rows."""

    def __init__(self, inner, n, only_key=""):
        self.inner, self.n, self.only = inner, n, only_key

    def list_missed(self):
        rows = self.inner.list_missed()
        if self.only:
            rows = [r for r in rows if r.key.startswith(self.only)]
        return rows[: self.n]

    def __getattr__(self, name):
        return getattr(self.inner, name)


def safe_report(store, log=print):
    rows = store.db.execute(
        "SELECT uuid,state,phone_masked,after_json,last_error FROM orders"
    ).fetchall()
    for key, state, pm, after, err in rows:
        plan = json.loads(after) if after else {}
        p = plan.get("plan", {})
        ev = store.db.execute(
            "SELECT note FROM events WHERE uuid=? ORDER BY id DESC LIMIT 1",
            (key,)).fetchone()
        note = (ev[0] if ev else "") or ""
        log(f"{key[:6]} | {pm} | {state} | fields={sorted(p)} | "
            f"gov={plan.get('gov_source')} addr={plan.get('addr_source')} "
            f"price={plan.get('price')}"
            + (f" | why={note}" if state in ("needs_review", "excluded", "failed") else "")
            + (f" | err={re.sub(r'[0-9]', '#', err)}" if err else ""))
    log(f"total={len(rows)} live={LIVE}")


def main():
    if LIVE and len(ONLY_KEY) < 6:
        print("live mode needs ONLY_KEY (>=6 chars): refusing to touch all orders")
        return 2
    raw = os.environ.get("EASYORDERS_STATE")
    if not raw:
        print("missing EASYORDERS_STATE secret"); return 2
    store = Store(":memory:")          # dry-run: nothing persisted, nothing leaked
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(storage_state=json.loads(raw))
        page = ctx.new_page()
        page.set_default_timeout(20000)
        try:
            driver = Limited(EasyOrdersDriver(page), MAX_ORDERS, ONLY_KEY if LIVE else "")
            process_cycle(store, driver, RealOrders(page), taager=None,
                          dry_run=not LIVE, owner="gha",
                          stop_after_save=(STAGE != "convert"),
                          log=lambda m: print("[cycle]", m))
        except ChallengeDetected as e:
            print(f"STOP: login/challenge page detected ({e}). "
                  "Refresh the session secret manually.")
            return 3
        finally:
            browser.close()
    safe_report(store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
