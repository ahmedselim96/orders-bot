"""Writes status.json for the dashboard. PUBLIC data only: masked phone, 6-char key,
province source, total, state, reason code. Never names, addresses or full phones."""
import json
import os
import time
from datetime import datetime, timezone, timedelta

CAIRO = timezone(timedelta(hours=3))
KEEP_ORDERS = 150
KEEP_CYCLES = 40


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rows(store):
    q = ("SELECT o.uuid,o.state,o.phone_masked,o.after_json,o.last_error,o.updated_at,"
         "(SELECT note FROM events e WHERE e.uuid=o.uuid ORDER BY e.id DESC LIMIT 1) "
         "FROM orders o ORDER BY o.updated_at DESC LIMIT ?")
    out = []
    for key, st, pm, after, err, ts, note in store.db.execute(q, (KEEP_ORDERS,)).fetchall():
        a = json.loads(after) if after else {}
        price = a.get("price") or {}
        why = (note or "") if st in ("needs_review", "excluded", "failed") else ""
        if st == "failed" and err:
            why = (why + " | " if why else "") + "".join("#" if c.isdigit() else c for c in err)[:80]
        out.append({"k": key[:6], "phone": pm, "state": st, "gov": a.get("gov_source"),
                    "addr": a.get("addr_source"), "total": price.get("total"),
                    "why": why[:120], "t": _iso(ts)})
    return out


def _today_counts(store):
    start = datetime.now(CAIRO).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    c = {"succeeded": 0, "needs_review": 0, "failed": 0, "excluded": 0}
    for st, n in store.db.execute(
            "SELECT state,COUNT(*) FROM orders WHERE updated_at>=? GROUP BY state", (start,)):
        if st in c:
            c[st] = n
    c["date"] = datetime.now(CAIRO).strftime("%Y-%m-%d")
    return c


def export(store, path, meta, started, finished=None):
    finished = finished or time.time()
    prev = {}
    try:
        with open(path, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:
        pass
    cyc_counts = {"succeeded": 0, "needs_review": 0, "failed": 0, "excluded": 0}
    touched = 0
    for st, n in store.db.execute(
            "SELECT state,COUNT(*) FROM orders WHERE updated_at>=? GROUP BY state", (started - 1,)):
        touched += n
        if st in cyc_counts:
            cyc_counts[st] = n
    cycle = {"t": _iso(started), "seconds": int(finished - started), "touched": touched, **cyc_counts}
    cycles = (prev.get("cycles") or [])[-(KEEP_CYCLES - 1):] + [cycle]
    data = {"v": 1, "updated": _iso(finished), **meta, "cycle": cycle, "cycles": cycles,
            "today": _today_counts(store), "orders": _rows(store)}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)
    return data
