"""State machine + SQLite store + overlap lock.

Safety properties (priority #1: never create duplicate orders):
  * every missed order has ONE row keyed by its EasyOrders UUID
  * transitions are validated; illegal ones raise
  * RESENDING is written BEFORE the click ("intent"), atomically; only one
    caller can win it, and after a crash the recovery action is VERIFY-ONLY
  * the 5-minute run is guarded by a lock with an expiry (TTL)
"""
import json
import sqlite3
import time
from contextlib import contextmanager

NEW, NEEDS_REVIEW, READY, PROCESSING = "new", "needs_review", "ready", "processing"
SAVED, RESENDING, SUCCEEDED, FAILED, EXCLUDED = (
    "saved", "resending", "succeeded", "failed", "excluded")

ALLOWED = {
    NEW: {NEEDS_REVIEW, READY, EXCLUDED},
    NEEDS_REVIEW: {READY, EXCLUDED},
    READY: {PROCESSING, NEEDS_REVIEW, EXCLUDED},
    PROCESSING: {SAVED, FAILED, NEEDS_REVIEW},
    SAVED: {RESENDING, FAILED},
    RESENDING: {SUCCEEDED, FAILED},
    FAILED: {READY, NEEDS_REVIEW},      # manual retry only
    SUCCEEDED: set(),
    EXCLUDED: set(),
}


class IllegalTransition(Exception):
    pass


def mask_phone(p: str) -> str:
    p = p or ""
    return p[:3] + "*" * max(len(p) - 6, 0) + p[-3:] if len(p) > 6 else "***"


SCHEMA = """
CREATE TABLE IF NOT EXISTS orders(
  uuid TEXT PRIMARY KEY, state TEXT NOT NULL, attempts INTEGER DEFAULT 0,
  phone_masked TEXT, before_json TEXT, after_json TEXT, flags TEXT,
  last_error TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT, ts REAL,
  from_state TEXT, to_state TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS locks(
  name TEXT PRIMARY KEY, owner TEXT, expires_at REAL);
"""


class Store:
    def __init__(self, path=":memory:", clock=time.time):
        self.db = sqlite3.connect(path, isolation_level=None)  # autocommit
        self.db.executescript(SCHEMA)
        self.clock = clock

    @contextmanager
    def _tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    # ---- lock -------------------------------------------------------
    def acquire_lock(self, owner, ttl=240, name="run"):
        now = self.clock()
        with self._tx():
            row = self.db.execute(
                "SELECT owner, expires_at FROM locks WHERE name=?", (name,)
            ).fetchone()
            if row and row[1] > now and row[0] != owner:
                return False
            self.db.execute(
                "INSERT OR REPLACE INTO locks VALUES(?,?,?)",
                (name, owner, now + ttl))
            return True

    def release_lock(self, owner, name="run"):
        self.db.execute("DELETE FROM locks WHERE name=? AND owner=?", (name, owner))

    # ---- orders -----------------------------------------------------
    def register(self, uuid, phone="", before=None):
        """Idempotent: an already-known order is never reset."""
        now = self.clock()
        with self._tx():
            if self.db.execute("SELECT 1 FROM orders WHERE uuid=?", (uuid,)).fetchone():
                return False
            self.db.execute(
                "INSERT INTO orders(uuid,state,phone_masked,before_json,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (uuid, NEW, mask_phone(phone), json.dumps(before or {}), now, now))
            self._event(uuid, None, NEW, "registered")
            return True

    def state(self, uuid):
        r = self.db.execute("SELECT state FROM orders WHERE uuid=?", (uuid,)).fetchone()
        return r[0] if r else None

    def transition(self, uuid, to, note="", expect=None, after=None, error=None):
        now = self.clock()
        with self._tx():
            cur = self.state(uuid)
            if cur is None:
                raise KeyError(uuid)
            if expect is not None and cur != expect:
                return False                     # lost the race, do nothing
            if to not in ALLOWED[cur]:
                raise IllegalTransition(f"{cur} -> {to}")
            sets, args = "state=?, updated_at=?", [to, now]
            if to in (PROCESSING, RESENDING):
                sets += ", attempts=attempts+1"
            if after is not None:
                sets += ", after_json=?"; args.append(json.dumps(after))
            if error is not None:
                sets += ", last_error=?"; args.append(error)
            self.db.execute(f"UPDATE orders SET {sets} WHERE uuid=?", (*args, uuid))
            self._event(uuid, cur, to, note)
            return True

    def begin_resend(self, uuid):
        """Record the intent to click 'convert' BEFORE clicking.
        Returns True for exactly one caller, only from SAVED."""
        return self.transition(uuid, RESENDING, "intent: convert", expect=SAVED)

    def recovery_action(self, uuid):
        """What a restarted worker may do with this order."""
        return {
            PROCESSING: "reverify_save",   # unsure if save happened -> read back
            SAVED: "may_resend",
            RESENDING: "verify_only",      # click may have happened -> NEVER click again
        }.get(self.state(uuid), "none")

    def _event(self, uuid, a, b, note):
        self.db.execute(
            "INSERT INTO events(uuid,ts,from_state,to_state,note) VALUES(?,?,?,?,?)",
            (uuid, self.clock(), a, b, note))

    def reopen_stale(self, nr_age=3 * 3600, inflight_age=900):
        """Unattended mode. Needs-review rows are re-checked after nr_age (the human
        may have fixed them); READY/PROCESSING/SAVED rows left over from a dead run
        are re-evaluated (safe: the shipping note + real-orders check guard against
        double edits). RESENDING and FAILED are NEVER reopened (human decides)."""
        now = self.clock()
        with self._tx():
            rows = self.db.execute(
                "SELECT uuid,state FROM orders WHERE (state=? AND updated_at<?) "
                "OR (state IN (?,?,?) AND updated_at<?)",
                (NEEDS_REVIEW, now - nr_age, READY, PROCESSING, SAVED, now - inflight_age)
            ).fetchall()
            for uuid, st in rows:
                self.db.execute("UPDATE orders SET state=?, updated_at=? WHERE uuid=?",
                                (NEW, now, uuid))
                self._event(uuid, st, NEW, "auto-reopen")
        return len(rows)

    def history(self, uuid):
        return self.db.execute(
            "SELECT from_state,to_state,note FROM events WHERE uuid=? ORDER BY id",
            (uuid,)).fetchall()
