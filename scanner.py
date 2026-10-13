
import os
import csv
import json
import time
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from iqoptionapi.stable_api import IQ_Option


# MOMENTUM 20 / MOMENTUM 3 SCANNER
# PRACTICE ACCOUNT ONLY

BALANCE_MODE = "PRACTICE"
STAKE = 1.0
CANDLE_SECONDS = 60
EXPIRY_MINUTES = 1

MOMENTUM_TREND_PERIOD = 20
MOMENTUM_ENTRY_PERIOD = 3
CANDLE_COUNT = 100

BASE_PAIRS = ["EURUSD", "AUDUSD", "GBPUSD", "USDJPY"]
SCAN_INTERVAL = 3
ASSET_REFRESH_SECONDS = 900
COOLDOWN_SECONDS = 60

DAILY_PROFIT_TARGET = 20.0
DAILY_LOSS_LIMIT = -5.0
MAX_DAILY_TRADES = 50

STATE_FILE = "momentum_state.json"
LOG_FILE = "momentum_signal_log.csv"
TIMEZONE = ZoneInfo("Africa/Lagos")

# Set False to scan and send signals without placing orders.
AUTO_TRADE = True

IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

api = None
active_codes = {}
last_signal_time = {}
state_lock = threading.Lock()
trade_lock = threading.Lock()
running_trade = False


def now_local():
    return datetime.now(TIMEZONE)


def today_string():
    return now_local().strftime("%Y-%m-%d")


def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(message)
        return

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )

    try:
        requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            timeout=10,
        )
    except Exception as exc:
        print("Telegram error:", exc)


def load_state():
    default = {
        "date": today_string(),
        "profit": 0.0,
        "trades": 0,
        "wins": 0,
        "losses": 0,
    }

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            saved = json.load(file)

        if saved.get("date") != today_string():
            return default

        for key in default:
            if key in saved:
                default[key] = saved[key]

        return default

    except Exception:
        return default


state = load_state()


def save_state():
    with state_lock:
        state["date"] = today_string()

        with open(STATE_FILE, "w", encoding="utf-8") as file:
            json.dump(state, file, indent=2)


def log_signal(pair, direction, m20, m3_previous, m3_current, result="SIGNAL"):
    exists = os.path.exists(LOG_FILE)

    try:
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)

            if not exists:
                writer.writerow([
                    "time",
                    "pair",
                    "direction",
                    "momentum20",
                    "momentum3_previous",
                    "momentum3_current",
                    "result",
                ])

            writer.writerow([
                now_local().isoformat(),
                pair,
                direction,
                m20,
                m3_previous,
                m3_current,
                result,
            ])

    except Exception as exc:
        print("Log error:", exc)


def analyze_momentum(momentum20, momentum3_prev, momentum3):
    # Momentum 20 must determine the direction.
    # Momentum 3 must cross zero on a closed candle.
    # Zero or disagreement means no trade.

    if momentum20 == 0 or momentum3 == 0:
        return None

    if momentum20 > 0:
        if momentum3_prev <= 0 and momentum3 > 0:
            return "call"
        return None

    if momentum20 < 0:
        if momentum3_prev >= 0 and momentum3 < 0:
            return "put"
        return None

    return None


def get_closed_candles(active_id):
    candles = api.get_candles(
        active_id,
        CANDLE_SECONDS,
        CANDLE_COUNT,
        time.time(),
    )

    if not candles:
        return []

    current_time = time.time()
    closed = []

    for candle in candles:
        candle_start = candle.get("from", 0)

        if candle_start + CANDLE_SECONDS <= current_time:
            try:
                closed.append({
                    "from": candle_start,
                    "close": float(candle["close"]),
                })
            except (KeyError, TypeError, ValueError):
                continue

    closed.sort(key=lambda item: item["from"])

    # Remove duplicate candle timestamps.
    unique = {}

    for candle in closed:
        unique[candle["from"]] = candle

    return list(unique.values())


