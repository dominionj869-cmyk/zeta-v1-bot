import os
import time
import math
import socket
import threading
import traceback
from datetime import datetime

from iqoptionapi.stable_api import IQ_Option
from iqoptionapi.constants import OP_code
import requests


# ============================================================
# ZETA V1
# IQ OPTION OTC DEMO SCANNER
# ============================================================

# -------------------- ENVIRONMENT --------------------

IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# -------------------- ACCOUNT --------------------

BALANCE_MODE = "PRACTICE"

# DEMO ONLY
STAKE = 1.0

# Reference expiry
EXPIRY_MINUTES = 2


# -------------------- MARKET --------------------

TIMEFRAME = 60
CANDLE_COUNT = 180
MAX_OTC_ASSETS = 60

# Scan frequently, but only evaluate closed candles
SCAN_INTERVAL = 5

# Telegram heartbeat
STATUS_INTERVAL = 300

# Refresh OTC list
OTC_REFRESH_INTERVAL = 1800


# -------------------- STRATEGY --------------------

FAST_LENGTH = 9
SLOW_LENGTH = 21
ATR_PERIOD = 14

# Minimum EMA distance measured in ATR
NORMALIZED_DISTANCE_MIN = 0.05

USE_CANDLE_CONFIRM = True
USE_TREND_SLOPE = True

# Prevent repeated signals on the same setup
SIGNAL_COOLDOWN_BARS = 3


# -------------------- CONNECTION --------------------

LOGIN_TIMEOUT = 25
SOCKET_TIMEOUT = 20

RECONNECT_INTERVAL = 30


# -------------------- TRADE TRACKING --------------------

TRADE_RESULT_CHECK_INTERVAL = 5
MAX_ACTIVE_TRADES = 1000


# ============================================================
# GLOBAL STATE
# ============================================================

api = None

otc_assets = []
active_trades = {}

last_signal_bar = {}
last_status_time = 0
last_otc_refresh = 0

total_trades = 0
wins = 0
losses = 0
draws = 0
net_profit = 0.0

bot_started_at = time.time()

connection_lock = threading.Lock()


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(message)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        requests.post(
            url,
            json=payload,
            timeout=15
        )
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")

    print(message)


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    if not values:
        return []

    if len(values) < period:
        return [None] * len(values)

    result = [None] * len(values)

    seed = sum(values[:period]) / period
    result[period - 1] = seed

    multiplier = 2.0 / (period + 1)

    previous = seed

    for i in range(period, len(values)):
        previous = (
            (values[i] - previous) * multiplier
        ) + previous

        result[i] = previous

    return result


def wilder_atr(candles, period=14):
    if len(candles) < period + 1:
        return [None] * len(candles)

    true_ranges = [None] * len(candles)

    for i in range(1, len(candles)):
        high = float(candles[i]["max"])
        low = float(candles[i]["min"])
        previous_close = float(candles[i - 1]["close"])

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )

        true_ranges[i] = tr

    atr = [None] * len(candles)

    start = period

    first_values = [
        x for x in true_ranges[1:start + 1]
        if x is not None
    ]

    if len(first_values) < period:
        return atr

    current = sum(first_values) / period
    atr[start] = current

    for i in range(start + 1, len(candles)):
        tr = true_ranges[i]

        if tr is None:
            continue

        current = (
            ((current * (period - 1)) + tr)
            / period
        )

        atr[i] = current

    return atr


# ============================================================
# CANDLE HELPERS
# ============================================================

def candle_bullish(candle):
    return float(candle["close"]) > float(candle["open"])


def candle_bearish(candle):
    return float(candle["close"]) < float(candle["open"])


def valid_number(value):
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


# ============================================================
# SIGNAL ENGINE
# ============================================================

