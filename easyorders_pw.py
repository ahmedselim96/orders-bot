"""Playwright driver for the EasyOrders dashboard.

STATUS: written from selectors observed read-only in the live dashboard
(MUI table; edit dialog inputs named full_name/email/phone/country/government/
address/note; buttons \u062a\u0639\u062f\u064a\u0644 / \u062d\u0641\u0638 / \u0625\u0644\u063a\u0627\u0621). It has NEVER run against the
site. First run must be dry-run only (the pipeline default).

Login is NOT handled here: pass a Playwright storage_state file that was
created from a signed-in session. On a login page / CAPTCHA / OTP it raises
ChallengeDetected and the cycle must stop (never bypass).
"""
import json
import re
from urllib.parse import quote

from pipeline import Row, Detail

BASE = "https://app.easy-orders.net/#"
MISSED = (BASE + "/missed-orders?filter=" +
          quote(json.dumps({"is_completed$eq": False}, separators=(",", ":")), safe="") +
          "&order=DESC&page={page}&perPage=100&sort=created_at")
FIELD_LABELS = {
    "\u0627\u0633\u0645 \u0627\u0644\u0639\u0645\u064a\u0644": "name", "\u0631\u0642\u0645 \u0647\u0627\u062a\u0641 \u0627\u0644\u0639\u0645\u064a\u0644": "phone", "\u0627\u0644\u062f\u0648\u0644\u0629": "country",
    "\u0627\u0644\u0645\u062d\u0627\u0641\u0638\u0629": "government", "\u0627\u0644\u0639\u0646\u0648\u0627\u0646 \u0628\u0627\u0644\u062a\u0641\u0635\u064a\u0644": "address", "\u0645\u0644\u0627\u062d\u0638\u0629": "note",
}


class ChallengeDetected(Exception):
    pass


class EasyOrdersDriver:
    def __init__(self, page):
        self.page = page

    # ---- helpers ----------------------------------------------------
    def _guard(self):
        url = self.page.url
        body = self.page.inner_text("body")[:3000].lower()
        if "login" in url or "captcha" in body or "otp" in url:
            raise ChallengeDetected(url.split("?")[0])

    # ---- reading ----------------------------------------------------
    def list_missed(self):
        rows, page_no = [], 1
        while True:
            self.page.goto(MISSED.format(page=page_no))
            self.page.wait_for_selector("tbody tr, [role=alert]", timeout=20000)
            self._guard()
            trs = self.page.query_selector_all("tbody tr")
            if not trs:
                break
            for i, tr in enumerate(trs):
                c = [td.inner_text().strip() for td in tr.query_selector_all("td")]
                if len(c) >= 6 and "\u062a\u062d\u062a \u0627\u0644\u0645\u0631\u0627\u062c\u0639\u0629" in c[1]:
                    rows.append(Row(name=c[2], phone_raw=c[3], reason=c[4],
                                    date=c[5], index=i))
            if len(trs) < 100:
                break
            page_no += 1
        return rows

    def open_detail(self, row: Row) -> Detail:
        # list rows have no href: click the row and read the uuid from the route
        self.page.goto(MISSED.format(page=1))
        self.page.wait_for_selector("tbody tr")
        for tr in self.page.query_selector_all("tbody tr"):
            t = tr.inner_text()
            if row.date in t and row.name in t:
                tr.click()
                break
        else:
            raise LookupError("row not found on first page")
        self.page.wait_for_url(re.compile(r"#/missed-orders/[0-9a-f-]{36}"))
        return self._read_detail()

    def _read_detail(self) -> Detail:
        uuid = re.search(r"([0-9a-f]{8}-[0-9a-f-]{27})", self.page.url).group(1)
        self.page.wait_for_selector("text=\u062a\u0641\u0627\u0635\u064a\u0644 \u0627\u0644\u0637\u0644\u0628")
        vals = self.page.evaluate("""(labels) => {
            const out = {};
            const leaves = [...document.querySelectorAll('main *')]
              .filter(e => e.children.length === 0);
            for (const l of labels) {
              const el = leaves.find(e => e.textContent.trim() === l);
              const nxt = el && (el.nextElementSibling ||
                                 el.parentElement.nextElementSibling);
              out[l] = nxt ? nxt.textContent.trim() : '';
            }
            return out;
        }""", list(FIELD_LABELS))
        clean = lambda v: "" if v.strip() == "-" else v.strip()
        d = Detail(uuid=uuid)
        for label, attr in FIELD_LABELS.items():
            setattr(d, attr, clean(vals.get(label, "")))
        # TODO(verify in dry-run): product rows under the "\u0627\u0644\u0645\u0646\u062a\u062c\u0627\u062a" section
        d.cart_items = self.page.evaluate(
            "() => [...document.querySelectorAll('main table tbody tr')].length")
        return d

    # ---- writing (never reached in dry-run) --------------------------
    def save(self, detail: Detail, changes: dict):
        self.page.goto(f"{BASE}/missed-orders/{detail.uuid}")
        self.page.click("button:has-text('\u062a\u0639\u062f\u064a\u0644')")
        dlg = self.page.wait_for_selector("[role=dialog]")
        names = {"phone": "phone", "government": "government",
                 "address": "address", "note": "note"}
        for key, val in changes.items():
            dlg.query_selector(f"[name='{names[key]}']").fill(val)
        dlg.query_selector("button:has-text('\u062d\u0641\u0638')").click()
        self.page.wait_for_selector("[role=dialog]", state="detached", timeout=15000)

    def read_back_matches(self, detail: Detail, changes: dict) -> bool:
        self.page.goto(f"{BASE}/missed-orders/{detail.uuid}")
        self.page.reload()
        fresh = self._read_detail()
        return all(getattr(fresh, k) == v for k, v in changes.items())

    def convert(self, detail: Detail):
        self.page.goto(f"{BASE}/missed-orders/{detail.uuid}")
        btn = self.page.wait_for_selector("button:has-text('\u062a\u062d\u0648\u064a\u0644')", timeout=10000)
        if btn.is_disabled():
            raise RuntimeError("convert button disabled (missing required data)")
        btn.click()


class RealOrders:
    """Dedup + post-convert verification against the REAL orders list,
    using the phone filter that works in the dashboard URL."""

    def __init__(self, page):
        self.page = page

    def exists(self, phone: str) -> bool:
        digits = phone.lstrip("0")
        flt = quote(json.dumps({"phone$cont": digits}, ensure_ascii=False,
                               separators=(",", ":")), safe="")
        self.page.goto(f"{BASE}/orders?filter={flt}")
        self.page.wait_for_selector("tbody tr, text=\u0644\u0627 \u064a\u0648\u062c\u062f", timeout=15000)
        return len(self.page.query_selector_all("tbody tr")) > 0
