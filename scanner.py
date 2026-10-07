import os
import csv
import time
import threading
from datetime import datetime, timezone

import requests
import iqoptionapi.constants as OP_code
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# MOMENTUM 10 EXTREME-REVERSAL
# EURUSD + EURUSD-OTC
# IQ OPTION - PRACTICE MODE
# ============================================================

MOMENTUM_PERIOD = 10
MOMENTUM_LOOKBACK = 50
EXTREME_PERCENTILE = 0.10

REQUIRE_TURN = True
MIN_TURN_DISTANCE = 0.20

CANDLE_SECONDS = 60
EXPIRY_MINUTES = 1

AUTO_TRADE = True
BALANCE_MODE = "PRACTICE"
STAKE = 1.0

TARGET_TRADES = 50

SCAN_INTERVAL = 2
ASSET_REFRESH_SECONDS = 900
HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 15

# ONLY THESE TWO ASSETS
TARGET_ASSETS = [
    "EURUSD-OTC",
    "EURUSD",
]

LOG_FILE = "momentum_signal_log.csv"
CANDLE_REQUEST_TIMEOUT = 8


# ============================================================
# SECRETS
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ============================================================
# GLOBAL STATE
# ============================================================

api = None

trading_assets = []
active_ids = {}

trade_count = 0
wins = 0
losses = 0
pending_results = 0

last_signal_candle = {}
extreme_state = {}

stop_event = threading.Event()


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )

    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }

    try:
        requests.post(url, data=data, timeout=15)
    except Exception as exc:
        print("Telegram error:", exc, flush=True)


# ============================================================
# CSV LOG
# ============================================================

def init_log():
    if os.path.exists(LOG_FILE):
        return

    try:
        with open(
            LOG_FILE,
            "w",
            newline="",
            encoding="utf-8",
        ) as file:

            writer = csv.writer(file)

            writer.writerow([
                "time",
                "asset",
                "direction",
                "signal_id",
                "entry_price",
                "result",
                "profit",
            ])

    except Exception as exc:
        print(
            "Log init error:",
            exc,
            flush=True,
        )


def log_trade(
    asset,
    direction,
    signal_id,
    entry_price,
    result,
    profit,
):
    try:
        with open(
            LOG_FILE,
            "a",
            newline="",
            encoding="utf-8",
        ) as file:

            writer = csv.writer(file)

            writer.writerow([
                datetime.now(timezone.utc).isoformat(),
                asset,
                direction,
                signal_id,
                entry_price,
                result,
                profit,
            ])

    except Exception as exc:
        print(
            "Log error:",
            exc,
            flush=True,
        )


# ============================================================
# IQ OPTION CONNECTION
# ============================================================

def connect_iq():
    global api

    print(
        "Connecting to IQ Option...",
        flush=True,
    )

    try:
        api = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD,
        )

        connected, reason = api.connect()

        if not connected:

            print(
                "IQ OPTION CONNECTION FAILED:",
                reason,
                flush=True,
            )

            api = None
            return False

        print(
            "🟢 IQ OPTION CONNECTED",
            flush=True,
        )

        try:
            api.change_balance(
                BALANCE_MODE
            )
        except Exception as exc:

            print(
                "Balance mode warning:",
                exc,
                flush=True,
            )

        print(
            "Practice mode active.",
            flush=True,
        )

        return True

    except Exception as exc:

        print(
            "Connection error:",
            exc,
            flush=True,
        )

        api = None
        return False


# ============================================================
# EURUSD / EURUSD-OTC ASSET DISCOVERY
# ============================================================

