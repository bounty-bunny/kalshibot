# EdgeHunter: a Kalshi bot for crypto, sports and politics

EdgeHunter runs on your own Windows laptop and looks for Kalshi bets whose price is wrong. It covers 15-minute crypto markets, sports games and politics questions. A team of AI agents (Claude, GPT, or both) double-checks each bet, and a live dashboard runs in your browser.

![EdgeHunter dashboard (example screenshot)](docs/dashboard-demo.png)
*Example screenshot with made-up numbers, not real trading results.*

> **Warning: real money.** This bot places real bets on your Kalshi account. You can lose money, and most people lose a little over time after fees. Nothing here is financial advice. Past results, yours or anyone's, don't predict future ones. Only use money you can afford to lose. The software is provided as-is (see [LICENSE](LICENSE)).

## What it bets on

| Area | How it finds a deal |
| --- | --- |
| **Coins** (BTC, ETH, SOL, XRP, BNB, DOGE, HYPE, ZEC, NEAR…) | Live coin price vs. Kalshi's target, minutes left and recent volatility give the real chance of YES. It bets only when Kalshi sells a side cheaper than that, after fees. |
| **Coins: late shots** | In the last 5 to 1 minutes, tiny bets ($0.10–$2) on 1–2.5¢ sides when the price is close enough to flip. These pay 40× or more, with a capped test budget. |
| **Sports** (NFL, NBA, NHL, MLB, college, Premier League, Champions League, MLS) | Compares Kalshi with the sportsbooks' fair odds (free key from the-odds-api.com). Only bets on games starting within 3 hours. |
| **Politics & others** | Matches Kalshi questions to the same questions on Polymarket and bets when the two disagree. |

## How it decides

- **The math finds the deal.** Each side (YES and NO) is checked against its real price after Kalshi's fee.
- **The AI team checks it (optional).** A researcher searches the news. Five analysts, split between Claude and GPT in mix mode, each give a probability. They work blind: they never see which side the bot wants or its odds, and half of them are asked about NO instead of YES. An auditor combines their numbers and can veto a bet for red flags.
- **The code, not the AI, makes the final call.** It blends the AI's odds with the math and buys only if the edge survives.
- **"Who predicts best?"** The dashboard scores every market the bot looks at, including the ones it skipped. It compares the bot's math, Kalshi's own price, Claude and GPT. The AI gets more say if it beats the math and less if it doesn't.
- **Prove first.** New strategies start with pretend bets and go live only after a convincing profit. Long shots need 100. Losing strategies pause themselves.
- **Your limits always win:** bet size, money in open bets, daily loss limit (minimum $10), and never more than your Kalshi cash.

## Dashboard

It opens automatically at **http://localhost:8765**, only on your own computer.

- **Overview:** today's and all-time profit, win rate vs. break-even, every current bet (what you paid and what it pays), the AI team's debates, strategies, profit chart, results by day, and the forecast scorecard.
- **Tabs:** **Coins**, **Sports** and **Politics & others**. Each tab shows what the bot sees right now and its own settings, in sections you can collapse.
- **Pause / Resume** button. Settings changes apply immediately.

## Setup (Windows, about 20 minutes)

1. **Install Python 3** from python.org. Tick **"Add python.exe to PATH"** on the first screen.
2. **Download this repo** (green **Code** button, then **Download ZIP**) and unzip it to `C:\edgehunter`.
3. **Create a Kalshi API key** on the Kalshi *website*: kalshi.com → Profile settings → **API Keys** → **Create New API Key**. Pick **Ed25519** and leave the "add your own public key" box empty. Copy the **Key ID**. Kalshi downloads a **key file (.txt)**; move it into the bot folder.
4. **Open `bot.py` in Notepad** and paste your keys between the quotes:
   - `API_KEY_ID`: your Kalshi Key ID (required)
   - `ODDS_API_KEY`: free key from the-odds-api.com (for sports; optional)
   - `ANTHROPIC_API_KEY` and/or `OPENAI_API_KEY`: for the AI team (optional; about 1–5¢ per debate, capped at $1/day)
5. **Double-click `check.bat`.** It shows your balance and the markets, and places no bets.
6. **Double-click `run.bat`** to start. Keep the black window open, and set Windows Sleep to **Never** while plugged in.

Without the optional keys, the bot still trades coins on its own math. `README.txt` has the plain-language guide.

## Troubleshooting

| You see | Do this |
| --- | --- |
| `'python' is not recognized` | Reinstall Python with **Add python.exe to PATH** ticked |
| Key file not found | Put the key file Kalshi downloaded into the bot folder |
| HTTP 401 / key rejected | The Key ID and key file don't match. Create a new key |
| `too many requests` | Normal now and then; the bot slows down and retries |
| "AI team set to mix, but only one key is pasted" | Add the other key, or pick Claude only / GPT only on the dashboard |
| Lots of NO TRADE / PROVING | Normal. Good deals are rare, and new strategies prove themselves with pretend bets first |

## Your files stay private

Your keys, settings, bet history, scorecard and logs are in `.gitignore`. **Never share your key file or any API key.** Anyone with your Kalshi key can trade your account.

## Support

If EdgeHunter is useful to you, you can [sponsor the project](https://github.com/sponsors/bounty-bunny) ☕. The dashboard has a small Support button and, rarely, a thank-you note. It only appears after a week of use and on a day you're up. To remove it, set `SUPPORT_URL = ""` in `bot.py`.

## License

MIT. Check Kalshi's [Developer Agreement](https://kalshi.com/developer-agreement), and the terms of The Odds API, Polymarket, Anthropic and OpenAI, before using their APIs.
