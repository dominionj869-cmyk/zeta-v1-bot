
import os
import time
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from iqoptionapi.stable_api import IQ_Option

# ==========================================
# FOREX OTC BREAKOUT + RETEST SCANNER
# ==========================================

IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

STAKE = 1.0
CANDLE_SECONDS = 60
EXPIRY_MINUTES = 1
LOOKBACK = 20

DAILY_LOSS_LIMIT = 3.0
MAX_CONSECUTIVE_LOSSES = 3
MAX_TRADES_PER_DAY = 20

RETEST_CANDLES = 6
SCAN_DELAY = 5
TIMEZONE = ZoneInfo("Africa/Lagos")

CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CHF",
    "CAD", "AUD", "NZD"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("forex_otc_scanner")

setups = {}
last_seen = {}
last_trade_time = {}

daily_pnl = 0.0
daily_trades = 0
consecutive_losses = 0
session_date = None
api = None


def notify(message):
    log.info(message)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as exc:
        log.warning("Telegram error: %s", exc)


def today_string():
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def reset_daily_stats():
    global daily_pnl
    global daily_trades
    global consecutive_losses
    global session_date

    daily_pnl = 0.0
    daily_trades = 0
    consecutive_losses = 0
    session_date = today_string()

    setups.clear()
    last_seen.clear()
    last_trade_time.clear()

    notify(
        "NEW DAILY SESSION\n"
        "Strategy: Forex OTC Breakout + Retest\n"
        "Stake: $1 fixed\n"
        "Daily loss limit: $3\n"
        "Max trades: 20"
    )


def connect_iq():
    global api

    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError(
            "Set IQ_EMAIL and IQ_PASSWORD in GitHub Secrets."
        )

    api = IQ_Option(IQ_EMAIL, IQ_PASSWORD)

    connected, reason = api.connect()

    if not connected:
        raise RuntimeError(
            "IQ Option connection failed: " + str(reason)
        )

    api.change_balance("PRACTICE")

    notify(
        "Connected to IQ Option.\n"
        "PRACTICE mode requested.\n"
        "Only Forex OTC pairs will be scanned."
    )


def is_forex_otc(name):
    name = str(name).upper()

    if not name.endswith("-OTC"):
        return False

    pair = name[:-4]

    if len(pair) != 6:
        return False

    base = pair[:3]
    quote = pair[3:]

    if base not in CURRENCIES:
        return False

    if quote not in CURRENCIES:
        return False

    if base == quote:
        return False

    return True


def discover_otc_pairs():
    try:
        open_times = api.get_all_open_time()
    except Exception as exc:
        log.warning("Could not load market list: %s", exc)
        return []

    found = set()

    for market_type in ("binary", "turbo"):
        market_data = open_times.get(market_type, {})

        for name, details in market_data.items():
            if not is_forex_otc(name):
                continue

            if not isinstance(details, dict):
                continue

            if details.get("open") is True:
                found.add(name)

    pairs = sorted(found)

    if pairs:
        notify(
            "Available Forex OTC pairs: "
            + ", ".join(pairs)
        )
    else:
        log.warning(
            "No open Forex OTC pairs found. "
            "The scanner will retry."
        )

    return pairs


def get_closed_candles(pair):
    candles = api.get_candles(
        pair,
        CANDLE_SECONDS,
        60,
        time.time()
    )

    if not candles:
        return []

    candles = sorted(
        candles,
        key=lambda candle: candle.get("from", 0)
    )

    now = time.time()
    closed = []

    for candle in candles:
        start = candle.get("from", 0)

        if start + CANDLE_SECONDS <= now:
            closed.append(candle)

    return closed


def candle_values(candle):
    opening = float(candle["open"])
    closing = float(candle["close"])
    high = float(candle["max"])
    low = float(candle["min"])

    return opening, closing, high, low


def calculate_tolerance(candles, level):
    recent = candles[-10:]
    total_range = 0.0
    count = 0

    for candle in recent:
        try:
            high = float(candle["max"])
            low = float(candle["min"])
            total_range += high - low
            count += 1
        except Exception:
            continue

    if count == 0:
        return abs(level) * 0.00015

    average_range = total_range / count

    return max(
        average_range * 0.25,
        abs(level) * 0.00005
    )


