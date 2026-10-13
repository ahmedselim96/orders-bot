"""Governorate shipping fees (EGP) as given by Ali. The fee is folded into the
unit price of the single cart line:  new_unit = unit + fee / qty.
Keys are the same 30 names used by governorates_data (province names)."""
import math
from fractions import Fraction

SHIPPING = {
    "\u0627\u0644\u0642\u0627\u0647\u0631\u0629": 95, "\u0627\u0644\u062c\u064a\u0632\u0629": 95,
    "\u0627\u0644\u0625\u0633\u0643\u0646\u062f\u0631\u064a\u0629": 95,
    "\u0627\u0644\u0628\u062d\u064a\u0631\u0629": 100, "\u0627\u0644\u062f\u0642\u0647\u0644\u064a\u0629": 100,
    "\u0627\u0644\u0634\u0631\u0642\u064a\u0629": 100, "\u0627\u0644\u063a\u0631\u0628\u064a\u0629": 100,
    "\u0627\u0644\u0642\u0644\u064a\u0648\u0628\u064a\u0629": 100, "\u0627\u0644\u0625\u0633\u0645\u0627\u0639\u064a\u0644\u064a\u0629": 100,
    "\u0627\u0644\u0633\u0648\u064a\u0633": 100, "\u0627\u0644\u0645\u0646\u0648\u0641\u064a\u0629": 100,
    "\u0628\u0648\u0631\u0633\u0639\u064a\u062f": 100, "\u0643\u0641\u0631 \u0627\u0644\u0634\u064a\u062e": 100,
    "\u0627\u0644\u0645\u062d\u0644\u0629": 100, "\u0627\u0644\u0639\u0627\u0634\u0631 \u0645\u0646 \u0631\u0645\u0636\u0627\u0646": 100,
    "\u0637\u0646\u0637\u0627": 100, "\u0628\u0646\u064a \u0633\u0648\u064a\u0641": 100, "\u062f\u0645\u064a\u0627\u0637": 100,
    "\u0633\u0648\u0647\u0627\u062c": 105, "\u0623\u0633\u064a\u0648\u0637": 105, "\u0627\u0644\u0641\u064a\u0648\u0645": 105,
    "\u0627\u0644\u0639\u064a\u0646 \u0627\u0644\u0633\u062e\u0646\u0629": 105, "\u0623\u0633\u0648\u0627\u0646": 105,
    "\u0642\u0646\u0627": 105, "\u0627\u0644\u0645\u0646\u064a\u0627": 105,
    "\u0648\u0627\u062f\u064a \u0627\u0644\u0646\u0637\u0631\u0648\u0646": 105, "\u0645\u0637\u0631\u0648\u062d": 105,
    "\u0627\u0644\u0623\u0642\u0635\u0631": 105, "\u0627\u0644\u0633\u0627\u062d\u0644 \u0627\u0644\u0634\u0645\u0627\u0644\u064a": 105,
    "\u0627\u0644\u0628\u062d\u0631 \u0627\u0644\u0623\u062d\u0645\u0631": 105,
}


# ---- editable from config.json (dashboard); invalid -> built-in table stays ----
import config as _config
_t = _config.shipping_table()
if _t:
    SHIPPING.clear()
    SHIPPING.update(_t)


def plan_price(province, unit, qty):
    """Return (ok, new_unit_or_None, reason). Anything unclear
    (unknown province, qty<1) -> not ok -> Needs Review. Fractions round UP."""
    fee = SHIPPING.get(province)
    if fee is None:
        return False, None, "no_shipping_for_province"
    if qty < 1 or unit <= 0:
        return False, None, "bad_cart_values"
    new = Fraction(unit).limit_denominator(100) + Fraction(fee, qty)
    return True, math.ceil(new), ""      # fractions are rounded UP (Ali's rule)


def plan_prices(province, cart, flat=0):
    """cart = [(unit, qty), ...]. The fee is spread over ALL pieces:
    every piece carries fee/total_pieces, each line's unit price is rounded UP.
    One line == the single-line rule. Returns (ok, [new_unit,...], reason)."""
    fee = SHIPPING.get(province)
    if fee is None:
        return False, [], "no_shipping_for_province"
    fee = fee - flat          # the platform itself adds `flat` at conversion
    if fee < 0:
        return False, [], "fee_below_platform_flat"
    if not cart or len(cart) > 6:
        return False, [], "cart_lines=%d" % len(cart)
    if any(q < 1 or u <= 0 for u, q in cart):
        return False, [], "bad_cart_values"
    total_qty = sum(q for _, q in cart)
    share = Fraction(fee, total_qty)
    return True, [math.ceil(Fraction(u).limit_denominator(100) + share) for u, _ in cart], ""