def analyze_asset(asset, candles):
    if not candles or len(candles) < SLOW_LENGTH + ATR_PERIOD + 10:
        return None

    try:
        # Remove current/open candle.
        # IQ Option candle feeds normally include the latest candle.
        closed = candles[:-1]

        if len(closed) < SLOW_LENGTH + ATR_PERIOD + 10:
            return None

        closes = [
            float(c["close"])
            for c in closed
        ]

        fast = ema(closes, FAST_LENGTH)
        slow = ema(closes, SLOW_LENGTH)
        atr = wilder_atr(closed, ATR_PERIOD)

        i = len(closed) - 1
        previous = i - 1

        if previous < 1:
            return None

        if (
            fast[i] is None
            or fast[previous] is None
            or slow[i] is None
            or slow[previous] is None
            or atr[i] is None
        ):
            return None

        current_close = closes[i]
        current_fast = fast[i]
        previous_fast = fast[previous]

        current_slow = slow[i]
        previous_slow = slow[previous]

        current_atr = float(atr[i])

        if current_atr <= 0:
            return None

        # ----------------------------------------------------
        # Trend state
        # ----------------------------------------------------

        previous_bull = previous_fast > previous_slow
        current_bull = current_fast > current_slow

        previous_bear = previous_fast < previous_slow
        current_bear = current_fast < current_slow

        bullish_flip = (
            not previous_bull
            and current_bull
        )

        bearish_flip = (
            not previous_bear
            and current_bear
        )

        # ----------------------------------------------------
        # Trend slope
        # ----------------------------------------------------

        slope_up = current_fast > previous_fast
        slope_down = current_fast < previous_fast

        # ----------------------------------------------------
        # EMA distance
        # ----------------------------------------------------

        normalized_distance = (
            abs(current_fast - current_slow)
            / current_atr
        )

        if normalized_distance < NORMALIZED_DISTANCE_MIN:
            return None

        # ----------------------------------------------------
        # Candle confirmation
        # ----------------------------------------------------

        candle = closed[i]

        bullish_candle = candle_bullish(candle)
        bearish_candle = candle_bearish(candle)

        # ----------------------------------------------------
        # Signal
        # ----------------------------------------------------

        direction = None

        if bullish_flip:
            if current_close > current_fast:

                if USE_CANDLE_CONFIRM and not bullish_candle:
                    return None

                if USE_TREND_SLOPE and not slope_up:
                    return None

                direction = "CALL"

        elif bearish_flip:
            if current_close < current_fast:

                if USE_CANDLE_CONFIRM and not bearish_candle:
                    return None

                if USE_TREND_SLOPE and not slope_down:
                    return None

                direction = "PUT"

        if direction is None:
            return None

        bar_time = int(
            closed[i].get(
                "from",
                time.time()
            )
        )

        # ----------------------------------------------------
        # Cooldown
        # ----------------------------------------------------

        previous_signal_bar = last_signal_bar.get(asset)

        if previous_signal_bar is not None:

            bars_since = (
                bar_time - previous_signal_bar
            ) / TIMEFRAME

            if bars_since < SIGNAL_COOLDOWN_BARS:
                return None

        last_signal_bar[asset] = bar_time

        return {
            "asset": asset,
            "direction": direction,
            "price": current_close,
            "ema_fast": current_fast,
            "ema_slow": current_slow,
            "atr": current_atr,
            "distance": normalized_distance,
            "bar_time": bar_time,
        }

    except Exception:
        return None


# ============================================================
# IQ OPTION CONNECTION
# ============================================================

def connect_iq():
    global api

    if not IQ_EMAIL or not IQ_PASSWORD:
        telegram(
            "🔴 <b>ZETA V1 CONFIGURATION ERROR</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "IQ_EMAIL or IQ_PASSWORD is missing."
        )
        return False

    with connection_lock:

        try:
            socket.setdefaulttimeout(SOCKET_TIMEOUT)

            telegram(
                "🟡 <b>ZETA V1 CONNECTING</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Connecting to IQ Option..."
            )

            new_api = IQ_Option(
                IQ_EMAIL,
                IQ_PASSWORD
            )

            result = {
                "success": False,
                "error": None
            }

            def worker():
                try:
                    result["success"] = bool(
                        new_api.connect()
                    )
                except Exception as e:
                    result["error"] = str(e)

            thread = threading.Thread(
                target=worker,
                daemon=True
            )

            thread.start()

            thread.join(
                timeout=LOGIN_TIMEOUT
            )

            if thread.is_alive():

                telegram(
                    "🔴 <b>ZETA V1 CONNECTION TIMEOUT</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"IQ Option did not complete login within "
                    f"{LOGIN_TIMEOUT} seconds."
                )

                return False

            if not result["success"]:

                error_text = result["error"]

                telegram(
                    "🔴 <b>ZETA V1 CONNECTION FAILED</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"{error_text or 'IQ Option rejected the connection.'}"
                )

                return False

            api = new_api

            try:
                api.change_balance(BALANCE_MODE)
            except Exception:
                pass

            telegram(
                "🟢 <b>IQ OPTION CONNECTION SUCCESSFUL</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"Account: {BALANCE_MODE}\n"
                "Auto-trading: ON\n"
                "Mode: PRACTICE"
            )

            return True

        except Exception as e:

            telegram(
                "🔴 <b>ZETA V1 CONNECTION ERROR</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"{str(e)}"
            )

            return False