def get_trading_assets():
    global trading_assets
    global active_ids

    print(
        "Preparing EURUSD assets...",
        flush=True,
    )

    try:

        found_assets = {}

        # ----------------------------------------------------
        # NORMALIZE ASSET NAME
        # ----------------------------------------------------

        def normalize_name(name):

            if name is None:
                return ""

            name = str(name).strip()

            if "." in name:
                parts = name.split(".")

                if parts[-1]:
                    name = parts[-1]

            return name.upper()

        # ----------------------------------------------------
        # CHECK ONE ACTIVE RECORD
        # ----------------------------------------------------

        def check_active(info, fallback_id=None):

            if not isinstance(info, dict):
                return

            name = info.get("name")

            if not name:
                name = info.get("symbol")

            if not name:
                name = info.get("instrument")

            normalized_name = normalize_name(name)

            if normalized_name not in TARGET_ASSETS:
                return

            active_id = info.get("active_id")

            if active_id is None:
                active_id = info.get("id")

            if active_id is None:
                active_id = fallback_id

            if active_id is None:
                return

            try:
                active_id = int(active_id)
            except Exception:
                return

            enabled = info.get("enabled")

            suspended = info.get("is_suspended")

            if enabled is False:
                return

            if suspended is True:
                return

            # Preserve the exact target name used by the bot.
            if normalized_name == "EURUSD-OTC":
                found_assets["EURUSD-OTC"] = active_id

            elif normalized_name == "EURUSD":
                found_assets["EURUSD"] = active_id

        # ----------------------------------------------------
        # RECURSIVE SEARCH
        #
        # IQ Option can return the asset information in
        # different nested structures. Search the entire
        # initialization response instead of assuming one
        # fixed location.
        # ----------------------------------------------------

        def search_structure(data):

            if isinstance(data, dict):

                # First check this dictionary itself.
                check_active(data)

                # Then inspect all nested values.
                for key, value in data.items():

                    # If the dictionary key itself is numeric,
                    # it may be the active ID.
                    fallback_id = None

                    try:
                        fallback_id = int(key)
                    except Exception:
                        pass

                    if isinstance(value, dict):
                        check_active(
                            value,
                            fallback_id,
                        )

                    search_structure(value)

            elif isinstance(data, list):

                for item in data:
                    search_structure(item)

        # ----------------------------------------------------
        # PRIMARY SOURCE
        # ----------------------------------------------------

        init_data = None

        try:

            init_data = api.get_all_init_v2()

            if isinstance(init_data, dict):

                search_structure(init_data)

        except Exception as exc:

            print(
                "get_all_init_v2 warning: "
                + str(exc),
                flush=True,
            )

        # ----------------------------------------------------
        # FALLBACK SOURCE
        # ----------------------------------------------------

        if not found_assets:

            print(
                "Primary asset discovery returned no target assets.",
                flush=True,
            )

            try:

                legacy_data = api.get_all_init()

                if isinstance(legacy_data, dict):

                    search_structure(
                        legacy_data
                    )

            except Exception as exc:

                print(
                    "get_all_init warning: "
                    + str(exc),
                    flush=True,
                )

        # ----------------------------------------------------
        # FINAL CHECK
        # ----------------------------------------------------

        if not found_assets:

            print(
                "❌ EURUSD / EURUSD-OTC are not currently open.",
                flush=True,
            )

            return False

        # ----------------------------------------------------
        # ORDER ASSETS CONSISTENTLY
        # ----------------------------------------------------

        new_assets = []

        for target in TARGET_ASSETS:

            if target in found_assets:
                new_assets.append(target)

        # ----------------------------------------------------
        # SAVE
        # ----------------------------------------------------

        trading_assets = new_assets

        active_ids = {
            asset: found_assets[asset]
            for asset in new_assets
        }

        # ----------------------------------------------------
        # REGISTER IQ ACTIVE IDs
        # ----------------------------------------------------

        for asset_name, active_id in active_ids.items():

            OP_code.ACTIVES[
                asset_name
            ] = active_id

        print(
            "",
            flush=True,
        )

        print(
            "🔎 TARGET ASSETS READY",
            flush=True,
        )

        print(
            "Allowed assets:",
            flush=True,
        )

        print(
            "EURUSD",
            flush=True,
        )

        print(
            "EURUSD-OTC",
            flush=True,
        )

        print(
            "Currently available: "
            + str(len(trading_assets)),
            flush=True,
        )

        for asset_name in trading_assets:

            print(
                "✅ "
                + asset_name
                + " | Active ID: "
                + str(active_ids[asset_name]),
                flush=True,
            )

        print(
            "1M scanner is now active.",
            flush=True,
        )

        return True

    except Exception as exc:

        print(
            "Asset discovery error:",
            exc,
            flush=True,
        )

        return False


# ============================================================
# CANDLE DATA
# ============================================================

