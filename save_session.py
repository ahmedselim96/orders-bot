"""Run ON YOUR OWN COMPUTER, once. Opens a real browser window; YOU log in to
EasyOrders yourself (any OTP/CAPTCHA included). Then press Enter in the
terminal. The session is saved to easyorders_state.json.

Paste that file's content into the GitHub secret EASYORDERS_STATE.
It is equivalent to being logged in: never share it, never commit it.
"""
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=False)
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto("https://app.easy-orders.net/")
    input("Log in in the browser window, open the '\u0627\u0644\u0637\u0644\u0628\u0627\u062a \u0627\u0644\u0645\u0641\u0642\u0648\u062f\u0629' page, "
          "then press Enter here... ")
    ctx.storage_state(path="easyorders_state.json")
    browser.close()
print("Saved easyorders_state.json  (keep it private)")
