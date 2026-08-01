#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ssl
import json
import os
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import certifi


POLYMARKET_DATA_API = "https://data-api.polymarket.com"
TELEGRAM_API = "https://api.telegram.org"


def log(message: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] {message}", flush=True)


def load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def decimal_env(name: str, default: str) -> Decimal:
    try:
        return Decimal(os.getenv(name, default))
    except InvalidOperation:
        raise SystemExit(f"{name} must be a number")


def int_env(name: str, default: str) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        raise SystemExit(f"{name} must be an integer")


@dataclass(frozen=True)
class Config:
    telegram_token: str
    telegram_chat_ids: list[str]
    min_pnl_usd: Decimal
    leaderboard_limit: int
    trade_min_usdc: Decimal
    poll_seconds: int
    leaderboard_refresh_seconds: int
    database_path: str
    max_probability: Decimal

    @classmethod
    def from_env(cls, require_chat_id: bool = True) -> "Config":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_ids_raw = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        chat_ids = [c.strip() for c in chat_ids_raw.split(",") if c.strip()]
        if not token:
            raise SystemExit("Missing TELEGRAM_BOT_TOKEN in environment or .env")
        if require_chat_id and not chat_ids:
            raise SystemExit("Missing TELEGRAM_CHAT_ID in environment or .env")
        
        return cls(
            telegram_token=token,
            telegram_chat_ids=chat_ids,
            min_pnl_usd=decimal_env("MIN_PNL_USD", "50000"),
            leaderboard_limit=max(1, int_env("LEADERBOARD_LIMIT", "1000")),
            trade_min_usdc=decimal_env("TRADE_MIN_USDC", "10000"),
            poll_seconds=max(15, int_env("POLL_SECONDS", "60")),
            max_probability=decimal_env("MAX_PROBABILITY", "0.80"),
            leaderboard_refresh_seconds=max(300, int_env("LEADERBOARD_REFRESH_SECONDS", "3600")),
            database_path=os.getenv("DATABASE_PATH", "polymarket_bot.sqlite3"),
        )


class HttpClient:
    def __init__(self, timeout: int = 120) -> None:
        self.timeout = timeout
        self.context = ssl.create_default_context(cafile=certifi.where())

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        if params:
            query = urllib.parse.urlencode(params, doseq=True)
            url = f"{url}?{query}"

        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "polymarket-whale-telegram-bot/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self.context) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GET {url} failed with HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"GET {url} failed: {exc.reason}") from exc


class TelegramClient:
    def __init__(self, token: str, http: HttpClient) -> None:
        self.base_url = f"{TELEGRAM_API}/bot{token}"
        self.http = http

    def send_message(self, chat_id: str, text: str) -> None:
        params = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
        self.http.get_json(f"{self.base_url}/sendMessage", params)

    def get_updates(self):

        params = {}

        if self.offset:
            params["offset"] = self.offset

        data = self.http.get_json(
            f"{self.base_url}/getUpdates",
            params
        )

        if data.get("result"):
            self.offset = data["result"][-1]["update_id"] + 1

        return data
def handle_commands(telegram, store):

    updates = telegram.get_updates()

    for update in updates.get("result", []):

        message = update.get("message")

        if not message:
            continue

        text = message.get("text", "")
        chat_id = str(message["chat"]["id"])


        if text.startswith("/track"):

            parts = text.split()

            if len(parts) < 2:
                telegram.send_message(
                    chat_id,
                    "Usage:\n/track WALLET"
                )
                continue


            wallet = parts[1].lower()


            store.add_user_wallet(
                chat_id,
                wallet
            )


            telegram.send_message(
                chat_id,
                f"✅ Now tracking:\n{wallet}"
            )
