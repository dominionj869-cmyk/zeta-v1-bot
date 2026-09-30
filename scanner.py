import os
import time
import math
import traceback
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code


# ============================================================
# ZETA V1 — IQ OPTION OTC DEMO TRADER
# ============================================================

BALANCE_MODE = "PRACTICE"
STAKE = 1.0

EXPIRY_MINUTES = 2
TIMEFRAME = 60
CANDLE_COUNT = 180

MAX_OTC_ASSETS = 60

SCAN_INTERVAL = 5
STATUS_INTERVAL = 300
RECONNECT_INTERVAL = 30

MAX_ACTIVE_TRADES = 1000

# ============================================================
# ZETA V1 SETTINGS
# ============================================================

FAST_LENGTH = 9
SLOW_LENGTH = 21
ATR_PERIOD = 14

NORMALIZED_DISTANCE_MIN = 0.05

USE_CANDLE_CONFIRM = True
USE_TREND_SLOPE = True

SIGNAL_COOLDOWN_BARS = 3

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

otc_assets = []

last_signal_time = {}

active_trades = {}

total_trades = 0
wins = 0
losses = 0
draws = 0
total_profit = 0.0

start_time = time.time()

last_status_time = 0
last_discovery_time = 0
last_connection_check = 0


# ============================================================
# HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def safe_float(value, default=0.0):
    try:
        value = float(value)

        if math.isfinite(value):
            return value

    except Exception:
        pass

    return default


def server_now():
    try:
        if api is not None:
            value = api.get_server_timestamp()

            if value:
                return int(value)

    except Exception:
        pass

    return int(time.time())


def runtime_string():
    seconds = int(time.time() - start_time)

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60

    return f"{hours}h {minutes}m"


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(text):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("\n[TELEGRAM DISABLED]")
        print(text)
        return False

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=15,
        )

        if response.ok:
            return True

        print(
            "[TELEGRAM ERROR]",
            response.status_code,
            response.text[:500],
        )

    except Exception as e:

        print(
            "[TELEGRAM EXCEPTION]",
            repr(e),
        )

    return False


# ============================================================
# OTC NAME DETECTION
# ============================================================

def is_otc_name(name):

    if not isinstance(name, str):
        return False

    upper = name.upper().strip()

    return (
        upper.endswith("-OTC")
        or upper.endswith("_OTC")
        or upper.endswith(" OTC")
        or "-OTC." in upper
        or "_OTC." in upper
        or " OTC." in upper
    )


def clean_active_name(raw_name):

    if raw_name is None:
        return None

    name = str(raw_name).strip()

    # IQ Option can return names such as:
    # "1.EURUSD-OTC"
    # "76.EURUSD-OTC"

    if "." in name:
        parts = name.split(".")

        if len(parts) >= 2:
            name = parts[-1]

    return name.strip()


# ============================================================
# ACTIVE ID REGISTRATION
# ============================================================

def register_active_id(name, active_id):

    if not name:
        return False

    try:
        active_id = int(active_id)

    except Exception:
        return False

    try:
        OP_code.ACTIVES[name] = active_id

        return True

    except Exception as e:

        print(
            "[ACTIVE MAP ERROR]",
            name,
            active_id,
            repr(e),
        )

        return False


# ============================================================
# RAW INITIALIZATION
# ============================================================

def get_raw_initialization():

    if api is None:
        return None

    print("\n" + "=" * 70)
    print("IQ OPTION MARKET INITIALIZATION")
    print("=" * 70)

    # --------------------------------------------------------
    # METHOD 1
    # --------------------------------------------------------

    try:

        print(
            "[RAW] Requesting get_all_init_v2()..."
        )

        data = api.get_all_init_v2()

        if data:

            print(
                "[RAW] get_all_init_v2 received."
            )

            print(
                "[RAW] Type:",
                type(data).__name__,
            )

            return data

    except Exception as e:

        print(
            "[RAW V2 ERROR]",
            repr(e),
        )

    # --------------------------------------------------------
    # METHOD 2
    # --------------------------------------------------------

    try:

        print(
            "[RAW] Trying get_all_init()..."
        )

        data = api.get_all_init()

        if data:

            print(
                "[RAW] get_all_init received."
            )

            print(
                "[RAW] Type:",
                type(data).__name__,
            )

            return data

    except Exception as e:

        print(
            "[RAW LEGACY ERROR]",
            repr(e),
        )

    print(
        "[RAW] No initialization data."
    )

    return None


# ============================================================
# RECURSIVE OTC DISCOVERY
# ============================================================

