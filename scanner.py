import os
import time
import math
import socket
import threading
import traceback

import requests
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code


# ============================================================
# ZETA V1 — IQ OPTION OTC DEMO SCANNER
# ============================================================

BALANCE_MODE = "PRACTICE"

STAKE = 1.0
EXPIRY_MINUTES = 2

TIMEFRAME = 60
CANDLE_COUNT = 180
MAX_OTC_ASSETS = 60

SCAN_INTERVAL = 5
STATUS_INTERVAL = 300
OTC_REFRESH_INTERVAL = 1800

FAST_LENGTH = 9
SLOW_LENGTH = 21
ATR_PERIOD = 14

NORMALIZED_DISTANCE_MIN = 0.05

USE_CANDLE_CONFIRM = True
USE_TREND_SLOPE = True

SIGNAL_COOLDOWN_BARS = 3

LOGIN_TIMEOUT = 25
SOCKET_TIMEOUT = 20

RECONNECT_INTERVAL = 30
TRADE_RESULT_CHECK_INTERVAL = 5

MAX_ACTIVE_TRADES = 1000


# ============================================================
# ENVIRONMENT
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


# ============================================================
# GLOBAL STATE
# ============================================================

api = None
otc_assets = {}

active_trades = {}
last_signal_bar = {}

last_status_time = 0
last_otc_refresh = 0

connection_lock = threading.Lock()


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        url = (
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        )

        requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            timeout=15,
        )

    except Exception as e:
        print("Telegram error:", e, flush=True)


# ============================================================
# LOGGING
# ============================================================

def log(message):
    print(message, flush=True)


# ============================================================
# SAFE FLOAT
# ============================================================

def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


# ============================================================
# EMA
# ============================================================

def ema(values, period):
    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)

    result = [sum(values[:period]) / period]

    for price in values[period:]:
        result.append(
            (price - result[-1]) * multiplier + result[-1]
        )

    return result


# ============================================================
# ATR
# ============================================================

def atr(candles, period=14):
    if len(candles) < period + 1:
        return 0.0

    trs = []

    for i in range(1, len(candles)):
        high = safe_float(candles[i].get("max"))
        low = safe_float(candles[i].get("min"))
        previous_close = safe_float(candles[i - 1].get("close"))

        true_range = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )

        trs.append(true_range)

    if len(trs) < period:
        return 0.0

    return sum(trs[-period:]) / period


# ============================================================
# CANDLE CONFIRMATION
# ============================================================

def bullish_candle(candle):
    open_price = safe_float(candle.get("open"))
    close_price = safe_float(candle.get("close"))
    high = safe_float(candle.get("max"))
    low = safe_float(candle.get("min"))

    body = abs(close_price - open_price)
    candle_range = high - low

    if candle_range <= 0:
        return False

    return (
        close_price > open_price
        and body / candle_range >= 0.35
    )


def bearish_candle(candle):
    open_price = safe_float(candle.get("open"))
    close_price = safe_float(candle.get("close"))
    high = safe_float(candle.get("max"))
    low = safe_float(candle.get("min"))

    body = abs(close_price - open_price)
    candle_range = high - low

    if candle_range <= 0:
        return False

    return (
        close_price < open_price
        and body / candle_range >= 0.35
    )


# ============================================================
# CONNECT IQ OPTION
# ============================================================

def connect_iq():

    global api

    if not IQ_EMAIL or not IQ_PASSWORD:
        log("ERROR: IQ_EMAIL or IQ_PASSWORD is missing.")
        telegram("🔴 ZETA V1 ERROR\nIQ Option credentials are missing.")
        return False

    log("")
    log("================================================")
    log("ZETA V1 — CONNECTING TO IQ OPTION")
    log("================================================")

    telegram(
        "🟡 ZETA V1 STARTING\n"
        "Connecting to IQ Option...\n"
        "Mode: PRACTICE\n"
        "Expiry: 2 minutes"
    )

    socket.setdefaulttimeout(SOCKET_TIMEOUT)

    try:
        api = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD
        )
    except Exception as e:
        log("IQ Option object creation failed:")
        log(str(e))
        return False

    result = {
        "connected": False,
        "message": ""
    }

    def worker():

        try:
            ok, reason = api.connect()

            result["connected"] = bool(ok)
            result["message"] = str(reason)

        except Exception as e:
            result["message"] = str(e)

    thread = threading.Thread(
        target=worker,
        daemon=True
    )

    thread.start()

    thread.join(LOGIN_TIMEOUT)

    if thread.is_alive():

        log("Connection timeout.")

        telegram(
            "🔴 ZETA V1 CONNECTION TIMEOUT\n"
            "IQ Option did not complete connection."
        )

        return False

    if not result["connected"]:

        log(
            "Connection failed: "
            + result["message"]
        )

        telegram(
            "🔴 ZETA V1 CONNECTION FAILED\n"
            + result["message"]
        )

        return False

    try:
        api.change_balance(BALANCE_MODE)
    except Exception:
        pass

    log("IQ Option connection: OK")

    telegram(
        "🟢 ZETA V1 CONNECTED\n"
        "IQ Option connection: OK\n"
        "Mode: PRACTICE\n"
        "Auto-trading: ON\n"
        "Expiry: 2 minutes"
    )

    return True