# ============================================================
# CONNECTION CHECK
# ============================================================

def connection_alive():
    global api

    if api is None:
        return False

    try:
        return bool(
            api.check_connect()
        )
    except Exception:
        return False


# ============================================================
# OTC DISCOVERY
# ============================================================

def discover_otc_assets():
    global otc_assets

    if api is None:
        return []

    discovered = {}

    try:

        # First initialization method
        try:
            api.get_all_init_v2()
        except Exception:
            pass

        time.sleep(2)

        # Second initialization method
        try:
            api.get_all_init()
        except Exception:
            pass

        time.sleep(2)

        data = None

        # Different iqoptionapi versions expose data differently
        candidates = [
            getattr(api, "all_init", None),
            getattr(api, "all_init_v2", None),
        ]

        for candidate in candidates:

            if isinstance(candidate, dict):
                data = candidate
                break

        if data is None:
            return []

        # ----------------------------------------------
        # Binary / Turbo OTC discovery
        # ----------------------------------------------

        for market_type in [
            "binary",
            "turbo",
            "digital"
        ]:

            section = data.get(
                market_type,
                {}
            )

            if not isinstance(section, dict):
                continue

            for asset, info in section.items():

                if not isinstance(asset, str):
                    continue

                upper = asset.upper()

                if (
                    "-OTC" not in upper
                    and "_OTC" not in upper
                    and "OTC" not in upper
                ):
                    continue

                active_id = None

                if isinstance(info, dict):

                    for key in [
                        "active_id",
                        "activeId",
                        "id"
                    ]:

                        if key in info:
                            try:
                                active_id = int(
                                    info[key]
                                )
                                break
                            except Exception:
                                pass

                if active_id is None:
                    continue

                discovered[asset] = active_id

        # ----------------------------------------------
        # Register active IDs
        # ----------------------------------------------

        try:
            for asset, active_id in discovered.items():
                OP_code.ACTIVES[asset] = active_id
        except Exception:
            pass

        assets = list(
            discovered.keys()
        )[:MAX_OTC_ASSETS]

        otc_assets = assets

        telegram(
            "🟢 <b>OTC DISCOVERY COMPLETE</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"OTC markets discovered: {len(assets)}"
        )

        return assets

    except Exception as e:

        telegram(
            "🟠 <b>OTC DISCOVERY ERROR</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"{str(e)}"
        )

        return []


# ============================================================
# CANDLE DATA
# ============================================================

def get_candles(asset):
    if api is None:
        return None

    try:

        candles = api.get_candles(
            asset,
            TIMEFRAME,
            CANDLE_COUNT,
            time.time()
        )

        if not candles:
            return None

        return candles

    except Exception:
        return None


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def signal_message(signal):

    direction = signal["direction"]

    emoji = (
        "🟢"
        if direction == "CALL"
        else "🔴"
    )

    return (
        f"{emoji} <b>ZETA V1 SIGNAL</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>Asset:</b> {signal['asset']}\n"
        f"<b>Direction:</b> {direction}\n"
        f"<b>Expiry:</b> {EXPIRY_MINUTES} minutes\n"
        f"<b>Price:</b> {signal['price']:.8f}\n"
        f"<b>EMA 9:</b> {signal['ema_fast']:.8f}\n"
        f"<b>EMA 21:</b> {signal['ema_slow']:.8f}\n"
        f"<b>ATR:</b> {signal['atr']:.8f}\n"
        f"<b>EMA distance:</b> "
        f"{signal['distance']:.2f} ATR\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Account:</b> PRACTICE"
    )


