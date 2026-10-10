"""One 5-minute cycle. Pure orchestration: all I/O goes through injected
drivers, so it is testable offline.

dry_run=True (default): reads, decides, logs the PLAN. Never saves or converts.
"""
import hashlib
from dataclasses import dataclass, field
from typing import Optional

from phone import normalize_phone
from governorate import resolve_governorate, resolve_address
from store import (Store, NEW, READY, PROCESSING, SAVED, RESENDING, SUCCEEDED,
                    FAILED, NEEDS_REVIEW, EXCLUDED)


@dataclass
class Row:                      # one line of the missed-orders list
    name: str
    phone_raw: str
    reason: str
    date: str
    index: int = 0

    @property
    def key(self) -> str:       # list rows carry no UUID -> stable fingerprint
        return hashlib.sha1(
            f"{self.name}|{self.phone_raw}|{self.date}".encode()).hexdigest()[:20]


@dataclass
class Detail:
    uuid: str
    name: str = ""
    phone: str = ""
    country: str = ""
    government: str = ""
    address: str = ""
    note: str = ""
    cart_items: int = 0


@dataclass
class TaagerHit:
    province: str = ""
    address: str = ""


def plan_changes(detail: Detail, phone, gov, addr) -> dict:
    """Only fields that actually change; the 'before' stays in the log."""
    want = {"phone": phone.phone, "government": gov.easyorders_value,
            "address": addr.address}
    note = detail.note or ""
    extras = []
    if phone.extra_phone:
        extras.append(f"\u0631\u0642\u0645 \u062a\u0627\u0646\u064a: {phone.extra_phone}")
    if gov.guessed:
        extras.append("\u0627\u0644\u0645\u062d\u0627\u0641\u0638\u0629 \u0645\u062e\u0645\u0651\u0646\u0629")
    if addr.flags:
        extras.append("\u0628\u064a\u0627\u0646\u0627\u062a: " + ",".join(addr.flags))
    if extras:
        want["note"] = (note + " | " if note else "") + " ; ".join(extras)
    cur = {"phone": detail.phone, "government": detail.government,
           "address": detail.address, "note": detail.note}
    return {k: v for k, v in want.items() if (cur.get(k) or "") != v}


def process_cycle(store: Store, driver, real_orders, taager=None,
                  dry_run=True, owner="run", log=print):
    if not store.acquire_lock(owner, ttl=240):
        log("another run holds the lock; exiting")
        return {"skipped": "locked"}
    summary = {}
    try:
        for row in driver.list_missed():
            key = row.key
            store.register(key, row.phone_raw, {"reason": row.reason})
            st = store.state(key)

            # ---- recovery rules: never re-click after intent ----------
            if st == RESENDING:
                ok = driver.verify_converted(row.phone_raw)
                store.transition(key, SUCCEEDED if ok else FAILED,
                                 "recovery verify", error=None if ok else "unverified after crash")
                summary[key] = st + "->verified"
                continue
            if st not in (NEW,):
                continue                       # known: needs_review/failed/etc. wait for a human

            detail = driver.open_detail(row)
            phone = normalize_phone(detail.phone or row.phone_raw, detail.address)
            if phone.status != "ok":
                store.transition(key, NEEDS_REVIEW, f"phone: {phone.reason}")
                continue
            if real_orders.exists(phone.phone):
                store.transition(key, EXCLUDED, "duplicate of a real order")
                continue
            if detail.cart_items < 1:
                store.transition(key, NEEDS_REVIEW, "empty cart")
                continue

            hit: Optional[TaagerHit] = None
            gov = resolve_governorate(detail.government, detail.address)
            need_taager = (gov.source == "fallback") or not (detail.address or "").strip()
            if need_taager and taager is not None:
                try:
                    hit = taager.lookup(phone.phone)
                except Exception as e:           # Taager down: continue without it
                    log(f"taager lookup failed: {type(e).__name__}")
            if hit:
                gov = resolve_governorate(detail.government, detail.address,
                                          taager_province=hit.province)
            addr = resolve_address(detail.address, gov,
                                   hit.address if hit else None,
                                   hit.province if hit else None)
            changes = plan_changes(detail, phone, gov, addr)

            store.transition(key, READY, "decided",
                             after={"uuid": detail.uuid, "plan": changes,
                                    "gov_source": gov.source,
                                    "addr_source": addr.source})
            if dry_run:
                summary[key] = {"plan": list(changes), "dry_run": True}
                continue

            # ---- real run -------------------------------------------
            store.transition(key, PROCESSING, "saving")
            try:
                driver.save(detail, changes)
                if not driver.read_back_matches(detail, changes):
                    raise RuntimeError("read-back mismatch after save")
                store.transition(key, SAVED, "save verified")
            except Exception as e:
                store.transition(key, FAILED, "save failed", error=str(e)[:200])
                continue
            if not store.begin_resend(key):
                continue
            try:
                driver.convert(detail)
                ok = driver.verify_converted(phone.phone)
                store.transition(key, SUCCEEDED if ok else FAILED,
                                 "converted" if ok else "convert not verified",
                                 error=None if ok else "no real order seen")
            except Exception as e:
                # intent was recorded: stay RESENDING -> next cycle verifies only
                log(f"convert raised, left in RESENDING: {type(e).__name__}")
        return summary
    finally:
        store.release_lock(owner)