# ============================================================
# OTC DISCOVERY
# ============================================================

def discover_otc_assets():

    global otc_assets

    log("")
    log("Discovering IQ Option OTC markets...")

    discovered = {}

    try:

        data = None

        try:
            data = api.get_all_init_v2()
        except Exception as e:
            log("get_all_init_v2 failed: " + str(e))

        if not data:

            try:
                data = api.get_all_init()
            except Exception as e:
                log("get_all_init failed: " + str(e))

        if not data:
            log("No market data received.")
            return False

        market_groups = [
            "binary",
            "turbo",
            "digital"
        ]

        for group in market_groups:

            group_data = data.get(group, {})

            if not isinstance(group_data, dict):
                continue

            for asset_name, info in group_data.items():

                name = str(asset_name)

                if "OTC" not in name.upper():
                    continue

                if not isinstance(info, dict):
                    continue

                active_id = (
                    info.get("active_id")
                    or info.get("id")
                )

                if active_id is None:
                    continue

                try:
                    active_id = int(active_id)
                except Exception:
                    continue

                discovered[name] = active_id

                if len(discovered) >= MAX_OTC_ASSETS:
                    break

            if len(discovered) >= MAX_OTC_ASSETS:
                break

        if not discovered:

            log("OTC markets discovered: 0")

            telegram(
                "🟠 ZETA V1 STATUS\n"
                "IQ Option connected\n"
                "OTC markets discovered: 0"
            )

            return False

        otc_assets = discovered

        # Register active IDs for candle streaming/access
        try:
            ids = list(discovered.values())

            if ids:
                api.subscribe_instrument(
                    "candle-generated",
                    ids
                )

        except Exception:
            pass

        log(
            "OTC markets discovered: "
            + str(len(otc_assets))
        )

        return True

    except Exception as e:

        log("OTC discovery error:")
        log(str(e))
        traceback.print_exc()

        return False


# ============================================================
# GET CANDLES
# ============================================================

def get_candles(asset):

    try:

        candles = api.get_candles(
            asset,
            TIMEFRAME,
            CANDLE_COUNT,
            time.time()
        )

        if not candles:
            return []

        candles = list(candles)

        candles.sort(
            key=lambda x: safe_float(x.get("from"))
        )

        # Remove current/open candle
        if len(candles) > 2:
            candles = candles[:-1]

        return candles

    except Exception as e:

        log(
            f"Candle error {asset}: {e}"
        )

        return []


# ============================================================
# SIGNAL ANALYSIS
# ============================================================

