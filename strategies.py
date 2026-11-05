"""
Strategy scoreboard: makes the bot correct itself.

Every bet belongs to a strategy ("crypto", "longshot", "sports:nfl", "politics", ...).
Each strategy is in one of three modes:

  shadow  - "prove it first": the bot only pretends to bet (no money) and tracks
            what it would have won. It switches itself to live only after enough
            finished pretend bets (SHADOW_MIN_BETS, more for long shots) with a profit
            that is statistically convincing (t-score >= PROMOTE_T), not just lucky.
  live    - real money. After LIVE_MIN_BETS finished bets it adjusts itself:
            losing more than PAUSE_ROI -> auto-paused; winning more than BOOST_ROI
            -> bets 1.5x; in between -> normal; slightly negative -> 0.5x.
  paused  - no bets (auto or by you). Shadow tracking continues so you can see
            whether it would have recovered.

Bet size by edge: with SIZING = "edge", each bet is a cautious fraction (KELLY_FRACTION)
of the Kelly amount for that edge, so great deals get bigger bets and thin ones tiny bets,
never more than your per-bet cap.
"""

import json
import os
import time

B = None

SHADOW_MIN_BETS = 40     # 20 was too few to tell skill from luck
LIVE_MIN_BETS = 20       # (pausing a loser early is cheap insurance, so this can stay lower)
PROMOTE_T = 1.0          # pretend profit must be ~1 standard error above zero to go live
PAUSE_ROI = -0.10        # live strategy losing more than 10% of what it staked -> pause
SOFT_ROI = -0.03         # slightly negative -> half size
BOOST_ROI = 0.05         # winning more than 5% -> 1.5x size
ALREADY_LIVE = {"crypto", "lateshot", "sports_late"}   # late shots stay live but are capped by their $ test budget
# long shots now prove themselves first: cheap contracts are often overpriced ("favorite-longshot bias")
MIN_BETS_FOR = {"longshot": 100, "lateshot": 150, "sports_late": 150}   # long shots need many bets before judging

LABELS = {"crypto": "Crypto value bets", "longshot": "Crypto long shots", "politics": "Politics",
          "lateshot": "Crypto late shots (40x+)", "sports_late": "Sports late shots (40x+)"}

_state = None


def _file():
    return os.path.join(B.HERE, "strategies.json")


def _load():
    global _state
    if _state is None:
        try:
            with open(_file(), encoding="utf-8") as f:
                _state = json.load(f)
        except (OSError, ValueError):
            _state = {}
        _state.setdefault("strategies", {})
        _state.setdefault("virtual", {})
        if not _state.get("v2"):                      # one-time: long shots must now prove themselves
            g = _state["strategies"].get("longshot")
            if g and g.get("mode") == "live":
                g["mode"], g["auto"] = "shadow", True
                B.log(f"Long shots moved to PROVING (pretend bets): real record {g['live'].get('bets', 0)} bets, "
                      f"net ${g['live'].get('net', 0):+.2f}. They go live again only after "
                      f"{MIN_BETS_FOR['longshot']} pretend bets with a convincing profit.")
            _state["v2"] = True
    return _state


def _save():
    tmp = _file() + ".tmp"
    with B.LOCK:
        data = json.dumps(_state, indent=1)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, _file())


def label(key):
    if key in LABELS:
        return LABELS[key]
    if key.startswith("sports:"):
        try:
            import sports
            return "Sports: " + sports.SPORTS[key.split(":", 1)[1]][0]
        except Exception:
            return "Sports: " + key.split(":", 1)[1].upper()
    return key


def _blank():
    return {"bets": 0, "wins": 0, "net": 0.0, "staked": 0.0, "rn": 0, "rsum": 0.0, "rsq": 0.0}


def t_score(rec):
    """How sure we are the return per $ is above zero (mean / standard error). None if too few."""
    n = rec.get("rn", 0)
    if n < 10:
        return None
    mean = rec["rsum"] / n
    var = max(rec["rsq"] / n - mean * mean, 1e-9) * n / (n - 1)
    return mean / (var / n) ** 0.5


def get(key):
    st = _load()["strategies"]
    if key not in st:
        st[key] = {"mode": "live" if key in ALREADY_LIVE else "shadow", "auto": False,
                   "live": _blank(), "shadow": _blank(), "since": time.strftime("%Y-%m-%d")}
    return st[key]


def mode(key):
    return get(key)["mode"]


def set_mode(key, new_mode):
    if new_mode not in ("live", "shadow", "paused"):
        return False
    g = get(key)
    g["mode"], g["auto"] = new_mode, False
    _save()
    B.log(f"Strategy '{label(key)}' set to {new_mode.upper()} from the dashboard.")
    return True


def roi(rec):
    return rec["net"] / rec["staked"] if rec["staked"] > 0 else 0.0


def multiplier(key):
    g = get(key)
    rec = g["live"]
    if g["mode"] != "live" or rec["bets"] < MIN_BETS_FOR.get(key, LIVE_MIN_BETS):
        return 1.0
    r = roi(rec)
    if r >= BOOST_ROI:
        return 1.5
    if r < SOFT_ROI:
        return 0.5
    return 1.0


