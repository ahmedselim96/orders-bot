"""Governorate + address rules (v1, approved by Ali).

Order of evidence:
  1. explicit governorate in the governorate field
  2. address text: governorate-level names (and "\u0645\u062d\u0627\u0641\u0638\u0629 X") -> city/area names
  3. Taager history (caller passes the old order's province/address)
  4. fixed fallback: \u0627\u0644\u0641\u064a\u0648\u0645, marked as guessed
Never raises; always returns a GovResult with a `source` and `guessed` flag.
"""
import re
from dataclasses import dataclass
from typing import Optional

from governorates_data import (
    ALIASES, EXTRA_OVERRIDES, EASYORDERS_NAME, FALLBACK_PROVINCE,
)

PLACEHOLDER_ADDRESS = "\u0627\u0644\u0639\u0646\u0648\u0627\u0646 \u0628\u0627\u0644\u062a\u0644\u064a\u0641\u0648\u0646"

_AR_DIGITS = str.maketrans("\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9", "01234567890123456789")
_DIACRITICS = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u0640]")
_NON_WORD = re.compile(r"[^\u0621-\u064a0-9]+")


def normalize(text: str) -> str:
    t = (text or "").translate(_AR_DIGITS)
    t = _DIACRITICS.sub("", t)
    t = (t.replace("\u0623", "\u0627").replace("\u0625", "\u0627").replace("\u0622", "\u0627")
          .replace("\u0649", "\u064a").replace("\u0629", "\u0647"))
    return _NON_WORD.sub(" ", t).strip()


def _strip_al(s: str) -> str:
    return s[2:] if s.startswith("\u0627\u0644") else s


def _build_entries():
    entries = []   # (tier, alias_norm, province, table_order)
    for i, (alias, prov) in enumerate(EXTRA_OVERRIDES):
        entries.append((0, normalize(alias), prov, -100 + i))
    for i, (alias, prov) in enumerate(ALIASES):
        a, p = normalize(alias), normalize(prov)
        tier = 1 if _strip_al(a) == _strip_al(p) else 2
        entries.append((tier, a, prov, i))
    return entries


_ENTRIES = _build_entries()
_PATTERNS = {}


def _pattern(alias_norm: str):
    p = _PATTERNS.get(alias_norm)
    if p is None:
        core = re.escape(alias_norm)
        if not alias_norm.startswith("\u0627\u0644"):
            core = r"(?:\u0627\u0644)?" + core
        p = re.compile(r"(?<![\u0621-\u064a0-9])[\u0648\u0628\u0644]?" + core +
                       r"(?![\u0621-\u064a0-9])")
        _PATTERNS[alias_norm] = p
    return p


@dataclass
class GovResult:
    province: str
    source: str            # field | address_gov | address_place | taager | fallback
    guessed: bool = False
    matched: str = ""      # the alias that matched (for the log)
    ambiguous_with: tuple = ()

    @property
    def easyorders_value(self) -> str:
        return EASYORDERS_NAME.get(self.province, self.province)


def _search(text_norm: str):
    """Return list of matches sorted by (tier, -len(alias), table_order)."""
    found = []
    for tier, alias, prov, order in _ENTRIES:
        if _pattern(alias).search(text_norm):
            # "\u0645\u062d\u0627\u0641\u0638\u0629 X" marker promotes a match to tier 0
            marked = re.search(r"(?<![\u0621-\u064a0-9])\u0645\u062d\u0627\u0641\u0638\u0647\s+(?:\u0627\u0644)?" +
                               re.escape(_strip_al(alias)) + r"(?![\u0621-\u064a0-9])",
                               text_norm)
            found.append((0 if marked else tier, -len(alias), order, alias, prov))
    found.sort()
    return found


def resolve_governorate(gov_field: str = "", address: str = "",
                        taager_province: Optional[str] = None) -> GovResult:
    # 1) explicit governorate field wins
    f = _search(normalize(gov_field))
    if f:
        t, _, _, alias, prov = f[0]
        return GovResult(prov, "field", False, alias)

    # 2) address: governorate-level first, then city/area
    a = _search(normalize(address))
    if a:
        t, _, _, alias, prov = a[0]
        others = tuple(sorted({m[4] for m in a if m[4] != prov}))
        src = "address_gov" if t <= 1 else "address_place"
        return GovResult(prov, src, False, alias, others)

    # 3) Taager history
    if taager_province:
        t = _search(normalize(taager_province))
        if t:
            return GovResult(t[0][4], "taager", False, t[0][3])

    # 4) fixed fallback (guessed)
    return GovResult(FALLBACK_PROVINCE, "fallback", True)


@dataclass
class AddressResult:
    address: str
    source: str      # order | taager | placeholder
    flags: tuple = ()


def resolve_address(address: str, gov: GovResult,
                    taager_address: Optional[str] = None,
                    taager_province: Optional[str] = None) -> AddressResult:
    """Empty address -> Taager history (only if it doesn't contradict an
    explicit governorate) -> placeholder. Fully-guessed orders get a flag."""
    addr = (address or "").strip()
    if addr:
        return AddressResult(addr, "order")

    if taager_address and taager_address.strip():
        contradicts = False
        if gov.source in ("field", "address_gov", "address_place") and taager_province:
            tp = _search(normalize(taager_province))
            contradicts = bool(tp) and tp[0][4] != gov.province
        if not contradicts:
            return AddressResult(taager_address.strip(), "taager",
                                 ("from_taager",))

    flags = ("fully_guessed",) if gov.source == "fallback" else ()
    return AddressResult(PLACEHOLDER_ADDRESS, "placeholder", flags)