def analyze(asset, candles):

    if len(candles) < max(
        SLOW_LENGTH + 10,
        ATR_PERIOD + 10
    ):
        return None

    closes = [
        safe_float(c.get("close"))
        for c in candles
    ]

    fast = ema(
        closes,
        FAST_LENGTH
    )

    slow = ema(
        closes,
        SLOW_LENGTH
    )

    if len(fast) < 5 or len(slow) < 5:
        return None

    fast_now = fast[-1]
    fast_previous = fast[-2]

    slow_now = slow[-1]
    slow_previous = slow[-2]

    price = closes[-1]

    current_candle = candles[-1]

    current_atr = atr(
        candles,
        ATR_PERIOD
    )

    if current_atr <= 0:
        return None

    distance = abs(
        fast_now - slow_now
    )

    normalized_distance = (
        distance / current_atr
    )

    bullish_trend = (
        fast_now > slow_now
    )

    bearish_trend = (
        fast_now < slow_now
    )

    bullish_slope = (
        fast_now > fast_previous
        and slow_now >= slow_previous
    )

    bearish_slope = (
        fast_now < fast_previous
        and slow_now <= slow_previous
    )

    bullish_candle_ok = bullish_candle(
        current_candle
    )

    bearish_candle_ok = bearish_candle(
        current_candle
    )

    previous_fast = fast[-2]
    previous_slow = slow[-2]

    bullish_flip = (
        previous_fast <= previous_slow
        and fast_now > slow_now
    )

    bearish_flip = (
        previous_fast >= previous_slow
        and fast_now < slow_now
    )

    # --------------------------------------------------------
    # CALL
    # --------------------------------------------------------

    call_conditions = 0

    if bullish_trend:
        call_conditions += 1

    if price > fast_now:
        call_conditions += 1

    if bullish_candle_ok:
        call_conditions += 1

    if bullish_slope:
        call_conditions += 1

    if bullish_flip:
        call_conditions += 1

    if (
        call_conditions >= 4
        and normalized_distance >= NORMALIZED_DISTANCE_MIN
    ):

        return {
            "direction": "CALL",
            "price": price,
            "atr": current_atr,
            "distance": normalized_distance,
            "conditions": call_conditions,
        }

    # --------------------------------------------------------
    # PUT
    # --------------------------------------------------------

    put_conditions = 0

    if bearish_trend:
        put_conditions += 1

    if price < fast_now:
        put_conditions += 1

    if bearish_candle_ok:
        put_conditions += 1

    if bearish_slope:
        put_conditions += 1

    if bearish_flip:
        put_conditions += 1

    if (
        put_conditions >= 4
        and normalized_distance >= NORMALIZED_DISTANCE_MIN
    ):

        return {
            "direction": "PUT",
            "price": price,
            "atr": current_atr,
            "distance": normalized_distance,
            "conditions": put_conditions,
        }

    return None


# ============================================================
# OPEN DEMO TRADE
# ============================================================

def open_trade(asset, signal):

    direction = signal["direction"]

    iq_direction = (
        "call"
        if direction == "CALL"
        else "put"
    )

    signal_id = (
        "ZETA-"
        + asset.replace("/", "")
        .replace("(", "")
        .replace(")", "")
        .replace(" ", "")
        + "-"
        + direction
        + "-"
        + str(int(time.time()))
    )

    log("")
    log("==============================================")
    log("NEW ZETA V1 SIGNAL")
    log("Asset: " + asset)
    log("Direction: " + direction)
    log(
        "Conditions: "
        + str(signal["conditions"])
        + "/5"
    )
    log(
        "Expiry: "
        + str(EXPIRY_MINUTES)
        + " minutes"
    )
    log("==============================================")

    telegram(
        "🟡 ZETA V1 SIGNAL\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Asset: {asset}\n"
        f"Direction: {direction}\n"
        f"Conditions: {signal['conditions']}/5\n"
        f"Expiry: {EXPIRY_MINUTES} minutes\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Signal ID: {signal_id}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Sending DEMO order..."
    )

    try:

        success, order_id = api.buy(
            STAKE,
            asset,
            iq_direction,
            EXPIRY_MINUTES
        )

        if not success:

            log(
                "DEMO order failed: "
                + str(order_id)
            )

            telegram(
                "🔴 ZETA V1 ORDER FAILED\n"
                f"Asset: {asset}\n"
                f"Direction: {direction}\n"
                f"Reason: {order_id}"
            )

            return False

        order_id = str(order_id)

        active_trades[order_id] = {
            "asset": asset,
            "direction": direction,
            "signal_id": signal_id,
            "opened": time.time(),
            "stake": STAKE,
        }

        log(
            "DEMO trade opened: "
            + order_id
        )

        telegram(
            "🟢 ZETA V1 DEMO TRADE OPENED\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Asset: {asset}\n"
            f"Direction: {direction}\n"
            f"Stake: ${STAKE:.2f}\n"
            f"Expiry: {EXPIRY_MINUTES} minutes\n"
            f"Order ID: {order_id}\n"
            f"Signal ID: {signal_id}\n"
            "━━━━━━━━━━━━━━━━━━"
        )

        return True

    except Exception as e:

        log("Order error:")
        log(str(e))

        telegram(
            "🔴 ZETA V1 ORDER ERROR\n"
            + str(e)
        )

        return False


# ============================================================
# CHECK TRADE RESULTS
# ============================================================