def size(key, prob, price, cap):
    """Dollars to put on one bet. Returns (stake, note)."""
    per = price + B.fee_per_contract(price)
    mult = multiplier(key)
    if getattr(B, "SIZING", "fixed") != "edge" or prob is None:
        return cap * mult, ("" if mult == 1 else f" (x{mult:g} from track record)")
    f = max(0.0, (prob - per) / (1 - per)) if per < 1 else 0.0
    bank = B.LIVE.get("balance")
    if bank is None:
        bank = cap * 4
    stake = min(cap, B.KELLY_FRACTION * f * bank) * mult
    note = f" (edge sizing: {B.KELLY_FRACTION * f * 100:.1f}% of ${bank:.0f} cash"
    note += f", x{mult:g} from track record)" if mult != 1 else ")"
    return stake, note


def record(key, pnl, staked, won, shadow=False):
    g = get(key)
    rec = g["shadow" if shadow else "live"]
    rec["bets"] += 1
    rec["wins"] += 1 if won else 0
    rec["net"] = round(rec["net"] + pnl, 2)
    rec["staked"] = round(rec["staked"] + staked, 2)
    if staked > 0:
        r = pnl / staked
        rec["rn"] = rec.get("rn", 0) + 1
        rec["rsum"] = rec.get("rsum", 0.0) + r
        rec["rsq"] = rec.get("rsq", 0.0) + r * r
    name = label(key)
    if not shadow and g["mode"] == "live" and rec["bets"] >= MIN_BETS_FOR.get(key, LIVE_MIN_BETS) and roi(rec) < PAUSE_ROI:
        g["mode"], g["auto"] = "paused", True
        B.log(f"AUTO-PAUSED '{name}': {rec['bets']} real bets, net ${rec['net']:+.2f} "
              f"({roi(rec) * 100:+.0f}% of staked). It keeps shadow-tracking; resume it on the dashboard.")
    need = max(SHADOW_MIN_BETS, MIN_BETS_FOR.get(key, 0))
    t = t_score(rec)
    if shadow and g["mode"] == "shadow" and rec["bets"] >= need and rec["net"] > 0 and (t is None or t >= PROMOTE_T):
        g["mode"], g["auto"] = "live", True
        g["live"] = _blank()
        B.log(f"PROMOTED '{name}' to real money: {rec['bets']} pretend bets made ${rec['net']:+.2f} "
              f"({roi(rec) * 100:+.0f}%, t-score {'?' if t is None else format(t, '.1f')}).")
    _save()


# ------------------------------------------------------------ shadow bets

def shadow_buy(key, ticker, side, price, count, cost, event=None, check_after=None, title=""):
    v = _load()["virtual"]
    v[ticker] = {"key": key, "side": side, "count": count, "entry": price, "cost": round(cost, 2),
                 "event": event, "check_after": check_after, "title": title, "t": time.time(), "_checked": 0}
    _save()
    B.log(f"SHADOW {ticker}: pretend bet {count} {side.upper()} @ {price:.2f} = ${cost:.2f} "
          f"for '{label(key)}' (no real money until it proves itself).")


def is_shadowed(ticker):
    return ticker in _load()["virtual"]


def settle_virtual():
    v = _load()["virtual"]
    now = B.utcnow()
    for ticker, p in list(v.items()):
        if p.get("check_after") and now < B.parse_time(p["check_after"]):
            continue
        gap = 60 if p["key"] in ("crypto", "longshot", "lateshot") else 600
        if time.time() - p.get("_checked", 0) < gap:
            continue
        p["_checked"] = time.time()
        res = B.lookup_result(ticker, p.get("event"))
        if res not in ("yes", "no"):
            if time.time() - p["t"] > 120 * 86400:      # give up after 120 days
                del v[ticker]
            continue
        won = res == p["side"]
        pnl = (p["count"] if won else 0) - p["cost"]
        del v[ticker]
        B.log(f"SHADOW {'WIN ' if won else 'LOSS'} {ticker}: pretend P&L ${pnl:+.2f} for '{label(p['key'])}'")
        record(p["key"], pnl, p["cost"], won, shadow=True)
    _save()


def status():
    st = _load()
    rows = []
    for key, g in st["strategies"].items():
        rows.append({"key": key, "label": label(key), "mode": g["mode"], "auto": g.get("auto", False),
                     "live": {**g["live"], "roi": roi(g["live"]), "t": t_score(g["live"])},
                     "shadow": {**g["shadow"], "roi": roi(g["shadow"]), "t": t_score(g["shadow"])},
                     "need": max(SHADOW_MIN_BETS, MIN_BETS_FOR.get(key, 0)),
                     "mult": multiplier(key)})
    rows.sort(key=lambda r: ({"live": 0, "shadow": 1, "paused": 2}[r["mode"]], r["label"]))
    open_virtual = [{"ticker": t, "label": label(p["key"]), "side": p["side"], "cost": p["cost"], "title": p.get("title", "")}
                    for t, p in st["virtual"].items()]
    return {"rows": rows, "virtual": open_virtual[-40:], "rules": {
        "shadow_min": SHADOW_MIN_BETS, "live_min": LIVE_MIN_BETS, "promote_t": PROMOTE_T, "pause_roi": PAUSE_ROI, "boost_roi": BOOST_ROI}}


def route(key, ticker):
    """Before a real bet: returns 'live', 'shadow' or 'paused'."""
    return mode(key)