def discover_otc_from_initialization(data):

    print("\n" + "=" * 70)
    print("ROBUST OTC DISCOVERY")
    print("=" * 70)

    if not isinstance(data, dict):

        print(
            "[DISCOVERY] Unexpected root type:",
            type(data).__name__,
        )

        return []

    found = []
    seen = set()

    # --------------------------------------------------------
    # RECURSIVE SEARCH
    # --------------------------------------------------------

    def walk(node, market_type="unknown"):

        if len(found) >= MAX_OTC_ASSETS:
            return

        if isinstance(node, dict):

            # ----------------------------------------------
            # Detect active dictionaries
            # ----------------------------------------------

            if "actives" in node:

                actives = node.get("actives")

                if isinstance(actives, dict):

                    for active_id, active in actives.items():

                        if len(found) >= MAX_OTC_ASSETS:
                            return

                        if not isinstance(active, dict):
                            continue

                        raw_name = active.get("name")

                        name = clean_active_name(
                            raw_name
                        )

                        if not is_otc_name(name):
                            continue

                        # ----------------------------------
                        # Status
                        # ----------------------------------

                        enabled = active.get(
                            "enabled",
                            True,
                        )

                        suspended = active.get(
                            "is_suspended",
                            False,
                        )

                        # Some IQ Option responses use
                        # integers instead of booleans.

                        enabled = bool(enabled)
                        suspended = bool(suspended)

                        if not enabled:
                            continue

                        if suspended:
                            continue

                        try:
                            numeric_id = int(
                                active_id
                            )

                        except Exception:
                            continue

                        register_active_id(
                            name,
                            numeric_id,
                        )

                        key = (
                            name,
                            numeric_id,
                        )

                        if key in seen:
                            continue

                        seen.add(key)

                        found.append(
                            {
                                "asset": name,
                                "market_type": market_type,
                                "active_id": numeric_id,
                            }
                        )

            # ----------------------------------------------
            # Continue recursively
            # ----------------------------------------------

            for key, value in node.items():

                child_market = market_type

                if key in (
                    "binary",
                    "turbo",
                    "digital",
                    "cfd",
                    "forex",
                    "crypto",
                ):

                    child_market = key

                if isinstance(
                    value,
                    (dict, list),
                ):

                    walk(
                        value,
                        child_market,
                    )

        elif isinstance(node, list):

            for item in node:

                if len(found) >= MAX_OTC_ASSETS:
                    return

                walk(
                    item,
                    market_type,
                )

    # --------------------------------------------------------
    # START SEARCH
    # --------------------------------------------------------

    root = data

    # Handle legacy:
    # {"result": {...}}

    if isinstance(
        data.get("result"),
        dict,
    ):

        print(
            "[DISCOVERY] result wrapper detected."
        )

        root = data["result"]

    print(
        "[DISCOVERY] Top-level keys:"
    )

    for key in root.keys():

        print(
            " -",
            key,
        )

    walk(root)

    # --------------------------------------------------------
    # SORT
    # --------------------------------------------------------

    found.sort(
        key=lambda item: (
            item["asset"],
            item["market_type"],
            item["active_id"],
        )
    )

    print("")
    print(
        "TOTAL OPEN OTC DISCOVERED:",
        len(found),
    )

    if found:

        print("")
        print(
            "[REAL IQ OPTION OTC ASSETS]"
        )

        for index, item in enumerate(
            found,
            start=1,
        ):

            print(
                f"{index:02d}. "
                f"{item['asset']:<20} "
                f"{item['market_type']:<10} "
                f"ID={item['active_id']}"
            )

    else:

        print("")
        print(
            "[DISCOVERY] No enabled OTC "
            "instruments found in initialization."
        )

    return found[:MAX_OTC_ASSETS]


# ============================================================
# CANDLE NORMALIZATION
# ============================================================

def normalize_candles(raw):

    if not raw:
        return []

    result = []

    for candle in raw:

        try:

            item = {
                "from": safe_float(
                    candle.get("from")
                ),

                "open": safe_float(
                    candle.get("open")
                ),

                "close": safe_float(
                    candle.get("close")
                ),

                "low": safe_float(
                    candle.get(
                        "min",
                        candle.get("low"),
                    )
                ),

                "high": safe_float(
                    candle.get(
                        "max",
                        candle.get("high"),
                    )
                ),

                "volume": safe_float(
                    candle.get("volume")
                ),
            }

            if (
                item["open"] > 0
                and item["close"] > 0
                and item["low"] > 0
                and item["high"] > 0
            ):

                result.append(item)

        except Exception:

            continue

    result.sort(
        key=lambda x: x["from"]
    )

    return result


# ============================================================
# CLOSED CANDLES ONLY
# ============================================================

