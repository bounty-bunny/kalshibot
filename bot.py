"""
EdgeHunter - a Kalshi bot for crypto, sports and politics, with an AI team and a dashboard  (Windows / Mac / Linux, Python 3.9+)

What it does, every 15-minute window, for every coin you picked:
  1. Gets the coin's live price (Coinbase, Kraken or OKX) and Kalshi's target price.
  2. Works out the real chance of YES from how far the price is from the target,
     how much time is left and how jumpy the coin has been in the last hour.
  3. Between minute 2 and minute 12, bets YES or NO only when Kalshi sells that side
     at least MIN_EDGE cheaper than it's worth (after Kalshi's fee).
  4. Cashes out early when Kalshi pays more than the bet is worth, or at +50%.
  5. Keeps trading every window all day. Never has more than MAX_AT_RISK in
     open trades, and stops for the day once the day's losses reach
     DAILY_LOSS_LIMIT. Restarts next day.

While it runs, a dashboard opens in your browser at http://localhost:8765
showing profit and loss, every trade, the live signal, and your limits
(which you can change there, and pause or resume the bot).

    python bot.py          -> trades with real money + dashboard
    python bot.py --check  -> tests your key and shows the markets, NO trades
"""

import base64
import csv
import datetime as dt
import json
import math
import os
import re
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
import sports
import strategies
import politics
import council
import forecasts
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding

# ============================ YOUR SETTINGS ============================

API_KEY_ID = "PASTE-YOUR-KEY-ID-HERE"   # from Kalshi > Profile settings > API Keys
ODDS_API_KEY = "PASTE-YOUR-ODDS-API-KEY"   # free key from the-odds-api.com (for sports)
ANTHROPIC_API_KEY = "PASTE-YOUR-CLAUDE-API-KEY"   # from console.anthropic.com (for the AI team)
OPENAI_API_KEY = "PASTE-YOUR-OPENAI-API-KEY"      # from platform.openai.com (optional: GPT on the AI team)
PRIVATE_KEY_FILE = "kalshi_key.pem"      # optional: any key file Kalshi downloaded into this folder works

# The numbers below are only the starting values. Once the bot runs, change
# them on the dashboard instead (they are then saved in settings.json).

SERIES = ["KXBTC15M"]          # coins to trade; easiest to pick them on the dashboard

STAKE_PER_TRADE = 5.00         # $ per trade
STAKE_PCT = 0.0                # or: bet this share of your Kalshi cash (0.10 = 10%); 0 = use STAKE_PER_TRADE
MAX_AT_RISK = 10.00            # max $ in open trades at any one time
DAILY_LOSS_LIMIT = 10.00       # stop for the day after losing this much (never set below $10)

ENTRY_START_MIN = 2.0          # earliest minute of the window to buy
ENTRY_END_MIN = 12.0           # latest minute of the window to buy
MIN_EDGE = 0.03                # only bet when the bot's odds beat Kalshi's price by 3c (after fees)
MIN_PRICE = 0.10               # never buy a side cheaper than 10c (long shots)
MAX_PRICE = 0.90               # never buy a side dearer than 90c (tiny profit)
TAKE_PROFIT = 0.50             # always cash out when a bet is up 50% (capped at 99c)

# ---- Long-shot mode (private): small bets on cheap sides the bot thinks are underpriced ----
BET_MODE = "both"              # crypto: "normal", "longshot", "both" or "off"
LS_MIN_BET, LS_MAX_BET = 0.50, 5.00      # long shots: the bot picks a size in this range (better deal = bigger)
LS_MAX_PRICE = 0.20            # only sides costing 20c or less (pays 5x or more)
LS_MIN_RATIO = 1.3             # bot's chance must be at least 1.3x the price (e.g. 13% for a 10c side)
LS_MARKET_WEIGHT = 0.5         # long-shot odds = half the bot's math + half the market price (guards against overpriced cheap sides)
LS_BUDGET = 10.00              # total test money: stops for good once long shots are down this much

# ---- Sports (private): bet when Kalshi is cheaper than the sportsbooks' fair odds ----
SPORTS_ON = True
SPORTS_LIST = ["nfl", "ncaaf", "nba", "ncaab", "nhl", "mlb", "epl", "ucl", "mls"]
SPORTS_STAKE = 5.00            # most $ per game
SPORTS_MAX_OPEN = 50.00        # most $ in open sports bets at once
SPORTS_MIN_EDGE = 0.04         # Kalshi must be 4c cheaper than fair (after fees)
SPORTS_MIN_PRICE = 0.25        # skip big underdogs
SPORTS_MAX_PRICE = 0.85        # skip near-certain favourites (tiny profit)
SPORTS_BET_HOURS = 3.0         # only bet in the last 3 hours before the game starts
SPORTS_MAX_ODDS_AGE_MIN = 120  # sportsbook odds must be this fresh to bet
SPORTS_MIN_BOOKS = 3           # need at least this many sportsbooks pricing the game

# ---- Politics (private): Kalshi vs Polymarket, matched automatically ----
POLITICS_ON = True
POLITICS_STAKE = 5.00          # most $ per politics bet
POLITICS_MAX_OPEN = 30.00      # most $ in open politics bets
POLITICS_MIN_EDGE = 0.06       # Kalshi must be 6c cheaper than Polymarket (after fees)
POLITICS_MIN_PRICE = 0.10
POLITICS_MAX_PRICE = 0.90
POLITICS_MAX_DAYS = 45         # only questions that resolve within 45 days
POLITICS_DATE_DAYS = 45        # matched questions must end within 45 days of each other
POLITICS_MAX_GAP = 0.15        # skip if the two sites disagree by more than 15 points (likely a mismatch)

# ---- Smarter sizing: bigger bets on better deals ----
SIZING = "edge"                # "edge" = size by how good the deal is; "fixed" = always the set amount
KELLY_FRACTION = 0.50          # medium: half of the mathematically "ideal" (Kelly) bet

# ---- Late shots: tiny bets on 40x+ sides in the last minutes, only when a flip is realistic ----
LATE_ON = True                 # crypto late shots
LATE_MIN_BET, LATE_MAX_BET = 0.10, 2.00  # late shots: the bot picks a size in this range (better deal = bigger)
LATE_MAX_PRICE = 0.025         # only sides costing 2.5c or less (pays 40x or more)
LATE_MIN_RATIO = 2.0           # bot's chance must be at least 2x the price (1c side needs a 2% chance)
LATE_MAX_Z = 2.0               # "not too far": price must be within 2 typical moves of the target
LATE_START_LEFT = 5.0          # start looking with 5 minutes left
LATE_SURE_LEFT = 2.0           # if unsure, keep watching until 2 minutes left
LATE_STOP_LEFT = 1.0           # last buy with 1 minute left
LATE_BUDGET = 10.00            # total test money for crypto late shots
SP_LATE_ON = True              # sports late shots (live games)
SP_LATE_MIN_BET, SP_LATE_MAX_BET = 0.10, 2.00
SP_LATE_MAX_PRICE = 0.025
SP_LATE_MIN_RATIO = 2.0
SP_LATE_BUDGET = 10.00

# ---- AI team: 6 AI agents (Claude, GPT or a mix) debate each bet (researcher + 5 analysts + judge) ----
COUNCIL_ON = True
COUNCIL_CRYPTO = True          # review crypto value bets
COUNCIL_SPORTS = True          # review sports bets (with news research)
COUNCIL_POLITICS = True        # review politics bets (with news research)
COUNCIL_DAILY_BUDGET = 1.00    # most $ per day spent on AI calls (about 30-80 debates)
COUNCIL_PROVIDER = "mix"       # "mix" = Claude + GPT together, "claude" = Claude only, "gpt" = GPT only
                               # (with only one key pasted, the team just uses that one)
COUNCIL_AGENT_MODEL = "claude-haiku-5-5"     # Claude analysts / researcher
COUNCIL_JUDGE_MODEL = "claude-sonnet-5-5"    # Claude judge
COUNCIL_GPT_AGENT_MODEL = "gpt-5.6-luna"     # GPT analysts / researcher
COUNCIL_GPT_JUDGE_MODEL = "gpt-5.6-terra"    # GPT judge

CHECK_EVERY_SEC = 15           # how often to look at the market

# Tip / sponsor link shown on the dashboard (a small button, plus a rare "thank you" note).
# Put your own Buy Me a Coffee / Ko-fi / GitHub Sponsors link here, or "" to hide it.
SUPPORT_URL = "https://github.com/sponsors/bounty-bunny"
DASHBOARD_PORT = 8765          # dashboard address: http://localhost:8765

# =======================================================================

BASE = os.environ.get("KALSHI_BASE", "https://api.elections.kalshi.com")
PREFIX = "/trade-api/v2"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "bot_state.json")
TRADES_FILE = os.path.join(HERE, "trades.csv")
LOG_FILE = os.path.join(HERE, "bot_log.txt")
SETTINGS_FILE = os.path.join(HERE, "settings.json")
DASHBOARD_FILE = os.path.join(HERE, "dashboard.html")
SUPPORT_FILE = os.path.join(HERE, "support.json")
LONGSHOT_FILE = os.path.join(HERE, "longshot.json")