class PolymarketClient:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def leaderboard(self, limit: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while len(rows) < limit:
            batch_limit = min(50, limit - len(rows))
            log(f"Fetching leaderboard rows {offset + 1}-{offset + batch_limit}...")
            data = self.http.get_json(
                f"{POLYMARKET_DATA_API}/v1/leaderboard",
                {
                    "timePeriod": "ALL",
                    "orderBy": "PNL",
                    "category": "OVERALL",
                    "limit": batch_limit,
                    "offset": offset,
                },
            )
            if not data:
                break
            rows.extend(data)
            log(f"Fetched {len(rows)} leaderboard rows so far.")
            offset += len(data)
            if len(data) < batch_limit:
                break
        return rows

    def trade_activity(self, wallet: str, start: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "user": wallet,
            "type": "TRADE",
            "limit": 500,
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC",
        }
        if start:
            params["start"] = start
        return self.http.get_json(f"{POLYMARKET_DATA_API}/activity", params)


class Store:

    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()


    def add_user_wallet(self, chat_id: str, wallet: str, username: str = "") -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO user_tracked_wallets
            (chat_id, wallet, username, added_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                chat_id,
                wallet.lower(),
                username,
                int(time.time())
            )
        )
        self.conn.commit()


    def get_user_wallets(self, chat_id: str) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT wallet
            FROM user_tracked_wallets
            WHERE chat_id = ?
            """,
            (chat_id,)
        )

        return [row["wallet"] for row in rows]


    def remove_user_wallet(self, chat_id: str, wallet: str):
        self.conn.execute(
            """
            DELETE FROM user_tracked_wallets
            WHERE chat_id = ? AND wallet = ?
            """,
            (
                chat_id,
                wallet.lower()
            )
        )
        self.conn.commit()

    def init_schema(self) -> None:
        self.conn.executescript(
              """
        CREATE TABLE IF NOT EXISTS tracked_wallets (
            wallet TEXT PRIMARY KEY,
            username TEXT,
            x_username TEXT,
            verified_badge INTEGER,
            pnl REAL NOT NULL,
            volume REAL,
            rank TEXT,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS seen_trades (
            trade_key TEXT PRIMARY KEY,
            wallet TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            inserted_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_tracked_wallets (
            chat_id TEXT NOT NULL,
            wallet TEXT NOT NULL,
            username TEXT,
            added_at INTEGER NOT NULL,
            PRIMARY KEY(chat_id, wallet)
        );
        """

        )
        self.add_column_if_missing("tracked_wallets", "x_username", "TEXT")
        self.add_column_if_missing("tracked_wallets", "verified_badge", "INTEGER")
        self.conn.commit()

    def add_column_if_missing(self, table: str, column: str, column_type: str) -> None:
        columns = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

    def upsert_wallet(self, row: dict[str, Any]) -> None:
        wallet = str(row.get("proxyWallet", "")).lower()
        if not wallet:
            return
        self.conn.execute(
            """
            INSERT INTO tracked_wallets (
                wallet, username, x_username, verified_badge, pnl, volume, rank, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet) DO UPDATE SET
                username = excluded.username,
                x_username = excluded.x_username,
                verified_badge = excluded.verified_badge,
                pnl = excluded.pnl,
                volume = excluded.volume,
                rank = excluded.rank,
                updated_at = excluded.updated_at
            """,
            (
                wallet,
                row.get("userName") or row.get("name") or "",
                row.get("xUsername") or "",
                1 if row.get("verifiedBadge") else 0,
                float(row.get("pnl") or 0),
                float(row.get("vol") or 0),
                str(row.get("rank") or ""),
                int(time.time()),
            ),
        )
        self.conn.commit()

    def wallets(self) -> list[sqlite3.Row]:
        cursor = self.conn.execute("SELECT * FROM tracked_wallets ORDER BY pnl DESC")
        return list(cursor.fetchall())

    def replace_wallets(self, rows: list[dict[str, Any]]) -> None:
        current_wallets = {
            str(row.get("proxyWallet", "")).lower()
            for row in rows
            if row.get("proxyWallet")
        }
        if not current_wallets:
            return

        placeholders = ",".join("?" for _ in current_wallets)
        self.conn.execute(
            f"DELETE FROM tracked_wallets WHERE wallet NOT IN ({placeholders})",
            tuple(current_wallets),
        )
        for row in rows:
            self.upsert_wallet(row)
        self.conn.commit()

    def mark_seen(self, trade_key: str, wallet: str, timestamp: int) -> bool:
        try:
            self.conn.execute(
                "INSERT INTO seen_trades (trade_key, wallet, timestamp, inserted_at) VALUES (?, ?, ?, ?)",
                (trade_key, wallet.lower(), timestamp, int(time.time())),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def mark_many_seen(self, rows: list[tuple[str, str, int]]) -> set[str]:
        if not rows:
            return set()

        inserted: set[str] = set()
        now = int(time.time())
        with self.conn:
            for trade_key_value, wallet, timestamp in rows:
                try:
                    self.conn.execute(
                        "INSERT INTO seen_trades (trade_key, wallet, timestamp, inserted_at) VALUES (?, ?, ?, ?)",
                        (trade_key_value, wallet.lower(), timestamp, now),
                    )
                    inserted.add(trade_key_value)
                except sqlite3.IntegrityError:
                    continue
        return inserted

    def is_bootstrapped(self) -> bool:
        cursor = self.conn.execute("SELECT COUNT(*) AS count FROM seen_trades")
        return int(cursor.fetchone()["count"]) > 0


def as_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except InvalidOperation:
        return Decimal("0")


def money(value: Any) -> str:
    amount = as_decimal(value)
    return f"${amount:,.2f}"


def trade_key(trade: dict[str, Any]) -> str:
    tx = str(trade.get("transactionHash") or "")
    wallet = str(trade.get("proxyWallet") or "")
    condition = str(trade.get("conditionId") or "")
    asset = str(trade.get("asset") or "")
    timestamp = str(trade.get("timestamp") or "")
    side = str(trade.get("side") or "")
    size = str(trade.get("size") or "")
    price = str(trade.get("price") or "")
    return "|".join([tx, wallet, condition, asset, timestamp, side, size, price])


def format_timestamp(timestamp: Any) -> str:
    try:
        dt = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OSError):
        return "unknown time"


def format_trade(wallet_row: sqlite3.Row, trade: dict[str, Any]) -> str:
    side = str(trade.get("side") or "TRADE").upper()
    outcome = trade.get("outcome") or "Unknown outcome"
    title = trade.get("title") or "Unknown market"
    username = wallet_row["username"] or wallet_row["wallet"]
    size = as_decimal(trade.get("size"))
    price = as_decimal(trade.get("price"))
    usdc_size = trade.get("usdcSize")
    if usdc_size is None:
        usdc_size = size * price
    slug = trade.get("slug") or trade.get("eventSlug") or ""
    market_url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"
    profile_url = f"https://polymarket.com/profile/{wallet_row['wallet']}"
    x_username = wallet_row["x_username"] or ""

    lines = [
        "Polymarket whale trade",
        f"Trader: {username}",
        f"Profile: {profile_url}",
        f"Wallet: {wallet_row['wallet']}",
        f"All-time PnL: {money(wallet_row['pnl'])}",
        f"Action: {side} {outcome}",
        f"Market: {title}",
        f"Price: {price}",
        f"Size: {size:,.4f} shares",
        f"Notional: {money(usdc_size)}",
        f"Time: {format_timestamp(trade.get('timestamp'))}",
        f"Market link: {market_url}",
    ]
    if x_username:
        lines.insert(3, f"X: https://x.com/{urllib.parse.quote(x_username)}")
    return "\n".join(lines)


def refresh_leaderboard(config: Config, polymarket: PolymarketClient, store: Store) -> int:
    qualifying_rows = []
    for row in polymarket.leaderboard(config.leaderboard_limit):
        if as_decimal(row.get("pnl")) < config.min_pnl_usd:
            continue
        qualifying_rows.append(row)
    store.replace_wallets(qualifying_rows)
    return len(qualifying_rows)


def poll_once(
    config: Config,
    polymarket: PolymarketClient,
    telegram: TelegramClient,
    store: Store,
    send_alerts: bool,
    min_alert_timestamp: int | None = None,
) -> int:
    alerts = 0
    start = int(time.time()) - 7 * 24 * 60 * 60
    wallets = []

    for chat_id in config.telegram_chat_ids:
        for wallet in store.get_user_wallets(chat_id):

            wallets.append({
                "wallet": wallet,
                "username": wallet,
                "pnl": 0,
                "x_username": ""
            })


    
    log(f"Polling {len(wallets)} tracked wallets for new trades >= {money(config.trade_min_usdc)}...")
    for index, wallet_row in enumerate(wallets, start=1):
        wallet = wallet_row["wallet"]
        if index == 1 or index % 50 == 0:
            log(f"Polling wallet {index}/{len(wallets)}: {wallet}")
        try:
            trades = polymarket.trade_activity(wallet, start=start)
        except RuntimeError as exc:
            log(f"Failed to fetch trades for {wallet}: {exc}")
            continue

        sorted_trades = sorted(trades, key=lambda item: int(item.get("timestamp") or 0))
        trade_rows = [
            (trade_key(trade), wallet, int(trade.get("timestamp") or 0))
            for trade in sorted_trades
        ]
        inserted_keys = store.mark_many_seen(trade_rows)

        for trade in sorted_trades:
            key = trade_key(trade)
            if key not in inserted_keys:
                continue
            if (
                min_alert_timestamp is not None
                and int(trade.get("timestamp") or 0) < min_alert_timestamp
            ):
                continue
            notional = trade.get("usdcSize")
            if notional is None:
                notional = as_decimal(trade.get("size")) * as_decimal(trade.get("price"))

            if as_decimal(notional) < config.trade_min_usdc:
                continue

            price = as_decimal(trade.get("price"))

            if price >= config.max_probability:
                continue
            if send_alerts:
                for chat_id in config.telegram_chat_ids:
                    telegram.send_message(chat_id, format_trade(wallet_row, trade))
                alerts += 1
                log(f"Sent alert {alerts}: {wallet} {money(notional)}")
                time.sleep(0.1)
    log(f"Polling cycle finished. Sent {alerts} alerts.")
    return alerts


def show_updates(config: Config, http: HttpClient) -> None:
    telegram = TelegramClient(config.telegram_token, http)
    print(json.dumps(telegram.get_updates(), indent=2))


def run_bot(config: Config) -> None:
    log("Starting Polymarket whale bot.")
    log(
        "Config: "
        f"min_pnl={money(config.min_pnl_usd)}, "
        f"trade_min={money(config.trade_min_usdc)}, "
        f"leaderboard_limit={config.leaderboard_limit}, "
        f"poll_seconds={config.poll_seconds}, "
        f"database={config.database_path}"
    )
    http = HttpClient()
    polymarket = PolymarketClient(http)
    telegram = TelegramClient(config.telegram_token, http)
    store = Store(config.database_path)

    should_stop = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal should_stop
        should_stop = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    log("Refreshing Polymarket all-time PnL leaderboard...")
    tracked = refresh_leaderboard(config, polymarket, store)
    log(f"Tracking {tracked} wallets with all-time PnL >= {money(config.min_pnl_usd)}")

    bootstrap = not store.is_bootstrapped()
    min_alert_timestamp = None
    if bootstrap:
        log("Bootstrapping recent trades as seen. Alerts start on new trades only.")
        poll_once(config, polymarket, telegram, store, send_alerts=False)
        min_alert_timestamp = int(time.time())

    next_leaderboard_refresh = time.time() + config.leaderboard_refresh_seconds
    while not should_stop:

        if time.time() >= next_leaderboard_refresh:
            tracked = refresh_leaderboard(config, polymarket, store)
            log(f"Refreshed leaderboard; tracking {tracked} qualifying wallets.")
            next_leaderboard_refresh = time.time() + config.leaderboard_refresh_seconds
        handle_commands(
            telegram,
            store
        )

        alerts = poll_once(
            config,
            polymarket,
            telegram,
            store,
            True
        )
        min_alert_timestamp = None
        if alerts:
            log(f"Sent {alerts} Telegram alerts.")
        else:
            log("No qualifying new trades this cycle.")

        log(f"Sleeping for {config.poll_seconds} seconds.")
        for _ in range(config.poll_seconds):
            if should_stop:
                break
            time.sleep(1)

    log("Stopped.")


def run_once(config: Config) -> None:
    log("Starting one-cycle Polymarket check.")
    log(
        "Config: "
        f"min_pnl={money(config.min_pnl_usd)}, "
        f"trade_min={money(config.trade_min_usdc)}, "
        f"leaderboard_limit={config.leaderboard_limit}, "
        f"database={config.database_path}"
    )
    http = HttpClient()
    polymarket = PolymarketClient(http)
    telegram = TelegramClient(config.telegram_token, http)
    store = Store(config.database_path)

    log("Refreshing Polymarket all-time PnL leaderboard...")
    tracked = refresh_leaderboard(config, polymarket, store)
    log(f"Tracking {tracked} wallets with all-time PnL >= {money(config.min_pnl_usd)}")
    alerts = poll_once(config, polymarket, telegram, store, send_alerts=True)
    log(f"One-cycle check complete. Sent {alerts} Telegram alerts.")


def send_test_message(config: Config) -> None:
    http = HttpClient()
    telegram = TelegramClient(config.telegram_token, http)
    for chat_id in config.telegram_chat_ids:
            telegram.send_message(
                chat_id,
                "Polymarket bot test: Telegram alerts are connected.",
            )
    log("Sent Telegram test message.")


def main() -> None:
    log("Process launched.")
    parser = argparse.ArgumentParser(description="Track profitable Polymarket wallets and alert Telegram.")
    parser.add_argument("--show-updates", action="store_true", help="Print Telegram getUpdates response and exit.")
    parser.add_argument("--once", action="store_true", help="Run one leaderboard refresh and trade polling cycle.")
    parser.add_argument("--test-telegram", action="store_true", help="Send a Telegram test message and exit.")
    args = parser.parse_args()

    load_dotenv()
    log("Loaded environment.")
    config = Config.from_env(require_chat_id=not args.show_updates)
    http = HttpClient()
    if args.show_updates:
        show_updates(config, http)
        return
    if args.test_telegram:
        send_test_message(config)
        return
    if args.once:
        run_once(config)
        return
    run_bot(config)


if __name__ == "__main__":
    main()