def remove_open_candle(
    candles,
    timeframe_seconds,
):

    if len(candles) < 2:
        return candles

    current_time = server_now()

    completed = []

    for candle in candles:

        candle_start = candle["from"]

        if (
            candle_start
            + timeframe_seconds
            <= current_time
        ):

            completed.append(
                candle
            )

    return completed


# ============================================================
# CANDLE FETCH
# ============================================================

def get_candles_safe(
    asset,
    interval,
    count,
):

    if api is None:
        return []

    try:

        raw = api.get_candles(
            asset,
            interval,
            count + 5,
            server_now(),
        )

        candles = normalize_candles(
            raw
        )

        candles = remove_open_candle(
            candles,
            interval,
        )

        return candles[-count:]

    except Exception as e:

        print(
            f"[CANDLE ERROR] "
            f"{asset} "
            f"{interval}s -> "
            f"{repr(e)}"
        )

        return []


# ============================================================
# EMA
# ============================================================

def ema(
    values,
    period,
):

    if len(values) < period:
        return []

    multiplier = (
        2.0 / (period + 1.0)
    )

    result = [None] * len(values)

    seed = (
        sum(values[:period])
        / period
    )

    result[period - 1] = seed

    previous = seed

    for i in range(
        period,
        len(values),
    ):

        previous = (
            (
                values[i]
                - previous
            )
            * multiplier
            + previous
        )

        result[i] = previous

    return result


# ============================================================
# ATR — WILDER RMA
# ============================================================

def pine_atr(
    candles,
    period=14,
):

    if len(candles) < period + 1:
        return []

    true_ranges = [None] * len(candles)

    for i in range(
        1,
        len(candles),
    ):

        high = candles[i]["high"]
        low = candles[i]["low"]

        previous_close = candles[
            i - 1
        ]["close"]

        true_ranges[i] = max(
            high - low,
            abs(
                high
                - previous_close
            ),
            abs(
                low
                - previous_close
            ),
        )

    result = [None] * len(candles)

    seed_values = [
        x
        for x in true_ranges
        if x is not None
    ]

    if len(seed_values) < period:
        return result

    current = (
        sum(seed_values[:period])
        / period
    )

    result[period] = current

    for i in range(
        period + 1,
        len(candles),
    ):

        tr = true_ranges[i]

        if tr is None:
            continue

        current = (
            (
                current
                * (period - 1)
            )
            + tr
        ) / period

        result[i] = current

    return result


# ============================================================
# ZETA V1 STRATEGY
# ============================================================

