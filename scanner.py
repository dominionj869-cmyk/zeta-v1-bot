
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
STATE_FILE = "scanner_state.json"

CURRENCIES = {
    "USD", "EUR", "GBP", "JPY",
    "CHF", "CAD", "AUD", "NZD"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("forex_otc_scanner")

api = None
setups = {}
last_seen = {}
last_trade_time = {}

daily_pnl = 0.0
daily_trades = 0
consecutive_losses = 0
session_date = None
halted_uncertain = False
stop_notice_sent = False


def notify(message):
    log.info(message)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )

    try:
        requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message
            },
            timeout=10
        )
    except Exception as exc:
        log.warning("Telegram error: %s", exc)


def today_string():
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def save_state():
    data = {
        "date": session_date,
        "daily_pnl": daily_pnl,
        "daily_trades": daily_trades,
        "consecutive_losses": consecutive_losses,
        "halted_uncertain": halted_uncertain
    }

    temp_file = STATE_FILE + ".tmp"

    try:
        with open(temp_file, "w", encoding="utf-8") as file:
            json.dump(data, file)

        os.replace(temp_file, STATE_FILE)
    except Exception as exc:
        log.error("STATE SAVE FAILED: %s", exc)


def load_state():
    global daily_pnl
    global daily_trades
    global consecutive_losses
    global session_date
    global halted_uncertain

    current_date = today_string()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        if data.get("date") == current_date:
            daily_pnl = float(data["daily_pnl"])
            daily_trades = int(data["daily_trades"])
            consecutive_losses = int(
                data["consecutive_losses"]
            )
            halted_uncertain = bool(
                data.get("halted_uncertain", False)
            )
            session_date = current_date

            notify(
                "DAILY STATE RESTORED\n"
                + "P/L: $" + format(daily_pnl, ".2f")
                + "\nTrades: " + str(daily_trades)
                + "\nLoss streak: "
                + str(consecutive_losses)
            )
            return

    except FileNotFoundError:
        log.info("No saved state found.")
    except Exception as exc:
        log.error("STATE FILE ERROR: %s", exc)
        halted_uncertain = True
        session_date = current_date
        notify(
            "SAFETY STOP: Cannot read risk state. "
            "Check scanner_state.json before trading."
        )
        return

    daily_pnl = 0.0
    daily_trades = 0
    consecutive_losses = 0
    session_date = current_date
    save_state()

    notify(
        "NEW DAILY SESSION\n"
        "Strategy: Forex OTC Breakout + Retest\n"
        "Stake: $1 fixed\n"
        "Daily loss limit: $3\n"
        "Maximum trades: 20"
    )


def reset_daily_stats():
    global daily_pnl
    global daily_trades
    global consecutive_losses
    global session_date
    global stop_notice_sent

    # Never clear an unresolved-order safety halt automatically.
    if halted_uncertain:
        notify(
            "DAILY RESET BLOCKED: An order remains uncertain."
        )
        return

    daily_pnl = 0.0
    daily_trades = 0
    consecutive_losses = 0
    session_date = today_string()
    stop_notice_sent = False

    setups.clear()
    last_seen.clear()
    last_trade_time.clear()

    save_state()

    notify(
        "NEW DAILY SESSION\n"
        "Forex OTC Breakout + Retest\n"
        "Fixed stake: $1\n"
        "Daily loss limit: $3\n"
        "Maximum trades: 20"
    )


