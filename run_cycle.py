"""Entry point for one cycle (GitHub Actions or any scheduler).

MODES (env BOT_MODE, set as a repository variable):
  unset / dry : read-only dry-run (default)
  live        : really saves + converts (needs LIVE_CONFIRM and ALLOW_BATCH too)
  off         : do nothing
Live also works for ONE order: DRY_RUN=0 + ONLY_KEY=<6+ chars> (live-one workflow).

SAFETY
  * Logs are PUBLIC on a public repo: only masked phones, state names, field
    NAMES and reason codes. Never names, addresses or full phones.
  * Login/CAPTCHA/OTP -> exit code 3 (safe stop, never bypass).
  * Per-cycle caps: time budget + max conversions.
"""
import json
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

from store import Store
from pipeline import process_cycle
from easyorders_pw import EasyOrdersDriver, RealOrders, ChallengeDetected
import notify

MODE = os.environ.get("BOT_MODE", "").strip().lower()
CONFIRMED = os.environ.get("LIVE_CONFIRM") == "I-ACCEPT-LIVE-CONVERSIONS"
LIVE = CONFIRMED and (os.environ.get("DRY_RUN", "1") == "0" or MODE == "live")
MAX_ORDERS = int(os.environ.get("MAX_ORDERS", "15" if not MODE else "300"))
ONLY_KEY = os.environ.get("ONLY_KEY", "").strip()      # live: ONE order (key prefix) or "auto"
AUTO = ONLY_KEY.lower() == "auto"                      # pick the first SAFE order by itself
if AUTO:
    ONLY_KEY = ""
ALLOW_BATCH = os.environ.get("ALLOW_BATCH") == "yes-process-all"
STAGE = os.environ.get("STAGE", "save")                # save = stop after verified save
BUDGET = int(os.environ.get("BUDGET_SECONDS", "270"))
MAX_ACTIONS = int(os.environ.get("MAX_ACTIONS", "5"))
STATE_DIR = os.environ.get("STATE_DIR", "").strip()
SHIP_MODE = os.environ.get("SHIPPING_MODE", "price").strip().lower()
FLAT = int(os.environ.get("PLATFORM_FLAT_SHIP", "0") or 0)   # fee EasyOrders adds itself on missed->order
if SHIP_MODE not in ("platform", "price"):
    SHIP_MODE = "price"


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


def safe_report(store, since_ts, log=print):
    rows = store.db.execute(
        "SELECT uuid,state,phone_masked,after_json,last_error,updated_at FROM orders "
        "WHERE updated_at>=?", (since_ts,)).fetchall()
    for key, state, pm, after, err, _ in rows:
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
    total = store.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    log(f"touched={len(rows)} known_total={total} live={LIVE} ship={SHIP_MODE} flat={FLAT}")


def _challenge_notice():
    """Tell the owner at most once per 3 hours that the session needs refreshing."""
    flag = os.path.join(STATE_DIR or ".", "challenge.flag")
    try:
        if os.path.exists(flag) and time.time() - os.path.getmtime(flag) < 3 * 3600:
            return
        os.makedirs(os.path.dirname(flag) or ".", exist_ok=True)
        open(flag, "w").write("1")
    except OSError:
        pass
    notify.send("\u26a0\ufe0f \u062c\u0644\u0633\u0629 EasyOrders \u0627\u0646\u062a\u0647\u062a: "
                "\u0627\u0644\u0623\u062f\u0627\u0629 \u0648\u0642\u0641\u062a \u0644\u062d\u062f "
                "\u0645\u0627 \u062a\u062c\u062f\u062f \u0627\u0644\u0640 Secret (EASYORDERS_STATE).")


def main():
    if MODE == "off":
        print("BOT_MODE=off: nothing to do"); return 0
    if LIVE and len(ONLY_KEY) < 6 and not ALLOW_BATCH and not AUTO:
        print("live mode needs ONLY_KEY (>=6 chars) or ALLOW_BATCH: refusing")
        return 2
    raw = os.environ.get("EASYORDERS_STATE")
    if not raw:
        print("missing EASYORDERS_STATE secret"); return 2

    # state is persisted ONLY in live batch mode (dry-run leaves no trace)
    persist = LIVE and ALLOW_BATCH and not ONLY_KEY and not AUTO and STATE_DIR
    if persist:
        os.makedirs(STATE_DIR, exist_ok=True)
        store = Store(os.path.join(STATE_DIR, "state.db"))
        print(f"[cycle] reopened stale rows: {store.reopen_stale()}")
    else:
        store = Store(":memory:")
    started = time.time()
    deadline = time.monotonic() + BUDGET if (LIVE and not ONLY_KEY) else None
    if AUTO:
        print("[cycle] AUTO test: first valid order only (unknown province = Fayoum fallback)")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(storage_state=json.loads(raw))
        page = ctx.new_page()
        page.set_default_timeout(45000)
        try:
            driver = Limited(EasyOrdersDriver(page), 40 if AUTO else MAX_ORDERS, ONLY_KEY if LIVE else "")
            process_cycle(store, driver, RealOrders(page), taager=None,
                          dry_run=not LIVE, owner="gha",
                          stop_after_save=(STAGE != "convert"),
                          deadline=deadline,
                          max_actions=1 if AUTO else (MAX_ACTIONS if (LIVE and not ONLY_KEY) else None),
                          require_safe=AUTO,
                          ship_mode=SHIP_MODE, platform_flat=FLAT if SHIP_MODE == "price" else 0,
                          log=lambda m: print("[cycle]", m))
        except ChallengeDetected as e:
            print(f"STOP: login/challenge page detected ({e}). "
                  "Refresh the session secret manually.")
            _challenge_notice()
            return 3
        finally:
            browser.close()
    safe_report(store, started - 1)
    if LIVE:
        msg = notify.summarize(store, started - 1)
        if msg:
            notify.send(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