KNOWN_SERIES = {                   # Kalshi 15-minute crypto markets (more are found automatically)
    "KXBTC15M": "Bitcoin", "KXETH15M": "Ethereum", "KXSOL15M": "Solana", "KXXRP15M": "XRP",
    "KXBNB15M": "BNB", "KXDOGE15M": "Dogecoin", "KXHYPE15M": "Hyperliquid", "KXZEC15M": "Zcash",
    "KXNEAR15M": "NEAR",
}
EDITABLE = {                       # name: (min, max)
    "STAKE_PER_TRADE": (0.5, 1000), "MAX_AT_RISK": (0.5, 10000), "DAILY_LOSS_LIMIT": (10, 100000),
    "ENTRY_START_MIN": (0, 14), "ENTRY_END_MIN": (0.5, 14.5),
    "MIN_PRICE": (0.01, 0.99), "MAX_PRICE": (0.01, 0.99), "TAKE_PROFIT": (0.05, 10),
    "MIN_EDGE": (0.0, 0.5), "STAKE_PCT": (0.0, 1.0),
    "SPORTS_STAKE": (0.5, 10000), "SPORTS_MAX_OPEN": (0.5, 100000), "SPORTS_MIN_EDGE": (0.0, 0.5),
    "SPORTS_MIN_PRICE": (0.01, 0.99), "SPORTS_MAX_PRICE": (0.01, 0.99), "SPORTS_BET_HOURS": (0.25, 72),
    "SPORTS_MAX_ODDS_AGE_MIN": (10, 1440),
    "POLITICS_STAKE": (0.5, 10000), "POLITICS_MAX_OPEN": (0.5, 100000), "POLITICS_MIN_EDGE": (0.0, 0.5),
    "POLITICS_MAX_DAYS": (1, 3650), "KELLY_FRACTION": (0.05, 1.0), "COUNCIL_DAILY_BUDGET": (0.05, 100),
    "LATE_MIN_BET": (0.10, 2.00), "LATE_MAX_BET": (0.10, 2.00), "LATE_MAX_PRICE": (0.01, 0.2), "LATE_MIN_RATIO": (1.0, 20), "LATE_MAX_Z": (0.2, 5),
    "LATE_START_LEFT": (1, 14), "LATE_SURE_LEFT": (0.5, 14), "LATE_STOP_LEFT": (0.25, 10), "LATE_BUDGET": (0.5, 100000),
    "SP_LATE_MIN_BET": (0.10, 2.00), "SP_LATE_MAX_BET": (0.10, 2.00), "SP_LATE_MAX_PRICE": (0.01, 0.2), "SP_LATE_MIN_RATIO": (1.0, 20),
    "SP_LATE_BUDGET": (0.5, 100000),
    "LS_MIN_BET": (0.50, 5.00), "LS_MAX_BET": (0.50, 5.00), "LS_MAX_PRICE": (0.02, 0.5), "LS_MIN_RATIO": (1.1, 10), "LS_BUDGET": (1, 100000),
}
BET_MODES = ("normal", "longshot", "both", "off")
TINY_RANGES = {                         # hard limits for small bets; the dashboard can narrow them, never widen
    "LATE_MIN_BET": (0.10, 2.00), "LATE_MAX_BET": (0.10, 2.00),
    "SP_LATE_MIN_BET": (0.10, 2.00), "SP_LATE_MAX_BET": (0.10, 2.00),
    "LS_MIN_BET": (0.50, 5.00), "LS_MAX_BET": (0.50, 5.00),
    "DAILY_LOSS_LIMIT": (10.00, 100000),
}


def smart_small_bet(key, chance, price, lo, hi):
    """Pick a bet size between lo and hi from how good the deal is (edge sizing + track record)."""
    if SIZING != "edge":
        return lo, ""
    stake, note = strategies.size(key, chance, price, hi)
    return min(hi, max(lo, stake)), note


def tiny_count(budget, per, lo, hi):
    """Contracts for a small bet costing (incl. ~1c fee rounding) between lo and hi; 0 if it can't reach lo."""
    cap = min(budget, hi)
    n = math.floor((cap - 0.01) / per + 1e-9)
    if n * per + 0.01 < lo - 1e-9:                      # round up to reach the minimum, if it fits
        n = math.ceil((lo - 0.01) / per - 1e-9)
        if n * per + 0.01 > min(cap + per, hi) + 1e-9:
            return 0
    return max(n, 0)
SETTINGS_VERSION = 5               # v3 value bets on; v4 balanced; v5 medium sizing + AI team
STRATEGY_KEYS = ("MIN_PRICE", "MAX_PRICE", "ENTRY_START_MIN", "ENTRY_END_MIN")

PAUSED = False
_private_key = None
LOCK = threading.RLock()
LIVE = {"markets": [], "balance": None, "updated": None, "error": None, "started": None, "fund_problem": None}
STATE = None
LOG_TAIL = []


# ----------------------------- helpers ---------------------------------

def log(msg):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    with LOCK:
        LOG_TAIL.append(line)
        del LOG_TAIL[:-60]
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def today():
    return dt.date.today().isoformat()


def parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def cstr(p):
    """Price in cents for messages; shows tenths below 1c (Kalshi now has sub-cent prices)."""
    return f"{p * 100:.1f}c" if p is not None and p < 0.0095 else f"{(p or 0) * 100:.0f}c"