def get_candles(asset, count):

    active_id = active_ids.get(asset)

    if active_id is None:

        print(
            "No active ID for "
            + asset,
            flush=True,
        )

        return []

    try:

        api.api.candles.candles_data = []

        server_time = time.time()

        try:
            server_time = (
                api.api.timesync.server_timestamp
            )
        except Exception:
            pass

        api.api.getcandles(
            active_id,
            CANDLE_SECONDS,
            count,
            server_time,
        )

        started = time.time()

        while (
            time.time() - started
            < CANDLE_REQUEST_TIMEOUT
        ):

            candles = (
                api.api.candles.candles_data
            )

            if candles:
                break

            time.sleep(0.1)

        candles = (
            api.api.candles.candles_data
        )

        if not candles:
            return []

        result = []

        for candle in candles:

            if not isinstance(candle, dict):
                continue

            try:

                result.append({
                    "from": float(
                        candle.get("from")
                    ),
                    "open": float(
                        candle.get("open")
                    ),
                    "close": float(
                        candle.get("close")
                    ),
                    "high": float(
                        candle.get("max")
                    ),
                    "low": float(
                        candle.get("min")
                    ),
                })

            except Exception:
                continue

        return result

    except Exception as exc:

        print(
            "Candle error "
            + asset
            + ": "
            + str(exc),
            flush=True,
        )

        return []


def remove_open_candle(candles):

    if len(candles) < 2:
        return candles

    now = time.time()

    last = candles[-1]

    candle_from = last.get(
        "from",
        0,
    )

    if now - candle_from < CANDLE_SECONDS:
        return candles[:-1]

    return candles


# ============================================================
# MOMENTUM 10
# ============================================================

def calculate_momentum(candles):

    values = []

    if len(candles) <= MOMENTUM_PERIOD:
        return values

    for index in range(
        MOMENTUM_PERIOD,
        len(candles),
    ):

        current = candles[index]["close"]

        previous = candles[
            index - MOMENTUM_PERIOD
        ]["close"]

        if previous == 0:
            continue

        momentum = (
            current / previous
        ) * 100.0

        values.append(momentum)

    return values


def analyze_momentum(candles):

    if len(candles) < (
        MOMENTUM_LOOKBACK + 3
    ):
        return None

    momentum_values = calculate_momentum(
        candles
    )

    if len(momentum_values) < (
        MOMENTUM_LOOKBACK + 3
    ):
        return None

    recent = momentum_values[
        -MOMENTUM_LOOKBACK:
    ]

    current = momentum_values[-1]
    previous = momentum_values[-2]
    previous_previous = momentum_values[-3]

    sorted_values = sorted(recent)

    low_index = int(
        len(sorted_values)
        * EXTREME_PERCENTILE
    )

    high_index = int(
        len(sorted_values)
        * (1.0 - EXTREME_PERCENTILE)
    )

    if low_index >= len(sorted_values):
        low_index = (
            len(sorted_values) - 1
        )

    if high_index >= len(sorted_values):
        high_index = (
            len(sorted_values) - 1
        )

    low_threshold = sorted_values[
        low_index
    ]

    high_threshold = sorted_values[
        high_index
    ]

    recent_low = min(recent)
    recent_high = max(recent)

    signal = None
    extreme = None
    reversal_strength = 0.0

    # --------------------------------------------------------
    # LOW EXTREME -> CALL
    # --------------------------------------------------------

    if current <= low_threshold:

        extreme = "LOW"

        if (
            current > previous
            and previous < previous_previous
        ):

            turn_distance = (
                current - previous
            )

            if (
                turn_distance
                >= MIN_TURN_DISTANCE
            ):

                signal = "CALL"

                if recent_low != 0:

                    reversal_strength = (
                        turn_distance
                        / abs(recent_low)
                    ) * 100.0

    # --------------------------------------------------------
    # HIGH EXTREME -> PUT
    # --------------------------------------------------------

    elif current >= high_threshold:

        extreme = "HIGH"

        if (
            current < previous
            and previous > previous_previous
        ):

            turn_distance = (
                previous - current
            )

            if (
                turn_distance
                >= MIN_TURN_DISTANCE
            ):

                signal = "PUT"

                if recent_high != 0:

                    reversal_strength = (
                        turn_distance
                        / abs(recent_high)
                    ) * 100.0

    return {
        "signal": signal,
        "extreme": extreme,
        "momentum": current,
        "previous": previous,
        "previous_previous": previous_previous,
        "recent_low": recent_low,
        "recent_high": recent_high,
        "low_threshold": low_threshold,
        "high_threshold": high_threshold,
        "reversal_strength": reversal_strength,
    }


