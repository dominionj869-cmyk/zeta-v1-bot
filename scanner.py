
import os
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from iqoptionapi.stable_api import IQ_Option

# =========================================================
# BREAKOUT + RETEST CRYPTO SCANNER
# IQ OPTION PRACTICE ACCOUNT ONLY
# No Momentum 10/20. No martingale. No recovery staking.
# =========================================================

BALANCE_MODE = "PRACTICE"

CANDLE_SECONDS = 60
EXPIRY_MINUTES = 1
STAKE = 1.0

LOOKBACK = 20
ATR_PERIOD = 14
BREAKOUT_ATR_BUFFER = 0.05
RETEST_ATR_TOLERANCE = 0.15
MAX_RETEST_CANDLES = 5

DAILY_LOSS_LIMIT = 3.0
MAX_TRADES_PER_DAY = 20
MAX_CONSECUTIVE_LOSSES = 3

SCAN_DELAY_SECONDS = 3
CANDLE_COUNT = 60
TIMEZONE = ZoneInfo("Africa/Lagos")

# Only these crypto symbols are considered.
# Actual availability depends on your IQ Option account.
CRYPTO_ACTIVES = [
    "BTCUSD",
    "ETHUSD",
    "LTCUSD",
    "XRPUSD",
    "BCHUSD",
    "EOSUSD",
    "ADAUSD",
    "DOTUSD",
    "SOLUSD",
    "DOGEUSD",
    "TRXUSD",
    "BNBUSD",
    "AVAXUSD",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("breakout_retest")


def local_day():
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def connect_iq():
    email = os.getenv("IQ_EMAIL")
    password = os.getenv("IQ_PASSWORD")

    if not email or not password:
        raise RuntimeError(
            "Set IQ_EMAIL and IQ_PASSWORD in GitHub Secrets."
        )

    api = IQ_Option(email, password)
    result = api.connect()

    if not result or not api.check_connect():
        raise RuntimeError("IQ Option connection failed.")

    api.change_balance(BALANCE_MODE)

    if hasattr(api, "get_balance_mode"):
        mode = api.get_balance_mode()
        if str(mode).upper() != "PRACTICE":
            raise RuntimeError(
                "Practice account verification failed. "
                "Current mode: " + str(mode)
            )

    logger.info("Connected. Practice mode requested.")
    return api


def get_candles(api, active):
    try:
        candles = api.get_candles(
            active,
            CANDLE_SECONDS,
            CANDLE_COUNT,
            time.time()
        )

        if not candles or len(candles) < LOOKBACK + ATR_PERIOD + 3:
            return None

        candles.sort(key=lambda item: item["from"])

        # The newest candle may still be forming.
        # Only completed candles are used for signals.
        completed = candles[:-1]

        if len(completed) < LOOKBACK + ATR_PERIOD + 2:
            return None

        return completed

    except Exception as error:
        logger.warning(
            "Candle fetch failed for %s: %s",
            active,
            error
        )
        return None


def calculate_atr(candles):
    if len(candles) < ATR_PERIOD + 1:
        return None

    ranges = []

    start = len(candles) - ATR_PERIOD

    for index in range(start, len(candles)):
        current = candles[index]
        previous_close = candles[index - 1]["close"]

        true_range = max(
            current["max"] - current["min"],
            abs(current["max"] - previous_close),
            abs(current["min"] - previous_close)
        )

        ranges.append(true_range)

    if not ranges:
        return None

    return sum(ranges) / len(ranges)


def get_levels(candles):
    previous = candles[-LOOKBACK - 1:-1]

    if len(previous) < LOOKBACK:
        return None, None

    resistance = max(item["max"] for item in previous)
    support = min(item["min"] for item in previous)

    return support, resistance


def new_setup(direction, level, atr, candle_time):
    return {
        "direction": direction,
        "level": level,
        "atr": atr,
        "breakout_time": candle_time,
        "waited": 0
    }


def inspect_candle(candles, state):
    candle = candles[-1]
    candle_time = candle["from"]

    atr = calculate_atr(candles)
    if atr is None or atr <= 0:
        return None

    support, resistance = get_levels(candles)
    if support is None or resistance is None:
        return None

    # Do not reuse the candle that was already processed.
    if state.get("last_candle") == candle_time:
        return None

    state["last_candle"] = candle_time

    setup = state.get("setup")

    if setup is None:
        bullish_breakout = (
            candle["close"]
            > resistance + atr * BREAKOUT_ATR_BUFFER
        )

        bearish_breakout = (
            candle["close"]
            < support - atr * BREAKOUT_ATR_BUFFER
        )

        if bullish_breakout:
            state["setup"] = new_setup(
                "call",
                resistance,
                atr,
                candle_time
            )

            logger.info(
                "BREAKOUT UP %s | level=%.8f",
                state["active"],
                resistance
            )

        elif bearish_breakout:
            state["setup"] = new_setup(
                "put",
                support,
                atr,
                candle_time
            )

            logger.info(
                "BREAKOUT DOWN %s | level=%.8f",
                state["active"],
                support
            )

        return None

    # Only candles after the breakout can confirm a retest.
    if candle_time <= setup["breakout_time"]:
        return None

    setup["waited"] += 1

    if setup["waited"] > MAX_RETEST_CANDLES:
        logger.info("Retest expired for %s", state["active"])
        state["setup"] = None
        return None

    level = setup["level"]
    tolerance = setup["atr"] * RETEST_ATR_TOLERANCE

    if setup["direction"] == "call":
        invalidated = candle["close"] < level - tolerance

        retest = (
            candle["min"] <= level + tolerance
            and candle["min"] >= level - tolerance
        )

        confirmation = (
            candle["close"] > level
            and candle["close"] > candle["open"]
        )

    else:
        invalidated = candle["close"] > level + tolerance

        retest = (
            candle["max"] >= level - tolerance
            and candle["max"] <= level + tolerance
        )

        confirmation = (
            candle["close"] < level
            and candle["close"] < candle["open"]
        )

    if invalidated:
        logger.info("Setup invalidated for %s", state["active"])
        state["setup"] = None
        return None

    if retest and confirmation:
        signal = {
            "active": state["active"],
            "direction": setup["direction"],
            "level": level,
            "candle_time": candle_time
        }

        state["setup"] = None
        return signal

    return None


def place_practice_trade(api, signal):
    active = signal["active"]
    direction = signal["direction"]

    logger.info(
        "ENTRY SIGNAL | %s | %s | level=%.8f",
        active,
        direction.upper(),
        signal["level"]
    )

    try:
        success, order_id = api.buy(
            STAKE,
            active,
            direction,
            EXPIRY_MINUTES
        )

        if not success or order_id is None:
            logger.warning("Order rejected for %s", active)
            return None

        logger.info(
            "ORDER ACCEPTED | %s | id=%s | stake=$%.2f",
            active,
            str(order_id),
            STAKE
        )

        return order_id

    except Exception as error:
        logger.error(
            "Order submission error for %s: %s",
            active,
            error
        )
        return None


def get_trade_profit(api, order_id):
    # Do not retry the trade or increase the stake if result
    # retrieval fails. Stop the session if the result is unclear.
    result = api.check_win_v4(order_id)

    if isinstance(result, (int, float)):
        return float(result)

    raise RuntimeError(
        "Unrecognized trade result for order " + str(order_id)
        + ". Stop and verify the order in IQ Option."
    )


def main():
    api = connect_iq()

    states = {}
    for active in CRYPTO_ACTIVES:
        states[active] = {
            "active": active,
            "setup": None,
            "last_candle": None
        }

    current_day = local_day()
    daily_profit = 0.0
    trades_today = 0
    consecutive_losses = 0

    logger.info("Strategy: Breakout + Retest")
    logger.info("Timeframe: 1 minute")
    logger.info("Stake: fixed $%.2f", STAKE)
    logger.info("Recovery staking: disabled")
    logger.info("Daily loss limit: $%.2f", DAILY_LOSS_LIMIT)

    while True:
        try:
            today = local_day()

            if today != current_day:
                current_day = today
                daily_profit = 0.0
                trades_today = 0
                consecutive_losses = 0

                for state in states.values():
                    state["setup"] = None

                logger.info("New daily session started.")

            if daily_profit <= -DAILY_LOSS_LIMIT:
                logger.warning("Daily loss limit reached. Trading stopped.")
                break

            if trades_today >= MAX_TRADES_PER_DAY:
                logger.warning("Daily trade limit reached. Trading stopped.")
                break

            if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                logger.warning(
                    "Consecutive loss limit reached. Trading stopped."
                )
                break

            signal = None

            for active in CRYPTO_ACTIVES:
                state = states[active]
                candles = get_candles(api, active)

                if candles is None:
                    continue

                candidate = inspect_candle(candles, state)

                if candidate is not None:
                    signal = candidate
                    break

            if signal is None:
                time.sleep(SCAN_DELAY_SECONDS)
                continue

            order_id = place_practice_trade(api, signal)

            if order_id is None:
                time.sleep(SCAN_DELAY_SECONDS)
                continue

            # One trade at a time. Wait for the result before
            # evaluating another entry, preventing overlapping trades.
            profit = get_trade_profit(api, order_id)

            daily_profit += profit
            trades_today += 1

            if profit > 0:
                consecutive_losses = 0
                outcome = "WIN"
            else:
                # Zero-profit outcomes count as a loss for the
                # consecutive-loss safety rule.
                consecutive_losses += 1
                outcome = "LOSS/TIE"

            logger.info(
                "TRADE RESULT | %s | profit=$%.2f | "
                "daily=$%.2f | trades=%d | consecutive_losses=%d",
                outcome,
                profit,
                daily_profit,
                trades_today,
                consecutive_losses
            )

            time.sleep(SCAN_DELAY_SECONDS)

        except KeyboardInterrupt:
            logger.info("Stopped by user.")
            break

        except Exception as error:
            logger.exception("Critical error: %s", error)
            logger.error(
                "Trading halted for safety. Verify account and open orders."
            )
            break

    logger.info(
        "SESSION SUMMARY | trades=%d | net=$%.2f",
        trades_today,
        daily_profit
    )


if __name__ == "__main__":
    main()