def evaluate_zeta(
    asset,
    candles,
):

    if len(candles) < 60:
        return None

    closes = [
        c["close"]
        for c in candles
    ]

    fast_ema = ema(
        closes,
        FAST_LENGTH,
    )

    slow_ema = ema(
        closes,
        SLOW_LENGTH,
    )

    atr_values = pine_atr(
        candles,
        ATR_PERIOD,
    )

    if (
        len(fast_ema) != len(candles)
        or len(slow_ema) != len(candles)
        or len(atr_values) != len(candles)
    ):

        return None

    i = len(candles) - 1
    previous_i = i - 1

    if previous_i < 1:
        return None

    f = fast_ema[i]
    s = slow_ema[i]

    fp = fast_ema[previous_i]
    sp = slow_ema[previous_i]

    current_atr = atr_values[i]

    if any(
        x is None
        for x in (
            f,
            s,
            fp,
            sp,
            current_atr,
        )
    ):

        return None

    if current_atr <= 0:
        return None

    # --------------------------------------------------------
    # TREND
    # --------------------------------------------------------

    bull_trend = f > s
    bear_trend = f < s

    # --------------------------------------------------------
    # SLOPE
    # --------------------------------------------------------

    fast_slope_up = f > fp
    fast_slope_down = f < fp

    slow_slope_up = s > sp
    slow_slope_down = s < sp

    # --------------------------------------------------------
    # NORMALIZED DISTANCE
    # --------------------------------------------------------

    ema_distance = abs(
        f - s
    )

    normalized_distance = (
        ema_distance / current_atr
        if current_atr > 0
        else 0.0
    )

    # --------------------------------------------------------
    # TREND STATE HISTORY
    # --------------------------------------------------------

    trend_states = [0] * len(candles)

    for j in range(
        len(candles)
    ):

        if (
            fast_ema[j] is None
            or slow_ema[j] is None
            or atr_values[j] is None
        ):

            if j > 0:

                trend_states[j] = (
                    trend_states[j - 1]
                )

            continue

        if j == 0:

            continue

        previous_fast = (
            fast_ema[j - 1]
        )

        previous_slow = (
            slow_ema[j - 1]
        )

        if (
            previous_fast is None
            or previous_slow is None
        ):

            continue

        fast_up = (
            fast_ema[j]
            > previous_fast
        )

        fast_down = (
            fast_ema[j]
            < previous_fast
        )

        slow_up = (
            slow_ema[j]
            > previous_slow
        )

        slow_down = (
            slow_ema[j]
            < previous_slow
        )

        distance = (
            abs(
                fast_ema[j]
                - slow_ema[j]
            )
            / atr_values[j]
            if atr_values[j] > 0
            else 0.0
        )

        historical_bull = (
            fast_ema[j] > slow_ema[j]
            and fast_up
            and slow_up
            and distance
            > NORMALIZED_DISTANCE_MIN
        )

        historical_bear = (
            fast_ema[j] < slow_ema[j]
            and fast_down
            and slow_down
            and distance
            > NORMALIZED_DISTANCE_MIN
        )

        if historical_bull:

            trend_states[j] = 1

        elif historical_bear:

            trend_states[j] = -1

        else:

            trend_states[j] = (
                trend_states[j - 1]
            )

    trend_state = trend_states[i]

    previous_state = trend_states[
        previous_i
    ]

    bull_flip = (
        trend_state == 1
        and previous_state != 1
    )

    bear_flip = (
        trend_state == -1
        and previous_state != -1
    )

    # --------------------------------------------------------
    # CANDLE CONFIRMATION
    # --------------------------------------------------------

    current_candle = candles[i]

    bull_candle = (
        current_candle["close"]
        > current_candle["open"]
    )

    bear_candle = (
        current_candle["close"]
        < current_candle["open"]
    )

    bull_confirmation = (
        not USE_CANDLE_CONFIRM
        or bull_candle
    )

    bear_confirmation = (
        not USE_CANDLE_CONFIRM
        or bear_candle
    )

    bull_slope = (
        not USE_TREND_SLOPE
        or (
            fast_slope_up
            and slow_slope_up
        )
    )

    bear_slope = (
        not USE_TREND_SLOPE
        or (
            fast_slope_down
            and slow_slope_down
        )
    )

    # --------------------------------------------------------
    # SIGNAL
    # --------------------------------------------------------

    raw_buy = (
        bull_flip
        and current_candle["close"] > f
        and bull_confirmation
        and bull_slope
    )

    raw_sell = (
        bear_flip
        and current_candle["close"] < f
        and bear_confirmation
        and bear_slope
    )

    if not raw_buy and not raw_sell:
        return None

    # --------------------------------------------------------
    # COOLDOWN
    # --------------------------------------------------------

    candle_time = current_candle["from"]

    previous_signal_time = (
        last_signal_time.get(
            asset,
            0,
        )
    )

    if (
        previous_signal_time
        and candle_time
        - previous_signal_time
        <= (
            SIGNAL_COOLDOWN_BARS
            * TIMEFRAME
        )
    ):

        return None

    # --------------------------------------------------------
    # DIRECTION
    # --------------------------------------------------------

    if raw_buy:

        direction = "CALL"

    else:

        direction = "PUT"

    signal_id = (
        "ZETA-"
        + asset.replace(
            "-",
            "",
        )
        + "-"
        + direction
        + "-"
        + datetime.now(
            timezone.utc
        ).strftime("%H%M%S")
    )

    signal = {
        "signal_id": signal_id,
        "asset": asset,
        "direction": direction,
        "price": current_candle["close"],
        "ema9": f,
        "ema21": s,
        "atr": current_atr,
        "normalized_distance": normalized_distance,
        "candle_time": candle_time,
        "timestamp": now_utc(),
    }

    last_signal_time[
        asset
    ] = candle_time

    return signal


# ============================================================
# SIGNAL FORMAT
# ============================================================

def format_signal(signal):

    direction = signal[
        "direction"
    ]

    emoji = (
        "🟢"
        if direction == "CALL"
        else "🔴"
    )

    return (
        f"{emoji} *ZETA V1 SIGNAL*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Asset:* {signal['asset']}\n"
        f"*Direction:* *{direction} / "
        f"{'UP' if direction == 'CALL' else 'DOWN'}*\n"
        "*Chart:* 1 Minute\n"
        f"*Expiry:* *{EXPIRY_MINUTES} Minutes*\n"
        f"*Price:* {signal['price']:.8f}\n"
        f"*EMA 9:* {signal['ema9']:.8f}\n"
        f"*EMA 21:* {signal['ema21']:.8f}\n"
        f"*ATR 14:* {signal['atr']:.8f}\n"
        f"*EMA/ATR:* "
        f"{signal['normalized_distance']:.3f}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Signal ID:* {signal['signal_id']}\n"
        f"*Time:* {signal['timestamp']}\n"
        "🤖 *ZETA V1 — DEMO AUTO TRADE*"
    )