def calculate_signal(candles):
    if len(candles) < MOMENTUM_TREND_PERIOD + 1:
        return None

    closes = [item["close"] for item in candles]

    # Latest fully closed candle.
    current_close = closes[-1]

    # Momentum 20 establishes the trend direction.
    momentum20 = (
        current_close
        - closes[-1 - MOMENTUM_TREND_PERIOD]
    )

    # Momentum 3 on the latest fully closed candle.
    momentum3_current = (
        closes[-1]
        - closes[-1 - MOMENTUM_ENTRY_PERIOD]
    )

    # Momentum 3 on the previous fully closed candle.
    momentum3_previous = (
        closes[-2]
        - closes[-2 - MOMENTUM_ENTRY_PERIOD]
    )

    direction = analyze_momentum(
        momentum20,
        momentum3_previous,
        momentum3_current,
    )

    if direction is None:
        return None

    return {
        "direction": direction,
        "momentum20": momentum20,
        "momentum3_previous": momentum3_previous,
        "momentum3_current": momentum3_current,
    }


def refresh_assets():
    global active_codes

    try:
        codes = api.get_all_ACTIVES_OPCODE()

        if not codes:
            print("No active asset codes returned.")
            return

        wanted = []

        for pair in BASE_PAIRS:
            wanted.append(pair)
            wanted.append(pair + "-OTC")

        found = {}

        for pair in wanted:
            if pair in codes:
                found[pair] = codes[pair]

        active_codes = found

        print("Available pairs:", list(active_codes.keys()))

    except Exception as exc:
        print("Asset refresh error:", exc)


def can_trade():
    global state

    if state.get("date") != today_string():
        state = {
            "date": today_string(),
            "profit": 0.0,
            "trades": 0,
            "wins": 0,
            "losses": 0,
        }
        save_state()

    if state["profit"] >= DAILY_PROFIT_TARGET:
        return False, "Daily profit target reached"

    if state["profit"] <= DAILY_LOSS_LIMIT:
        return False, "Daily loss limit reached"

    if state["trades"] >= MAX_DAILY_TRADES:
        return False, "Daily trade limit reached"

    return True, "Trading allowed"


def record_result(profit):
    global state

    with state_lock:
        if state.get("date") != today_string():
            state = {
                "date": today_string(),
                "profit": 0.0,
                "trades": 0,
                "wins": 0,
                "losses": 0,
            }

        state["profit"] += profit
        state["trades"] += 1

        if profit > 0:
            state["wins"] += 1
        else:
            # Zero profit is counted as a loss.
            state["losses"] += 1

        state["date"] = today_string()

        with open(STATE_FILE, "w", encoding="utf-8") as file:
            json.dump(state, file, indent=2)

    telegram(
        "TRADE RESULT\n"
        "Result: $" + format(profit, ".2f") + "\n"
        "Daily P/L: $" + format(state["profit"], ".2f") + "\n"
        "Trades: " + str(state["trades"]) + "/" +
        str(MAX_DAILY_TRADES) + "\n"
        "Wins: " + str(state["wins"]) + "\n"
        "Losses: " + str(state["losses"])
    )


