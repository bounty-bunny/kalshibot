"""
Sports value bets for the Kalshi bot (private).

How it decides
  * Kalshi game-winner markets (NFL, college football, NBA, college basketball,
    NHL, MLB, Premier League, Champions League, MLS) are matched to the same
    game at the big sportsbooks via The Odds API.
  * Each sportsbook's moneyline is turned into fair chances (the bookmaker's
    margin removed) and averaged across books: that is the "fair" win chance.
  * In the last few hours before kick-off, if Kalshi sells a side (team YES,
    team NO, or the draw) for at least SPORTS_MIN_EDGE less than that fair
    chance after Kalshi's fee, the bot buys it. One bet per game, singles only.

Odds credits (free plan = 500 a month)
  * The schedule (/events) and season list (/sports) are free. Odds cost 1
    credit per sport per check. The bot only buys odds for a sport when one of
    its games starts soon and the odds it has are getting old, and it spreads
    the remaining credits evenly over the rest of the month.
"""

import calendar
import datetime as dt
import difflib
import math
import re
import time

import requests

B = None                      # the main bot module (set by bot.py)

ODDS_BASE = "https://api.the-odds-api.com/v4"

# key: (label, Odds API sport key, known Kalshi series, words to find more series)
SPORTS = {
    "nfl":   ("NFL", "americanfootball_nfl", ["KXNFLGAME"], ["nfl", "pro football"]),
    "ncaaf": ("College football", "americanfootball_ncaaf", ["KXNCAAFGAME"], ["college football", "ncaaf", "ncaa football"]),
    "nba":   ("NBA", "basketball_nba", ["KXNBAGAME"], ["nba", "pro basketball"]),
    "ncaab": ("College basketball", "basketball_ncaab", ["KXNCAAMBGAME"], ["men's college basketball", "college basketball", "ncaab", "ncaa men"]),
    "nhl":   ("NHL", "icehockey_nhl", ["KXNHLGAME"], ["nhl", "hockey"]),
    "mlb":   ("MLB", "baseball_mlb", ["KXMLBGAME"], ["mlb", "baseball"]),
    "epl":   ("Premier League", "soccer_epl", ["KXEPLGAME"], ["premier league", "epl"]),
    "ucl":   ("Champions League", "soccer_uefa_champs_league", ["KXUCLGAME"], ["champions league"]),
    "mls":   ("MLS", "soccer_usa_mls", ["KXMLSGAME"], ["mls", "major league soccer"]),
}
SOCCER = {"epl", "ucl", "mls"}

KALSHI_SCAN_SEC = 60          # how often to read Kalshi's game prices
SCHEDULE_REFRESH_MIN = 180    # free schedule refresh
SEASON_REFRESH_MIN = 720      # free in-season list refresh
ODDS_MIN_GAP_MIN = 20         # never buy the same sport's odds more often than this

SERIES = {k: list(v[2]) for k, v in SPORTS.items()}
ODDS = {}                     # sport -> {"t": time, "events": {id: event}}
SCHED = {}                    # sport -> {"t": time, "events": [...]}
ACTIVE = {"t": 0, "keys": None}
CREDITS = {"remaining": None, "used": None, "day": None, "today": 0, "last_error": None}
VIEW = {"games": [], "t": None, "note": ""}
_last_scan = {"t": 0}


# ------------------------------------------------------------------ helpers

def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def parse_iso(s):
    return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def norm(name):
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b(fc|cf|afc|sc|the|club|de|ac)\b", " ", s)
    s = re.sub(r"\bst\b", "state", s)
    return re.sub(r"\s+", " ", s).strip()


