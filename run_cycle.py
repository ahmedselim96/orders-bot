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
import sys

from playwright.sync_api import sync_playwright

from store import Store
from pipeline import process_cycle
from easyorders_pw import EasyOrdersDriver, RealOrders, ChallengeDetected

LIVE = (os.environ.get("DRY_RUN", "1") == "0"
        and os.environ.get("LIVE_CONFIRM") == "I-ACCEPT-LIVE-CONVERSIONS")
MAX_ORDERS = int(os.environ.get("MAX_ORDERS", "15"))   # newest first


class Limited:
    """Wrap a driver so a cycle only looks at the newest N rows."""

    def __init__(self, inner, n):
        self.inner, self.n = inner, n

    def list_missed(self):
        return self.inner.list_missed()[: self.n]

    def __getattr__(self, name):
        return getattr(self.inner, name)


def safe_report(store, log=print):
    rows = store.db.execute(
        "SELECT uuid,state,phone_masked,after_json,last_error FROM orders"
    ).fetchall()
    for key, state, pm, after, err in rows:
        plan = json.loads(after) if after else {}
        p = plan.get("plan", {})
        log(f"{key[:6]} | {pm} | {state} | fields={sorted(p)} | "
            f"gov={plan.get('gov_source')} addr={plan.get('addr_source')}"
            + (f" | err={err}" if err else ""))
    log(f"total={len(rows)} live={LIVE}")


def main():
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
            driver = Limited(EasyOrdersDriver(page), MAX_ORDERS)
            process_cycle(store, driver, RealOrders(page), taager=None,
                          dry_run=not LIVE, owner="gha",
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
