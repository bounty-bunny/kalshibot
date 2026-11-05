"""
Forecast scorecard: is the bot actually smarter than the market?

Every market the bot looks at gets ONE snapshot of each forecaster's chance of YES, whether or not it bets:
  math    - the bot's own numbers (crypto price model / sportsbook fair odds / Polymarket price)
  market  - Kalshi's own price (middle of bid and ask)
  claude, gpt, ai - the AI team, when it debated that market
Once Kalshi settles the market, each forecast is scored with the Brier score
(average of (forecast - outcome)^2: 0 = perfect, 0.25 = coin flip; LOWER is better).

What it's used for:
  * the dashboard's "Who predicts best?" table and the calibration table
    ("when the bot says 30%, does it happen ~30% of the time?");
  * the AI team's weight: if the AI's forecasts beat the bot's math on the same markets,
    its say in the blended odds grows; if they're worse, it shrinks.
The point: a strategy only has an edge if its forecasts beat the market's price, measured on lots of markets.
"""

import json
import os
import threading
import time

B = None
_lock = threading.Lock()
_state = None
MAX_KEEP = 4000            # settled forecasts kept (oldest dropped)
MIN_FOR_WEIGHT = 30        # settled head-to-head forecasts before the AI weight changes
SOURCES = ["math", "market", "ai", "claude", "gpt"]
NAMES = {"math": "Bot's math", "market": "Kalshi price", "ai": "AI team (combined)", "claude": "Claude", "gpt": "GPT"}


def _file():
    return os.path.join(B.HERE, "forecasts.json")


def _load():
    global _state
    if _state is None:
        try:
            with open(_file(), encoding="utf-8") as f:
                _state = json.load(f)
        except (OSError, ValueError):
            _state = {}
        _state.setdefault("open", {})
        _state.setdefault("done", [])
    return _state


def save():
    st = _load()
    with _lock:
        data = json.dumps(st)
    tmp = _file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, _file())


def mid(q):
    yb, ya = q.get("yes_bid"), q.get("yes_ask")
    if yb is not None and ya is not None:
        return (yb + ya) / 2
    return ya if ya is not None else yb


def note(ticker, cat, check_after, math_p=None, market_p=None, title="", event=None):
    """First snapshot for a market (later calls for the same market are ignored)."""
    st = _load()
    with _lock:
        if ticker in st["open"] or math_p is None and market_p is None:
            return
        st["open"][ticker] = {"cat": cat, "after": check_after, "title": title[:120], "event": event, "t": time.time(),
                              "p": {k: round(v, 4) for k, v in (("math", math_p), ("market", market_p)) if v is not None},
                              "_checked": 0}
        st["_dirty"] = True


def add(ticker, probs):
    """Add AI forecasts for a market that already has a snapshot."""
    st = _load()
    with _lock:
        rec = st["open"].get(ticker)
        if rec:
            for k, v in probs.items():
                if v is not None and k not in rec["p"]:
                    rec["p"][k] = round(v, 4)
            st["_dirty"] = True


def settle(max_lookups=15):
    """Look up results for finished markets (a few per call, so we don't flood Kalshi)."""
    st = _load()
    now = B.utcnow()
    done_now = 0
    for ticker, rec in list(st["open"].items()):
        if max_lookups <= 0:
            break
        try:
            if rec.get("after") and now < B.parse_time(rec["after"]):
                continue
        except Exception:
            pass
        gap = 90 if rec["cat"] == "crypto" else 900
        if time.time() - rec.get("_checked", 0) < gap:
            continue
        rec["_checked"] = time.time()
        max_lookups -= 1
        res = B.lookup_result(ticker, rec.get("event"))
        if res not in ("yes", "no"):
            if time.time() - rec["t"] > 120 * 86400:
                with _lock:
                    st["open"].pop(ticker, None)
            continue
        with _lock:
            st["open"].pop(ticker, None)
            st["done"].append({"tk": ticker, "cat": rec["cat"], "y": 1 if res == "yes" else 0, "p": rec["p"],
                               "d": time.strftime("%Y-%m-%d")})
            del st["done"][:-MAX_KEEP]
        done_now += 1
    if done_now or st.pop("_dirty", False):
        save()


def _brier(rows, src):
    xs = [(r["p"][src] - r["y"]) ** 2 for r in rows if src in r["p"]]
    return (sum(xs) / len(xs), len(xs)) if xs else (None, 0)


def head_to_head(rows, a, b):
    """Brier of a and b on the SAME markets (fair comparison). Returns (brier_a, brier_b, n)."""
    both = [r for r in rows if a in r["p"] and b in r["p"]]
    if not both:
        return None, None, 0
    return (sum((r["p"][a] - r["y"]) ** 2 for r in both) / len(both),
            sum((r["p"][b] - r["y"]) ** 2 for r in both) / len(both), len(both))


def ai_weight(cat, base):
    """AI's share of the blended odds: grows if it beats the math head-to-head, shrinks if it's worse."""
    rows = [r for r in _load()["done"] if r["cat"] == cat]
    ba, bm, n = head_to_head(rows, "ai", "math")
    if n < MIN_FOR_WEIGHT:
        return base
    if ba < bm - 0.005:
        return min(0.6, base * 1.5)
    if ba > bm + 0.005:
        return max(0.05, base * 0.5)
    return base


def status():
    st = _load()
    done = st["done"]
    cats = []
    for cat in ("crypto", "sports", "politics"):
        rows = [r for r in done if r["cat"] == cat]
        if not rows:
            continue
        srcs = []
        for src in SOURCES:
            b, n = _brier(rows, src)
            if not n:
                continue
            bs, bmkt, nn = head_to_head(rows, src, "market") if src != "market" else (b, b, n)
            srcs.append({"src": src, "name": NAMES[src], "n": n, "brier": round(b, 4),
                         "vs_market": None if src == "market" or not nn else round(bmkt - bs, 4), "vs_n": nn})
        cal = []                                  # calibration of the bot's math
        for lo in range(0, 100, 10):
            bucket = [r for r in rows if "math" in r["p"] and lo / 100 <= r["p"]["math"] < (lo + 10) / 100 + (1e-9 if lo == 90 else 0)]
            if bucket:
                cal.append({"range": f"{lo}-{lo + 10}%", "n": len(bucket),
                            "said": round(sum(r["p"]["math"] for r in bucket) / len(bucket), 3),
                            "happened": round(sum(r["y"] for r in bucket) / len(bucket), 3)})
        cats.append({"cat": cat, "settled": len(rows), "sources": srcs, "calibration": cal,
                     "ai_weight": None})
    waiting = len(st["open"])
    return {"cats": cats, "waiting": waiting, "total": len(done)}