# ============================================================
# PRICE ACTION REVERSAL CONFIRMATION
# ============================================================

def price_action_confirmation(
    candles,
    direction,
):

    if len(candles) < 2:
        return False, "NOT_ENOUGH_CANDLES"

    current = candles[-1]
    previous = candles[-2]

    current_open = current["open"]
    current_close = current["close"]
    current_high = current["high"]
    current_low = current["low"]

    previous_open = previous["open"]
    previous_close = previous["close"]

    body = abs(
        current_close
        - current_open
    )

    upper_wick = (
        current_high
        - max(
            current_open,
            current_close,
        )
    )

    lower_wick = (
        min(
            current_open,
            current_close,
        )
        - current_low
    )

    candle_range = (
        current_high
        - current_low
    )

    if candle_range <= 0:
        return False, "ZERO_RANGE"

    # --------------------------------------------------------
    # CALL
    # --------------------------------------------------------

    if direction == "CALL":

        bullish_candle = (
            current_close
            > current_open
        )

        bullish_rejection = (
            bullish_candle
            and lower_wick >= body
            and lower_wick
            >= candle_range * 0.25
        )

        bullish_engulfing = (
            bullish_candle
            and previous_close
            < previous_open
            and current_open
            <= previous_close
            and current_close
            >= previous_open
        )

        if bullish_rejection:
            return True, "BULLISH_REJECTION"

        if bullish_engulfing:
            return True, "BULLISH_ENGULFING"

        return (
            False,
            "NO_BULLISH_CONFIRMATION",
        )

    # --------------------------------------------------------
    # PUT
    # --------------------------------------------------------

    if direction == "PUT":

        bearish_candle = (
            current_close
            < current_open
        )

        bearish_rejection = (
            bearish_candle
            and upper_wick >= body
            and upper_wick
            >= candle_range * 0.25
        )

        bearish_engulfing = (
            bearish_candle
            and previous_close
            > previous_open
            and current_open
            >= previous_close
            and current_close
            <= previous_open
        )

        if bearish_rejection:
            return True, "BEARISH_REJECTION"

        if bearish_engulfing:
            return True, "BEARISH_ENGULFING"

        return (
            False,
            "NO_BEARISH_CONFIRMATION",
        )

    return False, "INVALID_DIRECTION"


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def build_signal_message(
    asset,
    direction,
    analysis,
    price,
    signal_id,
    confirmation,
):

    return (
        "🔔 <b>MOMENTUM 10 SIGNAL</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Asset: "
        + asset
        + "\n"
        "Direction: "
        + direction
        + "\n"
        "Timeframe: 1M\n"
        "Expiry: 1 minute\n"
        "Strategy: Momentum 10 Extreme-Reversal\n"
        "Extreme: "
        + str(analysis["extreme"])
        + "\n"
        "Price Confirmation: "
        + confirmation
        + "\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Momentum: "
        + str(
            round(
                analysis["momentum"],
                5,
            )
        )
        + "\n"
        "Previous: "
        + str(
            round(
                analysis["previous"],
                5,
            )
        )
        + "\n"
        "Previous 2: "
        + str(
            round(
                analysis[
                    "previous_previous"
                ],
                5,
            )
        )
        + "\n"
        "Recent Low: "
        + str(
            round(
                analysis["recent_low"],
                5,
            )
        )
        + "\n"
        "Recent High: "
        + str(
            round(
                analysis["recent_high"],
                5,
            )
        )
        + "\n"
        "Low Threshold: "
        + str(
            round(
                analysis[
                    "low_threshold"
                ],
                5,
            )
        )
        + "\n"
        "High Threshold: "
        + str(
            round(
                analysis[
                    "high_threshold"
                ],
                5,
            )
        )
        + "\n"
        "Reversal Strength: "
        + str(
            round(
                analysis[
                    "reversal_strength"
                ],
                1,
            )
        )
        + "%\n"
        "Price: "
        + str(price)
        + "\n"
        "Signal ID: "
        + signal_id
    )


# ============================================================
# TRADE RESULT MONITOR
# ============================================================