def similar(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    if ta and (ta <= tb or tb <= ta):          # "detroit" vs "detroit lions"
        return 0.9
    return difflib.SequenceMatcher(None, a, b).ratio()


def is_draw(name):
    return norm(name) in ("draw", "tie", "tie game", "draw game")


def odds_key_set():
    k = getattr(B, "ODDS_API_KEY", "") or ""
    return bool(k) and "PASTE" not in k


def odds_get(path, params=None, costs=True):
    p = {"apiKey": B.ODDS_API_KEY, **(params or {})}
    r = requests.get(ODDS_BASE + path, params=p, timeout=20)
    rem, used = r.headers.get("x-requests-remaining"), r.headers.get("x-requests-used")
    if rem is not None:
        CREDITS["remaining"] = int(float(rem))
    if used is not None:
        CREDITS["used"] = int(float(used))
    if r.status_code == 401:
        CREDITS["last_error"] = "Odds API key rejected or out of credits"
        raise RuntimeError(CREDITS["last_error"])
    if r.status_code == 429:
        CREDITS["last_error"] = "Odds API says too many requests"
        raise RuntimeError(CREDITS["last_error"])
    r.raise_for_status()
    if costs:
        today = dt.date.today().isoformat()
        if CREDITS["day"] != today:
            CREDITS["day"], CREDITS["today"] = today, 0
        CREDITS["today"] += 1
    CREDITS["last_error"] = None
    return r.json()


def credits_allowed_today():
    """Spread the remaining credits evenly over the rest of the month."""
    rem = CREDITS["remaining"]
    if rem is None:
        return 3
    today = dt.date.today()
    days_left = calendar.monthrange(today.year, today.month)[1] - today.day + 1
    return max(1, int(rem // days_left)) if rem > 0 else 0


def credits_used_today():
    return CREDITS["today"] if CREDITS["day"] == dt.date.today().isoformat() else 0


# ------------------------------------------------------------ Kalshi side

def discover_series():
    try:
        data = B.api("GET", "/series", params={"category": "Sports"})
    except Exception as e:
        B.log(f"Sports: couldn't load Kalshi's sports list ({e}); using built-in list.")
        return
    added = 0
    for ser in data.get("series") or []:
        t = (ser.get("ticker") or "").upper()
        title = (ser.get("title") or "").lower()
        if not t.endswith("GAME") or "women" in title or "wnba" in title:
            continue
        for key, (_, _, known, words) in SPORTS.items():
            if t in SERIES[key]:
                break
            if any(w in title for w in words):
                SERIES[key].append(t)
                added += 1
                break
    if added:
        B.log(f"Sports: found {added} more game series on Kalshi.")


def ticker_date(event_ticker):
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker or "")
    if not m:
        return None
    try:
        return dt.datetime.strptime(f"{m.group(1)}{m.group(2)}{m.group(3)}", "%y%b%d").date()
    except ValueError:
        return None


def kalshi_games(sport):
    games = []
    for series in SERIES[sport]:
        try:
            data = B.api("GET", "/events", params={"series_ticker": series, "status": "open",
                                                    "with_nested_markets": "true", "limit": 200})
        except B.ApiError as e:
            if e.status == 404:
                continue
            raise
        for ev in data.get("events") or []:
            mkts = [m for m in ev.get("markets") or [] if (m.get("status") or "active") in ("active", "open", "")]
            if mkts:
                ev["markets"] = mkts
                ev["_series"] = series
                games.append(ev)
    return games


def market_team(m):
    for k in ("yes_sub_title", "subtitle", "no_sub_title"):
        if m.get(k):
            return m[k]
    t = m.get("title") or ""
    hit = re.search(r"(?i)will (?:the )?(.+?) win", t)
    return hit.group(1) if hit else ""


# --------------------------------------------------------------- odds side

def refresh_active():
    if ACTIVE["keys"] is not None and time.time() - ACTIVE["t"] < SEASON_REFRESH_MIN * 60:
        return
    data = odds_get("/sports", costs=False)
    ACTIVE["keys"] = {s["key"] for s in data if s.get("active")}
    ACTIVE["t"] = time.time()


def refresh_schedule(sport):
    c = SCHED.get(sport)
    if c and time.time() - c["t"] < SCHEDULE_REFRESH_MIN * 60:
        return
    data = odds_get(f"/sports/{SPORTS[sport][1]}/events", costs=False)
    SCHED[sport] = {"t": time.time(), "events": data}


def refresh_odds(sport):
    data = odds_get(f"/sports/{SPORTS[sport][1]}/odds",
                    params={"regions": "us", "markets": "h2h", "oddsFormat": "decimal"})
    ODDS[sport] = {"t": time.time(), "events": {e["id"]: e for e in data}}
    B.log(f"Sports: got fresh {SPORTS[sport][0]} odds for {len(data)} games "
          f"({CREDITS['remaining']} odds credits left this month).")


def fair_chances(ev, fresh_within=None, min_books=None):
    """Average no-margin win chances across sportsbooks. Returns (dict name->chance, books).
    fresh_within (seconds): for live games, only use books that updated their price recently."""
    books = []
    for bk in ev.get("bookmakers") or []:
        if fresh_within:
            try:
                if (now_utc() - parse_iso(bk.get("last_update"))).total_seconds() > fresh_within:
                    continue
            except Exception:
                continue
        for mk in bk.get("markets") or []:
            if mk.get("key") != "h2h":
                continue
            imp = {o["name"]: 1.0 / o["price"] for o in mk.get("outcomes") or [] if (o.get("price") or 0) > 1}
            tot = sum(imp.values())
            if len(imp) >= 2 and tot > 0:
                books.append({k: v / tot for k, v in imp.items()})
    if not books:
        return None, 0
    sizes = {}
    for b in books:
        sizes[frozenset(b)] = sizes.get(frozenset(b), 0) + 1
    names = max(sizes, key=sizes.get)
    books = [b for b in books if frozenset(b) == names]
    if len(books) < (min_books or B.SPORTS_MIN_BOOKS):
        return None, len(books)
    return {n: sum(b[n] for b in books) / len(books) for n in names}, len(books)


def match_game(kev, candidates):
    """Find the sportsbook event for a Kalshi game. Returns (event, {kalshi ticker: outcome name})."""
    kday = ticker_date(kev.get("event_ticker"))
    teams = [(m["ticker"], market_team(m)) for m in kev["markets"]]
    best, best_score, best_map = None, 0.0, None
    for ev in candidates:
        if kday:
            try:
                eday = parse_iso(ev["commence_time"]).astimezone(dt.timezone(dt.timedelta(hours=-5))).date()
            except Exception:
                continue
            if abs((eday - kday).days) > 1:
                continue
        names = [ev.get("home_team"), ev.get("away_team")]
        mapping, scores = {}, []
        for tk, team in teams:
            if is_draw(team):
                mapping[tk] = "Draw"
                continue
            sims = [(similar(team, n), n) for n in names]
            sc, n = max(sims)
            mapping[tk] = n
            scores.append(sc)
        real = [mapping[tk] for tk, team in teams if not is_draw(team)]
        if len(scores) < 2 or len(set(real)) != len(real):
            # single-market games: also accept when the event title names both teams
            title = kev.get("title") or ""
            if len(scores) == 1 and all(similar(n, title) > 0.3 or norm(n).split()[-1] in norm(title) for n in names):
                scores.append(scores[0])
            else:
                continue
        sc = min(scores)
        if sc > best_score:
            best, best_score, best_map = ev, sc, mapping
    if best is None or best_score < 0.72:
        return None, None
    return best, best_map


# --------------------------------------------------------------- decisions

def open_sports_cost(s):
    return sum(p["cost"] for p in s["open"].values() if p.get("kind") == "sports")


def judge(sport, kev, mapping, ev, s):
    """Decide about one Kalshi game. Returns a list of per-market rows for the dashboard (best first)."""
    label = SPORTS[sport][0]
    start = parse_iso(ev["commence_time"])
    hours = (start - now_utc()).total_seconds() / 3600
    od = ODDS.get(sport)
    oev = (od or {}).get("events", {}).get(ev["id"]) if od else None
    age_min = (time.time() - od["t"]) / 60 if od else None
    fair, nbooks = fair_chances(oev) if oev else (None, 0)
    rows = []
    event_ticker = kev.get("event_ticker")
    for m in kev["markets"]:
        q = B.quotes(m)
        name = mapping.get(m["ticker"])
        row = {"sport": label, "game": f"{ev.get('away_team')} at {ev.get('home_team')}",
               "start": start.isoformat(), "hours": round(hours, 2), "ticker": m["ticker"],
               "event": event_ticker, "team": "Draw" if name == "Draw" else name,
               "yes_ask": q["yes_ask"], "no_ask": q["no_ask"], "yes_bid": q["yes_bid"], "no_bid": q["no_bid"],
               "fair": None if not fair else fair.get(name), "books": nbooks,
               "odds_age": None if age_min is None else round(age_min), "pot": int(m.get("exchange_index") or kev.get("exchange_index") or 0),
               "decision": "WAIT", "reason": "", "side": None, "price": None, "edge": None, "count": 0, "cost": 0.0}
        rows.append(row)
        if row["fair"] is not None and 0 < hours <= B.SPORTS_BET_HOURS:
            try:
                import forecasts
                forecasts.note(m["ticker"], "sports", (start + dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                               row["fair"], forecasts.mid(q), f"{row['game']}: {row['team']}", event_ticker)
            except Exception:
                pass

    def finish(decision, reason):
        for r in rows:
            r["decision"], r["reason"] = decision, reason
        return rows

    held = [p for p in s["open"].values() if p.get("event") == event_ticker]
    if held:
        p = held[0]
        return finish("HOLDING", f"Holding {p['count']} {p['side'].upper()} on {p.get('team', '')} at "
                                 f"{p['entry'] * 100:.0f}c. Waiting for the result.")
    if event_ticker in s["traded"]:
        import strategies
        if any(strategies.is_shadowed(r["ticker"]) for r in rows):
            return finish("SHADOW", "Pretend bet placed - this league is proving itself before real money.")
        return finish("DONE", "Already bet on this game.")
    if hours <= 0.05:
        return finish("PASSED", "Game has started - the bot only bets before kick-off.")
    if hours > B.SPORTS_BET_HOURS:
        return finish("WAIT", f"Starts in {hours:.1f}h. Bets in the last {B.SPORTS_BET_HOURS:g}h before start, "
                              f"with fresh sportsbook odds.")
    if B.PAUSED or not B.SPORTS_ON:
        return finish("PAUSED", "Sports betting is paused.")
    if age_min is None or oev is None:
        return finish("WAIT", "Waiting for sportsbook odds for this game.")
    if age_min > B.SPORTS_MAX_ODDS_AGE_MIN:
        return finish("WAIT", f"Sportsbook odds are {age_min:.0f} min old - waiting for fresh ones before betting.")
    if not fair:
        return finish("NO TRADE", f"Only {nbooks} sportsbook(s) price this game; need {B.SPORTS_MIN_BOOKS}.")
    if s["realized"] <= -B.DAILY_LOSS_LIMIT:
        return finish("STOPPED", "Daily loss limit reached. Back tomorrow.")

    # best deal in this game
    best = None
    for r in rows:
        f = r["fair"]
        if f is None:
            continue
        for side, ask, chance in (("yes", r["yes_ask"], f), ("no", r["no_ask"], 1 - f)):
            if ask is None:
                continue
            edge = chance - ask - B.fee_per_contract(ask)
            # on a tie, prefer buying YES on the team (clearer than "NO on the other team")
            if best is None or edge > best[3] + 0.001 or (abs(edge - best[3]) <= 0.001 and side == "yes" and best[1] == "no"):
                best = (r, side, ask, edge, chance)
    if best is None:
        return finish("NO TRADE", "No Kalshi prices to compare yet.")
    r, side, px, edge, chance = best
    who = r["team"] if side == "yes" else f"not {r['team']}"
    r["side"], r["price"], r["edge"] = side, px, edge
    fair_txt = ", ".join("%s %.0f%%" % (x["team"], x["fair"] * 100) for x in rows if x["fair"] is not None)
    summary = f"Sportsbooks: {fair_txt} ({nbooks} books, {age_min:.0f} min old)."
    if edge < B.SPORTS_MIN_EDGE:
        return finish("NO TRADE", f"{summary} Best Kalshi deal: {who} at {px * 100:.0f}c vs fair {chance * 100:.0f}c "
                                  f"({edge * 100:+.0f}c after fees; needs +{B.SPORTS_MIN_EDGE * 100:.0f}c).")
    if px < B.SPORTS_MIN_PRICE or px > B.SPORTS_MAX_PRICE:
        return finish("NO TRADE", f"{summary} {who} is a deal at {px * 100:.0f}c but outside your "
                                  f"{B.SPORTS_MIN_PRICE * 100:.0f}-{B.SPORTS_MAX_PRICE * 100:.0f}c range.")
    per = px + B.fee_per_contract(px)
    cash = B.LIVE.get("balance")
    cash_room = 1e9 if cash is None else cash * 0.98 - 0.02
    loss_room = B.DAILY_LOSS_LIMIT + s["realized"] - B.open_cost(s, "all")
    import strategies
    stake, size_note = strategies.size("sports:" + sport, chance, px, B.SPORTS_STAKE)
    budget = min(stake, B.SPORTS_MAX_OPEN - open_sports_cost(s), loss_room, cash_room)
    count = math.floor(budget / per + 1e-9)
    if count < 1 and stake < per:
        return finish("NO TRADE", f"{summary} Small edge on {who} (+{edge * 100:.0f}c): edge sizing says it's worth "
                                  f"less than one contract{size_note}.")
    if count < 1:
        return finish("NO TRADE", f"{summary} Good deal on {who}, but no room: sports open ${open_sports_cost(s):.2f} "
                                  f"(max ${B.SPORTS_MAX_OPEN:.2f}), daily loss room ${max(loss_room, 0):.2f}, "
                                  f"cash ${max(cash or 0, 0):.2f}.")
    cost = count * per
    r["count"], r["cost"] = count, round(cost, 2)
    for x in rows:
        if x is not r:
            x["decision"], x["reason"] = "-", ""
    r["decision"] = "BUY"
    r["reason"] = (f"{summary} Kalshi sells {who} at {px * 100:.0f}c; fair is {chance * 100:.0f}c "
                   f"(+{edge * 100:.0f}c after fees). Buy {count} = ${cost:.2f}{size_note}: wins ${count - cost:.2f} "
                   f"if right, loses ${cost:.2f} if wrong.")
    return rows


def size_for(row, chance, s, sport):
    """Contracts and cost for a pre-game bet at row['price'] given a chance for the bought side."""
    import strategies
    px = row["price"]
    per = px + B.fee_per_contract(px)
    cash = B.LIVE.get("balance")
    cash_room = 1e9 if cash is None else cash * 0.98 - 0.02
    loss_room = B.DAILY_LOSS_LIMIT + s["realized"] - B.open_cost(s, "all")
    stake, note = strategies.size("sports:" + sport, chance, px, B.SPORTS_STAKE)
    cap = min(B.SPORTS_MAX_OPEN - open_sports_cost(s), loss_room, cash_room)
    return min(stake, cap), cap, per, note


def ai_review(sport, rows, s):
    """Let the AI team veto/resize a BUY, or promote a near miss. Returns the row to buy (or None)."""
    import council
    if not (B.COUNCIL_ON and B.COUNCIL_SPORTS and council.key_set()):
        return next((r for r in rows if r["decision"] == "BUY"), None)
    buy = next((r for r in rows if r["decision"] == "BUY"), None)
    cand = buy
    if cand is None:                      # near miss: best deal with at least half the required edge
        opts = [r for r in rows if r.get("side") and r.get("edge") is not None and r["decision"] == "NO TRADE"
                and r["edge"] >= B.SPORTS_MIN_EDGE / 2 and B.SPORTS_MIN_PRICE <= r["price"] <= B.SPORTS_MAX_PRICE
                and 0.05 < r["hours"] <= B.SPORTS_BET_HOURS]
        cand = max(opts, key=lambda r: r["edge"]) if opts else None
    if cand is None:
        return None
    side, px = cand["side"], cand["price"]
    when = parse_iso(cand["start"]).astimezone().strftime("%a %b %d %I:%M %p")
    q = f"Will {cand['team']} win? ({cand['sport']}: {cand['game']}, starts {when})"
    if cand["team"] == "Draw":
        q = f"Will {cand['game']} end in a draw? ({cand['sport']}, starts {when})"
    details = {"Sportsbook fair chance (no-margin average)": "-" if cand["fair"] is None else f"{cand['fair'] * 100:.1f}%",
               "Sportsbooks used": cand["books"], "Odds age (min)": cand["odds_age"], "Hours to start": cand["hours"],
               "Kalshi YES ask": f"{(cand['yes_ask'] or 0) * 100:.0f}c", "Kalshi NO ask": f"{(cand['no_ask'] or 0) * 100:.0f}c",
               "Other picks in this game": ", ".join(f"{r['team']} fair {r['fair'] * 100:.0f}%" for r in rows
                                                     if r is not cand and r["fair"] is not None)}
    v = council.review(f"sports ({cand['sport']})", cand["ticker"], q, side, px, cand["fair"], details, research=True,
                       min_edge=B.SPORTS_MIN_EDGE, yes_ask=cand.get("yes_ask"), no_ask=cand.get("no_ask"))
    if not v or not v.get("ok"):
        return buy                         # team unavailable: fall back to the math
    tag = f" AI team: {v['decision']} ({v['votes_for']}/5 lean {side.upper()}). {v['summary']}"
    if not v["approve"]:
        for r in rows:
            r["decision"], r["reason"] = ("NO TRADE", (r["reason"] or "") + tag) if r is cand else (r["decision"], r["reason"])
        return None
    chance = v["prob_yes"] if side == "yes" else 1 - v["prob_yes"]
    edge = chance - px - B.fee_per_contract(px)
    if buy is None and edge < B.SPORTS_MIN_EDGE:
        cand["reason"] += tag + f" Team's odds give only {edge * 100:+.0f}c - still not enough."
        return None
    stake, cap, per, note = size_for(cand, chance, s, cand_sport(cand))
    count = int(min(stake * v["mult"], B.SPORTS_STAKE, cap) // per)      # never above your per-bet cap
    if count < 1:
        return None
    cand["count"], cand["cost"] = count, round(count * per, 2)
    cand["decision"] = "BUY"
    cand["reason"] = (cand["reason"] or "") + tag + f" Size {v['size']}: {count} = ${cand['cost']:.2f}{note}."
    return cand


def cand_sport(row):
    for k, v in SPORTS.items():
        if v[0] == row["sport"]:
            return k
    return "other"


def place(row, kev, s, sport):
    import strategies
    ticker, side, px, count = row["ticker"], row["side"], row["price"], row["count"]
    try:                                       # live price re-check
        lq = B.live_quotes(ticker)
        if lq:
            live_px = lq["yes_ask"] if side == "yes" else lq["no_ask"]
            if live_px is None or live_px > px + 0.005:
                B.log(f"Sports {ticker}: price moved to {live_px} before buying - skipped.")
                return
    except Exception:
        pass
    key = "sports:" + sport
    start = parse_iso(row["start"])
    check_after = (start + dt.timedelta(hours=2)).isoformat()
    title = f"{row['sport']}: {row['game']} - {side.upper()} {row['team']}"
    with B.LOCK:
        s["traded"].append(row["event"])
    B.save_state(s)
    if strategies.route(key, ticker) != "live":
        strategies.shadow_buy(key, ticker, side, px, count, row["cost"], event=row["event"],
                              check_after=check_after, title=title)
        row["decision"] = "SHADOW"
        return
    B.buy_real(ticker, side, px, count, row["cost"], row["pot"], "sports", key,
               {"event": row["event"], "team": row["team"], "sport": SPORTS[sport][0], "sport_key": sport,
                "close_time": check_after, "title": title}, "Sports: " + row["reason"], s)


# --------------------------------------------------------------- late shots

LIVE_ODDS_GAP_MIN = 4          # live odds refresh at most this often per sport (costs credits)
LIVE_FRESH_SEC = 300           # a sportsbook's live price must be this fresh to count


def cheap_side(kev):
    for m in kev["markets"]:
        q = B.quotes(m)
        for a in (q["yes_ask"], q["no_ask"]):
            if a is not None and a <= B.SP_LATE_MAX_PRICE:
                return True
    return False


def is_live(ev):
    start = parse_iso(ev["commence_time"])
    return start < now_utc() < start + dt.timedelta(hours=4)


def judge_late(sport, kev, mapping, ev, s):
    import strategies
    label = SPORTS[sport][0]
    od = ODDS.get(sport)
    oev = (od or {}).get("events", {}).get(ev["id"]) if od else None
    fair, nbooks = fair_chances(oev, fresh_within=LIVE_FRESH_SEC, min_books=2) if oev else (None, 0)
    age = None if not od else round((time.time() - od["t"]) / 60)
    rows = []
    for m in kev["markets"]:
        q = B.quotes(m)
        name = mapping.get(m["ticker"])
        rows.append({"sport": label, "game": f"{ev.get('away_team')} at {ev.get('home_team')}",
                     "start": ev["commence_time"], "hours": round((parse_iso(ev["commence_time"]) - now_utc()).total_seconds() / 3600, 2),
                     "ticker": m["ticker"], "event": kev.get("event_ticker"), "team": "Draw" if name == "Draw" else name,
                     "yes_ask": q["yes_ask"], "no_ask": q["no_ask"], "fair": None if not fair else fair.get(name),
                     "books": nbooks, "odds_age": age, "pot": int(m.get("exchange_index") or kev.get("exchange_index") or 0),
                     "decision": "LIVE", "reason": "", "side": None, "price": None, "count": 0, "cost": 0.0, "late": True})

    def finish(dec, why):
        for r in rows:
            r["decision"], r["reason"] = dec, why
        return rows

    key = "late:" + str(kev.get("event_ticker"))
    if any(p.get("event") == kev.get("event_ticker") and p.get("kind") == "sports_late" for p in s["open"].values()):
        return finish("HOLDING", "Holding a late shot on this game. Waiting for the result.")
    if key in s["traded"]:
        return finish("DONE", "Already took a late shot on this game.")
    if not cheap_side(kev):
        return finish("LIVE", f"Game in progress. Late shots only when a side costs {B.SP_LATE_MAX_PRICE * 100:g}c or less.")
    if not B.SP_LATE_ON or B.PAUSED:
        return finish("PAUSED", "Sports late shots are off.")
    if not fair:
        return finish("WAIT", f"Cheap side on Kalshi, but no fresh live sportsbook prices yet ({nbooks} live books).")
    net = strategies.get("sports_late")["live"]["net"]
    lopen = sum(p["cost"] for p in s["open"].values() if p.get("kind") == "sports_late")
    room = B.SP_LATE_BUDGET + net - lopen
    if room < 0.01:
        return finish("STOPPED", f"Sports late-shot test budget used up (net ${net:+.2f}).")
    best = None
    for r in rows:
        if r["fair"] is None:
            continue
        for side, ask, chance in (("yes", r["yes_ask"], r["fair"]), ("no", r["no_ask"], 1 - r["fair"])):
            if ask is None or ask > B.SP_LATE_MAX_PRICE:
                continue
            ratio = chance / ask
            if best is None or ratio > best[4] * 1.01 or (abs(ratio - best[4]) <= best[4] * 0.01
                                                          and side == "yes" and best[1] == "no"):
                best = (r, side, ask, chance, ratio)
    if best is None:
        return finish("NO TRADE", "Cheap side has no sportsbook price to compare.")
    r, side, px, chance, ratio = best
    who = r["team"] if side == "yes" else f"not {r['team']}"
    r["side"], r["price"] = side, px
    live_txt = ", ".join("%s %.1f%%" % (x["team"], x["fair"] * 100) for x in rows if x["fair"] is not None)
    base = f"Live sportsbooks: {live_txt} ({nbooks} books). "
    if ratio < B.SP_LATE_MIN_RATIO:
        return finish("NO TRADE", base + f"{who} costs {px * 100:.0f}c (pays {1 / px:.0f}x) but has only "
                                         f"{chance * 100:.1f}% - needs {B.SP_LATE_MIN_RATIO:g}x the price. Too far gone.")
    per = px + B.fee_per_contract(px)
    cash = B.LIVE.get("balance")
    cash_room = 1e9 if cash is None else cash * 0.98 - 0.02
    loss_room = B.DAILY_LOSS_LIMIT + s["realized"] - B.open_cost(s, "all")
    stake, size_note = B.smart_small_bet("sports_late", chance, px, B.SP_LATE_MIN_BET, B.SP_LATE_MAX_BET)
    count = B.tiny_count(min(stake, room, loss_room, cash_room), per, B.SP_LATE_MIN_BET, B.SP_LATE_MAX_BET)
    if count < 1:
        return finish("NO TRADE", base + f"Late shot on {who}, but less than the ${B.SP_LATE_MIN_BET:.2f} minimum fits "
                                         f"(test budget ${max(room, 0):.2f}).")
    r["count"], r["cost"] = count, round(count * per + 0.01, 2)
    for x in rows:
        if x is not r:
            x["decision"], x["reason"] = "-", ""
    r["decision"] = "BUY"
    r["reason"] = (base + f"LATE SHOT: Kalshi sells {who} at {px * 100:.0f}c (pays {1 / px:.0f}x); live sportsbooks give it "
                          f"{chance * 100:.1f}% ({ratio:.1f}x the price). Buy {count} = ${r['cost']:.2f}{size_note}: pays ${count:.2f} if it happens.")
    return rows


def place_late(row, kev, s, sport):
    import strategies
    ticker, side, px, count = row["ticker"], row["side"], row["price"], row["count"]
    try:
        lq = B.live_quotes(ticker)
        if lq:
            live_px = lq["yes_ask"] if side == "yes" else lq["no_ask"]
            if live_px is None or live_px > px + 0.005:
                B.log(f"Sports late shot {ticker}: price moved before buying - skipped.")
                return
    except Exception:
        pass
    with B.LOCK:
        s["traded"].append("late:" + str(row["event"]))
    B.save_state(s)
    title = f"{row['sport']} late shot: {row['game']} - {side.upper()} {row['team']}"
    check_after = (now_utc() + dt.timedelta(minutes=20)).isoformat()
    if strategies.route("sports_late", ticker) != "live":
        strategies.shadow_buy("sports_late", ticker, side, px, count, row["cost"], event=row["event"],
                              check_after=check_after, title=title)
        return
    B.buy_real(ticker, side, px, count, row["cost"], row["pot"], "sports_late", "sports_late",
               {"event": row["event"], "team": row["team"], "sport": SPORTS[sport][0], "sport_key": sport,
                "close_time": check_after, "title": title}, row["reason"], s)


# ------------------------------------------------------------------- loop

def tick(s):
    """Called from the bot's main loop. Cheap when nothing to do."""
    if not B.SPORTS_ON and not VIEW["games"]:
        return
    if time.time() - _last_scan["t"] < KALSHI_SCAN_SEC:
        return
    _last_scan["t"] = time.time()
    if not odds_key_set():
        VIEW["note"] = "Add your free Odds API key in bot.py (ODDS_API_KEY) to turn on sports."
        return
    try:
        refresh_active()
    except Exception as e:
        VIEW["note"] = f"Couldn't reach the Odds API: {e}"
        return
    rows_all, notes = [], []
    soon = []
    for sport in B.SPORTS_LIST:
        okey = SPORTS[sport][1]
        if ACTIVE["keys"] is not None and okey not in ACTIVE["keys"]:
            continue                                         # out of season
        try:
            kgames = kalshi_games(sport)
        except Exception as e:
            notes.append(f"{SPORTS[sport][0]}: Kalshi error ({e})")
            continue
        if not kgames:
            continue
        try:
            refresh_schedule(sport)
        except Exception as e:
            notes.append(f"{SPORTS[sport][0]}: schedule error ({e})")
            continue
        sched = SCHED[sport]["events"]
        matched = []
        for kev in kgames:
            ev, mapping = match_game(kev, sched)
            if ev:
                matched.append((kev, ev, mapping))
        if not matched:
            continue
        next_start = min(parse_iso(ev["commence_time"]) for _, ev, _ in matched
                         if parse_iso(ev["commence_time"]) > now_utc()) if any(
            parse_iso(ev["commence_time"]) > now_utc() for _, ev, _ in matched) else None
        soon.append((next_start, sport, matched))

    # buy fresh odds for the sports whose games start soonest, within the daily credit allowance
    soon.sort(key=lambda x: x[0] or now_utc() + dt.timedelta(days=99))
    for next_start, sport, matched in soon:
        if not B.SPORTS_ON or next_start is None:
            continue
        hours = (next_start - now_utc()).total_seconds() / 3600
        od = ODDS.get(sport)
        age = (time.time() - od["t"]) / 60 if od else 1e9
        if hours > B.SPORTS_BET_HOURS or age < max(ODDS_MIN_GAP_MIN, B.SPORTS_MAX_ODDS_AGE_MIN * 0.85):
            continue
        if credits_used_today() >= credits_allowed_today():
            notes.append(f"Saving odds credits: {credits_used_today()} used today of {credits_allowed_today()} allowed.")
            continue
        try:
            refresh_odds(sport)
        except Exception as e:
            notes.append(f"{SPORTS[sport][0]} odds error: {e}")

    # late shots: live games where Kalshi sells a side for 2.5c or less
    if B.SP_LATE_ON and not B.PAUSED:
        for next_start, sport, matched in soon:
            live = [(k, e) for k, e, _ in matched if is_live(e) and cheap_side(k)]
            if not live:
                continue
            od = ODDS.get(sport)
            age = (time.time() - od["t"]) / 60 if od else 1e9
            if age < LIVE_ODDS_GAP_MIN:
                continue
            if credits_used_today() >= credits_allowed_today():
                notes.append("Saving odds credits for later (live late-shot check skipped).")
                continue
            try:
                refresh_odds(sport)
            except Exception as e:
                notes.append(f"{SPORTS[sport][0]} live odds error: {e}")

    for next_start, sport, matched in soon:
        for kev, ev, mapping in matched:
            if is_live(ev):
                rows = judge_late(sport, kev, mapping, ev, s)
                rows_all.extend(rows)
                buy = next((r for r in rows if r["decision"] == "BUY"), None)
                if buy:
                    place_late(buy, kev, s, sport)
                continue
            rows = judge(sport, kev, mapping, ev, s)
            rows_all.extend(rows)
            if not B.SPORTS_ON or B.PAUSED:
                continue
            buy = ai_review(sport, rows, s)
            if buy:
                place(buy, kev, s, sport)
    rows_all.sort(key=lambda r: (r["start"], r["game"]))
    VIEW["games"] = rows_all[:80]
    VIEW["t"] = time.strftime("%H:%M:%S")
    VIEW["note"] = " ".join(notes)


def status():
    return {"on": B.SPORTS_ON, "key_set": odds_key_set(), "credits_left": CREDITS["remaining"],
            "credits_today": credits_used_today(), "credits_allowed": credits_allowed_today(),
            "error": CREDITS["last_error"], "note": VIEW["note"], "updated": VIEW["t"], "games": VIEW["games"],
            "sports": {k: v[0] for k, v in SPORTS.items()}}


def check(s):
    """For check.bat: show matched games and what the bot would do, without betting.
    Uses 1 odds credit per sport that has Kalshi games."""
    if not odds_key_set():
        B.log("Sports: no Odds API key in bot.py, skipping sports check.")
        return
    refresh_active()
    B.log(f"Sports in season at the Odds API: {', '.join(SPORTS[k][0] for k in SPORTS if SPORTS[k][1] in ACTIVE['keys'])}")
    for sport in B.SPORTS_LIST:
        if SPORTS[sport][1] not in ACTIVE["keys"]:
            continue
        kgames = kalshi_games(sport)
        refresh_schedule(sport)
        matched = [(kev, *match_game(kev, SCHED[sport]["events"])) for kev in kgames]
        ok = [(k, e, mp) for k, e, mp in matched if e]
        B.log(f"{SPORTS[sport][0]}: {len(kgames)} Kalshi games, {len(ok)} matched to sportsbook games")
        if not ok:
            for k, _, _ in matched[:3]:
                B.log(f"   unmatched: {k.get('title')} [{', '.join(market_team(m) for m in k['markets'])}]")
            continue
        refresh_odds(sport)
        for kev, ev, mp in ok[:6]:
            saved_hours = B.SPORTS_BET_HOURS
            B.SPORTS_BET_HOURS = 1e6                          # show the decision even for later games
            rows = judge(sport, kev, mp, ev, s)
            B.SPORTS_BET_HOURS = saved_hours
            B.log(f"   {rows[0]['game']} ({rows[0]['hours']:.1f}h): " + " | ".join(
                f"{r['team']}: Kalshi {r['yes_ask']} fair {None if r['fair'] is None else round(r['fair'], 2)}" for r in rows))
            top = next((r for r in rows if r["decision"] == "BUY"), rows[0])
            B.log(f"      would: {top['decision']} - {top['reason'][:220]}")
    B.log(f"Odds credits left this month: {CREDITS['remaining']}")
