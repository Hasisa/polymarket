# Polymarket Whale Telegram Bot

Tracks top all-time Polymarket profit accounts and posts their new trade activity to Telegram.

## What it watches

- Pulls wallets from Polymarket's public all-time PnL leaderboard.
- Keeps wallets whose lifetime PnL is at least `MIN_PNL_USD`.
- Polls each wallet's trade activity.
- Sends one Telegram message per new trade.
- Stores seen trades in SQLite so restarts do not spam old activity.

## Setup

1. Create a Telegram bot with BotFather and copy the token.
2. Get your chat id. The easiest way is to message your bot once, then run:

```bash
TELEGRAM_BOT_TOKEN=your_token python3 bot.py --show-updates
```

3. Create `.env` from the example:

```bash
cp .env.example .env
```

4. Fill in `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
5. Run:

```bash
python3 bot.py
```

## Hosting

This project includes a `Dockerfile` and `render.yaml` for running as a hosted background worker on Render.

1. Push this project to GitHub.
2. Create a new Render Blueprint from the repo.
3. Add these secret environment variables in Render:

```bash
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
```

The included Render config runs the bot continuously and stores SQLite data on a persistent disk at `/data/polymarket_bot.sqlite3`.

## Configuration

All settings are optional except the Telegram token and chat id.

- `TELEGRAM_BOT_TOKEN`: Telegram bot token.
- `TELEGRAM_CHAT_ID`: Chat, group, or channel id to receive alerts.
- `MIN_PNL_USD`: Minimum all-time leaderboard PnL to track. Default: `50000`.
- `LEADERBOARD_LIMIT`: Number of leaderboard wallets to monitor, max effective public window is 1000. Default: `1000`.
- `TRADE_MIN_USDC`: Minimum trade notional to alert. Default: `10000`.
- `POLL_SECONDS`: Seconds between trade polling cycles. Default: `60`.
- `LEADERBOARD_REFRESH_SECONDS`: Seconds between leaderboard refreshes. Default: `3600`.
- `DATABASE_PATH`: SQLite database path. Default: `polymarket_bot.sqlite3`.

## Notes

This bot uses Polymarket public Data API endpoints. It tracks filled trade activity, not private unfilled orders. Public endpoints can be rate-limited, so do not set `POLL_SECONDS` too low when tracking many wallets.