import re

_NOTE = ("\u0645\u0644\u062d\u0648\u0638\u0629 \u0644\u0644\u0643\u0648\u0644 \u0633\u0646\u062a\u0631: \u0633\u0639\u0631 \u0627\u0644\u0645\u0646\u062a\u062c {p} \u062c\u0646\u064a\u0647 + "
         "\u0645\u0635\u0627\u0631\u064a\u0641 \u0627\u0644\u0634\u062d\u0646 {s} \u062c\u0646\u064a\u0647 = \u0627\u0644\u0625\u062c\u0645\u0627\u0644\u064a {t} \u062c\u0646\u064a\u0647 "
         "(\u0627\u0644\u0633\u0639\u0631 \u0634\u0627\u0645\u0644 \u0627\u0644\u0634\u062d\u0646).")
_NEW_RE = re.compile("\u0645\u0635\u0627\u0631\u064a\u0641 \u0627\u0644\u0634\u062d\u0646 (\\d+) \u062c\u0646\u064a\u0647")
_OLD_RE = re.compile(r"\[ship:(\d+)\]")


def make_note(product_total, ship, total):
    """Arabic note for the call center: product price + shipping = total (shipping included)."""
    return _NOTE.format(p=int(product_total), s=int(ship), t=int(total))


def shipping_marker(note):
    """('new', shipping) for our Arabic note, ('old', shipping) for the first [ship:N]
    marker, or None if shipping was not added to this order yet."""
    note = note or ""
    m = _NEW_RE.search(note)
    if m and _PLAT_SUFFIX in note:
        return "platform", int(m.group(1))
    if m:
        return "new", int(m.group(1))
    m = _OLD_RE.search(note)
    if m:
        return "old", int(m.group(1))
    return None


def strip_old_marker(note):
    return _OLD_RE.sub("", note or "").strip(" ;")


_PLAT_SUFFIX = "(\u0627\u0644\u0634\u062d\u0646 \u0628\u064a\u062a\u0636\u0627\u0641 \u062a\u0644\u0642\u0627\u0626\u064a \u0645\u0646 \u0627\u0644\u0645\u0646\u0635\u0629)"
_NOTE_PLAT = ("\u0645\u0644\u062d\u0648\u0638\u0629 \u0644\u0644\u0643\u0648\u0644 \u0633\u0646\u062a\u0631: \u0633\u0639\u0631 \u0627\u0644\u0645\u0646\u062a\u062c {p} \u062c\u0646\u064a\u0647 + "
              "\u0645\u0635\u0627\u0631\u064a\u0641 \u0627\u0644\u0634\u062d\u0646 {s} \u062c\u0646\u064a\u0647 = \u0627\u0644\u0625\u062c\u0645\u0627\u0644\u064a {t} \u062c\u0646\u064a\u0647 ") + _PLAT_SUFFIX


def make_platform_note(product_total, ship, total):
    """Platform mode: the price is NOT touched; EasyOrders adds the shipping itself."""
    return _NOTE_PLAT.format(p=int(product_total), s=int(ship), t=int(total))


_FLAT_TAG = "(\u0645\u0646\u0647\u0627 {f} \u062c\u0646\u064a\u0647 \u0628\u062a\u0636\u064a\u0641\u0647\u0627 \u0627\u0644\u0645\u0646\u0635\u0629 \u062a\u0644\u0642\u0627\u0626\u064a)"


def make_flat_note(product_total, ship, total, flat):
    """Price mode when the platform adds a fixed fee `flat` at conversion:
    `ship` and `total` are the REAL totals the customer pays."""
    return (_NOTE.rsplit(" (", 1)[0].format(p=int(product_total), s=int(ship), t=int(total))
            + " " + _FLAT_TAG.format(f=int(flat)))


def has_flat_tag(note, flat):
    return _FLAT_TAG.format(f=int(flat)) in (note or "")