# ============================================================
# DEMO ORDER
# ============================================================

def place_demo_trade(signal):

    global total_trades

    if api is None:
        return False

    if len(active_trades) >= MAX_ACTIVE_TRADES:
        return False

    asset = signal["asset"]
    direction = signal["direction"]

    order_direction = (
        "call"
        if direction == "CALL"
        else "put"
    )

    try:

        telegram(
            "🟡 <b>ZETA V1 SENDING DEMO ORDER</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"<b>Asset:</b> {asset}\n"
            f"<b>Direction:</b> {direction}\n"
            f"<b>Stake:</b> ${STAKE:.2f}\n"
            f"<b>Expiry:</b> {EXPIRY_MINUTES} minutes"
        )

        success, trade_id = api.buy(
            STAKE,
            asset,
            order_direction,
            EXPIRY_MINUTES
        )

        if not success:

            telegram(
                "🔴 <b>DEMO ORDER FAILED</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"Asset: {asset}"
            )

            return False

        total_trades += 1

        signal_id = (
            f"ZETA-{asset.replace('-', '')}-"
            f"{direction}-"
            f"{int(time.time())}"
        )

        active_trades[str(trade_id)] = {
            "trade_id": str(trade_id),
            "signal_id": signal_id,
            "asset": asset,
            "direction": direction,
            "stake": STAKE,
            "opened_at": time.time(),
            "signal": signal,
        }

        telegram(
            "🚀 <b>ZETA V1 DEMO TRADE OPENED</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"<b>Asset:</b> {asset}\n"
            f"<b>Direction:</b> {direction}\n"
            f"<b>Stake:</b> ${STAKE:.2f}\n"
            f"<b>Expiry:</b> {EXPIRY_MINUTES} minutes\n"
            f"<b>Signal ID:</b> {signal_id}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<b>Account:</b> PRACTICE"
        )

        return True

    except Exception as e:

        telegram(
            "🔴 <b>DEMO ORDER ERROR</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"{str(e)}"
        )

        return False


# ============================================================
# TRADE RESULT MONITOR
# ============================================================

def check_trade_results():

    global wins
    global losses
    global draws
    global net_profit

    if api is None:
        return

    if not active_trades:
        return

    completed = []

    for trade_id, trade in list(
        active_trades.items()
    ):

        try:

            result = api.check_win_v4(
                trade_id
            )

            if result is None:
                continue

            # iqoptionapi can return numeric result
            profit = float(result)

            # Some API states may not be final
            if math.isnan(profit):
                continue

            if profit > 0:
                outcome = "WIN"
                wins += 1

            elif profit < 0:
                outcome = "LOSS"
                losses += 1

            else:
                outcome = "DRAW"
                draws += 1

            net_profit += profit

            win_rate = (
                (wins / total_trades) * 100
                if total_trades > 0
                else 0
            )

            emoji = {
                "WIN": "🟢",
                "LOSS": "🔴",
                "DRAW": "🟡"
            }.get(
                outcome,
                "⚪"
            )

            telegram(
                f"{emoji} <b>ZETA V1 TRADE RESULT</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"<b>Signal ID:</b> "
                f"{trade['signal_id']}\n"
                f"<b>Asset:</b> "
                f"{trade['asset']}\n"
                f"<b>Direction:</b> "
                f"{trade['direction']}\n"
                f"<b>Result:</b> {outcome}\n"
                f"<b>P/L:</b> ${profit:.2f}\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"<b>Total:</b> {total_trades}\n"
                f"<b>Wins:</b> {wins}\n"
                f"<b>Losses:</b> {losses}\n"
                f"<b>Draws:</b> {draws}\n"
                f"<b>Win rate:</b> {win_rate:.2f}%\n"
                f"<b>Net demo P/L:</b> "
                f"${net_profit:.2f}"
            )

            completed.append(
                trade_id
            )

        except Exception:
            continue

    for trade_id in completed:
        active_trades.pop(
            trade_id,
            None
        )


# ============================================================
# HEARTBEAT
# ============================================================

