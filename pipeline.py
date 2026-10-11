"""One 5-minute cycle. Pure orchestration: all I/O goes through injected
drivers, so it is testable offline.

dry_run=True (default): reads, decides, logs the PLAN. Never saves or converts.
"""
import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from phone import normalize_phone
from governorate import resolve_governorate, resolve_address
from shipping import (SHIPPING, plan_prices, make_note, make_platform_note,
                      shipping_marker, strip_old_marker)
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
                  dry_run=True, owner="run", log=print, stop_after_save=False,
                  deadline=None, max_actions=None, ship_mode="platform", require_safe=False):
    if not store.acquire_lock(owner, ttl=600):
        log("another run holds the lock; exiting")
        return {"skipped": "locked"}
    summary = {}
    try:
        actions = 0
        seen_phones = set()
        for row in driver.list_missed():
            if deadline is not None and time.monotonic() > deadline:
                log("time budget reached; the rest waits for the next cycle")
                break
            if max_actions is not None and actions >= max_actions:
                log("per-cycle action cap reached; the rest waits for the next cycle")
                break
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
            if phone.phone in seen_phones:      # one order per phone per cycle: the 2nd waits
                log("same phone as another row in this cycle; it waits for the next cycle")
                continue
            try:
                dup = real_orders.exists(phone.phone)
            except Exception as e:               # ambiguous page: never guess
                store.transition(key, NEEDS_REVIEW, f"dup-check unsure: {type(e).__name__}")
                continue
            if dup:
                store.transition(key, EXCLUDED, "duplicate of a real order")
                continue
            if detail.cart_items < 1:
                store.transition(key, NEEDS_REVIEW, "empty cart")
                continue

            hit: Optional[TaagerHit] = None
            gov = resolve_governorate(detail.government, detail.address)
            need_taager = (gov.source == "fallback") or not (detail.address or "").strip()
            if gov.out_of_area:
                store.transition(key, NEEDS_REVIEW,
                                 "outside coverage: " + gov.province.split(":", 1)[1])
                continue
            if need_taager and taager is not None:
                try:
                    hit = taager.lookup(phone.phone)
                except Exception as e:           # Taager down: continue without it
                    log(f"taager lookup failed: {type(e).__name__}")
            if hit:
                gov = resolve_governorate(detail.government, detail.address,
                                          taager_province=hit.province)
            if gov.out_of_area:
                store.transition(key, NEEDS_REVIEW,
                                 "outside coverage: " + gov.province.split(":", 1)[1])
                continue
            addr = resolve_address(detail.address, gov,
                                   hit.address if hit else None,
                                   hit.province if hit else None)
            if require_safe and not (gov.source in ("field", "address_gov", "address_place")
                                     and not gov.ambiguous_with):
                log(f"auto-test: skipped (gov={gov.source} addr={addr.source} "
                    f"ambiguous={bool(gov.ambiguous_with)})")
                continue
            changes = plan_changes(detail, phone, gov, addr)

            # ---- shipping goes INTO the unit price (conversion cannot be undone) ----
            mk = shipping_marker(detail.note)
            fee = SHIPPING.get(gov.province)
            if ship_mode == "platform":
                # Platform adds its own shipping at conversion: the unit price is NOT touched.
                if fee is None:
                    store.transition(key, NEEDS_REVIEW, "price: no_shipping_for_province")
                    continue
                if mk and mk[0] in ("new", "old"):
                    store.transition(key, NEEDS_REVIEW, "shipping already folded into price")
                    continue
                if mk and mk[0] == "platform":
                    if mk[1] != fee:
                        store.transition(key, NEEDS_REVIEW, "shipping note does not match province")
                        continue
                    changes = {}
                    price_info = {"mode": "platform", "noted": mk[1]}
                else:
                    try:
                        cart = driver.read_cart(detail)      # opens the edit form, never saves
                    except Exception as e:
                        store.transition(key, NEEDS_REVIEW, f"cart read failed: {type(e).__name__}")
                        continue
                    if not cart or any(q < 1 or u <= 0 for u, q in cart):
                        store.transition(key, NEEDS_REVIEW, "price: bad_cart_values")
                        continue
                    ptotal = sum(u * q for u, q in cart)
                    base_note = changes.get("note", detail.note or "")
                    text = make_platform_note(ptotal, fee, ptotal + fee)
                    changes["note"] = (base_note + " ; " if base_note else "") + text
                    price_info = {"mode": "platform", "lines": [list(c) for c in cart],
                                  "fee": fee, "total": ptotal + fee}
            elif mk and mk[0] == "new":
                # Already shipped by an earlier save: NEVER add it again, never re-save.
                if fee is None or not (fee <= mk[1] <= fee + 10):
                    store.transition(key, NEEDS_REVIEW, "shipping note does not match province")
                    continue
                changes = {}
                price_info = {"already_shipped": mk[1]}
            else:
                try:
                    cart = driver.read_cart(detail)      # opens the edit form, never saves
                except Exception as e:
                    store.transition(key, NEEDS_REVIEW, f"cart read failed: {type(e).__name__}")
                    continue
                if mk and mk[0] == "old":
                    # legacy "[ship:N]" marker: price already includes N -> only swap the note text
                    total = sum(u * q for u, q in cart)
                    if not cart or fee is None or mk[1] != fee:
                        store.transition(key, NEEDS_REVIEW, "legacy shipping marker does not match province")
                        continue
                    base = strip_old_marker(detail.note)
                    text = make_note(total - mk[1], mk[1], total)
                    changes = {"note": (base + " ; " if base else "") + text}
                    price_info = {"legacy_marker": mk[1], "total": total}
                else:
                    ok, new_units, why = plan_prices(gov.province, cart)
                    if not ok:
                        store.transition(key, NEEDS_REVIEW, f"price: {why}")
                        continue
                    old_total = sum(u * q for u, q in cart)
                    new_total = sum(nu * q for nu, (u, q) in zip(new_units, cart))
                    changes["price"] = new_units
                    base_note = changes.get("note", detail.note or "")
                    text = make_note(old_total, new_total - old_total, new_total)
                    changes["note"] = (base_note + " ; " if base_note else "") + text
                    price_info = {"lines": [list(c) for c in cart], "fee": fee, "new": new_units,
                                  "total": new_total}

            store.transition(key, READY, "decided",
                             after={"uuid": detail.uuid, "plan": changes,
                                    "gov_source": gov.source,
                                    "addr_source": addr.source,
                                    "price": price_info})
            seen_phones.add(phone.phone)
            if dry_run:
                summary[key] = {"plan": list(changes), "dry_run": True}
                continue

            # ---- real run -------------------------------------------
            actions += 1
            store.transition(key, PROCESSING, "saving")
            try:
                if changes:
                    driver.save(detail, changes)
                    if not driver.read_back_matches(detail, changes):
                        raise RuntimeError("read-back mismatch after save")
                store.transition(key, SAVED, "save verified" if changes else "nothing to save")
            except Exception as e:
                store.transition(key, FAILED, "save failed", error=str(e)[:200])
                continue
            if stop_after_save:
                nk = Row(row.name, changes.get("phone", row.phone_raw), row.reason,
                         row.date, row.index).key
                log(f"saved; key after save = {nk[:6]} (use it as ONLY_KEY for the convert stage)")
                summary[key] = {"stopped": "after_save", "fields": sorted(changes)}
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
