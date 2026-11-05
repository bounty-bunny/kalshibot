"""
Politics value bets (private).

Matches Kalshi politics/election markets to the same question on Polymarket
(automatically) and bets when Kalshi sells a side clearly cheaper than
Polymarket's price. Polymarket is a big, separate crowd, so when the two
disagree and Kalshi is cheaper, that's the deal.

Safety filters for automatic matching (a wrong match = betting on the wrong question):
  * strong wording overlap, and every number/year in one question must be in the other
  * opposite words must agree (not/no, above/below, before/after, more/less ...)
  * close dates within POLITICS_DATE_DAYS of each other
  * both sides' prices within POLITICS_MAX_GAP of each other (a huge gap usually = mismatch)
  * Polymarket market has real money in it (liquidity)
  * starts in shadow mode ("prove it first") until it has a profitable record
"""

import datetime as dt
import difflib
import json
import math
import re
import time

import requests

B = None
GAMMA = "https://gamma-api.polymarket.com"
SCAN_MIN = 15
KALSHI_CATEGORIES = {"politics", "elections"}
MIN_LIQUIDITY = 1000
MATCH_SCORE = 0.50
POLY_TAGS = ("politics", "elections", "us-politics", "geopolitics", "world")

VIEW = {"rows": [], "t": None, "note": "", "kalshi": 0, "poly": 0, "matched": 0}
_last = {"t": 0}
_cache = {"kalshi": [], "poly": [], "t": 0}

STOP = set("""will the a an be by in on of to for at is are was with as from and or this that who what which
win wins winner next be before end market question yes""".split())
OPPOSITES = [("not", "no", "fail", "fails", "without", "never"), ("above", "over", "more", "higher", "exceed", "exceeds"),
             ("below", "under", "less", "lower", "fewer"), ("before",), ("after",), ("lose", "loses", "lost"),
             ("resign", "resigns", "out", "leave", "leaves", "removed")]


def words(text):
    t = (text or "").lower().replace("’", "'")
    t = re.sub(r"[^a-z0-9.%$ ]", " ", t)
    return [w.strip(".") for w in t.split() if w.strip(".")]


def content(text):
    return {w for w in words(text) if w not in STOP and len(w) > 1}


def numbers(text):
    return set(re.findall(r"\d+(?:\.\d+)?", (text or "").replace(",", "")))


def numbers_clash(a, b):
    """True if the two questions mention different thresholds/counts (years alone don't count)."""
    na, nb = numbers(a), numbers(b)
    diff = (na ^ nb)
    diff = {n for n in diff if not re.fullmatch(r"20\d\d", n)}       # a year in only one wording is fine
    return bool(diff) and bool(na - {n for n in na if re.fullmatch(r"20\d\d", n)}) and bool(
        nb - {n for n in nb if re.fullmatch(r"20\d\d", n)})


def flags(ws):
    s = set(ws)
    return tuple(bool(s & set(group)) for group in OPPOSITES)


def match_score(ktext, ptext, anchor=""):
    """0..1 similarity. `anchor` = the Kalshi outcome name (e.g. a candidate) - it must appear in Polymarket's question."""
    kc, pc = content(ktext), content(ptext)
    if not kc or not pc:
        return 0.0
    if numbers_clash(ktext, ptext):
        return 0.0
    if flags(words(ktext)) != flags(words(ptext)):
        return 0.0
    ac = content(anchor)
    if ac and not ac <= pc:
        return 0.0
    jac = len(kc & pc) / len(kc | pc)
    cover = len(kc & pc) / min(len(kc), len(pc))                     # shorter question mostly inside the longer
    seq = difflib.SequenceMatcher(None, " ".join(sorted(kc)), " ".join(sorted(pc))).ratio()
    sc = 0.35 * jac + 0.4 * cover + 0.25 * seq
    if ac:
        sc += 0.1                                                     # named outcome matched exactly
    return min(1.0, sc)