def monitor_trade(
    order_id,
    asset,
    direction,
    signal_id,
    entry_price,
):

    global pending_results
    global wins
    global losses

    try:

        time.sleep(
            (EXPIRY_MINUTES * 60)
            + 5
        )

        result = None

        for _ in range(20):

            try:

                result = (
                    api.check_win_v4(
                        order_id
                    )
                )

            except Exception:

                result = None

            if result is not None:
                break

            time.sleep(1)

        if result is None:

            print(
                "Could not get result for "
                + asset,
                flush=True,
            )

            return

        try:
            profit = float(result)
        except Exception:
            profit = 0.0

        if profit > 0:

            wins += 1
            result_text = "WIN"

        else:

            losses += 1
            result_text = "LOSS"

        log_trade(
            asset,
            direction,
            signal_id,
            entry_price,
            result_text,
            profit,
        )

        print(
            "📊 RESULT "
            + asset
            + " "
            + result_text
            + " "
            + str(profit),
            flush=True,
        )

        send_telegram(
            "📊 <b>MOMENTUM 10 RESULT</b>\n"
            "Asset: "
            + asset
            + "\n"
            "Direction: "
            + direction
            + "\n"
            "Result: "
            + result_text
            + "\n"
            "Profit: "
            + str(profit)
            + "\n"
            "Signal ID: "
            + signal_id
        )

    except Exception as exc:

        print(
            "Result monitor error: "
            + str(exc),
            flush=True,
        )

    finally:

        pending_results -= 1


# ============================================================
# EXECUTE TRADE
# ============================================================

def execute_trade(
    asset,
    direction,
    signal_id,
    entry_price,
):

    global trade_count
    global pending_results

    try:

        action = direction.lower()

        print(
            "Attempting trade: "
            + asset
            + " "
            + action,
            flush=True,
        )

        order_result = api.buy(
            STAKE,
            asset,
            action,
            EXPIRY_MINUTES,
        )

        if isinstance(
            order_result,
            tuple,
        ):

            success = order_result[0]
            order_id = order_result[1]

        else:

            success = bool(
                order_result
            )

            order_id = order_result

        if not success:

            print(
                "❌ TRADE FAILED "
                + asset
                + " "
                + direction,
                flush=True,
            )

            return False

        trade_count += 1
        pending_results += 1

        print(
            "✅ TRADE OPENED "
            + asset
            + " "
            + direction
            + " Order: "
            + str(order_id),
            flush=True,
        )

        send_telegram(
            "✅ <b>TRADE OPENED</b>\n"
            "Asset: "
            + asset
            + "\n"
            "Direction: "
            + direction
            + "\n"
            "Stake: $"
            + str(STAKE)
            + "\n"
            "Expiry: 1 minute\n"
            "Signal ID: "
            + signal_id
        )

        thread = threading.Thread(
            target=monitor_trade,
            args=(
                order_id,
                asset,
                direction,
                signal_id,
                entry_price,
            ),
            daemon=True,
        )

        thread.start()

        return True

    except Exception as exc:

        print(
            "",
            flush=True,
        )

        print(
            "❌ TRADE EXCEPTION",
            flush=True,
        )

        print(
            "Asset: "
            + asset,
            flush=True,
        )

        print(
            "Direction: "
            + direction,
            flush=True,
        )

        print(
            "Error: "
            + str(exc),
            flush=True,
        )

        return False


# ============================================================
# PROCESS ASSET
# ============================================================