def check_trade_results():

    if not active_trades:
        return

    finished = []

    for order_id, trade in list(
        active_trades.items()
    ):

        try:

            result = api.check_win_v4(
                order_id
            )

            if result is None:
                continue

            result_value = safe_float(
                result,
                None
            )

            if result_value is None:
                continue

            if result_value > 0:

                outcome = "WIN"
                emoji = "🟢"

            elif result_value < 0:

                outcome = "LOSS"
                emoji = "🔴"

            else:

                outcome = "DRAW"
                emoji = "🟡"

            asset = trade["asset"]

            log(
                f"{emoji} {outcome} | "
                f"{asset} | "
                f"${result_value:.2f}"
            )

            telegram(
                f"{emoji} ZETA V1 TRADE RESULT\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"Asset: {asset}\n"
                f"Direction: {trade['direction']}\n"
                f"Result: {outcome}\n"
                f"P/L: ${result_value:.2f}\n"
                f"Signal ID: {trade['signal_id']}\n"
                "━━━━━━━━━━━━━━━━━━"
            )

            finished.append(order_id)

        except Exception:
            continue

    for order_id in finished:

        active_trades.pop(
            order_id,
            None
        )


# ============================================================
# HEARTBEAT
# ============================================================

def heartbeat():

    global last_status_time

    now = time.time()

    if (
        now - last_status_time
        < STATUS_INTERVAL
    ):
        return

    last_status_time = now

    message = (
        "🟢 ZETA V1 HEARTBEAT\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"OTC markets: {len(otc_assets)}\n"
        f"Active trades: {len(active_trades)}\n"
        "Strategy: EMA 9/21\n"
        "Timeframe: 1M\n"
        f"Expiry: {EXPIRY_MINUTES} minutes\n"
        "Mode: PRACTICE\n"
        "Auto-trading: ON\n"
        "Status: RUNNING\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    log(message)
    telegram(message)


# ============================================================
# MAIN SCANNER
# ============================================================

def scanner_loop():

    global last_otc_refresh

    last_otc_refresh = 0

    while True:

        try:

            now = time.time()

            # ------------------------------------------------
            # Refresh OTC markets
            # ------------------------------------------------

            if (
                not otc_assets
                or now - last_otc_refresh
                >= OTC_REFRESH_INTERVAL
            ):

                if discover_otc_assets():
                    last_otc_refresh = now

            # ------------------------------------------------
            # Check results
            # ------------------------------------------------

            check_trade_results()

            # ------------------------------------------------
            # Heartbeat
            # ------------------------------------------------

            heartbeat()

            # ------------------------------------------------
            # Scan OTC assets
            # ------------------------------------------------

            if otc_assets:

                for asset in list(
                    otc_assets.keys()
                ):

                    try:

                        candles = get_candles(
                            asset
                        )

                        if not candles:
                            continue

                        signal = analyze(
                            asset,
                            candles
                        )

                        if signal is None:
                            continue

                        # ------------------------------------
                        # Prevent repeated signals
                        # ------------------------------------

                        latest_bar = safe_float(
                            candles[-1].get("from")
                        )

                        previous_bar = last_signal_bar.get(
                            asset,
                            0
                        )

                        if (
                            latest_bar
                            <= previous_bar
                        ):
                            continue

                        last_signal_bar[asset] = (
                            latest_bar
                        )

                        # ------------------------------------
                        # Open demo trade
                        # ------------------------------------

                        if (
                            len(active_trades)
                            >= MAX_ACTIVE_TRADES
                        ):
                            continue

                        open_trade(
                            asset,
                            signal
                        )

                    except Exception as e:

                        log(
                            f"Scan error {asset}: {e}"
                        )

            time.sleep(
                SCAN_INTERVAL
            )

        except KeyboardInterrupt:

            log("Scanner stopped.")
            break

        except Exception as e:

            log("Scanner loop error:")
            log(str(e))
            traceback.print_exc()

            time.sleep(
                RECONNECT_INTERVAL
            )


# ============================================================
# MAIN
# ============================================================

def main():

    log("")
    log("================================================")
    log("ZETA V1 BOT")
    log("================================================")
    log("Strategy: EMA 9/21")
    log("Chart: 1 Minute")
    log("Expiry: 2 Minutes")
    log("Mode: PRACTICE")
    log("Auto-trading: ON")
    log("================================================")

    if not connect_iq():

        log("Unable to connect to IQ Option.")

        return

    if not discover_otc_assets():

        log(
            "WARNING: No OTC assets discovered."
        )

    telegram(
        "🔵 ZETA V1 SCANNER STARTED\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"OTC markets: {len(otc_assets)}\n"
        "Strategy: EMA 9/21\n"
        "Chart: 1M\n"
        "Expiry: 2M\n"
        "Mode: PRACTICE\n"
        "Auto-trading: ON\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Scanning..."
    )

    scanner_loop()


if __name__ == "__main__":
    main()