def execute_trade(pair, direction, signal):
    global running_trade

    allowed, reason = can_trade()

    if not allowed:
        print(reason)
        return

    with trade_lock:
        if running_trade:
            print("Trade already unresolved. Skipping:", pair)
            return

        running_trade = True

    try:
        message = (
            "MOMENTUM SIGNAL\n"
            "Pair: " + pair + "\n"
            "Direction: " +
            ("HIGHER (BUY)" if direction == "call" else "LOWER (SELL)") +
            "\nMomentum 20: " +
            format(signal["momentum20"], ".8f") +
            "\nMomentum 3 previous: " +
            format(signal["momentum3_previous"], ".8f") +
            "\nMomentum 3 current: " +
            format(signal["momentum3_current"], ".8f") +
            "\nExpiry: 1 minute"
        )

        print(message)
        telegram(message)

        log_signal(
            pair,
            direction,
            signal["momentum20"],
            signal["momentum3_previous"],
            signal["momentum3_current"],
        )

        if not AUTO_TRADE:
            return

        allowed, reason = can_trade()

        if not allowed:
            telegram(reason)
            return

        active_id = active_codes.get(pair)

        if active_id is None:
            print("Missing active ID:", pair)
            return

        success, order_id = api.buy(
            STAKE,
            active_id,
            direction,
            EXPIRY_MINUTES,
        )

        if not success:
            telegram("Order rejected: " + pair)
            return

        telegram(
            "ORDER PLACED\n"
            "Pair: " + pair + "\n"
            "Direction: " + direction.upper() + "\n"
            "Stake: $" + format(STAKE, ".2f") + "\n"
            "Order ID: " + str(order_id)
        )

        # Wait for the binary order to resolve.
        # Do not place another order while this one is unresolved.
        profit = None

        for attempt in range(24):
            time.sleep(5)

            try:
                result = api.check_win_v4(order_id)

                if result is not None:
                    profit = float(result)
                    break

            except Exception as exc:
                print("Result check error:", exc)

        if profit is None:
            telegram(
                "WARNING: Result not confirmed for order "
                + str(order_id)
                + ". Trading paused until manually checked."
            )

            # Fail closed: do not assume an unresolved order lost.
            # This process stops scanning to prevent duplicate exposure.
            raise RuntimeError("Order result unresolved")

        record_result(profit)

        if state["profit"] >= DAILY_PROFIT_TARGET:
            telegram("Daily profit target reached. Stop trading.")

        if state["profit"] <= DAILY_LOSS_LIMIT:
            telegram("Daily loss limit reached. Stop trading.")

    finally:
        with trade_lock:
            running_trade = False


def connect():
    global api

    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError("Missing IQ_EMAIL or IQ_PASSWORD secret")

    api = IQ_Option(IQ_EMAIL, IQ_PASSWORD)

    connected, reason = api.connect()

    if not connected:
        raise RuntimeError("IQ Option connection failed: " + str(reason))

    api.change_balance(BALANCE_MODE)

    telegram(
        "Momentum 20 / Momentum 3 scanner connected.\n"
        "Balance: PRACTICE\n"
        "Stake: $1\n"
        "Expiry: 1 minute\n"
        "Pairs: normal and OTC currency pairs"
    )

    print("Connected to IQ Option Practice account.")


def main():
    global api

    connect()
    refresh_assets()

    last_refresh = time.time()

    while True:
        try:
            if not api.check_connect():
                print("Connection lost. Reconnecting...")
                connected, reason = api.connect()

                if connected:
                    api.change_balance(BALANCE_MODE)
                    refresh_assets()
                else:
                    print("Reconnect failed:", reason)
                    time.sleep(10)
                    continue

            if time.time() - last_refresh >= ASSET_REFRESH_SECONDS:
                refresh_assets()
                last_refresh = time.time()

            allowed, reason = can_trade()

            if not allowed:
                print(reason)
                time.sleep(30)
                continue

            if running_trade:
                time.sleep(SCAN_INTERVAL)
                continue

            for pair, active_id in list(active_codes.items()):
                if running_trade:
                    break

                if time.time() - last_signal_time.get(pair, 0) < COOLDOWN_SECONDS:
                    continue

                try:
                    candles = get_closed_candles(active_id)
                    signal = calculate_signal(candles)

                    if signal is None:
                        continue

                    last_signal_time[pair] = time.time()

                    execute_trade(
                        pair,
                        signal["direction"],
                        signal,
                    )

                    # Only one trade at a time.
                    if AUTO_TRADE:
                        break

                except Exception as exc:
                    print("Scan error for", pair, ":", exc)

            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            telegram("Momentum scanner stopped.")
            break

        except Exception as exc:
            print("Main loop error:", exc)
            time.sleep(5)


if __name__ == "__main__":
    main()