def send_status():

    global last_status_time

    now = time.time()

    if (
        now - last_status_time
        < STATUS_INTERVAL
    ):
        return

    last_status_time = now

    runtime_seconds = (
        now - bot_started_at
    )

    runtime_hours = (
        runtime_seconds / 3600
    )

    win_rate = (
        (wins / total_trades) * 100
        if total_trades > 0
        else 0
    )

    telegram(
        "🟡 <b>ZETA V1 STATUS</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>OTC assets:</b> {len(otc_assets)}\n"
        f"<b>Trades:</b> {total_trades}\n"
        f"<b>Wins:</b> {wins}\n"
        f"<b>Losses:</b> {losses}\n"
        f"<b>Draws:</b> {draws}\n"
        f"<b>Win rate:</b> {win_rate:.2f}%\n"
        f"<b>Net demo P/L:</b> ${net_profit:.2f}\n"
        f"<b>Active trades:</b> {len(active_trades)}\n"
        f"<b>Runtime:</b> {runtime_hours:.2f}h\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Account:</b> PRACTICE\n"
        f"<b>Expiry:</b> {EXPIRY_MINUTES} minutes\n"
        "<b>Scanner:</b> RUNNING"
    )


# ============================================================
# MAIN SCANNER LOOP
# ============================================================

def scanner_loop():

    global last_otc_refresh

    telegram(
        "🟢 <b>ZETA V1 SCANNER STARTED</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Strategy: EMA 9/21 Trend Flip\n"
        "Chart: 1M\n"
        "Expiry: 2 minutes\n"
        "Mode: PRACTICE\n"
        "Auto-trading: ON\n"
        "Scanning OTC markets..."
    )

    last_otc_refresh = 0

    while True:

        try:

            # --------------------------------------------
            # Connection
            # --------------------------------------------

            if not connection_alive():

                telegram(
                    "🟠 <b>ZETA V1 CONNECTION LOST</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"Retrying in {RECONNECT_INTERVAL} seconds."
                )

                time.sleep(
                    RECONNECT_INTERVAL
                )

                continue

            # --------------------------------------------
            # OTC refresh
            # --------------------------------------------

            now = time.time()

            if (
                not otc_assets
                or now - last_otc_refresh
                >= OTC_REFRESH_INTERVAL
            ):

                assets = discover_otc_assets()

                last_otc_refresh = now

                if not assets:

                    telegram(
                        "🟠 <b>NO OTC MARKETS FOUND</b>\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        "Will retry OTC discovery."
                    )

                    time.sleep(
                        SCAN_INTERVAL
                    )

                    continue

            # --------------------------------------------
            # Check trade results
            # --------------------------------------------

            check_trade_results()

            # --------------------------------------------
            # Scan assets
            # --------------------------------------------

            for asset in list(otc_assets):

                if not connection_alive():
                    break

                candles = get_candles(
                    asset
                )

                if not candles:
                    continue

                signal = analyze_asset(
                    asset,
                    candles
                )

                if signal is None:
                    continue

                telegram(
                    signal_message(
                        signal
                    )
                )

                # Automatically places a DEMO/PRACTICE
                # order only.
                place_demo_trade(
                    signal
                )

                # Give API a moment before next order
                time.sleep(0.5)

            # --------------------------------------------
            # Heartbeat
            # --------------------------------------------

            send_status()

            time.sleep(
                SCAN_INTERVAL
            )

        except KeyboardInterrupt:

            telegram(
                "🛑 <b>ZETA V1 STOPPED</b>"
            )

            break

        except Exception as e:

            print(
                traceback.format_exc()
            )

            telegram(
                "🟠 <b>ZETA V1 LOOP ERROR</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"{str(e)}\n"
                "Scanner will continue."
            )

            time.sleep(
                SCAN_INTERVAL
            )


# ============================================================
# STARTUP
# ============================================================

def main():

    telegram(
        "🟡 <b>ZETA V1 STARTING</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "IQ Option OTC scanner initializing...\n"
        "Account: PRACTICE\n"
        "Expiry: 2 minutes"
    )

    connected = connect_iq()

    if not connected:

        telegram(
            "🔴 <b>ZETA V1 STARTUP HALTED</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "IQ Option connection was not established.\n"
            "The bot will retry without starting the strategy."
        )

        while True:

            time.sleep(
                RECONNECT_INTERVAL
            )

            if connect_iq():
                break

        scanner_loop()

    else:

        scanner_loop()


if __name__ == "__main__":
    main()