def process_asset(asset):

    try:

        print(
            "Scanning: "
            + asset,
            flush=True,
        )

        candles = get_candles(
            asset,
            MOMENTUM_LOOKBACK
            + MOMENTUM_PERIOD
            + 10,
        )

        if not candles:
            return

        candles = remove_open_candle(
            candles
        )

        if len(candles) < (
            MOMENTUM_LOOKBACK + 3
        ):
            return

        analysis = analyze_momentum(
            candles
        )

        if not analysis:
            return

        signal = analysis["signal"]

        if signal is None:
            return

        # ----------------------------------------------------
        # PRICE ACTION CONFIRMATION
        # ----------------------------------------------------

        confirmed, confirmation = (
            price_action_confirmation(
                candles,
                signal,
            )
        )

        if not confirmed:
            return

        # ----------------------------------------------------
        # DUPLICATE SIGNAL PROTECTION
        # ----------------------------------------------------

        current_candle = candles[-1]

        candle_id = current_candle[
            "from"
        ]

        if (
            last_signal_candle.get(
                asset
            )
            == candle_id
        ):
            return

        extreme = analysis["extreme"]

        if (
            extreme_state.get(asset)
            == extreme
        ):
            return

        last_signal_candle[
            asset
        ] = candle_id

        extreme_state[
            asset
        ] = extreme

        price = current_candle[
            "close"
        ]

        signal_id = (
            "M10-"
            + asset.replace(
                "-",
                "",
            )
            + "-"
            + signal
            + "-"
            + str(
                int(
                    time.time()
                )
            )
        )

        message = build_signal_message(
            asset,
            signal,
            analysis,
            price,
            signal_id,
            confirmation,
        )

        print(
            "",
            flush=True,
        )

        print(
            "🔔 MOMENTUM 10 SIGNAL",
            flush=True,
        )

        print(
            "━━━━━━━━━━━━━━━━━━",
            flush=True,
        )

        print(
            "Asset: "
            + asset,
            flush=True,
        )

        print(
            "Direction: "
            + signal,
            flush=True,
        )

        print(
            "Timeframe: 1M",
            flush=True,
        )

        print(
            "Expiry: 1 minute",
            flush=True,
        )

        print(
            "Strategy: Momentum 10 Extreme-Reversal",
            flush=True,
        )

        print(
            "Extreme: "
            + str(extreme),
            flush=True,
        )

        print(
            "Price Confirmation: "
            + confirmation,
            flush=True,
        )

        print(
            "━━━━━━━━━━━━━━━━━━",
            flush=True,
        )

        print(
            "Momentum: "
            + str(
                round(
                    analysis["momentum"],
                    5,
                )
            ),
            flush=True,
        )

        print(
            "Previous: "
            + str(
                round(
                    analysis["previous"],
                    5,
                )
            ),
            flush=True,
        )

        print(
            "Previous 2: "
            + str(
                round(
                    analysis[
                        "previous_previous"
                    ],
                    5,
                )
            ),
            flush=True,
        )

        print(
            "Recent Low: "
            + str(
                round(
                    analysis["recent_low"],
                    5,
                )
            ),
            flush=True,
        )

        print(
            "Recent High: "
            + str(
                round(
                    analysis["recent_high"],
                    5,
                )
            ),
            flush=True,
        )

        print(
            "Low Threshold: "
            + str(
                round(
                    analysis[
                        "low_threshold"
                    ],
                    5,
                )
            ),
            flush=True,
        )

        print(
            "High Threshold: "
            + str(
                round(
                    analysis[
                        "high_threshold"
                    ],
                    5,
                )
            ),
            flush=True,
        )

        print(
            "Reversal Strength: "
            + str(
                round(
                    analysis[
                        "reversal_strength"
                    ],
                    1,
                )
            )
            + "%",
            flush=True,
        )

        print(
            "Price: "
            + str(price),
            flush=True,
        )

        print(
            "Signal ID: "
            + signal_id,
            flush=True,
        )

        send_telegram(message)

        if AUTO_TRADE:

            execute_trade(
                asset,
                signal,
                signal_id,
                price,
            )

    except Exception as exc:

        print(
            "Process asset error "
            + asset
            + ": "
            + str(exc),
            flush=True,
        )


# ============================================================
# HEARTBEAT
# ============================================================

