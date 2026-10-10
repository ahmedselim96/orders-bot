"""Phone normalization rules (v1, approved by Ali).

Pure functions, no I/O. Returns a PhoneResult.
Status is either "ok" or "needs_review". Never guesses.
"""
import re
from dataclasses import dataclass
from typing import Optional

_AR_DIGITS = str.maketrans(
    "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9", "01234567890123456789"
)
_VALID = re.compile(r"^01[0125]\d{8}$")
_IN_TEXT = re.compile(r"(?<!\d)01[0125]\d{8}(?!\d)")
_SPLIT = re.compile(r"[/\\|,\u060c;\n]+|\s+\u0648\s+|\s+-\s+")


@dataclass
class PhoneResult:
    status: str                      # "ok" | "needs_review"
    phone: Optional[str] = None      # 11 digits, 01xxxxxxxxx
    extra_phone: Optional[str] = None  # second number -> goes to the Note field
    source: str = "phone_field"      # "phone_field" | "address"
    reason: str = ""                 # machine-readable reason / rule applied


def to_ascii_digits(text: str) -> str:
    return (text or "").translate(_AR_DIGITS)


def _digits_only(text: str) -> str:
    return re.sub(r"\D", "", to_ascii_digits(text))


def _classify(d: str):
    """Classify one digit string. Returns (phone|None, reason)."""
    if not d:
        return None, "empty"

    if d.startswith("002"):
        rest = d[3:]
        if len(rest) == 10 and rest.startswith("1"):
            return _check("0" + rest, "strip_002_add_0")
        if len(rest) == 11 and rest.startswith("01"):
            return _check(rest, "strip_002")
        return None, "002_unrecognized"

    if len(d) == 12 and d.startswith("20"):
        return _check("0" + d[2:], "strip_20_add_0")

    if len(d) == 10 and d.startswith("1"):
        return _check("0" + d, "add_leading_0")

    if len(d) == 10 and d.startswith("01"):
        return None, "missing_digit"

    if len(d) == 11:
        return _check(d, "as_is")

    if len(d) == 12 and _VALID.match(d[:11]):
        return d[:11], "extra_trailing_digit"

    return None, "invalid_length"


def _check(p: str, rule: str):
    if _VALID.match(p):
        return p, rule
    if re.match(r"^01[34]\d{8}$", p):
        return None, "unsupported_prefix_013_014"
    return None, "invalid_prefix"


def normalize_phone(raw: str, address: str = "") -> PhoneResult:
    raw = raw or ""
    d_all = _digits_only(raw)

    # two valid numbers glued together (22 digits)
    if len(d_all) == 22 and _VALID.match(d_all[:11]) and _VALID.match(d_all[11:]):
        return PhoneResult("ok", d_all[:11], d_all[11:], reason="two_numbers_glued")

    phone, reason = _classify(d_all)
    if phone:
        return PhoneResult("ok", phone, reason=reason)

    # two numbers in one cell, separated
    parts = [p for p in _SPLIT.split(to_ascii_digits(raw)) if _digits_only(p)]
    if len(parts) > 1:
        found = []
        for p in parts:
            ph, _ = _classify(_digits_only(p))
            if ph and ph not in found:
                found.append(ph)
        if found:
            return PhoneResult(
                "ok", found[0], found[1] if len(found) > 1 else None,
                reason="multi_number_cell",
            )

    # fallback: a valid number sitting in the address
    m = _IN_TEXT.search(to_ascii_digits(address or ""))
    if m:
        return PhoneResult("ok", m.group(0), source="address",
                           reason="phone_from_address")

    return PhoneResult("needs_review", reason=reason)