# ============================================================
# TRADE RESULT
# ============================================================

def check_trade_result(trade):

    global wins
    global losses
    global draws
    global total_profit

    trade_id = trade["trade_id"]

    try:

        result = api.check_win_v4(
            trade_id
        )

        if result is None:
            return None

        result = safe_float(
            result,
            None,
        )

        if result is None:
            return None

        profit = result

        total_profit += profit

        if profit > 0:

            outcome = "WIN"
            wins += 1
            emoji = "✅"

        elif profit < 0:

            outcome = "LOSS"
            losses += 1
            emoji = "❌"

        else:

            outcome = "DRAW"
            draws += 1
            emoji = "⚪"

        message = (
            f"{emoji} *ZETA V1 {outcome}*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"*Asset:* {trade['asset']}\n"
            f"*Direction:* {trade['direction']}\n"
            f"*Signal ID:* {trade['signal_id']}\n"
            f"*Result:* {profit:+.2f}\n"
            f"*Wins:* {wins}\n"
            f"*Losses:* {losses}\n"
            f"*Draws:* {draws}\n"
            f"*Net P/L:* {total_profit:+.2f}\n"
            "━━━━━━━━━━━━━━━━━━"
        )

        send_telegram(
            message
        )

        print(
            "[TRADE RESULT]",
            outcome,
            trade["asset"],
            profit,
        )

        return outcome

    except Exception as e:

        print(
            "[RESULT ERROR]",
            trade_id,
            repr(e),
        )

        return None


def update_active_trades():

    if not active_trades:
        return

    finished = []

    for trade_id, trade in list(
        active_trades.items()
    ):

        result = check_trade_result(
            trade
        )

        if result is not None:

            finished.append(
                trade_id
            )

    for trade_id in finished:

        active_trades.pop(
            trade_id,
            None,
        )


# ============================================================
# EXECUTE DEMO TRADE
# ============================================================