def heartbeat():

    win_rate = 0.0

    if trade_count > 0:

        win_rate = (
            wins
            / trade_count
        ) * 100.0

    print(
        "",
        flush=True,
    )

    print(
        "💚 MOMENTUM 10 BOT ALIVE",
        flush=True,
    )

    print(
        "━━━━━━━━━━━━━━━━━━",
        flush=True,
    )

    print(
        "Connection: OK",
        flush=True,
    )

    print(
        "Balance: "
        + BALANCE_MODE,
        flush=True,
    )

    print(
        "Auto Trading: "
        + str(AUTO_TRADE),
        flush=True,
    )

    print(
        "Strategy: Momentum 10",
        flush=True,
    )

    print(
        "Assets: EURUSD + EURUSD-OTC",
        flush=True,
    )

    print(
        "Timeframe: 1M",
        flush=True,
    )

    print(
        "Expiry: 1 minute",
        flush=True,
    )

    print(
        "Available target assets: "
        + str(
            len(trading_assets)
        ),
        flush=True,
    )

    print(
        "Trades: "
        + str(trade_count)
        + "/"
        + str(TARGET_TRADES),
        flush=True,
    )

    print(
        "Pending Results: "
        + str(pending_results),
        flush=True,
    )

    print(
        "Wins: "
        + str(wins),
        flush=True,
    )

    print(
        "Losses: "
        + str(losses),
        flush=True,
    )

    print(
        "Win Rate: "
        + str(
            round(
                win_rate,
                1,
            )
        )
        + "%",
        flush=True,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    global api

    init_log()

    print(
        "🚀 MOMENTUM 10 BOT STARTING",
        flush=True,
    )

    print(
        "━━━━━━━━━━━━━━━━━━",
        flush=True,
    )

    print(
        "Mode: "
        + BALANCE_MODE,
        flush=True,
    )

    print(
        "Auto Trading: "
        + str(AUTO_TRADE),
        flush=True,
    )

    print(
        "Strategy: Momentum 10 Extreme-Reversal",
        flush=True,
    )

    print(
        "Assets: EURUSD + EURUSD-OTC",
        flush=True,
    )

    print(
        "Timeframe: 1M",
        flush=True,
    )

    print(
        "Expiry: 1 minute",
        flush=True,
    )

    print(
        "Stake: $"
        + str(STAKE),
        flush=True,
    )

    print(
        "Minimum Turn Distance: "
        + str(MIN_TURN_DISTANCE),
        flush=True,
    )

    print(
        "Target: "
        + str(TARGET_TRADES),
        flush=True,
    )

    print(
        "Continuous scanning: ON",
        flush=True,
    )

    print(
        "",
        flush=True,
    )

    last_asset_refresh = 0
    last_heartbeat = 0

    while not stop_event.is_set():

        # ----------------------------------------------------
        # CONNECTION
        # ----------------------------------------------------

        if api is None:

            if not connect_iq():

                time.sleep(
                    RECONNECT_SECONDS
                )

                continue

        try:

            if not api.check_connect():

                print(
                    "IQ Option disconnected.",
                    flush=True,
                )

                api = None

                time.sleep(
                    RECONNECT_SECONDS
                )

                continue

        except Exception:

            api = None

            time.sleep(
                RECONNECT_SECONDS
            )

            continue

        # ----------------------------------------------------
        # ASSET DISCOVERY
        # ----------------------------------------------------

        if not trading_assets:

            if not get_trading_assets():

                print(
                    "EURUSD assets unavailable. Retrying...",
                    flush=True,
                )

                time.sleep(
                    RECONNECT_SECONDS
                )

                continue

            last_asset_refresh = (
                time.time()
            )

            last_heartbeat = (
                time.time()
            )

        now = time.time()

        # ----------------------------------------------------
        # REFRESH ASSET STATUS
        # ----------------------------------------------------

        if (
            now - last_asset_refresh
            >= ASSET_REFRESH_SECONDS
        ):

            if get_trading_assets():

                last_asset_refresh = now

        # ----------------------------------------------------
        # HEARTBEAT
        # ----------------------------------------------------

        if (
            now - last_heartbeat
            >= HEARTBEAT_SECONDS
        ):

            heartbeat()

            last_heartbeat = now

        # ----------------------------------------------------
        # 50-TRADE BLOCK
        # ----------------------------------------------------

        if trade_count >= TARGET_TRADES:

            print(
                "🎯 TARGET TRADES REACHED.",
                flush=True,
            )

            send_telegram(
                "🎯 <b>MOMENTUM 10 "
                "50-TRADE BLOCK COMPLETE</b>\n"
                "Assets: EURUSD + EURUSD-OTC\n"
                "Trades: "
                + str(trade_count)
                + "\n"
                "Wins: "
                + str(wins)
                + "\n"
                "Losses: "
                + str(losses)
                + "\n"
                "Win Rate: "
                + str(
                    round(
                        (
                            wins
                            / max(
                                trade_count,
                                1,
                            )
                        )
                        * 100.0,
                        1,
                    )
                )
                + "%"
            )

            break

        # ----------------------------------------------------
        # SCAN ONLY TARGET ASSETS
        # ----------------------------------------------------

        for asset in list(
            trading_assets
        ):

            if stop_event.is_set():
                break

            if trade_count >= TARGET_TRADES:
                break

            process_asset(asset)

            time.sleep(
                SCAN_INTERVAL
            )

        time.sleep(
            SCAN_INTERVAL
        )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        print(
            "Bot stopped.",
            flush=True,
        )

    except Exception as exc:

        print(
            "FATAL ERROR: "
            + str(exc),
            flush=True,
                ) )
