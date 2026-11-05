EDGEHUNTER (KALSHI BOT) - WINDOWS SETUP
=======================================

1. Install Python
   - Go to python.org/downloads and install Python 3.
   - On the first install screen, TICK "Add python.exe to PATH".

2. Put this folder somewhere simple, e.g. C:\kalshi-bot

3. Create your Kalshi API key (on the Kalshi WEBSITE, not the docs site)
   - Log in at kalshi.com, go to kalshi.com/account/profile, find "API Keys",
     click "Create New API Key". Either key type works.
   - Kalshi shows a Key ID and downloads a private key file (.txt).
   - Move that downloaded file into this folder. No need to rename it.
   - Keep it secret: anyone with this file can trade your account.

4. Paste your Key ID
   - Right-click bot.py > Open with > Notepad.
   - Replace PASTE-YOUR-KEY-ID-HERE with your Key ID (keep the quotes). Save.

5. Test (no trades): double-click  check.bat
   - You should see your cash balance and the current BTC market.

6. Start trading: double-click  run.bat
   - Leave the black window open. Press Ctrl+C to stop.
   - Your browser opens the DASHBOARD at http://localhost:8765
     (profit/loss, every bet, what the bot is thinking right now,
     pause/resume, and your limits). If it doesn't open, type that
     address into your browser.
   - Stop your laptop sleeping: Settings > System > Power > Sleep = Never (when plugged in).

AI TEAM (6 AI agents debate each bet - Claude, GPT, or both mixed)
   1. Get a key from one or both:
        Claude: console.anthropic.com  -> paste into ANTHROPIC_API_KEY in bot.py
        GPT:    platform.openai.com    -> paste into OPENAI_API_KEY in bot.py
      (add a few dollars of credit to whichever you use)
   2. Pick "Which AI" on the dashboard (Settings > AI team):
        Mix: Claude + GPT  - the 5 analysts are split between Claude and GPT, so two
                             different AIs look at every bet. Researcher + judge use Claude.
        Claude only / GPT only.
      With only one key pasted, the team just uses that one, whatever you pick.
      If one AI is down or out of credit, the other one answers instead.
   When the math finds a deal (or a near miss in sports/politics), a team debates it:
   a Researcher searches the latest news, then Quant, Skeptic, News analyst,
   Contrarian and Risk manager each vote, and a Judge makes the final call:
   APPROVE (small / normal / large size) or SKIP. You can read every agent's
   reason, and which AI it was, on the dashboard ("AI team"). Cost: about 1-5
   cents per debate, capped at $1.00 a day (change on the dashboard). Without
   any key the bot still works on its own math.
   Models (change at the top of bot.py if you like): Claude haiku/sonnet,
   GPT gpt-5.6-luna (analysts) / gpt-5.6-terra (judge).
   Bets are "Medium": up to $5 each, edge sizing at 50% Kelly. Daily loss limit $10.

HOW THE BOT DECIDES (v2 - "prove it, don't guess it")
   * The AI agents forecast BLIND: they never see which side the bot wants or the bot's own odds,
     and half of them are asked for the chance of NO instead of YES, so nobody gets nudged.
   * The AI never places a bet by itself. The CODE blends the AI's odds with the bot's math
     (AI weight: coins 15%, sports 30%, politics 40%), checks BOTH YES and NO against the real
     prices after fees, and buys only if the edge is still big enough. An auditor can veto any
     bet for a red flag (wrong question, stale data, rules gotcha).
   * "Who predicts best?" on the dashboard scores every market the bot looks at - including the
     ones it skipped - for the bot's math, Kalshi's price, Claude and GPT. If the AI beats the
     math over 30+ markets its weight grows; if it's worse, it shrinks.
     The number that matters: "vs Kalshi" staying above 0 over hundreds of markets.
   * Long shots now PROVE themselves with pretend bets first (100 of them, with a convincing
     profit) and their odds are blended 50/50 with the market price: cheap contracts are often
     overpriced. New strategies need 40 pretend bets and a t-score of 1+ (not just luck).

