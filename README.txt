KALSHI 15-MIN BOT - WINDOWS SETUP
=================================

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