def jl(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


# ---------------------------------------------------------------- loading

def load_kalshi():
    out, cursor = [], None
    for _ in range(25):                       # up to ~5,000 open events
        params = {"status": "open", "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        d = B.api("GET", "/events", params=params)
        for ev in d.get("events") or []:
            if (ev.get("category") or "").lower() not in KALSHI_CATEGORIES:
                continue
            for m in ev.get("markets") or []:
                if (m.get("status") or "active") not in ("active", "open"):
                    continue
                sub = m.get("yes_sub_title") or ""
                text = m.get("title") or ev.get("title") or ""
                if sub and sub.lower() not in text.lower():
                    text = f"{ev.get('title', '')} {sub}"
                close = m.get("expected_expiration_time") or m.get("close_time") or ev.get("close_time")
                out.append({"m": m, "event": ev.get("event_ticker"), "text": text.strip(), "anchor": sub,
                            "close": close})
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


def load_poly():
    out, seen = [], set()
    for tag in POLY_TAGS:
      for offset in range(0, 3000, 500):
        try:
            r = requests.get(f"{GAMMA}/events", params={"tag_slug": tag, "active": "true", "closed": "false",
                                                         "limit": 500, "offset": offset}, timeout=20)
            r.raise_for_status()
            evs = r.json()
        except Exception:
            break
        for ev in evs:
            for m in ev.get("markets") or []:
                if m.get("closed") or not m.get("active", True):
                    continue
                outs = jl(m.get("outcomes")) or []
                prices = jl(m.get("outcomePrices")) or []
                if [str(o).lower() for o in outs] != ["yes", "no"] or len(prices) != 2:
                    continue
                liq = B.fnum(m.get("liquidity") or m.get("liquidityNum"), 0) or 0
                bb, ba = B.fnum(m.get("bestBid")), B.fnum(m.get("bestAsk"))
                if bb is not None and ba is not None and 0 < bb < ba < 1 and ba - bb <= 0.05:
                    fair = (bb + ba) / 2
                else:
                    fair = B.fnum(prices[0])
                if fair is None or not 0 < fair < 1:
                    continue
                mid = m.get("id") or m.get("conditionId") or m.get("slug")
                if mid in seen:
                    continue
                seen.add(mid)
                out.append({"q": m.get("question") or ev.get("title") or "", "fair": fair, "liq": liq,
                            "end": m.get("endDate") or ev.get("endDate"), "slug": m.get("slug") or ev.get("slug"),
                            "spread": None if bb is None or ba is None else ba - bb})
        if len(evs) < 500:
            break
    return out


def close_days(a, b):
    try:
        return abs((B.parse_time(a) - B.parse_time(b)).total_seconds()) / 86400
    except Exception:
        return 999


NEAR = []
STATS = {"date": 0}


def match_all(kal, poly):
    """Best Polymarket question for each Kalshi market, one-to-one."""
    NEAR.clear()
    STATS["date"] = 0
    pairs = []
    by_word = {}
    for i, p in enumerate(poly):
        for w in content(p["q"]):
            by_word.setdefault(w, set()).add(i)
    for k in kal:
        cand = set()
        for w in content(k["text"]):
            if len(by_word.get(w, ())) < 300:
                cand |= by_word.get(w, set())
        best, best_sc = None, 0.0
        for i in cand:
            p = poly[i]
            if p["liq"] < MIN_LIQUIDITY:
                continue
            sc = match_score(k["text"], p["q"], k.get("anchor", ""))
            if sc <= 0:
                continue
            gap = close_days(k["close"], p["end"]) if p["end"] and k["close"] else 0
            if gap > B.POLITICS_DATE_DAYS:
                if gap > 4 * B.POLITICS_DATE_DAYS:
                    STATS["date"] += 1
                    continue
                sc -= 0.1                                    # dates a bit apart: needs stronger wording match
            if sc > best_sc:
                best, best_sc = i, sc
        if best is not None and best_sc >= MATCH_SCORE:
            pairs.append((best_sc, k, poly[best], best))
        elif best is not None and best_sc >= 0.35:
            NEAR.append((best_sc, k["text"][:90], poly[best]["q"][:90]))
    pairs.sort(key=lambda x: -x[0])
    used, final = set(), []
    for sc, k, p, i in pairs:
        if i in used:
            continue
        used.add(i)
        final.append((sc, k, p))
    return final


# --------------------------------------------------------------- deciding

def judge(sc, k, p, s):
    m = k["m"]
    q = B.quotes(m)
    fair = p["fair"]
    days = None
    try:
        days = (B.parse_time(k["close"]) - B.utcnow()).total_seconds() / 86400
    except Exception:
        pass
    row = {"ticker": m["ticker"], "event": k["event"], "kalshi": k["text"][:140], "poly": p["q"][:140],
           "slug": p["slug"], "score": round(sc, 2), "fair": fair, "yes_ask": q["yes_ask"], "no_ask": q["no_ask"],
           "days": None if days is None else round(days, 1), "liq": round(p["liq"]),
           "decision": "WAIT", "reason": "", "side": None, "price": None, "edge": None, "count": 0, "cost": 0.0,
           "pot": int(m.get("exchange_index") or 0), "close": k["close"]}
    try:
        import forecasts
        forecasts.note(m["ticker"], "politics", k["close"], fair, forecasts.mid(q), k["text"], k["event"])
    except Exception:
        pass

    def out(dec, why):
        row["decision"], row["reason"] = dec, why
        return row

    pos = s["open"].get(m["ticker"])
    if pos:
        return out("HOLDING", f"Holding {pos['count']} {pos['side'].upper()} at {pos['entry'] * 100:.0f}c.")
    if m["ticker"] in s["traded"]:
        import strategies
        if strategies.is_shadowed(m["ticker"]):
            return out("SHADOW", "Pretend bet placed - politics is proving itself before real money.")
        return out("DONE", "Already bet on this question.")
    if not B.POLITICS_ON or B.PAUSED:
        return out("PAUSED", "Politics betting is off or the bot is paused.")
    if days is not None and days > B.POLITICS_MAX_DAYS:
        return out("WAIT", f"Resolves in {days:.0f} days; only bets on questions ending within {B.POLITICS_MAX_DAYS:g} days "
                           f"(so money isn't tied up for months).")
    if q["yes_bid"] is not None and q["yes_ask"] is not None:
        kmid = (q["yes_bid"] + q["yes_ask"]) / 2
        if abs(kmid - fair) > B.POLITICS_MAX_GAP:
            return out("NO TRADE", f"Kalshi ({kmid * 100:.0f}%) and Polymarket ({fair * 100:.0f}%) are too far apart - "
                                   f"probably not the same question. Skipping to be safe.")
    best = None
    for side, ask, chance in (("yes", q["yes_ask"], fair), ("no", q["no_ask"], 1 - fair)):
        if ask is None:
            continue
        edge = chance - ask - B.fee_per_contract(ask)
        if best is None or edge > best[2]:
            best = (side, ask, edge, chance)
    if not best:
        return out("NO TRADE", "No Kalshi prices.")
    side, px, edge, chance = best
    row["side"], row["price"], row["edge"] = side, px, edge
    base = f"Polymarket says YES {fair * 100:.0f}% (${p['liq']:,.0f} liquidity). "
    if edge < B.POLITICS_MIN_EDGE:
        return out("NO TRADE", base + f"Best Kalshi deal: {side.upper()} at {px * 100:.0f}c vs fair {chance * 100:.0f}c "
                                      f"({edge * 100:+.0f}c; needs +{B.POLITICS_MIN_EDGE * 100:.0f}c).")
    if px < B.POLITICS_MIN_PRICE or px > B.POLITICS_MAX_PRICE:
        return out("NO TRADE", base + f"{side.upper()} is a deal at {px * 100:.0f}c but outside your price range.")
    import strategies
    stake, note = strategies.size("politics", chance, px, B.POLITICS_STAKE)
    per = px + B.fee_per_contract(px)
    cash = B.LIVE.get("balance")
    cash_room = 1e9 if cash is None else cash * 0.98 - 0.02
    loss_room = B.DAILY_LOSS_LIMIT + s["realized"] - B.open_cost(s, "all")
    open_pol = sum(x["cost"] for x in s["open"].values() if x.get("kind") == "politics")
    budget = min(stake, B.POLITICS_MAX_OPEN - open_pol, loss_room, cash_room)
    count = math.floor(budget / per + 1e-9)
    if count < 1:
        why = "edge sizing says this deal is worth less than one contract" if stake < per else "no room inside your limits"
        return out("NO TRADE", base + f"Deal on {side.upper()} at {px * 100:.0f}c (+{edge * 100:.0f}c), but {why}.")
    row["count"], row["cost"] = count, round(count * per, 2)
    return out("BUY", base + f"Kalshi sells {side.upper()} at {px * 100:.0f}c; fair is {chance * 100:.0f}c "
                             f"(+{edge * 100:.0f}c after fees). Buy {count} = ${row['cost']:.2f}{note}.")


def ai_ok(row):
    import council
    if not (B.COUNCIL_ON and B.COUNCIL_POLITICS and council.key_set()):
        return False
    if row["decision"] == "BUY":
        return True
    return (row.get("edge") is not None and row["edge"] >= B.POLITICS_MIN_EDGE / 2 and row.get("price")
            and B.POLITICS_MIN_PRICE <= row["price"] <= B.POLITICS_MAX_PRICE
            and (row.get("days") is None or row["days"] <= B.POLITICS_MAX_DAYS) and "too far apart" not in row["reason"])


def ai_review(row, s):
    import council, strategies
    side, px = row["side"], row["price"]
    details = {"Polymarket's price for YES (same question)": f"{row['fair'] * 100:.1f}%",
               "Polymarket question": row["poly"], "Polymarket money in market": f"${row['liq']:,}",
               "Kalshi YES ask": f"{(row['yes_ask'] or 0) * 100:.0f}c", "Kalshi NO ask": f"{(row['no_ask'] or 0) * 100:.0f}c",
               "Days until it resolves": row["days"], "Match confidence between the two questions": f"{row['score'] * 100:.0f}%"}
    v = council.review("politics", row["ticker"], row["kalshi"], side, px, row["fair"], details, research=True,
                       min_edge=B.POLITICS_MIN_EDGE, yes_ask=row.get("yes_ask"), no_ask=row.get("no_ask"))
    if not v or not v.get("ok"):
        return row
    tag = f" AI team: {v['decision']} ({v['votes_for']}/5 lean {side.upper()}). {v['summary']}"
    if not v["approve"]:
        row["decision"], row["reason"] = "NO TRADE", row["reason"] + tag
        return row
    chance = v["prob_yes"] if side == "yes" else 1 - v["prob_yes"]
    edge = chance - px - B.fee_per_contract(px)
    if row["decision"] != "BUY" and edge < B.POLITICS_MIN_EDGE:
        row["reason"] += tag + f" Team's odds give only {edge * 100:+.0f}c - still not enough."
        return row
    per = px + B.fee_per_contract(px)
    stake, note = strategies.size("politics", chance, px, B.POLITICS_STAKE)
    cash = B.LIVE.get("balance")
    cash_room = 1e9 if cash is None else cash * 0.98 - 0.02
    loss_room = B.DAILY_LOSS_LIMIT + s["realized"] - B.open_cost(s, "all")
    open_pol = sum(x["cost"] for x in s["open"].values() if x.get("kind") == "politics")
    count = int(min(stake * v["mult"], B.POLITICS_STAKE, B.POLITICS_MAX_OPEN - open_pol, loss_room, cash_room) // per)
    if count < 1:
        row["decision"], row["reason"] = "NO TRADE", row["reason"] + tag + " (no room inside your limits)"
        return row
    row["count"], row["cost"], row["decision"] = count, round(count * per, 2), "BUY"
    row["reason"] += tag + f" Size {v['size']}: {count} = ${row['cost']:.2f}{note}."
    return row


def place(row, s):
    import strategies
    ticker, side, px, count = row["ticker"], row["side"], row["price"], row["count"]
    try:
        lq = B.live_quotes(ticker)
        if lq:
            live_px = lq["yes_ask"] if side == "yes" else lq["no_ask"]
            if live_px is None or live_px > px + 0.005:
                B.log(f"Politics {ticker}: price moved before buying - skipped.")
                return
    except Exception:
        pass
    mode = strategies.route("politics", ticker)
    with B.LOCK:
        s["traded"].append(ticker)
    B.save_state(s)
    title = f"Politics: {row['kalshi'][:80]}"
    if mode != "live":
        strategies.shadow_buy("politics", ticker, side, px, count, row["cost"], event=row["event"],
                              check_after=row["close"], title=title)
        return
    B.buy_real(ticker, side, px, count, row["cost"], row["pot"], "politics", "politics",
               {"event": row["event"], "close_time": row["close"], "title": title}, row["reason"], s)


def tick(s):
    if not B.POLITICS_ON:
        return
    if time.time() - _last["t"] < SCAN_MIN * 60:
        return
    _last["t"] = time.time()
    try:
        kal = load_kalshi()
        poly = load_poly()
    except Exception as e:
        VIEW["note"] = f"Couldn't load markets: {e}"
        return
    pairs = match_all(kal, poly)
    rows = []
    for sc, k, p in pairs:
        row = judge(sc, k, p, s)
        rows.append(row)
        if ai_ok(row) and row["decision"] in ("BUY", "NO TRADE"):
            row = ai_review(row, s)
        if row["decision"] == "BUY":
            place(row, s)
    order = {"BUY": 0, "HOLDING": 1, "NO TRADE": 2, "WAIT": 3, "DONE": 4, "PAUSED": 5}
    rows.sort(key=lambda r: (order.get(r["decision"], 9), -(r["edge"] or -1)))
    VIEW.update(rows=rows[:60], t=time.strftime("%H:%M"), note="", kalshi=len(kal), poly=len(poly), matched=len(pairs))
    B.log(f"Politics: {len(kal)} Kalshi questions, {len(poly)} Polymarket questions, {len(pairs)} matched.")
    if len(pairs) < 3:
        for sc, kt, pq in sorted(NEAR, reverse=True)[:5]:
            B.log(f"   near miss ({sc:.2f}): Kalshi '{kt}' vs Polymarket '{pq}'")
        if not NEAR:
            liq = sum(1 for p in poly if p["liq"] >= MIN_LIQUIDITY)
            B.log(f"   no near misses. Polymarket questions with enough money: {liq}; skipped for dates far apart: "
                  f"{STATS['date']}.")
            for k in kal[:3]:
                B.log(f"   sample Kalshi: '{k['text'][:90]}' (sub '{k.get('anchor', '')}', ends {str(k['close'])[:10]})")
            for p in poly[:3]:
                B.log(f"   sample Polymarket: '{p['q'][:90]}' (ends {str(p['end'])[:10]}, ${p['liq']:,.0f})")


def status():
    return {"on": B.POLITICS_ON, **VIEW}


def check(s):
    kal, poly = load_kalshi(), load_poly()
    pairs = match_all(kal, poly)
    B.log(f"Politics: {len(kal)} Kalshi questions, {len(poly)} Polymarket questions, {len(pairs)} matched. Top matches:")
    for sc, k, p in pairs[:10]:
        row = judge(sc, k, p, s)
        B.log(f"   [{sc:.2f}] Kalshi: {k['text'][:90]}")
        B.log(f"          Poly:   {p['q'][:90]}")
        B.log(f"          would: {row['decision']} - {row['reason'][:180]}")