LATE SHOTS (40x+ bets in the last minutes)
   Crypto: with 5 to 1 minutes left in a 15-minute window, if one side costs
   2.5c or less (pays 40x+) AND the coin is close to the target (within 2
   typical price moves for the time left) AND the bot's chance is at least 2x
   the price, it bets $0.10-$2.00 (more on better
   deals). If unsure it keeps watching until 2 min left.
   Sports: during live games, if Kalshi sells a team for 2.5c or less but the
   live sportsbooks give it at least 2x that chance, it bets $0.10-$2.00. (Free odds
   data has no game clock, so it goes by price, not minutes left.)
   Each has a $10 test budget. These are lottery-style bets: most lose, the
   occasional win pays 40-100x. Judge them after 100+ bets, not a few.

HOW THE BOT STAYS SMART (private version)
   * Bets only when the price is wrong in your favour (crypto: live coin
     prices; sports: sportsbook odds; politics: Polymarket).
   * Bet size by edge: bigger bets on better deals, tiny ones on thin deals,
     never above each per-bet amount (dashboard: "How to size bets").
   * Prove it first: new strategies (each sport league, politics) make
     PRETEND bets with no money. After 20 pretend bets with a profit they
     switch to real money by themselves.
   * Self-correcting: after 20 real bets, a strategy losing more than 10%
     pauses itself; one winning more than 5% bets 1.5x. See "Strategies" on
     the dashboard, where you can also Go live / Prove first / Pause by hand.

POLITICS (private version)
   Every 15 minutes it matches Kalshi politics/election questions to the same
   question on Polymarket (automatically, with safety checks: same numbers and
   dates, no opposite wording, prices not wildly apart) and bets when Kalshi
   is 6c+ cheaper than Polymarket. Only questions ending within 45 days.
   Starts with pretend bets. Politics questions can take weeks to settle, so
   it may prove itself slowly; you can press "Go live" yourself.

SPORTS (private version)
   1. Get a FREE odds key: go to the-odds-api.com, click "Get API Key",
      choose the free Starter plan, and they email you a key.
   2. Open bot.py in Notepad and paste it into ODDS_API_KEY (keep the quotes).
   3. Run check.bat: it lists Kalshi games, the matching sportsbook games,
      the fair odds and what the bot WOULD bet - without betting.
   How it bets: in the last 3 hours before a game, if Kalshi sells a team
   (or the draw) at least 4c cheaper than the sportsbooks' fair chance after
   fees, it bets $10 on it. One bet per game. Max $50 in open sports bets.
   NFL, college football, NBA, college basketball, NHL, MLB (in season),
   Premier League, Champions League, MLS. Change all of this on the dashboard.
   Free plan = 500 odds checks a month; the bot spreads them out by itself.
   NOTE: your Daily loss limit also covers sports. At $10 per game, raise it
   (e.g. to $30-50) or one losing game will stop betting for the day.

LONG-SHOT MODE (crypto, private version, on by default)
   Small bets ($0.50-$5.00, sized by the deal) on cheap sides (20c or less, paying 5x or more) only when
   the bot's odds are at least 1.5x the price. Test budget: $10 total - once
   long shots are down $10 overall it stops for good until you raise it.
   Switch between Normal / Long shots / Both on the dashboard ("What to bet on").

HOW IT DECIDES
   For each coin you tick, it gets the live price (Coinbase, Kraken or OKX),
   compares it with Kalshi's target and the time left, and works out the
   real chance of YES. It bets YES or NO only when Kalshi's price is at least
   5c cheaper than that chance (after fees). Otherwise: NO TRADE, with the reason.

LIMITS (easiest: change them on the dashboard, under "Your limits")
   STAKE_PER_TRADE  = 5.00   -> $ per trade
   MAX_AT_RISK      = 10.00  -> max $ in open trades at once
   DAILY_LOSS_LIMIT = 10.00  -> stops for the day after losing $10
   It trades every 15-minute window all day until the loss limit is hit.
   MIN_PRICE / MAX_PRICE     -> only buys the leader when it costs 55c-85c

FILES THE BOT CREATES
   trades.csv   - every buy, sell and result (opens in Excel)
   bot_log.txt  - everything the bot did and why it skipped
   bot_state.json - today's totals (don't edit; lets it restart safely)
   settings.json  - limits you saved on the dashboard
