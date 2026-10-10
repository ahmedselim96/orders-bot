"""Optional Telegram notices (free). Needs two repo secrets:
TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID. Without them this does nothing.
Messages contain only: counts, order key (6 chars), masked phone, reason code.
Never names, addresses or full phones."""
import json
import os
import urllib.parse
import urllib.request


def send(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:3900]}).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data),
            timeout=15).read()
        return True
    except Exception as e:                       # a failed notice must never break the cycle
        print("[notify] failed:", type(e).__name__)
        return False


def summarize(store, since_ts):
    rows = store.db.execute(
        "SELECT e.uuid, e.to_state, e.note, o.phone_masked FROM events e "
        "JOIN orders o ON o.uuid=e.uuid "
        "WHERE e.ts>=? AND e.to_state IN ('succeeded','needs_review','failed') "
        "ORDER BY e.id", (since_ts,)).fetchall()
    ok = [r for r in rows if r[1] == "succeeded"]
    review = [r for r in rows if r[1] == "needs_review"]
    failed = [r for r in rows if r[1] == "failed"]
    if not rows:
        return ""
    lines = ["\u0637\u0644\u0628\u0627\u062a \u0645\u0641\u0642\u0648\u062f\u0629 - \u0646\u062a\u064a\u062c\u0629 \u0627\u0644\u062c\u0648\u0644\u0629:",
             f"\u2705 \u0627\u062a\u062d\u0648\u0644: {len(ok)}   \u26a0\ufe0f \u0645\u0631\u0627\u062c\u0639\u0629: {len(review)}   \u274c \u0641\u0634\u0644: {len(failed)}"]
    for key, st, note, pm in review + failed:
        lines.append(f"- {key[:6]} {pm} : {note}")
    return "\n".join(lines)