def execute_demo_trade(signal):

    global total_trades

    if api is None:

        send_telegram(
            "🔴 *ZETA V1 TRADE ERROR*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "IQ Option API is not connected."
        )

        return False

    if (
        len(active_trades)
        >= MAX_ACTIVE_TRADES
    ):

        print(
            "[TRADE BLOCKED] "
            f"Active trades={len(active_trades)}"
        )

        return False

    asset = signal["asset"]

    direction = signal[
        "direction"
    ]

    option_type = (
        "call"
        if direction == "CALL"
        else "put"
    )

    # --------------------------------------------------------
    # REAL ACTIVE ID
    # --------------------------------------------------------

    active_id = OP_code.ACTIVES.get(
        asset
    )

    if active_id is None:

        print(
            "[TRADE ERROR] No active ID:",
            asset,
        )

        send_telegram(
            "🔴 *ZETA V1 TRADE ERROR*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"*Asset:* {asset}\n"
            f"*Direction:* {direction}\n"
            "*Reason:* No real IQ Option "
            "active ID is mapped."
        )

        return False

    print(
        "\n" + "=" * 70
    )

    print(
        "[ZETA AUTO EXECUTION]"
    )

    print(
        "Asset:",
        asset,
    )

    print(
        "Active ID:",
        active_id,
    )

    print(
        "Direction:",
        direction,
    )

    print(
        "Stake:",
        STAKE,
    )

    print(
        "Expiry:",
        EXPIRY_MINUTES,
    )

    print(
        "Account:",
        BALANCE_MODE,
    )

    print(
        "=" * 70
    )

    send_telegram(
        "🟡 *ZETA V1 SENDING DEMO ORDER*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Asset:* {asset}\n"
        f"*Active ID:* {active_id}\n"
        f"*Direction:* {direction}\n"
        f"*Stake:* ${STAKE:.2f}\n"
        f"*Expiry:* {EXPIRY_MINUTES} minutes\n"
        f"*Signal ID:* {signal['signal_id']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Sending order to IQ Option..."
    )

    try:

        result = api.buy(
            STAKE,
            asset,
            option_type,
            EXPIRY_MINUTES,
        )

        print(
            "[BUY RAW RESULT]:",
            repr(result),
        )

    except Exception as e:

        print(
            "[BUY EXCEPTION]",
            repr(e),
        )

        traceback.print_exc()

        send_telegram(
            "🔴 *ZETA V1 TRADE ERROR*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"*Asset:* {asset}\n"
            f"*Direction:* {direction}\n"
            f"*Error:* {str(e)[:700]}"
        )

        return False

    # --------------------------------------------------------
    # PARSE RESULT
    # --------------------------------------------------------

    success = False
    trade_id = None

    if isinstance(
        result,
        (tuple, list),
    ):

        if len(result) >= 2:

            success = bool(
                result[0]
            )

            trade_id = result[1]

        elif len(result) == 1:

            success = bool(
                result[0]
            )

    else:

        success = bool(
            result
        )

    # --------------------------------------------------------
    # FAILED
    # --------------------------------------------------------

    if not success:

        print(
            "[BUY FAILED]",
            repr(result),
        )

        send_telegram(
            "🔴 *ZETA V1 TRADE REJECTED*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"*Asset:* {asset}\n"
            f"*Direction:* {direction}\n"
            f"*Signal ID:* {signal['signal_id']}\n"
            f"*IQ Option response:* "
            f"{str(result)[:700]}"
        )

        return False

    # --------------------------------------------------------
    # NO ID
    # --------------------------------------------------------

    if trade_id is None:

        send_telegram(
            "🟡 *ZETA V1 ORDER ACCEPTED*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"*Asset:* {asset}\n"
            f"*Direction:* {direction}\n"
            f"*Stake:* ${STAKE:.2f}\n"
            f"*Expiry:* {EXPIRY_MINUTES} minutes\n"
            f"*Signal ID:* {signal['signal_id']}\n"
            "*Trade ID:* NOT RETURNED"
        )

        return True

    # --------------------------------------------------------
    # SUCCESS
    # --------------------------------------------------------

    trade_id = str(
        trade_id
    )

    total_trades += 1

    active_trades[
        trade_id
    ] = {
        "trade_id": trade_id,
        "signal_id": signal["signal_id"],
        "asset": asset,
        "direction": direction,
        "stake": STAKE,
        "opened_at": time.time(),
    }

    print(
        "[BUY SUCCESS]",
        trade_id,
    )

    send_telegram(
        "🚀 *ZETA V1 DEMO TRADE OPENED*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Asset:* {asset}\n"
        f"*Direction:* *{direction}*\n"
        f"*Stake:* ${STAKE:.2f}\n"
        f"*Expiry:* *{EXPIRY_MINUTES} minutes*\n"
        f"*Trade ID:* {trade_id}\n"
        f"*Signal ID:* {signal['signal_id']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🤖 *PRACTICE / DEMO ONLY*"
    )

    return True


# ============================================================
# CANDLE FEED TEST
# ============================================================

def test_candle_access(assets):

    print("\n" + "=" * 70)
    print("TESTING REAL OTC CANDLE FEEDS")
    print("=" * 70)

    working = []

    for item in assets:

        asset = item["asset"]

        mapped_id = OP_code.ACTIVES.get(
            asset
        )

        if mapped_id is None:
            continue

        candles = get_candles_safe(
            asset,
            TIMEFRAME,
            10,
        )

        if len(candles) >= 5:

            working.append(
                item
            )

            print(
                "[FEED OK]",
                asset,
                "| candles=",
                len(candles),
            )

        else:

            print(
                "[FEED FAILED]",
                asset,
                "| candles=",
                len(candles),
            )

    print(
        "\nWORKING OTC CANDLE FEEDS:",
        len(working),
    )

    return working


# ============================================================
# DISCOVER + TEST OTC
# ============================================================

def refresh_otc_assets():

    global otc_assets
    global last_discovery_time

    raw_data = get_raw_initialization()

    if not raw_data:
        return False

    discovered = (
        discover_otc_from_initialization(
            raw_data
        )
    )

    if not discovered:

        print(
            "[OTC] 0 open OTC assets."
        )

        return False

    working = test_candle_access(
        discovered
    )

    if working:

        otc_assets = working

        last_discovery_time = (
            time.time()
        )

        return True

    print(
        "[OTC] Instruments found, "
        "but no candle feeds responded."
    )

    return False


# ============================================================
# CONNECTION CHECK
# ============================================================

def connection_is_alive():

    global last_connection_check

    if api is None:
        return False

    if (
        time.time()
        - last_connection_check
        < RECONNECT_INTERVAL
    ):

        return True

    last_connection_check = (
        time.time()
    )

    try:

        if hasattr(
            api,
            "check_connect",
        ):

            return bool(
                api.check_connect()
            )

    except Exception:

        pass

    return True


# ============================================================
# HEARTBEAT
# ============================================================

