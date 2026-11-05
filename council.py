"""
AI team ("council"): 6 AI agents debate a bet before the bot places it.

Works with Claude, GPT (OpenAI), or a MIX of both (COUNCIL_PROVIDER in bot.py / dashboard):
  * "mix"    - the five analysts are split between Claude and GPT, so two different "brains" look
               at every bet; the researcher and judge use Claude. If one AI fails, the other covers.
  * "claude" - all agents on Claude.     * "gpt" - all agents on GPT.
If you only pasted one key, the team simply uses that one.

  1. Researcher  - searches the web for the latest news on the question (politics/sports).
  2-6. Analysts  - five different personalities each give their own chance for YES and a vote:
        Quant (trusts the numbers), Skeptic (hunts for why the bet is wrong), News analyst,
        Contrarian (asks what the crowd is missing), Risk manager (fees, timing, bad data).
  7. Judge       - reads all of it and makes the final call: BUY YES / BUY NO / SKIP, its chance,
                   and a size (small / normal / large).

Since v2 the agents forecast BLIND (no proposed side, no bot odds; half are asked about NO) and the auditor
only combines their numbers and looks for red flags. The CODE blends the AI's odds with the bot's math
(AI weight grows or shrinks with its measured track record) and checks both sides after fees. The team can:
  * veto a bet the math liked,
  * make a bet smaller or bigger (within your limits),
  * approve a "near miss" the math almost liked, if the judge's odds make it a real deal.

Costs: a few cents per debate (web searches $0.01 each). Hard daily cap: COUNCIL_DAILY_BUDGET.
"""

import concurrent.futures as cf
import datetime as dt
import json
import re
import threading
import time

import requests

B = None
API = "https://api.anthropic.com/v1/messages"
OPENAI_API = "https://api.openai.com/v1/responses"
PRICES = {  # $ per million tokens (input, output)
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-sol": (4.00, 20.00),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.5": (5.00, 30.00),
}
SEARCH_COST = 0.01        # both Claude and OpenAI charge about $10 per 1,000 web searches
NAMES = {"claude": "Claude", "gpt": "GPT"}
CACHE_MIN = 10            # re-use a verdict on the same market+side for this many minutes

PERSONAS = [
    ("Quant", "You trust numbers. Start from the bot's math estimate and the market price, adjust only for concrete evidence."),
    ("Skeptic", "Your job is to find why this bet is WRONG: stale data, mismatched question, rules gotchas, the market knowing something."),
    ("News analyst", "Focus on the latest news, injuries, polls, announcements and what they imply for the outcome."),
    ("Contrarian", "Ask what the crowd might be over- or under-reacting to. Only disagree with the market when you can say why."),
    ("Risk manager", "Weigh fees, time left, how much can still change, and whether the edge is big enough to be worth real money."),
]

_lock = threading.Lock()
SPEND = {"day": None, "usd": 0.0, "debates": 0}
CACHE = {}                # (ticker, side) -> (time, verdict)
LOG = []                  # recent debates for the dashboard
_WARNED = {}              # provider -> last time we logged its failure


def _has(name):
    k = getattr(B, name, "") or ""
    return bool(k.strip()) and "PASTE" not in k


def claude_ok():
    return _has("ANTHROPIC_API_KEY")


def gpt_ok():
    return _has("OPENAI_API_KEY")


def key_set():
    return claude_ok() or gpt_ok()


def providers():
    """Which AIs the team uses right now, honouring COUNCIL_PROVIDER and which keys are pasted."""
    want = getattr(B, "COUNCIL_PROVIDER", "mix")
    have = [p for p, ok in (("claude", claude_ok()), ("gpt", gpt_ok())) if ok]
    if want in ("claude", "gpt") and want in have:
        return [want]
    return have          # "mix" -> both if both keys are there; otherwise whichever one works


def model_for(provider, role):
    if provider == "gpt":
        return B.COUNCIL_GPT_JUDGE_MODEL if role == "judge" else B.COUNCIL_GPT_AGENT_MODEL
    return B.COUNCIL_JUDGE_MODEL if role == "judge" else B.COUNCIL_AGENT_MODEL


def spent_today():
    today = dt.date.today().isoformat()
    with _lock:
        if SPEND["day"] != today:
            SPEND.update(day=today, usd=0.0, debates=0)
        return SPEND["usd"]


def _add_cost(usd):
    spent_today()
    with _lock:
        SPEND["usd"] += usd


def _price(model, u_in, u_out, searches):
    pin, pout = PRICES.get(model, (2.0, 12.0))
    cost = (u_in * pin + u_out * pout) / 1e6 + searches * SEARCH_COST
    _add_cost(cost)
    return cost


