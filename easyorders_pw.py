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
    def _open_edit(self, detail: Detail):
        self.page.goto(f"{BASE}/missed-orders/{detail.uuid}")
        self.page.click("button:has-text('\u062a\u0639\u062f\u064a\u0644')")
        self.page.wait_for_selector("[role=dialog]")
        return self.page.locator("[role=dialog]").first

    def _close_edit(self):
        try:
            self.page.keyboard.press("Escape")
            self.page.wait_for_selector("[role=dialog]", state="detached", timeout=5000)
        except Exception:
            btn = self.page.query_selector("[role=dialog] button:has-text('\u0625\u0644\u063a\u0627\u0621')")
            if btn:
                btn.click()
                self.page.wait_for_selector("[role=dialog]", state="detached", timeout=5000)

    @staticmethod
    def _num(txt):
        tr = str.maketrans("\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669", "0123456789")
        m = re.search(r"\d+(?:\.\d+)?", (txt or "").translate(tr).replace(",", ""))
        return float(m.group()) if m else None

    def read_cart(self, detail: Detail):
        """[(unit_price, qty), ...] read from the edit form. Opens it and closes it
        WITHOUT saving. Empty list if the fields are not found (-> Needs Review)."""
        dlg = self._open_edit(detail)
        try:
            prices = dlg.get_by_label(re.compile(r"^\u0627\u0644\u0633\u0639\u0631")).all()
            qtys = dlg.get_by_label(re.compile(r"^\u0627\u0644\u0643\u0645\u064a\u0629")).all()
            if len(prices) != len(qtys):
                print(f"[cart] mismatch prices={len(prices)} qtys={len(qtys)}")
                return []
            out = []
            for pr, q in zip(prices, qtys):
                u, n = self._num(pr.input_value()), self._num(q.input_value())
                if u is None or n is None or n != int(n):
                    return []
                out.append((u if u != int(u) else int(u), int(n)))
            return out
        finally:
            self._close_edit()

    def save(self, detail: Detail, changes: dict):
        dlg = self._open_edit(detail)
        names = {"phone": "phone", "government": "government",
                 "address": "address", "note": "note"}
        for key, val in changes.items():
            if key == "price":
                boxes = dlg.get_by_label(re.compile(r"^\u0627\u0644\u0633\u0639\u0631"))
                if boxes.count() != len(val):
                    raise RuntimeError("price fields count changed")
                for i, v in enumerate(val):
                    boxes.nth(i).fill(str(v))
            else:
                dlg.locator(f"[name='{names[key]}']").fill(val)
        dlg.locator("button:has-text('\u062d\u0641\u0638')").click()
        self.page.wait_for_selector("[role=dialog]", state="detached", timeout=15000)

    def read_back_matches(self, detail: Detail, changes: dict) -> bool:
        self.page.goto(f"{BASE}/missed-orders/{detail.uuid}")
        self.page.reload()
        fresh = self._read_detail()
        if not all(getattr(fresh, k) == v for k, v in changes.items() if k != "price"):
            return False
        if "price" in changes:
            cart = self.read_cart(detail)
            return [c[0] for c in cart] == list(changes["price"])
        return True

    def convert(self, detail: Detail):
        self.page.goto(f"{BASE}/missed-orders/{detail.uuid}")
        btn = self.page.wait_for_selector(
            "button:has-text('\u062a\u062d\u0648\u064a\u0644 \u0644\u0637\u0644\u0628')", timeout=10000)
        if btn.is_disabled():
            raise RuntimeError("convert button disabled (missing required data)")
        btn.click()
        self.page.wait_for_timeout(2500)
        dlg = self.page.query_selector("[role=dialog]")
        if dlg:
            # unknown confirmation step: report the button labels only, do not click
            labels = [b.inner_text().strip()[:20] for b in dlg.query_selector_all("button")]
            print(f"[convert] dialog appeared, buttons={labels}")
            raise RuntimeError("convert needs confirmation dialog")

    def verify_converted(self, phone: str) -> bool:
        """A real order with this phone must now exist (poll up to ~60 s)."""
        ro = RealOrders(self.page)
        for _ in range(6):
            try:
                if ro.exists(phone):
                    return True
            except RuntimeError:
                pass
            self.page.wait_for_timeout(5000)
        return False


class RealOrders:
    """Dedup + post-convert verification against the REAL orders list,
    using the phone filter that works in the dashboard URL."""

    def __init__(self, page):
        self.page = page

    def exists(self, phone: str) -> bool:
        """True  = a real order with this phone is in the filtered list.
        False = the list loaded OK (a 200 from the orders API was seen) and stayed
                empty for 3 consecutive checks.
        Anything else (skeleton rows, unfiltered list, API error, unknown page)
        raises -> the order goes to Needs Review. Never guessed."""
        digits = phone.lstrip("0")
        flt = quote(json.dumps({"phone$cont": digits}, ensure_ascii=False,
                               separators=(",", ":")), safe="")
        seen = []                                  # (status, masked path) of order API calls

        def on_resp(r):
            try:
                if r.request.resource_type in ("xhr", "fetch") and "order" in r.url.lower():
                    path = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27}|\d+", "#", r.url.split("?")[0])
                    seen.append((r.status, path[-60:]))
            except Exception:
                pass

        self.page.on("response", on_resp)
        try:
            self.page.goto(f"{BASE}/orders?filter={flt}")
            try:
                self.page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            tr = str.maketrans("\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669",
                               "0123456789")
            rows = self.page.locator("tbody tr")
            total = filled = matched = empty_polls = 0
            for _ in range(20):                    # up to ~20 s of 1 s polls
                self.page.wait_for_timeout(1000)
                total = rows.count()
                if total == 0:
                    empty_polls += 1
                    if empty_polls >= 3:
                        if any(st >= 400 for st, _ in seen):
                            raise RuntimeError("orders API error")
                        if any(st == 200 for st, _ in seen):
                            print("[dup-check] list loaded, empty -> no duplicate")
                            return False
                    continue
                empty_polls = 0
                texts = [re.sub(r"[\s\-]", "", rows.nth(i).inner_text().translate(tr))
                         for i in range(min(total, 50))]
                filled = sum(1 for t in texts if t)
                matched = sum(1 for t in texts if digits in t)
                if filled == len(texts) and matched == len(texts):
                    print(f"[dup-check] rows={total} all match -> duplicate")
                    return True
            print(f"[dup-check] UNSURE rows={total} filled={filled} matched={matched} "
                  f"api={sorted(set(seen))[:4]}")
            raise RuntimeError("orders list state unknown")
        finally:
            try:
                self.page.remove_listener("response", on_resp)
            except Exception:
                pass
