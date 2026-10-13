"""Editable settings (config.json at the repo root, edited from the dashboard).

Every value is validated; anything invalid is IGNORED (the built-in default is
used) and reported in CONFIG_PROBLEMS, so a typo can never break a cycle.
"""
import json
import os

PATH = os.environ.get("CONFIG_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
CONFIG_PROBLEMS = []


def _load():
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("config.json must be an object")
        return data
    except FileNotFoundError:
        return {}
    except Exception as e:                      # corrupted file -> defaults
        CONFIG_PROBLEMS.append("config.json unreadable: %s" % type(e).__name__)
        return {}


CFG = _load()


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def shipping_table():
    """{province: fee} or None when absent/invalid."""
    t = CFG.get("shipping")
    if t is None:
        return None
    if not isinstance(t, dict) or not t:
        CONFIG_PROBLEMS.append("shipping: must be a non-empty object")
        return None
    out = {}
    for k, v in t.items():
        if not isinstance(k, str) or not k.strip() or not _num(v) or v <= 0 or v > 100000:
            CONFIG_PROBLEMS.append("shipping: bad entry %r" % (k,))
            return None
        out[k.strip()] = int(v) if float(v).is_integer() else float(v)
    return out


def get_int(name, default, lo=0, hi=1000000):
    v = CFG.get(name, default)
    if not _num(v) or isinstance(v, float) and not v.is_integer() or not (lo <= v <= hi):
        CONFIG_PROBLEMS.append("%s: invalid value %r" % (name, v))
        return default
    return int(v)


def get_str(name, default):
    v = CFG.get(name, default)
    if not isinstance(v, str) or not v.strip():
        CONFIG_PROBLEMS.append("%s: invalid value" % name)
        return default
    return v.strip()