def call_claude(model, system, user, max_tokens=500, web=False):
    body = {"model": model, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}]}
    if web:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
    r = requests.post(API, json=body, timeout=90, headers={
        "x-api-key": B.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    if r.status_code >= 400:
        raise RuntimeError(f"Claude API {r.status_code}: {r.text[:200]}")
    d = r.json()
    u = d.get("usage") or {}
    cost = _price(model, u.get("input_tokens", 0), u.get("output_tokens", 0),
                  (u.get("server_tool_use") or {}).get("web_search_requests", 0))
    text = "\n".join(b.get("text", "") for b in d.get("content") or [] if b.get("type") == "text").strip()
    return text, cost


def call_gpt(model, system, user, max_tokens=500, web=False):
    # OpenAI Responses API. Reasoning models count thinking in max_output_tokens, so give them room.
    body = {"model": model, "instructions": system, "input": user, "max_output_tokens": max(1500, max_tokens * 3),
            "reasoning": {"effort": "low"}}
    if web:
        body["tools"] = [{"type": "web_search"}]
    hdr = {"Authorization": "Bearer " + B.OPENAI_API_KEY, "Content-Type": "application/json"}
    r = requests.post(OPENAI_API, json=body, timeout=90, headers=hdr)
    if r.status_code == 400 and "reasoning" in r.text:          # model without reasoning settings
        body.pop("reasoning")
        r = requests.post(OPENAI_API, json=body, timeout=90, headers=hdr)
    if r.status_code >= 400:
        raise RuntimeError(f"OpenAI API {r.status_code}: {r.text[:200]}")
    d = r.json()
    u = d.get("usage") or {}
    out = d.get("output") or []
    searches = sum(1 for o in out if o.get("type") == "web_search_call")
    cost = _price(model, u.get("input_tokens", 0), u.get("output_tokens", 0), searches)
    text = d.get("output_text") or "\n".join(c.get("text", "") for o in out if o.get("type") == "message"
                                             for c in o.get("content") or [] if c.get("type") == "output_text")
    return text.strip(), cost


def call(provider, role, system, user, max_tokens=500, web=False):
    """Ask one AI. If it fails and the other AI is available, the other one answers instead.
    Returns (text, cost, provider_used)."""
    order = [provider] + [p for p in providers() if p != provider]
    err = None
    for p in order:
        try:
            fn = call_gpt if p == "gpt" else call_claude
            txt, c = fn(model_for(p, role), system, user, max_tokens, web)
            return txt, c, p
        except Exception as e:      # noqa: BLE001 - try the other AI
            err = e
            if len(order) > 1 and p == provider and time.time() - _WARNED.get(p, 0) > 300:
                _WARNED[p] = time.time()
                B.log(f"AI team: {NAMES[p]} failed ({str(e)[:120]}); asking {NAMES[order[1]]} instead.")
    raise err


def parse_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def _num(v, lo=0.0, hi=1.0):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x > 1 and hi == 1.0:
        x /= 100.0                      # "62" -> 0.62
    return min(hi, max(lo, x))


BASE_AI_WEIGHT = {"crypto": 0.15, "sports": 0.30, "politics": 0.40}   # how much the AI's odds count vs the math


def category(kind):
    k = kind.lower()
    return "crypto" if k.startswith("crypto") else "sports" if k.startswith("sports") else "politics"


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return None if not n else xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _edge(p_side, ask):
    return None if ask is None or p_side is None else p_side - ask - B.fee_per_contract(ask)


def review(kind, ticker, question, side, price, math_prob, details, research=False,
           min_edge=0.03, yes_ask=None, no_ask=None):
    """Ask the team about a market. The agents forecast BLIND: they never see the proposed side or the bot's own odds,
    and half of them are asked for the chance of NO (so nobody is nudged toward YES).
    The code (not the AI) then blends the AI's odds with the bot's math, checks BOTH sides against the real prices
    after fees, and approves only if the proposed side still has at least `min_edge`.
    Returns a verdict dict, or None if the team is unavailable (then the bot decides on its own math)."""
    if not getattr(B, "COUNCIL_ON", False) or not key_set():
        return None
    if side == "yes" and yes_ask is None:
        yes_ask = price
    if side == "no" and no_ask is None:
        no_ask = price
    ck = (ticker, side)
    hit = CACHE.get(ck)
    if hit and time.time() - hit[0] < CACHE_MIN * 60:
        return hit[1]
    if spent_today() >= B.COUNCIL_DAILY_BUDGET:
        return {"ok": False, "skip_reason": f"AI team's daily budget (${B.COUNCIL_DAILY_BUDGET:.2f}) used up",
                "approve": None}
    cat = category(kind)
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    facts = "\n".join(f"- {k}: {v}" for k, v in details.items() if "bot" not in k.lower())   # raw data only
    case = (f"Time now: {now} (US Central).\nPrediction market on Kalshi ({kind}): {question}\n"
            f"Market prices right now: YES costs {'?' if yes_ask is None else f'{yes_ask * 100:.1f}c'}, "
            f"NO costs {'?' if no_ask is None else f'{no_ask * 100:.1f}c'} (a contract pays $1 if right).\n"
            f"Facts:\n{facts}\n")
    t0 = time.time()
    cost = 0.0
    brief = ""
    team = providers()
    lead = "claude" if "claude" in team else team[0]       # researcher + auditor
    seats = [team[i % len(team)] for i in range(len(PERSONAS))]   # mix: alternate Claude / GPT
    asks_no = [i % 2 == 1 for i in range(len(PERSONAS))]         # half the analysts estimate NO instead of YES
    try:
        if research:
            brief, c, _ = call(lead, "agent",
                               "You are a fast research assistant for a prediction-market forecaster. Search for the latest, "
                               "most relevant facts about the question (news, injuries, polls, official statements, schedule, "
                               "resolution rules). Reply with at most 8 short bullet points with dates. No opinions.",
                               case + "\nFind the latest facts that matter for this outcome.", max_tokens=600, web=True)
            cost += c
        user = case + (f"\nResearcher's notes:\n{brief}\n" if brief else "")

        def ask(args):
            (name, style), prov, flip = args
            target = "NO" if flip else "YES"
            sysp = (f"You are an independent forecaster on a team; you work alone and never see the others. "
                    f"Estimate the probability that this market resolves {target}. The market price is a strong "
                    f"baseline that is usually well calibrated; move away from it only for concrete evidence. "
                    f"First list the best evidence for YES and the best evidence for NO, then give your number. "
                    f"Your style: {name}. {style}\n"
                    f'Reply ONLY with JSON: {{"evidence_yes": "...", "evidence_no": "...", "prob_{target.lower()}": 0-1, '
                    f'"confidence": 0-1}}')
            txt, c, used = call(prov, "agent", sysp, user, max_tokens=350)
            j = parse_json(txt) or {}
            pv = _num(j.get("prob_" + target.lower()))
            if pv is None:                                   # model answered with the other field
                alt = _num(j.get("prob_" + ("yes" if flip else "no")))
                pv = None if alt is None else 1 - alt
            py = None if pv is None else (1 - pv if flip else pv)
            return {"agent": name, "ai": NAMES[used], "asked": target, "prob_yes": py, "confidence": _num(j.get("confidence")),
                    "reason": f"YES: {str(j.get('evidence_yes', ''))[:150]} | NO: {str(j.get('evidence_no', ''))[:150]}"
                              if j else txt[:200], "cost": c}

        with cf.ThreadPoolExecutor(max_workers=len(PERSONAS)) as ex:
            agents = list(ex.map(ask, zip(PERSONAS, seats, asks_no)))
        cost += sum(a["cost"] for a in agents)
        ps = [a["prob_yes"] for a in agents if a["prob_yes"] is not None]
        if not ps:
            raise RuntimeError("no analyst gave a usable number")
        listing = "\n".join(f"- {a['agent']} ({a['ai']}): P(YES)={'?' if a['prob_yes'] is None else round(a['prob_yes'], 3)}"
                            f" - {a['reason']}" for a in agents)
        jt, c, judge_ai = call(lead, "judge",
                               "You are the team's auditor. Do NOT give a new opinion on who wins. Combine the forecasters' "
                               "numbers (weight the ones with concrete evidence more) into one probability, and check for "
                               "red flags that make ANY bet unsafe: the facts don't match the question, data looks stale or "
                               "wrong, resolution-rule gotchas, or the outcome may already be known. "
                               'Reply ONLY with JSON: {"prob_yes": 0-1, "red_flag": true|false, '
                               '"red_flag_reason": "...", "summary": "two short sentences for the owner"}',
                               user + f"\nForecasters:\n{listing}\n", max_tokens=400)
        cost += c
        j = parse_json(jt) or {}
    except Exception as e:
        B.log(f"AI team unavailable ({e}); the bot decides on its own math.")
        return None

    med = _median(ps)
    pj = _num(j.get("prob_yes"))
    ai_p = med if pj is None else 0.5 * med + 0.5 * pj
    by_ai = {}
    for a in agents:
        if a["prob_yes"] is not None:
            by_ai.setdefault(a["ai"].lower(), []).append(a["prob_yes"])
    by_ai = {k: _median(v) for k, v in by_ai.items()}
    # how much the AI counts: base weight, adjusted by its measured track record (forecast scorecard)
    w = BASE_AI_WEIGHT[cat]
    try:
        import forecasts
        w = forecasts.ai_weight(cat, w)
        forecasts.add(ticker, {**{k: v for k, v in by_ai.items()}, "ai": ai_p})
    except Exception:
        pass
    if math_prob is None:
        mids = [x for x in (yes_ask, None if no_ask is None else 1 - no_ask) if x is not None]
        base = sum(mids) / len(mids) if mids else ai_p
    else:
        base = math_prob
    p = (1 - w) * base + w * ai_p
    ey, en = _edge(p, yes_ask), _edge(1 - p, no_ask)
    red = bool(j.get("red_flag")) and str(j.get("red_flag")).lower() != "false"
    decision = "SKIP"
    if not red:
        if ey is not None and ey >= min_edge and (en is None or ey >= en):
            decision = "BUY_YES"
        elif en is not None and en >= min_edge:
            decision = "BUY_NO"
    wanted = "BUY_" + side.upper()
    side_edge = ey if side == "yes" else en
    spread = (sum((x - med) ** 2 for x in ps) / len(ps)) ** 0.5 if len(ps) > 1 else 0.0
    size = "small" if spread > 0.12 else "large" if (side_edge or 0) >= 2 * min_edge and spread < 0.06 else "normal"
    mult = {"small": 0.5, "normal": 1.0, "large": 1.5}[size]
    for a in agents:                                    # each analyst's own lean, computed by code
        e_y = _edge(a["prob_yes"], yes_ask) if a["prob_yes"] is not None else None
        e_n = _edge(None if a["prob_yes"] is None else 1 - a["prob_yes"], no_ask)
        a["vote"] = ("BUY_YES" if (e_y or -1) > 0 and (e_y or -1) >= (e_n or -1) else
                     "BUY_NO" if (e_n or -1) > 0 else "SKIP")
    yes_votes = sum(1 for a in agents if a["vote"] == wanted)
    why = (f"Red flag: {str(j.get('red_flag_reason', ''))[:160]}. " if red else "") + str(j.get("summary", ""))[:300]
    why += (f" Blended odds {p * 100:.0f}% YES (math {'-' if math_prob is None else f'{math_prob * 100:.0f}%'}, "
            f"AI {ai_p * 100:.0f}%, AI weight {w * 100:.0f}%): {side.upper()} edge "
            f"{'?' if side_edge is None else f'{side_edge * 100:+.1f}c'} vs {min_edge * 100:.0f}c needed.")
    verdict = {"ok": True, "approve": decision == wanted, "decision": decision, "prob_yes": p, "ai_prob": ai_p,
               "ai_weight": w, "red_flag": red, "size": size, "mult": mult, "summary": why[:600], "agents": agents,
               "brief": brief[:1200], "votes_for": yes_votes, "cost": round(cost, 4), "secs": round(time.time() - t0, 1),
               "judge_ai": NAMES[judge_ai], "team": "+".join(NAMES[p_] for p_ in team), "by_ai": by_ai}
    CACHE[ck] = (time.time(), verdict)
    with _lock:
        SPEND["debates"] += 1
        LOG.append({"t": time.strftime("%H:%M"), "kind": kind, "ticker": ticker, "question": question[:140],
                    "side": side, "price": price, "math": math_prob, **{k: verdict[k] for k in
                    ("approve", "decision", "prob_yes", "ai_prob", "ai_weight", "red_flag", "size", "summary", "agents",
                     "votes_for", "cost", "secs", "judge_ai", "team")}})
        del LOG[:-25]
    B.log(f"AI TEAM [{verdict['team']}] {ticker}: {decision} (AI {ai_p * 100:.0f}% YES, blended {p * 100:.0f}%, "
          f"{yes_votes}/5 lean {side.upper()}, size {size}, ${cost:.3f}){' RED FLAG' if red else ''}")
    return verdict


def describe():
    team = providers()
    if not team:
        return "no AI key"
    parts = []
    for p in team:
        parts.append(f"{NAMES[p]} ({model_for(p, 'agent')} / {model_for(p, 'judge')})")
    return " + ".join(parts)


def status():
    team = providers()
    return {"on": getattr(B, "COUNCIL_ON", False), "key_set": key_set(), "spent": round(spent_today(), 3),
            "budget": getattr(B, "COUNCIL_DAILY_BUDGET", 0), "debates": SPEND["debates"], "log": list(LOG[::-1][:12]),
            "provider": getattr(B, "COUNCIL_PROVIDER", "mix"), "team": [NAMES[p] for p in team],
            "claude_key": claude_ok(), "gpt_key": gpt_ok(), "models": [describe()]}