def send_heartbeat():

    balance = None

    try:

        balance = api.get_balance()

    except Exception:

        pass

    win_rate = (
        wins
        / total_trades
        * 100
        if total_trades > 0
        else 0.0
    )

    message = (
        "🟡 *ZETA V1 HEARTBEAT*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "*Status:* ONLINE\n"
        f"*OTC feeds:* {len(otc_assets)}\n"
        f"*Trades:* {total_trades}\n"
        f"*Wins:* {wins}\n"
        f"*Losses:* {losses}\n"
        f"*Draws:* {draws}\n"
        f"*Win rate:* {win_rate:.2f}%\n"
        f"*Net demo P/L:* "
        f"${total_profit:+.2f}\n"
        f"*Active trades:* "
        f"{len(active_trades)} / "
        f"{MAX_ACTIVE_TRADES}\n"
        f"*Runtime:* {runtime_string()}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Account:* {BALANCE_MODE}\n"
        "*Auto-trading:* ON\n"
        f"*Stake:* ${STAKE:.2f}\n"
        f"*Expiry:* {EXPIRY_MINUTES} minutes\n"
    )

    if balance is not None:

        message += (
            f"*Demo balance:* "
            f"${safe_float(balance):.2f}\n"
        )

    message += (
        "━━━━━━━━━━━━━━━━━━\n"
        "🟢 *ZETA V1 SCANNING 1M CANDLES*"
    )

    send_telegram(
        message
    )


# ============================================================
# CONNECT
# ============================================================

def connect_iq():

    global api

    print("\n" + "=" * 70)
    print("CONNECTING TO IQ OPTION")
    print("=" * 70)

    api = IQ_Option(
        IQ_EMAIL,
        IQ_PASSWORD,
    )

    connected, reason = (
        api.connect()
    )

    print(
        "[LOGIN]",
        connected,
        reason,
    )

    if not connected:
        return False

    try:

        api.change_balance(
            BALANCE_MODE
        )

    except Exception as e:

        print(
            "[BALANCE WARNING]",
            repr(e),
        )

    try:

        balance = api.get_balance()

        print(
            "[BALANCE]",
            balance,
        )

    except Exception:

        pass

    return True


# ============================================================
# MAIN TRADER
# ============================================================