def connect_iq():
    global api

    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError(
            "Set IQ_EMAIL and IQ_PASSWORD in GitHub Secrets."
        )

    candidate = IQ_Option(IQ_EMAIL, IQ_PASSWORD)

    connected, reason = candidate.connect()

    if not connected:
        raise RuntimeError(
            "IQ Option connection failed: " + str(reason)
        )

    candidate.change_balance("PRACTICE")

    if not candidate.check_connect():
        raise RuntimeError(
            "Connection lost after selecting PRACTICE."
        )

    api = candidate

    notify(
        "Connected to IQ Option.\n"
        "PRACTICE mode selected.\n"
        "Forex OTC only."
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

    return (
        base in CURRENCIES
        and quote in CURRENCIES
        and base != quote
    )


def discover_otc_pairs():
    # Avoid get_all_open_time(), which can trigger the
    # failing digital-underlying data request.
    try:
        market_data = api.get_all_init_v2()
    except Exception as exc:
        log.warning(
            "Market initialization failed: %s", exc
        )
        return []

    if not isinstance(market_data, dict):
        log.warning("Invalid market initialization response.")
        return []

    found = set()

    for market_type in ("binary", "turbo"):
        section = market_data.get(market_type, {})

        if not isinstance(section, dict):
            continue

        actives = section.get("actives", {})

        if not isinstance(actives, dict):
            continue

        for active in actives.values():
            if not isinstance(active, dict):
                continue

            name = str(active.get("name", ""))

            if not is_forex_otc(name):
                continue

            if active.get("enabled") is not True:
                continue

            if active.get("is_suspended") is True:
                continue

            found.add(name)

    pairs = sorted(found)

    if pairs:
        notify(
            "Forex OTC pairs discovered: "
            + ", ".join(pairs)
        )
    else:
        log.warning(
            "No eligible pairs found in initialization data."
        )

    return pairs


def get_closed_candles(pair):
    try:
        candles = api.get_candles(
            pair,
            CANDLE_SECONDS,
            LOOKBACK + 5,
            time.time()
        )
    except Exception as exc:
        log.warning(
            "Candle request failed for %s: %s", pair, exc
        )
        return []

    if not isinstance(candles, list) or not candles:
        return []

    candles = sorted(
        candles,
        key=lambda candle: candle.get("from", 0)
    )

    now = time.time()
    closed = []

    for candle in candles:
        try:
            start = float(candle["from"])

            if start + CANDLE_SECONDS <= now:
                closed.append(candle)
        except (KeyError, TypeError, ValueError):
            continue

    return closed


def candle_values(candle):
    return (
        float(candle["open"]),
        float(candle["close"]),
        float(candle["max"]),
        float(candle["min"])
    )


def calculate_tolerance(candles, level):
    recent = candles[-10:]
    total_range = 0.0
    count = 0

    for candle in recent:
        try:
            total_range += (
                float(candle["max"]) - float(candle["min"])
            )
            count += 1
        except (KeyError, TypeError, ValueError):
            continue

    if count == 0:
        return abs(level) * 0.00015

    return max(
        (total_range / count) * 0.25,
        abs(level) * 0.00005
    )


def inspect_pair(pair):
    candles = get_closed_candles(pair)

    if len(candles) < LOOKBACK + 1:
        return None

    current = candles[-1]

    try:
        candle_time = int(current["from"])
    except (KeyError, TypeError, ValueError):
        return None

    if last_seen.get(pair) == candle_time:
        return None

    last_seen[pair] = candle_time

    try:
        opening, closing, high, low = candle_values(current)

        previous = candles[-LOOKBACK - 1:-1]

        resistance = max(
            float(candle["max"]) for candle in previous
        )
        support = min(
            float(candle["min"]) for candle in previous
        )
    except (KeyError, TypeError, ValueError):
        return None

    tolerance = calculate_tolerance(
        previous, resistance
    )

    setup = setups.get(pair)

    if setup:
        age = int(
            (candle_time - setup["time"]) / CANDLE_SECONDS
        )

        if age > RETEST_CANDLES:
            log.info("Setup expired: %s", pair)
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
                    "pair": pair,
                    "direction": "call",
                    "level": level
                }

            if closing < level - tolerance:
                setups.pop(pair, None)
                log.info("Bullish retest failed: %s", pair)

        else:
            touched = high >= level - tolerance
            held = closing <= level
            bearish = closing < opening

            if touched and held and bearish:
                setups.pop(pair, None)
                return {
                    "pair": pair,
                    "direction": "put",
                    "level": level
                }

            if closing > level + tolerance:
                setups.pop(pair, None)
                log.info("Bearish retest failed: %s", pair)

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
            + "\nWaiting for retest confirmation."
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
            + "\nWaiting for retest confirmation."
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
        "Trade result is unresolved: " + str(result)
    )


def place_trade(signal):
    global daily_pnl
    global daily_trades
    global consecutive_losses
    global halted_uncertain

    if trading_stopped():
        return

    pair = signal["pair"]
    direction = signal["direction"]
    now = time.time()

    if now - last_trade_time.get(pair, 0) < 60:
        return

    action = "BUY" if direction == "call" else "SELL"

    notify(
        "ENTRY SIGNAL\n"
        + "Pair: " + pair
        + "\nDirection: " + action
        + "\nStake: $1.00"
        + "\nExpiry: 1 minute"
        + "\nLevel: " + str(round(signal["level"], 6))
    )

    try:
        accepted, order_id = api.buy(
            STAKE,
            pair,
            direction,
            EXPIRY_MINUTES
        )
    except Exception as exc:
        halted_uncertain = True
        save_state()

        notify(
            "SAFETY HALT: ORDER STATUS UNKNOWN\n"
            + pair + "\n" + str(exc)
            + "\nCheck IQ Option before restarting."
        )
        return

    if not accepted:
        notify("ORDER REJECTED: " + pair)
        return

    daily_trades += 1
    last_trade_time[pair] = now
    save_state()

    notify(
        "ORDER ACCEPTED\n"
        + pair + " " + action
        + "\nOrder ID: " + str(order_id)
    )

    try:
        profit = get_profit(order_id)
    except Exception as exc:
        halted_uncertain = True
        save_state()

        notify(
            "SAFETY HALT: RESULT UNKNOWN\n"
            + pair + "\n" + str(exc)
            + "\nVerify the trade manually."
        )
        return

    daily_pnl += profit

    if profit > 0:
        consecutive_losses = 0
        outcome = "WIN"
    else:
        # Zero P/L counts as a loss.
        consecutive_losses += 1
        outcome = "LOSS"

    save_state()

    notify(
        "TRADE RESULT: " + outcome
        + "\nPair: " + pair
        + "\nProfit: $" + format(profit, ".2f")
        + "\nDaily P/L: $" + format(daily_pnl, ".2f")
        + "\nTrades: " + str(daily_trades)
        + "\nLoss streak: " + str(consecutive_losses)
    )


def trading_stopped():
    global stop_notice_sent

    reason = None

    if halted_uncertain:
        reason = "An order or saved state is uncertain."
    elif daily_pnl <= -DAILY_LOSS_LIMIT:
        reason = "Daily loss limit reached."
    elif consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        reason = "Three consecutive losses reached."
    elif daily_trades >= MAX_TRADES_PER_DAY:
        reason = "Daily trade limit reached."

    if reason:
        if not stop_notice_sent:
            notify("TRADING STOPPED\n" + reason)
            stop_notice_sent = True
        return True

    return False


def main():
    load_state()
    connect_iq()

    pairs = []
    last_discovery = 0

    notify(
        "SCANNER STARTED\n"
        "Strategy: Forex OTC Breakout + Retest\n"
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
            time.sleep(30)
            continue

        try:
            connected = api.check_connect()
        except Exception:
            connected = False

        if not connected:
            notify("Connection lost. Reconnecting.")

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
                    "Scan error for %s: %s", pair, exc
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