def fnum(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ----------------------------- settings --------------------------------

def current_settings():
    d = {k: globals()[k] for k in EDITABLE}
    d["SERIES"] = list(SERIES)
    d["PAUSED"] = PAUSED
    d["BET_MODE"] = BET_MODE
    d["SPORTS_ON"] = SPORTS_ON
    d["POLITICS_ON"] = POLITICS_ON
    d["LATE_ON"] = LATE_ON
    for k in ("COUNCIL_ON", "COUNCIL_CRYPTO", "COUNCIL_SPORTS", "COUNCIL_POLITICS"):
        d[k] = globals()[k]
    d["SP_LATE_ON"] = SP_LATE_ON
    d["SIZING"] = SIZING
    d["COUNCIL_PROVIDER"] = COUNCIL_PROVIDER
    d["SPORTS_LIST"] = list(SPORTS_LIST)
    d["VERSION"] = SETTINGS_VERSION
    return d


def apply_settings(d):
    """Validate and apply a dict of settings. Returns list of error strings."""
    global PAUSED, SERIES, BET_MODE, SPORTS_ON, SPORTS_LIST, POLITICS_ON, SIZING, LATE_ON, SP_LATE_ON, COUNCIL_PROVIDER
    errors, new = [], {}
    d = dict(d)
    for k, (lo, hi) in TINY_RANGES.items():   # small bets and the daily loss limit always stay in range
        v = fnum(d.get(k))
        if v is not None:
            d[k] = min(hi, max(lo, v))
    if "BET_MODE" in d and d["BET_MODE"] not in BET_MODES:
        errors.append("Bet mode must be normal, longshot, both or off")
    if "SIZING" in d and d["SIZING"] not in ("edge", "fixed"):
        errors.append("Sizing must be edge or fixed")
    if "COUNCIL_PROVIDER" in d and d["COUNCIL_PROVIDER"] not in ("mix", "claude", "gpt"):
        errors.append("AI team must be mix, claude or gpt")
    sl = d.get("SPORTS_LIST")
    if sl is not None:
        sl = [x for x in sl if x in sports.SPORTS]
    for k, (lo, hi) in EDITABLE.items():
        if k in d:
            v = fnum(d[k])
            if v is None or not (lo <= v <= hi):
                errors.append(f"{k} must be a number between {lo} and {hi}")
            else:
                new[k] = v
    merged = {**{k: globals()[k] for k in EDITABLE}, **new}
    if merged["SPORTS_MIN_PRICE"] >= merged["SPORTS_MAX_PRICE"]:
        errors.append("Sports: lowest price must be below highest price")
    for a, b in (("LATE_MIN_BET", "LATE_MAX_BET"), ("SP_LATE_MIN_BET", "SP_LATE_MAX_BET"), ("LS_MIN_BET", "LS_MAX_BET")):
        if merged[a] > merged[b]:
            errors.append(f"{a} must not be above {b}")
    if not merged["LATE_STOP_LEFT"] < merged["LATE_START_LEFT"]:
        errors.append("Late shots: must stop buying before they start (minutes left)")
    if merged["MIN_PRICE"] >= merged["MAX_PRICE"]:
        errors.append("Lowest price must be below highest price")
    if merged["ENTRY_START_MIN"] >= merged["ENTRY_END_MIN"]:
        errors.append("Buying must start before it ends")
    series = d.get("SERIES")
    if series is not None:
        series = [s for s in series if s in KNOWN_SERIES]
        if not series and d.get("BET_MODE", BET_MODE) != "off":
            errors.append("Pick at least one coin (or set crypto to 'No crypto bets')")
    if errors:
        return errors
    with LOCK:
        globals().update(new)
        if series is not None:
            SERIES = series
        if "PAUSED" in d:
            PAUSED = bool(d["PAUSED"])
        if "BET_MODE" in d:
            BET_MODE = d["BET_MODE"]
        if "SPORTS_ON" in d:
            SPORTS_ON = bool(d["SPORTS_ON"])
        if "POLITICS_ON" in d:
            POLITICS_ON = bool(d["POLITICS_ON"])
        if "LATE_ON" in d:
            LATE_ON = bool(d["LATE_ON"])
        for k in ("COUNCIL_ON", "COUNCIL_CRYPTO", "COUNCIL_SPORTS", "COUNCIL_POLITICS"):
            if k in d:
                globals()[k] = bool(d[k])
        if "SP_LATE_ON" in d:
            SP_LATE_ON = bool(d["SP_LATE_ON"])
        if "SIZING" in d:
            SIZING = d["SIZING"]
        if "COUNCIL_PROVIDER" in d:
            COUNCIL_PROVIDER = d["COUNCIL_PROVIDER"]
        if sl is not None:
            SPORTS_LIST = sl
    return []


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("VERSION", 1) < 2:                    # old strategy: keep money limits and coins only
            for k in STRATEGY_KEYS:
                d.pop(k, None)
        if d.get("VERSION", 1) < 3 and d.get("BET_MODE") == "longshot":
            d["BET_MODE"] = "both"                      # value bets were accidentally off in the first private version
            log("Settings updated: crypto now does value bets AND long shots ('Both').")
        if d.get("VERSION", 1) < 4:                    # balanced: a bit less strict than before
            d["MIN_EDGE"] = min(fnum(d.get("MIN_EDGE"), 0.03), 0.03)
            d["LS_MIN_RATIO"] = min(fnum(d.get("LS_MIN_RATIO"), 1.3), 1.3)
            log("Settings updated to Balanced: value bets need a 3c edge, long shots 1.3x the price.")
        if d.get("VERSION", 1) < 5:                    # medium bet sizing
            d["KELLY_FRACTION"] = 0.50
            d["STAKE_PER_TRADE"] = 5.00
            d["SPORTS_STAKE"] = 5.00
            d["POLITICS_STAKE"] = 5.00
            log("Settings updated to Medium bets: up to $5 per bet, edge sizing doubled (50% Kelly).")
        old_version = d.get("VERSION", 1)
        errs = apply_settings(d)
        if errs:
            log("settings.json ignored: " + "; ".join(errs))
        elif old_version < SETTINGS_VERSION:
            save_settings()                             # remember the upgrade so it happens only once
    except (OSError, ValueError):
        pass


def save_settings():
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(current_settings(), f, indent=1)
    os.replace(tmp, SETTINGS_FILE)


# ------------------------------- API -----------------------------------

class ApiError(Exception):
    def __init__(self, status, text):
        super().__init__(f"HTTP {status}: {text}")
        self.status = status


def find_key_file():
    """Use kalshi_key.pem if present, else any key file Kalshi downloaded into this folder."""
    path = PRIVATE_KEY_FILE if os.path.isabs(PRIVATE_KEY_FILE) else os.path.join(HERE, PRIVATE_KEY_FILE)
    if os.path.exists(path):
        return path
    for name in sorted(os.listdir(HERE)):
        if name.lower().endswith((".pem", ".txt", ".key")) and name.lower() not in ("readme.txt", "bot_log.txt"):
            full = os.path.join(HERE, name)
            try:
                with open(full, "rb") as f:
                    if b"PRIVATE KEY" in f.read(4096):
                        return full
            except OSError:
                pass
    return None


def load_key():
    global _private_key
    path = find_key_file()
    if not path:
        sys.exit("Key file not found.\nPut the private key file Kalshi downloaded into this folder (see the guide).")
    with open(path, "rb") as f:
        _private_key = serialization.load_pem_private_key(f.read(), password=None)
    log(f"Using key file: {os.path.basename(path)}")


def auth_headers(method, path):
    ts = str(int(time.time() * 1000))
    msg = (ts + method + path).encode()
    if isinstance(_private_key, ed25519.Ed25519PrivateKey):
        sig = _private_key.sign(msg)                       # Ed25519 key (Kalshi's default)
    else:
        sig = _private_key.sign(                           # RSA key
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    return {
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }


_api_lock = threading.Lock()
_last_call = {"any": 0.0, "balance": 0.0}
MIN_GAP_SEC = 0.35            # at most ~3 requests a second, well under Kalshi's limits
BALANCE_GAP_SEC = 1.2         # Kalshi allows balance checks only about once a second


def api(method, path, params=None, body=None):
    for attempt in range(5):
        with _api_lock:                                   # pace every request
            gap = BALANCE_GAP_SEC if path == "/portfolio/balance" else MIN_GAP_SEC
            key = "balance" if path == "/portfolio/balance" else "any"
            wait = max(_last_call["any"] + MIN_GAP_SEC, _last_call[key] + gap) - time.time()
            if wait > 0:
                time.sleep(wait)
            _last_call["any"] = _last_call[key] = time.time()
        headers = auth_headers(method, PREFIX + path)  # sign path without query string
        if body is not None:
            headers["Content-Type"] = "application/json"
        r = requests.request(method, BASE + PREFIX + path, params=params, json=body,
                             headers=headers, timeout=15)
        if r.status_code == 429 and attempt < 4:          # "too many requests": back off and retry
            time.sleep(1.5 * (2 ** attempt))
            continue
        if r.status_code >= 400:
            raise ApiError(r.status_code, r.text[:400])
        return r.json() if r.text else {}


def get_balance():
    return api("GET", "/portfolio/balance").get("balance", 0) / 100.0


def get_balances():
    """Total cash and cash per exchange 'pot' (Kalshi keeps crypto money in its own pot)."""
    d = api("GET", "/portfolio/balance")
    total = fnum(d.get("balance_dollars"))
    if total is None:
        total = d.get("balance", 0) / 100.0
    pots = {}
    for b in d.get("balance_breakdown") or []:
        idx = int(b.get("exchange_index", 0))
        v = fnum(b.get("balance"))
        if v is None:
            v = fnum(b.get("balance_dollars"), 0.0)
        pots[idx] = v
    if not pots:
        pots[0] = total
    return total, pots


def move_money(amount, to_pot, from_pot=0):
    """Move cash between Kalshi exchange pots (no fee, no delay)."""
    body = {"source": "event_contract", "destination": "event_contract",
            "amount": int(round(amount * 10000)),              # Kalshi wants centicents
            "source_exchange_shard": from_pot, "destination_exchange_shard": to_pot,
            "client_transfer_id": str(uuid.uuid4())}
    return api("POST", "/portfolio/intra_exchange_instance_transfer", body=body)


FUND_HELP = ("Kalshi keeps crypto money in a separate pot and the bot could not move money into it. "
             "Fix: in the Kalshi app or website, place one small crypto bet by hand (Kalshi then moves "
             "money for you), or ask Kalshi support how to move cash to your crypto balance.")


def ensure_funds(pot, needed):
    """Make sure the exchange pot holding this market has at least `needed` dollars."""
    total, pots = get_balances()
    LIVE["balance"] = total
    have = pots.get(pot, 0.0)
    if have + 1e-9 >= needed:
        return True, ""
    if total + 1e-9 < needed:
        return False, "Not enough cash in your Kalshi account."
    # move enough for a few bets at once so we don't transfer every time
    want = min(max(MAX_AT_RISK, needed) + 0.50 - have, total - have)
    sources = sorted((p for p in pots if p != pot), key=lambda p: -pots[p])
    moved = 0.0
    for src in sources:
        amt = round(min(want - moved, pots[src]), 2)
        if amt < 0.01:
            continue
        try:
            move_money(amt, pot, src)
            moved += amt
            log(f"Moved ${amt:.2f} from Kalshi pot {src} to pot {pot} (the crypto pot) to fund bets.")
        except ApiError as e:
            log(f"Could not move money to pot {pot}: {e}")
            return False, FUND_HELP
        if moved + have + 1e-9 >= needed:
            break
    for _ in range(10):                      # transfers are quick but not instant
        time.sleep(1)
        total, pots = get_balances()
        if pots.get(pot, 0.0) + 1e-9 >= needed:
            return True, ""
    return False, "Money transfer to the crypto pot has not arrived yet; will try again."


def get_open_markets(series):
    data = api("GET", "/markets", params={"series_ticker": series, "status": "open", "limit": 50})
    return data.get("markets", [])


def get_market(ticker):
    return api("GET", f"/markets/{ticker}").get("market", {})


def clean_coin_name(title, ticker):
    t = re.sub(r"(?i)\b(15[ -]?(minute|min|m)s?|up or down|price)\b", "", title or "")
    t = re.sub(r"\s+", " ", t).strip(" -?")
    return t or coin_symbol(ticker)


def discover_series():
    """Ask Kalshi for all 15-minute crypto series so new coins show up on the dashboard."""
    try:
        data = api("GET", "/series", params={"category": "Crypto"})
    except Exception as e:
        log(f"Could not load Kalshi's coin list ({e}); using the built-in list.")
        return
    found = 0
    for ser in data.get("series") or []:
        t = (ser.get("ticker") or "").upper()
        title = ser.get("title") or ""
        if not re.fullmatch(r"KX[A-Z]{2,6}15M", t) or t in KNOWN_SERIES:
            continue
        if re.search(r"(?i)comparison|race|\bvs\b|versus|index", title):
            continue                                       # not a single-coin up/down market
        KNOWN_SERIES[t] = clean_coin_name(title, t)
        found += 1
    if found:
        log(f"Found {found} more 15-minute coin(s) on Kalshi.")


def price(m, name):
    """Read a price in dollars. Handles new '..._dollars' strings and old integer cents."""
    v = fnum(m.get(name + "_dollars"))
    if v is None:
        cents = m.get(name)
        v = cents / 100.0 if isinstance(cents, (int, float)) else None
    if v is None or v <= 0 or v >= 1:
        return None
    return v


def quotes(m):
    yb, ya = price(m, "yes_bid"), price(m, "yes_ask")
    nb, na = price(m, "no_bid"), price(m, "no_ask")
    if na is None and yb is not None:
        na = round(1 - yb, 4)
    if nb is None and ya is not None:
        nb = round(1 - ya, 4)
    if ya is None and nb is not None:
        ya = round(1 - nb, 4)
    if yb is None and na is not None:
        yb = round(1 - na, 4)
    return {"yes_bid": yb, "yes_ask": ya, "no_bid": nb, "no_ask": na}


def live_quotes(ticker):
    """Fresh best prices straight from the order book (the market list can lag a few seconds)."""
    d = api("GET", f"/markets/{ticker}/orderbook")
    ob = d.get("orderbook_fp") or d.get("orderbook") or {}

    def best(*keys):
        for k in keys:
            levels = ob.get(k) or []
            prices = []
            for lv in levels:
                p = fnum(lv[0] if isinstance(lv, (list, tuple)) else (lv.get("price") if isinstance(lv, dict) else None))
                if p is None:
                    continue
                if k in ("yes", "no") and p > 1:
                    p = p / 100.0
                prices.append(p)
            if prices:
                return max(prices)
        return None

    yb, nb = best("yes_dollars", "yes"), best("no_dollars", "no")
    if yb is None and nb is None:
        return None
    return {"yes_bid": yb, "no_bid": nb,
            "yes_ask": None if nb is None else round(1 - nb, 4),
            "no_ask": None if yb is None else round(1 - yb, 4)}


# ---------------- live coin prices and the odds model ----------------

SPOT_UA = {"User-Agent": "kalshi-15m-bot/1.0"}
_coin_cache = {}          # symbol -> {"t", "price", "sigma", "src"}
_coin_src = {}            # symbol -> source that worked last time


def coin_symbol(series_or_ticker):
    t = series_or_ticker.split("-")[0].upper()
    return t[2:-3] if t.startswith("KX") and t.endswith("15M") else t


def _closes_sigma(closes):
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 10:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


def _from_coinbase(sym):
    base = f"https://api.exchange.coinbase.com/products/{sym}-USD"
    price = fnum(requests.get(base + "/ticker", headers=SPOT_UA, timeout=6).json().get("price"))
    rows = requests.get(base + "/candles", params={"granularity": 60}, headers=SPOT_UA, timeout=6).json()
    closes = [float(r[4]) for r in rows[:61]][::-1]          # newest first -> oldest first
    return price, closes


def _from_kraken(sym):
    pair = ("XBT" if sym == "BTC" else sym) + "USD"
    t = requests.get("https://api.kraken.com/0/public/Ticker", params={"pair": pair}, headers=SPOT_UA, timeout=6).json()
    res = t.get("result") or {}
    price = fnum(next(iter(res.values()))["c"][0]) if res else None
    o = requests.get("https://api.kraken.com/0/public/OHLC", params={"pair": pair, "interval": 1},
                     headers=SPOT_UA, timeout=6).json().get("result") or {}
    rows = next((v for k, v in o.items() if k != "last"), [])
    closes = [float(r[4]) for r in rows[-61:]]
    return price, closes


def _from_okx(sym):
    inst = f"{sym}-USDT"
    t = requests.get("https://www.okx.com/api/v5/market/ticker", params={"instId": inst},
                     headers=SPOT_UA, timeout=6).json().get("data") or []
    price = fnum(t[0]["last"]) if t else None
    rows = requests.get("https://www.okx.com/api/v5/market/candles", params={"instId": inst, "bar": "1m", "limit": 61},
                        headers=SPOT_UA, timeout=6).json().get("data") or []
    closes = [float(r[4]) for r in rows][::-1]
    return price, closes


SOURCES = {"Coinbase": _from_coinbase, "Kraken": _from_kraken, "OKX": _from_okx}


def coin_data(sym):
    """Live price and 1-minute volatility for a coin, from the first exchange that has it."""
    c = _coin_cache.get(sym)
    if c and time.time() - c["t"] < 8:
        return c
    order = [_coin_src[sym]] if sym in _coin_src else []
    order += [n for n in SOURCES if n not in order]
    for name in order:
        try:
            price, closes = SOURCES[name](sym)
            sigma = _closes_sigma(closes)
            if price and price > 0 and sigma:
                _coin_src[sym] = name
                _coin_cache[sym] = {"t": time.time(), "price": price, "sigma": sigma, "src": name}
                return _coin_cache[sym]
        except Exception:
            continue
    return None


def get_target(m):
    """Kalshi's target price for this window (the price the coin must finish at or above for YES)."""
    for k in ("floor_strike", "strike", "cap_strike"):
        v = fnum(m.get(k))
        if v and v > 0:
            return v
    for k in ("yes_sub_title", "subtitle", "title", "rules_primary"):
        txt = m.get(k) or ""
        hit = re.search(r"\$\s?([0-9][0-9,]*\.?[0-9]*)", txt)
        if hit:
            v = fnum(hit.group(1).replace(",", ""))
            if v and v > 0:
                return v
    return None


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


MAX_DISAGREE = 0.25      # skip if the bot and Kalshi disagree by more than 25 points (likely bad data)
VOL_SAFETY = 1.25        # assume prices are a bit wilder than recent history (protects against overconfidence)


def chance_yes(spot, target, sigma_min, minutes_left, vol=None, lo=0.03, hi=0.97):
    """Chance the coin finishes at/above target. Kalshi settles on a 60-second average, so
    the last half-minute moves the result less; we allow for that."""
    tau = max(minutes_left - 0.5, 0.2)
    sd = sigma_min * math.sqrt(tau) * (VOL_SAFETY if vol is None else vol)
    if sd <= 0:
        return None
    p = norm_cdf(math.log(spot / target) / sd)
    return min(hi, max(lo, p))


def fee_per_contract(p):
    """Kalshi's taker fee is about 7% x price x (1 - price) per contract."""
    return 0.07 * p * (1 - p)


def send_order(ticker, yes_side, yes_price, count, reduce_only=False, pot=None):
    """yes_side: 'bid' = buy YES, 'ask' = sell YES (= buy NO). Price is the YES price."""
    body = {
        "ticker": ticker,
        "side": yes_side,
        "count": str(int(count)),
        "price": f"{yes_price:.4f}",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": str(uuid.uuid4()),
    }
    if reduce_only:
        body["reduce_only"] = True
    if pot is not None:
        body["exchange_index"] = int(pot)     # send the order to the exchange pot that holds this market
    return api("POST", "/portfolio/events/orders", body=body)


# ------------------------------ state ----------------------------------

def new_day_state(old=None):
    return {"day": today(), "spent": 0.0, "realized": 0.0,
            "traded": (old or {}).get("traded", [])[-300:],
            "open": (old or {}).get("open", {})}


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
        if s.get("day") != today():
            s = new_day_state(s)
        return s
    except (OSError, ValueError):
        return new_day_state()


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    with LOCK:
        data = json.dumps(s, indent=1)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, STATE_FILE)


TRADE_COLS = ["time", "event", "ticker", "side", "contracts", "price",
              "amount_$", "pnl_$", "day_spent_$", "day_pnl_$", "note"]


def record(event, pos, contracts, px, amount, pnl, s, note=""):
    new = not os.path.exists(TRADES_FILE)
    with LOCK, open(TRADES_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(TRADE_COLS)
        w.writerow([f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}", event, pos["ticker"],
                    pos["side"].upper(), contracts, f"{px:.2f}", f"{amount:.2f}",
                    "" if pnl is None else f"{pnl:+.2f}", f"{s['spent']:.2f}",
                    f"{s['realized']:+.2f}", note])


def read_trades():
    try:
        with open(TRADES_FILE, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def open_cost(s, which="crypto"):
    """Money in open bets. 'crypto' = coin bets only (sports have their own cap), 'all' = everything."""
    return sum(p["cost"] for p in s["open"].values() if which == "all" or p.get("kind") != "sports")


# --------------------------- long-shot test ----------------------------

def load_longshot():
    try:
        with open(LONGSHOT_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    for k, v in (("net", 0.0), ("bets", 0), ("wins", 0), ("best", 0.0), ("staked", 0.0)):
        d.setdefault(k, v)
    return d


def longshot_result(pnl, won, payout):
    d = load_longshot()
    d["net"] = round(d["net"] + pnl, 2)
    d["bets"] += 1
    d["wins"] += 1 if won else 0
    d["best"] = max(d["best"], round(payout, 2)) if won else d["best"]
    with LOCK, open(LONGSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    if d["net"] <= -LS_BUDGET + 0.005:
        log(f"Long-shot test budget used up (net ${d['net']:+.2f}). No more long shots until you raise it.")


# ----------------------------- decisions -------------------------------

def evaluate(m, s, think=None):
    """Work out what the bot thinks about this market right now (no orders placed).
    think=True forces the odds model; by default it runs only when the bot might act."""
    ticker = m["ticker"]
    mins = (utcnow() - parse_time(m["open_time"])).total_seconds() / 60
    left = (parse_time(m["close_time"]) - utcnow()).total_seconds() / 60
    q = quotes(m)
    sym = coin_symbol(m.get("series_ticker") or ticker)
    info = {"ticker": ticker, "title": m.get("title", ""), "minute": round(mins, 1),
            "minutes_left": round(left, 1), "yes_bid": q["yes_bid"], "yes_ask": q["yes_ask"],
            "no_bid": q["no_bid"], "no_ask": q["no_ask"], "leader": None, "price": None,
            "coin": KNOWN_SERIES.get(m.get("series_ticker") or ticker.split("-")[0], sym), "symbol": sym,
            "count": 0, "cost": 0.0, "pot": int(m.get("exchange_index") or 0),
            "spot": None, "target": get_target(m), "chance_yes": None, "edge_yes": None, "edge_no": None,
            "kind": "normal"}

    def out(decision, reason):
        info["decision"], info["reason"] = decision, reason
        return info

    pos = s["open"].get(ticker)
    in_window = ENTRY_START_MIN <= mins <= ENTRY_END_MIN and BET_MODE != "off"
    late_window = LATE_ON and LATE_STOP_LEFT <= left <= LATE_START_LEFT
    can_act = (not pos and ticker not in s["traded"] and not PAUSED and (in_window or late_window)
               and s["realized"] > -DAILY_LOSS_LIMIT)
    if think is None:
        think = can_act or bool(pos)

    # ---- the odds model ----
    p_yes = None
    if think and info["target"] and left > 0:
        cd = coin_data(sym)
        if cd:
            info["spot"], info["src"], info["sigma"] = cd["price"], cd["src"], cd["sigma"]
            p_yes = chance_yes(cd["price"], info["target"], cd["sigma"], left)
            info["chance_yes"] = p_yes
            forecasts.note(ticker, "crypto", m["close_time"], p_yes, forecasts.mid(q), info["title"])
            if q["yes_ask"] is not None:
                info["edge_yes"] = p_yes - q["yes_ask"] - fee_per_contract(q["yes_ask"])
            if q["no_ask"] is not None:
                info["edge_no"] = (1 - p_yes) - q["no_ask"] - fee_per_contract(q["no_ask"])

    if pos:
        now_px = q["yes_bid"] if pos["side"] == "yes" else q["no_bid"]
        target = min(0.99, pos["entry"] * (1 + TAKE_PROFIT))
        info["kind"] = pos.get("kind", "normal")
        if info["kind"] in ("longshot", "lateshot"):
            target = 0.95
        info["holding"] = {"side": pos["side"], "count": pos["count"], "entry": pos["entry"],
                           "now": now_px, "target": target}
        val = "" if now_px is None else f", Kalshi pays {now_px * 100:.0f}c now"
        mine = ""
        if p_yes is not None:
            ch = p_yes if pos["side"] == "yes" else 1 - p_yes
            mine = f" Bot puts its chance at {ch * 100:.0f}%."
        return out("HOLDING", f"Holding {pos['count']} {pos['side'].upper()} bought at "
                              f"{pos['entry'] * 100:.0f}c{val}.{mine} Cashes out early if Kalshi pays more than "
                              f"it's worth or at {target * 100:.0f}c; otherwise waits for the result.")
    if ticker in s["traded"]:
        return out("DONE", "Already traded this window.")
    if PAUSED:
        return out("PAUSED", "Bot is paused. Press Resume to let it trade.")
    if mins < ENTRY_START_MIN:
        return out("WAIT", f"Watching. Looks for a good deal between minute {ENTRY_START_MIN:g} and {ENTRY_END_MIN:g}.")
    if (mins > ENTRY_END_MIN or BET_MODE == "off") and not late_window:
        if LATE_ON and left > LATE_START_LEFT:
            return out("WAIT", f"Normal buying time is over. Watching for a late shot with {LATE_START_LEFT:g}-"
                               f"{LATE_STOP_LEFT:g} min left.")
        return out("PASSED", "Buying time for this window has passed. Next window soon.")
    loss_room = DAILY_LOSS_LIMIT + s["realized"] - open_cost(s, "all")
    if s["realized"] <= -DAILY_LOSS_LIMIT:
        return out("STOPPED", f"Daily loss limit reached (${s['realized']:+.2f}). Back tomorrow.")
    if not info["target"]:
        return out("NO TRADE", "Kalshi hasn't shown this window's target price yet.")
    if p_yes is None:
        return out("NO TRADE", f"Couldn't get a live {sym} price from Coinbase, Kraken or OKX, so the bot "
                               f"can't judge the odds. Skipping {info['coin']}.")

    if q["yes_bid"] is not None and q["yes_ask"] is not None:
        kalshi_yes = (q["yes_bid"] + q["yes_ask"]) / 2
        if abs(p_yes - kalshi_yes) > MAX_DISAGREE:          # safety: probably bad price data
            return out("NO TRADE", f"Bot's odds (YES {p_yes * 100:.0f}%) and Kalshi's ({kalshi_yes * 100:.0f}%) are too "
                                   f"far apart - probably a price-data mismatch. Skipping to be safe.")

    gap = info["spot"] - info["target"]
    where = (f"{sym} is ${abs(gap):,.4g} {'above' if gap >= 0 else 'below'} the target with "
             f"{left:.0f} min left. Bot's odds: YES {p_yes * 100:.0f}% / NO {(1 - p_yes) * 100:.0f}%.")
    cash = LIVE.get("balance")
    cash_room = 1e9 if cash is None else cash * 0.98 - 0.02     # keep a few cents spare for fees

    def normal_pick():
        # pick the side with the better deal
        options = []
        if info["edge_yes"] is not None:
            options.append(("yes", q["yes_ask"], info["edge_yes"], p_yes))
        if info["edge_no"] is not None:
            options.append(("no", q["no_ask"], info["edge_no"], 1 - p_yes))
        if not options:
            return out("NO TRADE", "Nobody is selling on Kalshi right now.")
        side, px, edge, chance = max(options, key=lambda o: o[2])
        info["leader"], info["price"] = side, px
        if edge < MIN_EDGE:
            best = f"best is {side.upper()} at {px * 100:.0f}c, worth {chance * 100:.0f}c to the bot"
            return out("NO TRADE", f"{where} No good deal: {best} (needs +{MIN_EDGE * 100:.0f}c after fees).")
        if px < MIN_PRICE or px > MAX_PRICE:
            return out("NO TRADE", f"{where} {side.upper()} looks good but costs {px * 100:.0f}c, outside your "
                                   f"{MIN_PRICE * 100:.0f}-{MAX_PRICE * 100:.0f}c range.")

        per = px + fee_per_contract(px)
        info["per"] = per
        info["cap"] = min(MAX_AT_RISK - open_cost(s), loss_room, cash_room)
        stake, size_note = strategies.size("crypto", chance, px, bet_size())
        budget = min(stake, MAX_AT_RISK - open_cost(s), loss_room, cash_room)
        count = math.floor(budget / per + 1e-9)
        if count < 1:
            if cash_room <= min(stake, MAX_AT_RISK - open_cost(s), loss_room):
                return out("NO TRADE", f"Good deal, but only ${max(cash or 0, 0):.2f} cash in your Kalshi account - "
                                       f"not enough for one contract at {px * 100:.0f}c. Add money in Kalshi.")
            if loss_room < stake and loss_room <= MAX_AT_RISK - open_cost(s):
                return out("NO TRADE", f"Good deal, but only ${max(loss_room, 0):.2f} of today's loss limit is left "
                                       f"- not enough for one contract at {px * 100:.0f}c.")
            if stake < per:
                return out("NO TRADE", f"{where} Small edge on {side.upper()} (+{edge * 100:.0f}c): edge sizing says it's "
                                       f"worth less than one contract{size_note}.")
            return out("NO TRADE", f"Good deal, but ${open_cost(s):.2f} is already in open bets (max ${MAX_AT_RISK:.2f}).")
        cost = count * per
        info["count"], info["cost"] = count, round(cost, 2)
        smaller = ""
        if budget < stake - 0.01:
            smaller = " (smaller bet: not much cash left)" if budget == cash_room else " (smaller bet to stay inside your limits)"
        return out("BUY", f"{where} {side.upper()} costs {px * 100:.0f}c but is worth about {chance * 100:.0f}c "
                          f"(+{edge * 100:.0f}c after fees). Buy {count} {side.upper()} = ${cost:.2f}{smaller}{size_note}: "
                          f"wins ${count - cost:.2f} if right, loses ${cost:.2f} if wrong.")

    def longshot_pick():
        info["kind"] = "longshot"
        ls = load_longshot()
        ls_open = sum(p["cost"] for p in s["open"].values() if p.get("kind") == "longshot")
        ls_room = LS_BUDGET + ls["net"] - ls_open
        if ls["net"] <= -LS_BUDGET + 0.005:
            return out("STOPPED", f"Long-shot test budget used up (net ${ls['net']:+.2f} of ${LS_BUDGET:.2f}). "
                                  f"Raise it on the dashboard to keep testing.")
        p_raw = chance_yes(info["spot"], info["target"], info['sigma'], left, vol=1.0, lo=0.001, hi=0.999)
        cands = []
        for sd, ask, bid in (("yes", q["yes_ask"], q["yes_bid"]), ("no", q["no_ask"], q["no_bid"])):
            if ask is None or ask > LS_MAX_PRICE or ask < 0.02:
                continue
            mdl = p_raw if sd == "yes" else 1 - p_raw
            mkt = (ask + bid) / 2 if bid else ask
            # cheap sides are often overpriced, so don't trust the math alone: blend it with the market's price
            ch = (1 - LS_MARKET_WEIGHT) * mdl + LS_MARKET_WEIGHT * mkt
            cands.append((sd, ask, ch, ch - ask - fee_per_contract(ask), ch / ask))
        if not cands:
            return out("NO TRADE", f"{where} Long shots: nothing priced at {LS_MAX_PRICE * 100:.0f}c or less right now.")
        sd, px, ch, edge, ratio = max(cands, key=lambda c: c[4])
        info["leader"], info["price"] = sd, px
        if ratio < LS_MIN_RATIO or edge < 0.02:
            return out("NO TRADE", f"{where} Long shot {sd.upper()} at {px * 100:.0f}c would pay {1 / px:.0f}x, but the bot "
                                   f"only gives it {ch * 100:.1f}% after blending its math with the market "
                                   f"(needs at least {px * LS_MIN_RATIO * 100:.0f}%).")
        per = px + fee_per_contract(px)
        ls_stake, size_note = smart_small_bet("longshot", ch, px, LS_MIN_BET, LS_MAX_BET)
        budget = min(ls_stake, ls_room, MAX_AT_RISK - open_cost(s), loss_room, cash_room)
        count = tiny_count(budget, per, LS_MIN_BET, LS_MAX_BET)
        if count < 1:
            return out("NO TRADE", f"{where} Long-shot deal on {sd.upper()}, but no room left "
                                   f"(test budget ${max(ls_room, 0):.2f}, cash ${max(cash or 0, 0):.2f}, "
                                   f"daily loss room ${max(loss_room, 0):.2f}).")
        cost = count * per + 0.01
        info["count"], info["cost"] = count, round(cost, 2)
        return out("BUY", f"{where} LONG SHOT: {sd.upper()} costs {px * 100:.0f}c (pays {1 / px:.0f}x) and the bot gives "
                          f"it {ch * 100:.1f}%. Buy {count} = ${cost:.2f}{size_note}: pays ${count:.2f} if it hits, "
                          f"loses ${cost:.2f} if not.")

    def late_pick():
        info["kind"] = "lateshot"
        net = strategies.get("lateshot")["live"]["net"]
        lopen = sum(p["cost"] for p in s["open"].values() if p.get("kind") == "lateshot")
        room = LATE_BUDGET + net - lopen
        if room < 0.01:
            return out("STOPPED", f"Late-shot test budget used up (net ${net:+.2f} of ${LATE_BUDGET:.2f}). "
                                  f"Raise it on the dashboard to keep going.")
        sig = info["sigma"]
        tau = max(left - 0.5, 0.2)
        z = abs(math.log(info["spot"] / info["target"])) / (sig * math.sqrt(tau))
        p_raw = chance_yes(info["spot"], info["target"], sig, left, vol=1.0, lo=0.0005, hi=0.9995)
        cands = []
        for sd, ask in (("yes", q["yes_ask"]), ("no", q["no_ask"])):
            if ask is None or ask > LATE_MAX_PRICE:
                continue
            ch = p_raw if sd == "yes" else 1 - p_raw
            cands.append((sd, ask, ch, ch / ask))
        cheapest = min([a for a in (q["yes_ask"], q["no_ask"]) if a is not None], default=None)
        if not cands:
            return out("WAIT" if left > LATE_STOP_LEFT else "NO TRADE",
                       f"{where} Late shots: nothing pays {1 / LATE_MAX_PRICE:.0f}x+ yet"
                       + (f" (cheapest side {cstr(cheapest)})." if cheapest else "."))
        sd, px, ch, ratio = max(cands, key=lambda c: c[3])
        info["leader"], info["price"] = sd, px
        gap = abs(info["spot"] - info["target"])
        if z > LATE_MAX_Z:
            return out("NO TRADE", f"{where} Late shot {sd.upper()} at {cstr(px)} pays {1 / px:.0f}x, but {sym} is "
                                   f"${gap:,.4g} away - {z:.1f} typical moves with {left:.1f} min left. Too far to flip.")
        if ratio < LATE_MIN_RATIO:
            if left > LATE_SURE_LEFT:
                return out("WAIT", f"{where} Unsure: {sd.upper()} at {cstr(px)} pays {1 / px:.0f}x, bot gives it "
                                   f"{ch * 100:.1f}% (needs {LATE_MIN_RATIO:g}x the price). Watching until "
                                   f"{LATE_SURE_LEFT:g} min left.")
            return out("NO TRADE", f"{where} Late shot {sd.upper()} at {cstr(px)}: bot gives it only {ch * 100:.1f}% "
                                   f"(needs {px * LATE_MIN_RATIO * 100:.1f}%).")
        per = px + fee_per_contract(px)
        stake, size_note = smart_small_bet("lateshot", ch, px, LATE_MIN_BET, LATE_MAX_BET)
        budget = min(stake, room, MAX_AT_RISK - open_cost(s), loss_room, cash_room)
        count = tiny_count(budget, per, LATE_MIN_BET, LATE_MAX_BET)
        if count < 1:
            return out("NO TRADE", f"{where} Late-shot deal on {sd.upper()}, but less than the ${LATE_MIN_BET:.2f} minimum fits "
                                   f"(test budget ${max(room, 0):.2f}, cash ${max(cash or 0, 0):.2f}).")
        cost = count * per + 0.01
        info["count"], info["cost"] = count, round(cost, 2)
        return out("BUY", f"LATE SHOT: {sym} is only ${gap:,.4g} from the target with {left:.1f} min left "
                          f"({z:.1f} typical moves). {sd.upper()} costs {cstr(px)} (pays {1 / px:.0f}x); bot gives it "
                          f"{ch * 100:.1f}% ({ratio:.1f}x the price). Buy {count} = ${cost:.2f}{size_note}: pays ${count:.2f} if it flips.")

    saved = None
    if in_window:
        if BET_MODE == "longshot":
            longshot_pick()
        else:
            normal_pick()
            if BET_MODE == "both" and info["decision"] != "BUY":
                saved = dict(info)
                longshot_pick()
                if info["decision"] != "BUY":
                    info.clear()
                    info.update(saved)
        if info.get("decision") == "BUY" or not late_window:
            return info
    if late_window:
        saved = dict(info) if "decision" in info else None
        late_pick()
        if info["decision"] != "BUY" and saved and saved.get("decision") not in (None, "WAIT"):
            late_reason = info["reason"]
            info.clear()
            info.update(saved)
            info["reason"] += " | " + late_reason
    return info


def bet_size():
    """Dollars for one bet: a fixed amount, or a share of your Kalshi cash."""
    cash = LIVE.get("balance")
    if STAKE_PCT > 0 and cash is not None:
        return max(1.0, round(cash * STAKE_PCT, 2))
    return STAKE_PER_TRADE


_last_reason = {}


def note_reason(info):
    key = info["ticker"]
    if info["decision"] in ("NO TRADE", "STOPPED") and _last_reason.get(key) != info["reason"]:
        _last_reason[key] = info["reason"]
        log(f"{key}: skip - {info['reason']}")


def try_enter(m, s):
    info = evaluate(m, s)
    if info["decision"] == "BUY":
        try:                                   # double-check with live order-book prices
            lq = live_quotes(m["ticker"])
            if lq:
                m = {**m, **{k + "_dollars": v for k, v in lq.items() if v is not None}}
                for k in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
                    m.pop(k, None)
                info = evaluate(m, s)
        except Exception as e:
            log(f"{m['ticker']}: order book check failed ({e}); using market prices")
    note_reason(info)
    if info["decision"] != "BUY":
        return info
    ticker, side, px, count = info["ticker"], info["leader"], info["price"], info["count"]
    if COUNCIL_ON and COUNCIL_CRYPTO and info.get("kind") == "normal":
        v = council.review("crypto 15-min", ticker, info.get("title") or ticker, side, px, info.get("chance_yes"),
                           {"Coin": info["coin"], "Live coin price": info.get("spot"), "Target price": info.get("target"),
                            "Minutes left": info.get("minutes_left"), "Kalshi YES ask": cstr(info["yes_ask"]) if info["yes_ask"] else "?",
                            "Kalshi NO ask": cstr(info["no_ask"]) if info["no_ask"] else "?",
                            "Recent volatility (typical 1-min move)": f"{(info.get('sigma') or 0) * 100:.3f}%"},
                           min_edge=MIN_EDGE, yes_ask=info.get("yes_ask"), no_ask=info.get("no_ask"))
        if v and v.get("ok") and not v["approve"]:
            info["decision"] = "NO TRADE"
            info["reason"] = f"AI team: {v['decision']} ({v['votes_for']}/5 lean {side.upper()}). {v['summary']}"
            note_reason(info)
            return info
        if v and v.get("ok") and v["approve"]:
            per = info.get("per") or (px + fee_per_contract(px))
            count = max(1, min(int(count * v["mult"]), int(info.get("cap", 1e9) // per), int(bet_size() // per)))
            info["count"], info["cost"] = count, round(count * per, 2)
            info["reason"] += (f" AI team agrees (blended {v['prob_yes'] * 100:.0f}% YES, size {v['size']}) -> buying {count} = "
                               f"${info['cost']:.2f}.")
    key = {"longshot": "longshot", "lateshot": "lateshot"}.get(info.get("kind"), "crypto")
    mode = strategies.route(key, ticker)
    if mode != "live":                          # shadow / paused: pretend bet, no money
        with LOCK:
            s["traded"].append(ticker)
        save_state(s)
        strategies.shadow_buy(key, ticker, side, px, count, info["cost"], check_after=m["close_time"],
                              title=m.get("title", ""))
        info["decision"] = "SHADOW"
        info["reason"] = (f"[{mode.upper()}] {info['reason']} - pretend bet only; "
                          f"'{strategies.label(key)}' isn't trading real money right now.")
        return info
    log(f"{ticker}: {info['reason']}")
    pot = info["pot"]
    try:
        ok, why = ensure_funds(pot, info["cost"] + 0.02)          # cost already includes the fee
    except Exception as e:
        log(f"{ticker}: could not check balance ({e})")
        return info
    if not ok:
        info["decision"], info["reason"] = "NO TRADE", why
        note_reason(info)
        LIVE["fund_problem"] = why if why == FUND_HELP else None
        return info
    LIVE["fund_problem"] = None
    try:
        before = get_balances()[1].get(pot)
    except Exception:
        before = None

    with LOCK:
        s["traded"].append(ticker)   # mark first, so a network hiccup can never cause a double buy
    save_state(s)
    try:
        if side == "yes":
            resp = send_order(ticker, "bid", info["yes_ask"], count, pot=pot)
        else:
            resp = send_order(ticker, "ask", round(1 - info["no_ask"], 4), count, pot=pot)
    except ApiError as e:
        log(f"{ticker}: order refused by Kalshi - {e}")
        if "shard" in str(e).lower():
            LIVE["fund_problem"] = FUND_HELP
        return evaluate(m, s)

    filled = int(round(fnum(resp.get("fill_count"), 0)))
    if filled <= 0:
        log(f"{ticker}: order for {count} {side.upper()} @ {px:.2f} did not fill")
        return evaluate(m, s)
    avg_yes = fnum(resp.get("average_fill_price"))
    entry = px if avg_yes is None else (avg_yes if side == "yes" else 1 - avg_yes)
    fees = fnum(resp.get("average_fee_paid"), 0.0) * filled
    total = filled * entry + fees
    if before is not None:                     # what Kalshi actually took from the account
        try:
            tot_after, pots_after = get_balances()
            LIVE["balance"] = tot_after                  # keep cash up to date for the next bet
            after = pots_after.get(pot)
            spent = None if after is None else before - after
            if spent and 0 < spent <= filled * 1.0 + 1:
                total = round(spent, 2)
                entry = max(0.01, (total - fees) / filled)
        except Exception:
            pass
    pos = {"ticker": ticker, "side": side, "count": filled, "entry": entry, "pot": pot, "kind": info.get("kind", "normal"),
           "strategy": key,
           "cost": total, "close_time": m["close_time"], "title": m.get("title", "")}
    with LOCK:
        s["spent"] += filled * entry
        s["open"][ticker] = pos
    log(f"BUY  {ticker}: {filled} {side.upper()} @ {entry:.2f} = ${total:.2f} (fees ${fees:.2f})")
    record("BUY", pos, filled, entry, total, None, s,
           {"longshot": "long shot", "lateshot": "late shot"}.get(pos["kind"], ""))
    return evaluate(m, s)


def try_take_profit(m, s):
    pos = s["open"].get(m["ticker"])
    if not pos:
        return
    q = quotes(m)
    bid = q["yes_bid"] if pos["side"] == "yes" else q["no_bid"]
    if bid is None:
        return
    target = min(0.99, pos["entry"] * (1 + TAKE_PROFIT))
    if pos.get("kind") in ("longshot", "lateshot"):
        target = 0.95                      # let long/late shots ride for the big payout
    why = "take profit" if bid >= target else None
    if not why:
        info = evaluate(m, s, think=True)
        if info["chance_yes"] is not None:
            worth = info["chance_yes"] if pos["side"] == "yes" else 1 - info["chance_yes"]
            if bid - fee_per_contract(bid) >= worth + MIN_EDGE:
                why = f"Kalshi pays {bid * 100:.0f}c, bot thinks it's worth {worth * 100:.0f}c"
    if not why:
        return
    if pos["side"] == "yes":
        resp = send_order(pos["ticker"], "ask", q["yes_bid"], pos["count"], reduce_only=True, pot=pos.get("pot"))
    else:
        resp = send_order(pos["ticker"], "bid", round(1 - q["no_bid"], 4), pos["count"], reduce_only=True,
                          pot=pos.get("pot"))
    sold = int(round(fnum(resp.get("fill_count"), 0)))
    if sold <= 0:
        return
    avg_yes = fnum(resp.get("average_fill_price"))
    sell_px = bid if avg_yes is None else (avg_yes if pos["side"] == "yes" else 1 - avg_yes)
    fees = fnum(resp.get("average_fee_paid"), 0.0) * sold
    cost_part = pos["cost"] * sold / pos["count"]
    pnl = sold * sell_px - fees - cost_part
    with LOCK:
        s["realized"] += pnl
        pos["count"] -= sold
        pos["cost"] -= cost_part
        if pos["count"] <= 0:
            del s["open"][pos["ticker"]]
    log(f"SELL {pos['ticker']}: {sold} {pos['side'].upper()} @ {sell_px:.2f} ({why}), "
        f"P&L ${pnl:+.2f} | today ${s['realized']:+.2f}")
    record("SELL", pos, sold, sell_px, sold * sell_px - fees, pnl, s, why)
    if pos.get("kind") == "longshot":
        longshot_result(pnl, pnl > 0, sold * sell_px - fees)
    strategies.record(strategy_of(pos), pnl, cost_part, pnl > 0)


def strategy_of(pos):
    if pos.get("strategy"):
        return pos["strategy"]
    k = pos.get("kind")
    if k in ("longshot", "lateshot"):
        return k
    if k == "sports":
        return "sports:" + str(pos.get("sport_key") or "other")
    if k == "sports_late":
        return "sports_late"
    if k == "politics":
        return "politics"
    return "crypto"


def lookup_result(ticker, event=None):
    """'yes' / 'no' once Kalshi has settled the market, else None."""
    m = None
    try:
        m = get_market(ticker)
    except Exception:
        if event:
            try:
                ev = api("GET", f"/events/{event}", params={"with_nested_markets": "true"})
                ev = ev.get("event", ev)
                m = next((x for x in ev.get("markets") or [] if x.get("ticker") == ticker), None)
            except Exception:
                return None
    r = ((m or {}).get("result") or "").lower()
    return r if r in ("yes", "no") else None


def buy_real(ticker, side, px, count, est_cost, pot, kind, strategy, extra, reason, s):
    """Place a real buy (sports/politics) and record the position."""
    ok, why = ensure_funds(pot, est_cost + 0.02)
    if not ok:
        log(f"{ticker}: {why}")
        return None
    try:
        before = get_balances()[1].get(pot)
    except Exception:
        before = None
    with LOCK:
        if ticker not in s["traded"]:
            s["traded"].append(ticker)
    save_state(s)
    log(f"{ticker}: {reason}")
    try:
        if side == "yes":
            resp = send_order(ticker, "bid", px, count, pot=pot)
        else:
            resp = send_order(ticker, "ask", round(1 - px, 4), count, pot=pot)
    except ApiError as e:
        log(f"{ticker}: order refused by Kalshi - {e}")
        return None
    filled = int(round(fnum(resp.get("fill_count"), 0)))
    if filled <= 0:
        log(f"{ticker}: order for {count} {side.upper()} @ {px:.2f} did not fill")
        return None
    fees = fnum(resp.get("average_fee_paid"), 0.0) * filled
    total = filled * px + fees
    if before is not None:
        try:
            tot, pots = get_balances()
            LIVE["balance"] = tot
            spent = before - pots.get(pot, before)
            if 0 < spent <= filled + 1:
                total = round(spent, 2)
        except Exception:
            pass
    entry = max(0.01, (total - fees) / filled)
    pos = {"ticker": ticker, "side": side, "count": filled, "entry": entry, "pot": pot, "kind": kind,
           "strategy": strategy, "cost": total, **extra}
    with LOCK:
        s["spent"] += filled * entry
        s["open"][ticker] = pos
    log(f"BUY  {ticker}: {filled} {side.upper()} @ {entry:.2f} = ${total:.2f}")
    record("BUY", pos, filled, entry, total, None, s, f"{kind} {strategies.label(strategy)}")
    return pos


def settle_finished(s):
    for ticker, pos in list(s["open"].items()):
        if utcnow() < parse_time(pos["close_time"]) + dt.timedelta(seconds=20):
            continue
        gap = {"sports": 300, "sports_late": 120, "politics": 3600}.get(pos.get("kind"), 0)
        if gap:                                             # long events: don't ask Kalshi every 15 seconds
            if time.time() - pos.get("_checked", 0) < gap:
                continue
            pos["_checked"] = time.time()
        result = lookup_result(ticker, pos.get("event"))
        if result not in ("yes", "no"):
            continue
        won = result == pos["side"]
        payout = pos["count"] * 1.0 if won else 0.0
        pnl = payout - pos["cost"]
        with LOCK:
            s["realized"] += pnl
            del s["open"][ticker]
        log(f"{'WIN ' if won else 'LOSS'} {ticker}: {pos['count']} {pos['side'].upper()} -> "
            f"P&L ${pnl:+.2f} | today ${s['realized']:+.2f}")
        record("SETTLE", pos, pos["count"], 1.0 if won else 0.0, payout, pnl, s,
               ("won" if won else "lost") + {"longshot": " long shot", "lateshot": " late shot",
                                            "sports_late": " late shot"}.get(pos.get("kind"), ""))
        if pos.get("kind") == "longshot":
            longshot_result(pnl, won, payout)
        strategies.record(strategy_of(pos), pnl, pos["cost"], won)
        if s["realized"] <= -DAILY_LOSS_LIMIT:
            log(f"Daily loss limit reached (${s['realized']:+.2f}). No more trades until tomorrow.")


# ----------------------------- dashboard -------------------------------

SUPPORT_AFTER_DAYS = 7         # thank-you note appears only after a week of use ...
SUPPORT_AFTER_BETS = 50        # ... or 50 finished bets, and only on a day you're up
SUPPORT_SNOOZE_DAYS = 30       # "Maybe later" hides it this long
SUPPORT_THANKS_DAYS = 90       # after someone clicks "Buy me a coffee"


def load_support():
    try:
        with open(SUPPORT_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    if "first_run" not in d:
        d["first_run"] = today()
        save_support(d)
    return d


def save_support(d):
    try:
        with open(SUPPORT_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
    except OSError:
        pass


def support_status(stats, today_pnl):
    if not SUPPORT_URL:
        return {"url": "", "ask": False}
    d = load_support()
    days = (dt.date.today() - dt.date.fromisoformat(d["first_run"])).days
    used_enough = days >= SUPPORT_AFTER_DAYS or stats["settled"] >= SUPPORT_AFTER_BETS
    snoozed = d.get("snooze_until") and today() < d["snooze_until"]
    ask = bool(used_enough and not d.get("never") and not snoozed and today_pnl > 0)
    return {"url": SUPPORT_URL, "ask": ask}


def support_action(action):
    d = load_support()
    if action == "never":
        d["never"] = True
    else:
        days = SUPPORT_THANKS_DAYS if action == "clicked" else SUPPORT_SNOOZE_DAYS
        d["snooze_until"] = (dt.date.today() + dt.timedelta(days=days)).isoformat()
    save_support(d)

def stats_from_trades(rows):
    results, buys, days = [], [], {}
    cum, series = 0.0, []
    for r in rows:
        if r.get("event") == "BUY":
            p = fnum(r.get("price"))
            if p is not None:
                buys.append(p)
        pnl = fnum(r.get("pnl_$"))
        if r.get("event") in ("SETTLE", "SELL") and pnl is not None:
            results.append(pnl)
            cum += pnl
            series.append({"t": r["time"], "v": round(cum, 2)})
            d = r["time"][:10]
            day = days.setdefault(d, {"day": d, "pnl": 0.0, "wins": 0, "losses": 0})
            day["pnl"] = round(day["pnl"] + pnl, 2)
            day["wins" if pnl > 0 else "losses"] += 1
    n = len(results)
    wins = sum(1 for p in results if p > 0)
    avg_price = sum(buys) / len(buys) if buys else None
    return {
        "total_pnl": round(cum, 2), "settled": n, "wins": wins, "losses": n - wins,
        "win_rate": wins / n if n else None, "avg_price": avg_price,
        "best": max(results) if results else None, "worst": min(results) if results else None,
        "series": series[-500:], "days": sorted(days.values(), key=lambda x: x["day"], reverse=True)[:30],
    }


def status_payload():
    rows = read_trades()
    with LOCK:
        s = STATE or new_day_state()
        payload = {
            "now": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "paused": PAUSED,
            "settings": current_settings(),
            "known_series": KNOWN_SERIES,
            "today": {"day": s["day"], "pnl": round(s["realized"], 2), "spent": round(s["spent"], 2),
                      "open_cost": round(open_cost(s, "all"), 2), "loss_left": round(DAILY_LOSS_LIMIT + s["realized"], 2)},
            "open": list(s["open"].values()),
            "live": dict(LIVE),
            "log": list(LOG_TAIL[-25:]),
        }
    payload["stats"] = stats_from_trades(rows)
    payload["support"] = support_status(payload["stats"], payload["today"]["pnl"])
    payload["sports"] = sports.status()
    payload["strategies"] = strategies.status()
    payload["politics"] = politics.status()
    payload["council"] = council.status()
    try:
        fc = forecasts.status()
        for c in fc["cats"]:
            c["ai_weight"] = forecasts.ai_weight(c["cat"], council.BASE_AI_WEIGHT[c["cat"]])
        payload["forecasts"] = fc
    except Exception as e:
        payload["forecasts"] = {"cats": [], "waiting": 0, "total": 0, "error": str(e)}
    ls = load_longshot()
    with LOCK:
        ls_open = sum(p["cost"] for p in (STATE or {"open": {}})["open"].values() if p.get("kind") == "longshot")
    payload["longshot"] = {**ls, "open": round(ls_open, 2), "left": round(max(0.0, LS_BUDGET + ls["net"] - ls_open), 2),
                           "budget": LS_BUDGET}
    payload["trades"] = rows[-100:][::-1]
    return payload


class Dashboard(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                with open(DASHBOARD_FILE, "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                return self.send(404, "dashboard.html is missing from the bot folder", "text/plain")
        if self.path.startswith("/api/status"):
            return self.send(200, json.dumps(status_payload()))
        self.send(404, "{}")

    def do_POST(self):
        if self.path not in ("/api/settings", "/api/support", "/api/strategy"):
            return self.send(404, "{}")
        # only accept requests from the dashboard page itself
        origin = self.headers.get("Origin", "")
        if origin and not origin.startswith((f"http://localhost:{DASHBOARD_PORT}", f"http://127.0.0.1:{DASHBOARD_PORT}")):
            return self.send(403, json.dumps({"errors": ["Not allowed"]}))
        try:
            n = int(self.headers.get("Content-Length", 0))
            d = json.loads(self.rfile.read(min(n, 10000)) or b"{}")
        except ValueError:
            return self.send(400, json.dumps({"errors": ["Bad request"]}))
        if self.path == "/api/strategy":
            ok = strategies.set_mode(str(d.get("key", "")), str(d.get("mode", "")))
            return self.send(200 if ok else 400, json.dumps({"ok": ok}))
        if self.path == "/api/support":
            support_action(str(d.get("action", "later")))
            return self.send(200, json.dumps({"ok": True}))
        errs = apply_settings(d)
        if errs:
            return self.send(400, json.dumps({"errors": errs}))
        save_settings()
        if "PAUSED" in d and len(d) == 1:
            log("Bot PAUSED from dashboard" if PAUSED else "Bot RESUMED from dashboard")
        else:
            log("Settings changed from dashboard: " + ", ".join(f"{k}={v}" for k, v in d.items() if k != "PAUSED"))
        self.send(200, json.dumps({"ok": True, "settings": current_settings()}))


def start_dashboard():
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", DASHBOARD_PORT), Dashboard)
    except OSError as e:
        log(f"Dashboard could not start on port {DASHBOARD_PORT} ({e}). Is the bot already running?")
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://localhost:{DASHBOARD_PORT}"
    log(f"Dashboard: {url}  (open this in your browser)")
    if not os.environ.get("BOT_NO_BROWSER"):
        try:
            webbrowser.open(url)
        except Exception:
            pass


# ------------------------------- main ----------------------------------

MARKET_CACHE = {}


def needs_refresh(series, s):
    """Only ask Kalshi for fresh prices when the bot might act, to stay under Kalshi's request limits."""
    c = MARKET_CACHE.get(series)
    if not c:
        return True
    age = time.time() - c["t"]
    if not c["markets"]:
        return age > 30                                   # no market yet: check twice a minute
    for m in c["markets"]:
        now = utcnow()
        if now >= parse_time(m["close_time"]):
            return True                                   # window over: get the next one
        if m["ticker"] in s["open"]:
            return True                                   # holding: watch for cash-out price
        mins = (now - parse_time(m["open_time"])).total_seconds() / 60
        if (ENTRY_START_MIN - 0.25 <= mins <= ENTRY_END_MIN + 0.1
                and m["ticker"] not in s["traded"] and not PAUSED and BET_MODE != "off"):
            return True                                   # buying time: need live prices
        left = (parse_time(m["close_time"]) - now).total_seconds() / 60
        if (LATE_ON and LATE_STOP_LEFT - 0.1 <= left <= LATE_START_LEFT + 0.25
                and m["ticker"] not in s["traded"] and not PAUSED):
            return True                                   # late-shot time: need live prices
    return age > 90                                       # otherwise refresh the display now and then

def check_mode():
    log("CHECK MODE - no orders will be placed")
    log(f"Connected. Cash balance: ${get_balance():.2f}")
    s = new_day_state()
    for series in SERIES:
        mkts = get_open_markets(series)
        log(f"{series}: {len(mkts)} open market(s)")
        for m in mkts:
            info = evaluate(m, s, think=True)
            log(f"  {m['ticker']}  {m.get('title', '')}")
            log(f"    minute {info['minute']} of 15 | YES bid/ask {info['yes_bid']}/{info['yes_ask']} "
                f"| NO bid/ask {info['no_bid']}/{info['no_ask']}")
            log(f"    bot would: {info['decision']} - {info['reason']}")
    try:
        sports.discover_series()
        sports.check(s)
    except Exception as e:
        log(f"Sports check failed: {e}")
    try:
        politics.check(s)
    except Exception as e:
        log(f"Politics check failed: {e}")
    log("Check finished. If you see a balance and markets above, run run.bat to trade.")


def main():
    global STATE
    sports.B = strategies.B = politics.B = council.B = forecasts.B = sys.modules[__name__]
    if "PASTE" in API_KEY_ID:
        sys.exit("Open bot.py in Notepad and paste your Kalshi API Key ID into API_KEY_ID.")
    load_key()
    load_settings()
    if "--check" in sys.argv:
        try:
            check_mode()
        except ApiError as e:
            log(f"Check failed: {e}")
            if e.status == 401:
                log("401 = Kalshi rejected the key. Check API_KEY_ID and the key file match.")
        return

    log("=" * 60)
    size = f"{STAKE_PCT * 100:.0f}% of your cash" if STAKE_PCT > 0 else f"${STAKE_PER_TRADE:.2f}"
    log(f"Bot started - LIVE MONEY. {size}/bet (never more than your cash), max in open bets ${MAX_AT_RISK:.2f}, "
        f"daily loss limit ${DAILY_LOSS_LIMIT:.2f}")
    log(f"Bet sizing: {'by edge (' + format(KELLY_FRACTION * 100, '.0f') + '% Kelly), capped at each per-bet amount' if SIZING == 'edge' else 'fixed amounts'}. "
        f"New strategies prove themselves with pretend bets first; losing ones pause themselves.")
    if COUNCIL_ON:
        log("AI team: " + (f"ON - {council.describe()}. Researcher, quant, skeptic, news analyst, contrarian, risk manager "
                           f"+ judge review bets. Daily AI budget ${COUNCIL_DAILY_BUDGET:.2f}." if council.key_set()
                           else "add ANTHROPIC_API_KEY and/or OPENAI_API_KEY in bot.py to turn it on (the bot works without it)."))
        if COUNCIL_PROVIDER == "mix" and council.key_set() and len(council.providers()) == 1:
            log(f"AI team: set to mix, but only the {council.NAMES[council.providers()[0]]} key is pasted - using that one.")
        if COUNCIL_PROVIDER in ("claude", "gpt") and council.key_set() and COUNCIL_PROVIDER not in council.providers():
            log(f"AI team: set to {COUNCIL_PROVIDER} but that key isn't pasted - using {council.describe()} instead.")
    if SPORTS_ON:
        log(f"Sports: ${SPORTS_STAKE:.2f}/game on {', '.join(sports.SPORTS[k][0] for k in SPORTS_LIST)} when Kalshi is "
            f"{SPORTS_MIN_EDGE * 100:.0f}c+ cheaper than the sportsbooks' fair odds, in the last {SPORTS_BET_HOURS:g}h "
            f"before start." + ("" if sports.odds_key_set() else " (add ODDS_API_KEY in bot.py to turn this on)"))
    if LATE_ON:
        log(f"Late shots: ${LATE_MIN_BET:.2f}-${LATE_MAX_BET:.2f} (sized by how good the deal is) on sides costing {LATE_MAX_PRICE * 100:g}c or less (pays {1 / LATE_MAX_PRICE:.0f}x+) "
            f"with {LATE_START_LEFT:g}-{LATE_STOP_LEFT:g} min left, only when the price is within {LATE_MAX_Z:g} typical "
            f"moves of the target and the bot's chance is {LATE_MIN_RATIO:g}x the price. Test budget ${LATE_BUDGET:.2f}.")
    if BET_MODE not in ("normal", "off"):
        log(f"Long-shot mode ({BET_MODE}): ${LS_MIN_BET:.2f}-${LS_MAX_BET:.2f} bets (sized by the deal) on sides costing {LS_MAX_PRICE * 100:.0f}c or less when "
            f"the bot's odds are {LS_MIN_RATIO:g}x the price. Test budget ${LS_BUDGET:.2f} total.")
    log(f"Strategy: compares each coin's live price with Kalshi's target and bets YES or NO only when "
        f"Kalshi's price is at least {MIN_EDGE * 100:.0f}c better than the bot's odds (after fees).")
    try:
        LIVE["balance"] = get_balance()
        log(f"Cash balance: ${LIVE['balance']:.2f}")
    except ApiError as e:
        sys.exit(f"Could not connect to Kalshi: {e}")
    LIVE["started"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    discover_series()
    sports.discover_series()
    load_settings()          # again, in case settings.json picked a newly found coin
    STATE = load_state()
    start_dashboard()
    log("Press Ctrl+C (or close this window) to stop.")

    loops = 0
    hour = {"t": time.time(), "seen": 0, "counts": {}, "bets": 0}
    while True:
        s = STATE
        try:
            if s["day"] != today():
                log(f"New day. Yesterday: spent ${s['spent']:.2f}, P&L ${s['realized']:+.2f}")
                with LOCK:
                    STATE = s = new_day_state(s)
            LIVE["balance"] = get_balance()          # bets are sized against your real cash
            markets = []
            for series in (list(SERIES) if (BET_MODE != "off" or LATE_ON) else []):
                if needs_refresh(series, s):
                    MARKET_CACHE[series] = {"t": time.time(), "markets": get_open_markets(series)}
                    fresh = True
                else:
                    fresh = False
                for m in MARKET_CACHE[series]["markets"]:
                    m.setdefault("series_ticker", series)
                    if not fresh:
                        markets.append(evaluate(m, s))
                    elif m["ticker"] in s["open"]:
                        try_take_profit(m, s)
                        markets.append(evaluate(m, s))
                    else:
                        markets.append(try_enter(m, s))
            settle_finished(s)
            try:
                sports.tick(s)
            except Exception as e:
                log(f"Sports error: {e!r}")
            try:
                politics.tick(s)
            except Exception as e:
                log(f"Politics error: {e!r}")
            try:
                strategies.settle_virtual()
            except Exception as e:
                log(f"Shadow-bet check error: {e!r}")
            try:
                forecasts.settle()
            except Exception as e:
                log(f"Forecast scorecard error: {e!r}")
            for mk in markets:
                hour["seen"] += 1
                dec = mk.get("decision", "?")
                hour["counts"][dec] = hour["counts"].get(dec, 0) + 1
            if time.time() - hour["t"] >= 3600:
                top = ", ".join(f"{k} {v}" for k, v in sorted(hour["counts"].items(), key=lambda x: -x[1])[:5])
                st_rows = strategies.status()["rows"]
                modes = ", ".join(f"{r['label']}: {r['mode']}" for r in st_rows) or "none yet"
                log(f"HOURLY SUMMARY: {hour['seen']} crypto checks ({top}). Strategies - {modes}. "
                    f"Today P&L ${s['realized']:+.2f}, cash ${LIVE.get('balance') or 0:.2f}.")
                hour = {"t": time.time(), "seen": 0, "counts": {}, "bets": 0}
            with LOCK:
                LIVE["markets"] = markets
                LIVE["updated"] = dt.datetime.now().strftime("%H:%M:%S")
                LIVE["error"] = None
            save_state(s)
        except KeyboardInterrupt:
            raise
        except ApiError as e:
            if e.status == 429:
                log("Kalshi says too many requests - slowing down and retrying.")
            else:
                log(f"Kalshi error: {e}")
            LIVE["error"] = str(e)
            if e.status == 401:
                log("Key rejected - check API_KEY_ID and your key file.")
            save_state(s)
        except Exception as e:
            log(f"Error: {e!r} - will retry")
            LIVE["error"] = repr(e)
            save_state(s)
        loops += 1
        time.sleep(CHECK_EVERY_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped by you.")