def run_trader():

    global last_status_time
    global last_discovery_time

    if not IQ_EMAIL or not IQ_PASSWORD:

        message = (
            "🔴 *ZETA V1*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "IQ_EMAIL or IQ_PASSWORD "
            "is missing from GitHub Secrets."
        )

        print(message)

        send_telegram(
            message
        )

        return

    # --------------------------------------------------------
    # CONNECT
    # --------------------------------------------------------

    if not connect_iq():

        send_telegram(
            "🔴 *ZETA V1*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "IQ Option connection failed."
        )

        return

    print(
        "\n" + "=" * 70
    )

    print(
        "🟢 ZETA V1 DEMO TRADER ONLINE"
    )

    print(
        "Strategy: ZETA STYLE V1"
    )

    print(
        "Chart: 1 Minute"
    )

    print(
        "Expiry:",
        EXPIRY_MINUTES,
        "Minutes",
    )

    print(
        "Account:",
        BALANCE_MODE,
    )

    print(
        "Automatic trading: ON"
    )

    print(
        "=" * 70
    )

    send_telegram(
        "🟢 *ZETA V1 DEMO TRADER ONLINE*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "*Strategy:* ZETA STYLE V1\n"
        "*Chart:* 1 Minute\n"
        f"*Expiry:* {EXPIRY_MINUTES} Minutes\n"
        f"*Account:* {BALANCE_MODE}\n"
        "*Automatic trading:* ON\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔎 Discovering REAL IQ Option OTC instruments..."
    )

    # --------------------------------------------------------
    # INITIAL DISCOVERY
    # --------------------------------------------------------

    refresh_success = (
        refresh_otc_assets()
    )

    if not refresh_success:

        send_telegram(
            "🟠 *ZETA V1 OTC STATUS*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "IQ Option connection: OK\n"
            "OTC initialization returned "
            "no working candle feeds yet.\n"
            "The scanner will keep retrying."
        )

    else:

        send_telegram(
            "🟢 *ZETA V1 OTC FEEDS READY*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"*Working OTC feeds:* "
            f"{len(otc_assets)}\n"
            "*Data:* REAL IQ OPTION CANDLES\n"
            "*Scanning:* 1M\n"
            f"*Expiry:* {EXPIRY_MINUTES} minutes\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "ZETA V1 scanning started."
        )

    last_status_time = time.time()

    last_scan_cycle = 0

    # --------------------------------------------------------
    # CONTINUOUS LOOP
    # --------------------------------------------------------

    while True:

        try:

            current_time = time.time()

            # ------------------------------------------------
            # CONNECTION
            # ------------------------------------------------

            if not connection_is_alive():

                print(
                    "[CONNECTION] Connection lost."
                )

                send_telegram(
                    "🔴 *ZETA V1*\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "IQ Option connection "
                    "appears lost.\n"
                    "Attempting reconnect..."
                )

                try:

                    api.connect()

                    api.change_balance(
                        BALANCE_MODE
                    )

                    print(
                        "[CONNECTION] Reconnected."
                    )

                except Exception as e:

                    print(
                        "[RECONNECT ERROR]",
                        repr(e),
                    )

                    time.sleep(
                        RECONNECT_INTERVAL
                    )

                    continue

            # ------------------------------------------------
            # RESULTS
            # ------------------------------------------------

            update_active_trades()

            # ------------------------------------------------
            # OTC REFRESH
            # ------------------------------------------------

            if (
                not otc_assets
                or (
                    current_time
                    - last_discovery_time
                    >= 1800
                )
            ):

                print(
                    "\n[OTC] Refreshing "
                    "real OTC instruments..."
                )

                refresh_otc_assets()

            # ------------------------------------------------
            # SCAN
            # ------------------------------------------------

            if (
                current_time
                - last_scan_cycle
                >= SCAN_INTERVAL
            ):

                last_scan_cycle = (
                    current_time
                )

                if not otc_assets:

                    print(
                        "[SCAN] No working OTC feeds. "
                        "Waiting for next discovery..."
                    )

                else:

                    print(
                        "\n"
                        + "-" * 70
                    )

                    print(
                        "[ZETA SCAN]",
                        now_utc(),
                    )

                    print(
                        "OTC feeds:",
                        len(otc_assets),
                    )

                    print(
                        "Active trades:",
                        len(active_trades),
                    )

                    for item in list(
                        otc_assets
                    ):

                        asset = item[
                            "asset"
                        ]

                        try:

                            candles = (
                                get_candles_safe(
                                    asset,
                                    TIMEFRAME,
                                    CANDLE_COUNT,
                                )
                            )

                            if len(candles) < 60:
                                continue

                            signal = (
                                evaluate_zeta(
                                    asset,
                                    candles,
                                )
                            )

                            if signal is None:
                                continue

                            print(
                                "\n[ZETA SIGNAL]",
                                asset,
                                signal[
                                    "direction"
                                ],
                                signal[
                                    "signal_id"
                                ],
                            )

                            # Telegram signal
                            send_telegram(
                                format_signal(
                                    signal
                                )
                            )

                            # Demo trade
                            execute_demo_trade(
                                signal
                            )

                        except Exception as e:

                            print(
                                "[ASSET ERROR]",
                                asset,
                                repr(e),
                            )

                            traceback.print_exc()

                    print(
                        "[ZETA SCAN COMPLETE]",
                        now_utc(),
                    )

            # ------------------------------------------------
            # HEARTBEAT
            # ------------------------------------------------

            if (
                time.time()
                - last_status_time
                >= STATUS_INTERVAL
            ):

                last_status_time = (
                    time.time()
                )

                send_heartbeat()

            time.sleep(1)

        except KeyboardInterrupt:

            print(
                "\n[STOP] Keyboard interrupt."
            )

            break

        except Exception as e:

            print(
                "\n[MAIN LOOP ERROR]",
                repr(e),
            )

            traceback.print_exc()

            time.sleep(5)


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "=" * 70
    )

    print(
        "ZETA V1 — IQ OPTION OTC DEMO TRADER"
    )

    print(
        "=" * 70
    )

    print(
        "Started:",
        now_utc(),
    )

    print(
        "Strategy: ZETA STYLE V1"
    )

    print(
        "Chart: 1 Minute"
    )

    print(
        "Expiry:",
        EXPIRY_MINUTES,
        "Minutes"
    )

    print(
        "Account:",
        BALANCE_MODE
    )

    print(
        "Stake:",
        STAKE
    )

    print(
        "Automatic trading: ENABLED"
    )

    print(
        "=" * 70
    )

    try:

        run_trader()

    except Exception as e:

        print(
            "\n[FATAL ERROR]",
            repr(e),
        )

        traceback.print_exc()

        send_telegram(
            "🔴 *ZETA V1 FATAL ERROR*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"{str(e)[:800]}"
        )

    finally:

        if api is not None:

            try:
                api.close()
            except Exception:
                pass

        print(
            "\nZETA V1 stopped."
        )


if __name__ == "__main__":
    main()