def inspect_pair(pair):
    global setups

    candles = get_closed_candles(pair)

    if len(candles) < LOOKBACK + 2:
        return None

    current = candles[-1]
    candle_time = int(current.get("from", 0))

    if last_seen.get(pair) == candle_time:
        return None

    last_seen[pair] = candle_time

    opening, closing, high, low = candle_values(current)

    previous = candles[-LOOKBACK - 1:-1]

    resistance = max(
        float(candle["max"]) for candle in previous
    )

    support = min(
        float(candle["min"]) for candle in previous
    )

    tolerance = calculate_tolerance(previous, resistance)

    setup = setups.get(pair)

    if setup:
        age = int(
            (candle_time - setup["time"]) / CANDLE_SECONDS
        )

        if age > RETEST_CANDLES:
            log.info("Setup expired for %s", pair)
            setups.pop(pair, None)
            setup = None

    if setup:
        level = setup["level"]
        direction = setup["direction"]

        if direction == "call":
            touched = low <= level + tolerance
            held = closing >= level
            bullish = closing > opening

            if touched and held and bullish:
                setups.pop(pair, None)
                return {
                    "direction": "call",
                    "level": level,
                    "pair": pair
                }

            if closing < level - tolerance:
                log.info("Retest failed for %s", pair)
                setups.pop(pair, None)

        elif direction == "put":
            touched = high >= level - tolerance
            held = closing <= level
            bearish = closing < opening

            if touched and held and bearish:
                setups.pop(pair, None)
                return {
                    "direction": "put",
                    "level": level,
                    "pair": pair
                }

            if closing > level + tolerance:
                log.info("Retest failed for %s", pair)
                setups.pop(pair, None)

        return None

    if closing > resistance and closing > opening:
        setups[pair] = {
            "direction": "call",
            "level": resistance,
            "time": candle_time
        }

        notify(
            "BREAKOUT UP: " + pair
            + "\nResistance: " + str(round(resistance, 6))
            + "\nWaiting for retest and bullish confirmation."
        )

        return None

    if closing < support and closing < opening:
        setups[pair] = {
            "direction": "put",
            "level": support,
            "time": candle_time
        }

        notify(
            "BREAKOUT DOWN: " + pair
            + "\nSupport: " + str(round(support, 6))
            + "\nWaiting for retest and bearish confirmation."
        )

    return None


def get_profit(order_id):
    result = api.check_win_v4(order_id)

    if isinstance(result, (int, float)):
        return float(result)

    if isinstance(result, (list, tuple)):
        for item in result:
            if isinstance(item, (int, float)):
                return float(item)

    raise RuntimeError(
        "Could not read trade result: " + str(result)
    )


def place_trade(signal):
    global daily_pnl
    global daily_trades
    global consecutive_losses

    pair = signal["pair"]
    direction = signal["direction"]

    if daily_trades >= MAX_TRADES_PER_DAY:
        return

    if daily_pnl <= -DAILY_LOSS_LIMIT:
        return

    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        return

    now = time.time()
    previous_trade = last_trade_time.get(pair, 0)

    if now - previous_trade < 60:
        return

    action = "BUY" if direction == "call" else "SELL"

    notify(
        "ENTRY SIGNAL\n"
        + "Pair: " + pair + "\n"
        + "Direction: " + action + "\n"
        + "Stake: $1.00\n"
        + "Expiry: 1 minute\n"
        + "Level: " + str(round(signal["level"], 6))
    )

    try:
        accepted, order_id = api.buy(
            STAKE,
            pair,
            direction,
            EXPIRY_MINUTES
        )
    except Exception as exc:
        notify("ORDER ERROR: " + pair + "\n" + str(exc))
        return

    if not accepted:
        notify("ORDER REJECTED: " + pair)
        return

    daily_trades += 1
    last_trade_time[pair] = now

    notify(
        "ORDER ACCEPTED\n"
        + pair + " " + action
        + "\nOrder ID: " + str(order_id)
    )

    try:
        profit = get_profit(order_id)
    except Exception as exc:
        notify(
            "RESULT ERROR\n"
            + pair + "\n"
            + str(exc)
            + "\nCheck the order in IQ Option manually."
        )
        return

    daily_pnl += profit

    # A zero result is counted as a loss for the streak.
    if profit > 0:
        consecutive_losses = 0
        outcome = "WIN"
    else:
        consecutive_losses += 1
        outcome = "LOSS"

    notify(
        "TRADE RESULT: " + outcome
        + "\nPair: " + pair
        + "\nProfit: $" + format(profit, ".2f")
        + "\nDaily P/L: $" + format(daily_pnl, ".2f")
        + "\nTrades: " + str(daily_trades)
        + "\nConsecutive losses: "
        + str(consecutive_losses)
    )


def trading_stopped():
    if daily_pnl <= -DAILY_LOSS_LIMIT:
        notify("STOP: Daily loss limit reached.")
        return True

    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        notify("STOP: Three consecutive losses.")
        return True

    if daily_trades >= MAX_TRADES_PER_DAY:
        notify("STOP: Daily trade limit reached.")
        return True

    return False


def main():
    reset_daily_stats()
    connect_iq()

    pairs = []
    last_discovery = 0

    notify(
        "SCANNER STARTED\n"
        "Strategy: Breakout + Retest\n"
        "Market: Forex OTC only\n"
        "Candles: 1 minute\n"
        "Expiry: 1 minute\n"
        "Stake: Fixed $1\n"
        "Recovery staking: Disabled"
    )

    while True:
        if today_string() != session_date:
            reset_daily_stats()

        if trading_stopped():
            time.sleep(60)
            continue

        if not api.check_connect():
            notify("Connection lost. Reconnecting...")

            try:
                connect_iq()
            except Exception as exc:
                log.error("Reconnect failed: %s", exc)
                time.sleep(15)
                continue

            pairs = []
            last_discovery = 0

        if time.time() - last_discovery >= 300:
            pairs = discover_otc_pairs()
            last_discovery = time.time()

        for pair in pairs:
            if trading_stopped():
                break

            try:
                signal = inspect_pair(pair)

                if signal:
                    place_trade(signal)

            except Exception as exc:
                log.warning(
                    "Scan error for %s: %s",
                    pair,
                    exc
                )

            time.sleep(0.2)

        time.sleep(SCAN_DELAY)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        notify("Scanner stopped manually.")
    except Exception as exc:
        log.exception("Scanner stopped: %s", exc)
        notify("SCANNER ERROR: " + str(exc))
        raise
