import os
import time
import math
import traceback
from datetime import datetime, timezone

import requests

from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP


# ============================================================
# ZETA V2.4 - DIAGNOSTIC
# ============================================================

VERSION = "ZETA V2.4-DIAGNOSTIC"

BALANCE_MODE = "PRACTICE"

STAKE = 1.0
EXPIRY_MINUTES = 2

TF5 = 300
TF1 = 60

CANDLE_COUNT_5M = 180
CANDLE_COUNT_1M = 180

MAX_OTC_ASSETS = 70

SCAN_INTERVAL = 5
STATUS_INTERVAL = 300
RECONNECT_INTERVAL = 30
DISCOVERY_INTERVAL = 1800

TARGET_TRADES = 50

ASSET_LOCK_SECONDS = 300

# ============================================================
# STRATEGY SETTINGS
# ============================================================

EMA_FAST = 20
EMA_SLOW = 50

RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14

MIN_SCORE = 80
MIN_ADX = 18

MIN_ROOM_ATR = 0.80

MIN_PULLBACK_ATR = 0.20
MAX_PULLBACK_ATR = 1.50

MAX_EXTENSION_ATR = 1.80

ZONE_TOLERANCE_ATR = 0.35

MIN_CANDLE_BODY_RATIO = 0.40

RSI_BULL_MIN = 43
RSI_BULL_MAX = 68

RSI_BEAR_MIN = 32
RSI_BEAR_MAX = 57


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

iq = None

otc_assets = []

last_connect_time = 0
last_discovery_time = 0
last_status_time = 0

last_signal_time = {}

trade_count = 0
wins = 0
losses = 0

total_profit = 0.0

diagnostics = {}

reason_counts = {}

active_trade_ids = {}

recent_trades = []


# ============================================================
# DIAGNOSTICS
# ============================================================

def diag_inc(key, amount=1):
    diagnostics[key] = diagnostics.get(key, 0) + amount


def reason(message):
    reason_counts[message] = reason_counts.get(message, 0) + 1


def reset_diagnostics():
    global diagnostics
    global reason_counts

    diagnostics = {}
    reason_counts = {}


def diagnostics_summary():
    keys = [
        "assets_scanned",
        "trend_pass",
        "adx_pass",
        "pullback_pass",
        "zone_pass",
        "rejection_pass",
        "structure_pass",
        "confirmation_pass",
        "momentum_pass",
        "rsi_pass",
        "room_pass",
        "score_pass",
    ]

    return "\n".join(
        f"{key}: {diagnostics.get(key, 0)}"
        for key in keys
    )


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }

    try:
        requests.post(
            url,
            json=payload,
            timeout=10,
        )
    except Exception:
        pass


# ============================================================
# CONNECTION
# ============================================================

def connect_iq():
    global iq
    global last_connect_time

    print("Connecting to IQ Option...")

    try:
        new_iq = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD,
        )

        check, reason_text = new_iq.connect()

        if not check:
            print(
                f"Connection failed: {reason_text}"
            )
            return False

        iq = new_iq

        try:
            iq.change_balance(BALANCE_MODE)
        except Exception:
            pass

        last_connect_time = time.time()

        print(
            f"Connected successfully - "
            f"{BALANCE_MODE}"
        )

        telegram(
            "🟢 ZETA V2.4 CONNECTED\n"
            f"Mode: {BALANCE_MODE}\n"
            f"Stake: ${STAKE:.2f}\n"
            f"Expiry: {EXPIRY_MINUTES}M"
        )

        return True

    except Exception as e:
        print(
            f"Connection exception: {e}"
        )
        return False


def ensure_connection():
    global iq

    if iq is None:
        return connect_iq()

    try:
        if not iq.check_connect():
            print("Connection lost. Reconnecting...")
            return connect_iq()

        return True

    except Exception:
        return connect_iq()


# ============================================================
# OTC DISCOVERY
# ============================================================

def discover_otc_assets():
    global otc_assets
    global last_discovery_time

    if not ensure_connection():
        return []

    print("Discovering OTC assets...")

    found = []

    try:
        init_data = None

        try:
            init_data = iq.get_all_init_v2()
        except Exception:
            pass

        if init_data is None:
            try:
                init_data = iq.get_all_init()
            except Exception:
                pass

        if not init_data:
            print("No initialization data returned.")
            return otc_assets

        # ----------------------------------------------------
        # Recursive extraction of instrument names
        # ----------------------------------------------------

        def walk(obj):
            if len(found) >= MAX_OTC_ASSETS:
                return

            if isinstance(obj, dict):
                for key, value in obj.items():

                    if isinstance(key, str):
                        key_upper = key.upper()

                        if (
                            "OTC" in key_upper
                            and isinstance(value, (dict, list))
                        ):
                            extract_names(value)

                    walk(value)

            elif isinstance(obj, list):
                for item in obj:
                    walk(item)

        def extract_names(obj):

            if len(found) >= MAX_OTC_ASSETS:
                return

            if isinstance(obj, dict):

                for key in (
                    "name",
                    "symbol",
                    "active",
                    "instrument",
                    "asset",
                ):
                    value = obj.get(key)

                    if isinstance(value, str):
                        if "OTC" in value.upper():
                            if value not in found:
                                found.append(value)

                for key, value in obj.items():
                    if isinstance(value, (dict, list)):
                        extract_names(value)

            elif isinstance(obj, list):

                for item in obj:
                    extract_names(item)

        walk(init_data)

        # ----------------------------------------------------
        # Direct OP_code active mapping
        # ----------------------------------------------------

        try:
            active_map = getattr(
                OP,
                "ACTIVES",
                {},
            )

            if isinstance(active_map, dict):

                for name in active_map.keys():

                    if len(found) >= MAX_OTC_ASSETS:
                        break

                    if (
                        isinstance(name, str)
                        and "OTC" in name.upper()
                        and name not in found
                    ):
                        found.append(name)

        except Exception:
            pass

        # ----------------------------------------------------
        # Clean and validate
        # ----------------------------------------------------

        cleaned = []

        for asset in found:

            if not isinstance(asset, str):
                continue

            asset = asset.strip()

            if not asset:
                continue

            if "OTC" not in asset.upper():
                continue

            if asset not in cleaned:
                cleaned.append(asset)

        otc_assets = cleaned[:MAX_OTC_ASSETS]

        last_discovery_time = time.time()

        print(
            f"OTC discovery complete: "
            f"{len(otc_assets)} assets"
        )

        return otc_assets

    except Exception as e:

        print(
            f"OTC discovery error: {e}"
        )

        return otc_assets


# ============================================================
# CANDLE NORMALIZATION
# ============================================================

def normalize_candle(candle):
    try:

        return {
            "t": float(
                candle.get(
                    "from",
                    candle.get(
                        "at",
                        candle.get("time", 0),
                    ),
                )
            ),

            "o": float(
                candle.get(
                    "open",
                    candle.get("o", 0),
                )
            ),

            "c": float(
                candle.get(
                    "close",
                    candle.get("c", 0),
                )
            ),

            "h": float(
                candle.get(
                    "max",
                    candle.get(
                        "high",
                        candle.get("h", 0),
                    ),
                )
            ),

            "l": float(
                candle.get(
                    "min",
                    candle.get(
                        "low",
                        candle.get("l", 0),
                    ),
                )
            ),

        }

    except Exception:
        return None


# ============================================================
# LOW-LEVEL CANDLE FETCH
# Bypasses stable_api get_candles infinite retry loop
# ============================================================

def get_candles_safe(
    asset,
    interval,
    count,
    endtime=None,
):
    if iq is None:
        return []

    try:

        if endtime is None:
            endtime = int(
                time.time()
            )

        api_obj = getattr(
            iq,
            "api",
            None,
        )

        if api_obj is None:
            return []

        low_level = getattr(
            api_obj,
            "getcandles",
            None,
        )

        if low_level is None:
            return []

        active_code = None

        try:
            active_code = OP.ACTIVES[asset]
        except Exception:
            pass

        if active_code is None:
            return []

        result_holder = {}

        def worker():
            try:
                result_holder["data"] = (
                    low_level(
                        active_code,
                        interval,
                        count,
                        endtime,
                    )
                )
            except Exception as e:
                result_holder["error"] = e

        import threading

        thread = threading.Thread(
            target=worker,
            daemon=True,
        )

        thread.start()

        thread.join(timeout=10)

        if thread.is_alive():
            print(
                f"Candle timeout: {asset}"
            )
            return []

        raw = result_holder.get(
            "data",
            [],
        )

        if not raw:
            return []

        candles = []

        for item in raw:

            normalized = normalize_candle(
                item
            )

            if normalized is None:
                continue

            if (
                normalized["o"] <= 0
                or normalized["c"] <= 0
                or normalized["h"] <= 0
                or normalized["l"] <= 0
            ):
                continue

            candles.append(
                normalized
            )

        candles.sort(
            key=lambda x: x["t"]
        )

        # Remove duplicate timestamps
        unique = {}

        for candle in candles:
            unique[
                candle["t"]
            ] = candle

        candles = list(
            unique.values()
        )

        candles.sort(
            key=lambda x: x["t"]
        )

        return candles

    except Exception as e:

        print(
            f"get_candles_safe error "
            f"{asset}: {e}"
        )

        return []


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2.0 / (
        period + 1
    )

    value = sum(
        values[:period]
    ) / period

    for price in values[period:]:
        value = (
            (price - value)
            * multiplier
            + value
        )

    return value


def ema_series(values, period):
    if len(values) < period:
        return []

    multiplier = 2.0 / (
        period + 1
    )

    current = sum(
        values[:period]
    ) / period

    result = [
        current
    ]

    for price in values[period:]:

        current = (
            (price - current)
            * multiplier
            + current
        )

        result.append(
            current
        )

    return result


def true_range(candles):
    if not candles:
        return []

    result = []

    previous_close = None

    for candle in candles:

        high = candle["h"]
        low = candle["l"]

        if previous_close is None:
            tr = high - low

        else:
            tr = max(
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

        result.append(tr)

        previous_close = candle["c"]

    return result


def atr(candles, period=14):
    trs = true_range(candles)

    if len(trs) < period:
        return None

    return (
        sum(
            trs[-period:]
        )
        / period
    )


def rsi(candles, period=14):
    if len(candles) < period + 1:
        return None

    closes = [
        x["c"]
        for x in candles
    ]

    gains = []
    losses_local = []

    for i in range(1, len(closes)):

        change = (
            closes[i]
            - closes[i - 1]
        )

        if change > 0:
            gains.append(change)
            losses_local.append(0.0)

        else:
            gains.append(0.0)
            losses_local.append(
                abs(change)
            )

    avg_gain = sum(
        gains[-period:]
    ) / period

    avg_loss = sum(
        losses_local[-period:]
    ) / period

    if avg_loss == 0:
        return 100.0

    rs = (
        avg_gain
        / avg_loss
    )

    return 100.0 - (
        100.0
        / (1.0 + rs)
    )


def adx(candles, period=14):
    if len(candles) < (
        period * 2
    ):
        return None

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(candles)):

        current = candles[i]
        previous = candles[i - 1]

        up_move = (
            current["h"]
            - previous["h"]
        )

        down_move = (
            previous["l"]
            - current["l"]
        )

        if (
            up_move > down_move
            and up_move > 0
        ):
            pdm = up_move
        else:
            pdm = 0.0

        if (
            down_move > up_move
            and down_move > 0
        ):
            mdm = down_move
        else:
            mdm = 0.0

        tr = max(
            current["h"]
            - current["l"],
            abs(
                current["h"]
                - previous["c"]
            ),
            abs(
                current["l"]
                - previous["c"]
            ),
        )

        trs.append(tr)
        plus_dm.append(pdm)
        minus_dm.append(mdm)

    if len(trs) < period:
        return None

    tr_avg = (
        sum(trs[-period:])
        / period
    )

    plus_avg = (
        sum(
            plus_dm[-period:]
        )
        / period
    )

    minus_avg = (
        sum(
            minus_dm[-period:]
        )
        / period
    )

    if tr_avg == 0:
        return 0.0

    plus_di = (
        100.0
        * plus_avg
        / tr_avg
    )

    minus_di = (
        100.0
        * minus_avg
        / tr_avg
    )

    denominator = (
        plus_di
        + minus_di
    )

    if denominator == 0:
        return 0.0

    dx = (
        100.0
        * abs(
            plus_di
            - minus_di
        )
        / denominator
    )

    return dx


# ============================================================
# CORRECTED SUPPORT / RESISTANCE
# ============================================================

def support_resistance(
    candles,
    lookback=30,
):
    """
    Find the nearest confirmed swing support
    below price and nearest confirmed swing
    resistance above price.

    Uses completed candles only.

    Unlike the previous version, this does NOT
    simply use the absolute lowest low and
    highest high of the whole 30-candle window.
    """

    recent = candles[-lookback:]

    if len(recent) < 7:
        return None, None

    current_price = recent[-1]["c"]

    swing_lows = []
    swing_highs = []

    # --------------------------------------------------------
    # Confirmed 5-candle swing points
    # --------------------------------------------------------

    for i in range(
        2,
        len(recent) - 2,
    ):

        left_2 = recent[i - 2]
        left_1 = recent[i - 1]

        current = recent[i]

        right_1 = recent[i + 1]
        right_2 = recent[i + 2]

        # Swing low
        if (
            current["l"]
            <= left_1["l"]
            and current["l"]
            <= left_2["l"]
            and current["l"]
            <= right_1["l"]
            and current["l"]
            <= right_2["l"]
        ):
            swing_lows.append(
                current["l"]
            )

        # Swing high
        if (
            current["h"]
            >= left_1["h"]
            and current["h"]
            >= left_2["h"]
            and current["h"]
            >= right_1["h"]
            and current["h"]
            >= right_2["h"]
        ):
            swing_highs.append(
                current["h"]
            )

    # --------------------------------------------------------
    # Nearest support below current price
    # --------------------------------------------------------

    supports = [
        level
        for level in swing_lows
        if level <= current_price
    ]

    # --------------------------------------------------------
    # Nearest resistance above current price
    # --------------------------------------------------------

    resistances = [
        level
        for level in swing_highs
        if level >= current_price
    ]

    support = (
        max(supports)
        if supports
        else None
    )

    resistance = (
        min(resistances)
        if resistances
        else None
    )

    return support, resistance


# ============================================================
# CANDLE HELPERS
# ============================================================

def candle_body_ratio(candle):

    candle_range = (
        candle["h"]
        - candle["l"]
    )

    if candle_range <= 0:
        return 0.0

    body = abs(
        candle["c"]
        - candle["o"]
    )

    return body / candle_range


def bullish_candle(candle):
    return (
        candle["c"]
        > candle["o"]
    )


def bearish_candle(candle):
    return (
        candle["c"]
        < candle["o"]
    )


def bullish_engulfing(
    current,
    previous,
):
    return (
        previous["c"]
        < previous["o"]
        and current["c"]
        > current["o"]
        and current["o"]
        <= previous["c"]
        and current["c"]
        >= previous["o"]
    )


def bearish_engulfing(
    current,
    previous,
):
    return (
        previous["c"]
        > previous["o"]
        and current["c"]
        < current["o"]
        and current["o"]
        >= previous["c"]
        and current["c"]
        <= previous["o"]
    )


def bullish_rejection(
    candle,
):
    body = abs(
        candle["c"]
        - candle["o"]
    )

    lower_wick = (
        min(
            candle["o"],
            candle["c"],
        )
        - candle["l"]
    )

    return (
        lower_wick
        > body
    )


def bearish_rejection(
    candle,
):
    body = abs(
        candle["c"]
        - candle["o"]
    )

    upper_wick = (
        candle["h"]
        - max(
            candle["o"],
            candle["c"],
        )
    )

    return (
        upper_wick
        > body
    )


# ============================================================
# ZETA V2.4 EVALUATION
# ============================================================

def evaluate_zeta_v2(
    asset,
    candles5,
    candles1,
):

    diag_inc(
        "assets_scanned"
    )

    if (
        len(candles5)
        < 80
        or len(candles1)
        < 80
    ):
        reason(
            "Not enough candles"
        )
        return None

    # --------------------------------------------------------
    # 5M INDICATORS
    # --------------------------------------------------------

    closes5 = [
        x["c"]
        for x in candles5
    ]

    ema_fast5_series = ema_series(
        closes5,
        EMA_FAST,
    )

    ema_slow5_series = ema_series(
        closes5,
        EMA_SLOW,
    )

    if (
        len(ema_fast5_series) < 2
        or len(ema_slow5_series) < 2
    ):
        reason(
            "5M EMA unavailable"
        )
        return None

    fast5 = ema_fast5_series[-1]
    previous_fast5 = (
        ema_fast5_series[-2]
    )

    slow5 = ema_slow5_series[-1]
    previous_slow5 = (
        ema_slow5_series[-2]
    )

    atr_value5 = atr(
        candles5,
        ATR_PERIOD,
    )

    adx_value5 = adx(
        candles5,
        ADX_PERIOD,
    )

    if (
        atr_value5 is None
        or atr_value5 <= 0
    ):
        reason(
            "5M ATR unavailable"
        )
        return None

    if adx_value5 is None:
        reason(
            "5M ADX unavailable"
        )
        return None

    # --------------------------------------------------------
    # 5M TREND
    # --------------------------------------------------------

    bull_trend = (
        fast5 > slow5
        and previous_fast5
        >= previous_slow5
        and fast5
        > previous_fast5
        and slow5
        >= previous_slow5
    )

    bear_trend = (
        fast5 < slow5
        and previous_fast5
        <= previous_slow5
        and fast5
        < previous_fast5
        and slow5
        <= previous_slow5
    )

    if not (
        bull_trend
        or bear_trend
    ):
        diag_inc(
            "trend_invalid"
        )
        reason(
            "5M trend invalid"
        )
        return None

    diag_inc(
        "trend_pass"
    )

    direction = (
        "CALL"
        if bull_trend
        else "PUT"
    )

    # --------------------------------------------------------
    # ADX
    # --------------------------------------------------------

    if adx_value5 < MIN_ADX:

        diag_inc(
            "adx_fail"
        )

        reason(
            "ADX below 18"
        )

        return None

    diag_inc(
        "adx_pass"
    )

    # --------------------------------------------------------
    # 1M INDICATORS
    # --------------------------------------------------------

    closes1 = [
        x["c"]
        for x in candles1
    ]

    ema_fast1_series = ema_series(
        closes1,
        EMA_FAST,
    )

    ema_slow1_series = ema_series(
        closes1,
        EMA_SLOW,
    )

    if (
        len(ema_fast1_series) < 2
        or len(ema_slow1_series) < 2
    ):
        reason(
            "1M EMA unavailable"
        )
        return None

    fast1 = ema_fast1_series[-1]
    previous_fast1 = (
        ema_fast1_series[-2]
    )

    slow1 = ema_slow1_series[-1]
    previous_slow1 = (
        ema_slow1_series[-2]
    )

    atr_value1 = atr(
        candles1,
        ATR_PERIOD,
    )

    rsi_value1 = rsi(
        candles1,
        RSI_PERIOD,
    )

    if (
        atr_value1 is None
        or atr_value1 <= 0
    ):
        reason(
            "1M ATR unavailable"
        )
        return None

    if rsi_value1 is None:
        reason(
            "1M RSI unavailable"
        )
        return None

    price = candles1[-1]["c"]

    # --------------------------------------------------------
    # PULLBACK
    # --------------------------------------------------------

    pullback_atr = (
        abs(
            price
            - fast1
        )
        / atr_value1
    )

    if (
        pullback_atr
        < MIN_PULLBACK_ATR
    ):

        diag_inc(
            "pullback_too_small"
        )

        reason(
            "Pullback too small"
        )

        return None

    if (
        pullback_atr
        > MAX_PULLBACK_ATR
    ):

        diag_inc(
            "pullback_too_large"
        )

        reason(
            "Pullback too large"
        )

        return None

    extension_atr = (
        abs(
            price
            - slow1
        )
        / atr_value1
    )

    if (
        extension_atr
        > MAX_EXTENSION_ATR
    ):

        diag_inc(
            "price_extended"
        )

        reason(
            "Price too extended"
        )

        return None

    diag_inc(
        "pullback_pass"
    )

    # --------------------------------------------------------
    # ZONE
    # --------------------------------------------------------

    support, resistance = (
        support_resistance(
            candles1,
            30,
        )
    )

    if (
        support is None
        or resistance is None
    ):

        diag_inc(
            "zone_fail"
        )

        reason(
            "No nearby swing zone"
        )

        return None

    zone_tolerance = (
        atr_value1
        * ZONE_TOLERANCE_ATR
    )

    if direction == "CALL":

        price_to_support = (
            abs(
                price
                - support
            )
            / atr_value1
        )

        ema_to_support = (
            abs(
                fast1
                - support
            )
            / atr_value1
        )

        zone_ok = (
            price_to_support
            <= ZONE_TOLERANCE_ATR
            or
            ema_to_support
            <= ZONE_TOLERANCE_ATR
        )

        if not zone_ok:

            diag_inc(
                "zone_fail"
            )

            reason(
                "CALL zone invalid"
            )

            return None

    else:

        price_to_resistance = (
            abs(
                price
                - resistance
            )
            / atr_value1
        )

        ema_to_resistance = (
            abs(
                fast1
                - resistance
            )
            / atr_value1
        )

        zone_ok = (
            price_to_resistance
            <= ZONE_TOLERANCE_ATR
            or
            ema_to_resistance
            <= ZONE_TOLERANCE_ATR
        )

        if not zone_ok:

            diag_inc(
                "zone_fail"
            )

            reason(
                "PUT zone invalid"
            )

            return None

    diag_inc(
        "zone_pass"
    )

    # --------------------------------------------------------
    # REJECTION CANDLE
    # --------------------------------------------------------

    current = candles1[-1]
    previous = candles1[-2]

    if direction == "CALL":

        rejection_ok = (
            bullish_rejection(
                current
            )
            or
            bullish_engulfing(
                current,
                previous,
            )
        )

    else:

        rejection_ok = (
            bearish_rejection(
                current
            )
            or
            bearish_engulfing(
                current,
                previous,
            )
        )

    if not rejection_ok:

        diag_inc(
            "rejection_fail"
        )

        reason(
            "Rejection invalid"
        )

        return None

    diag_inc(
        "rejection_pass"
    )

    # --------------------------------------------------------
    # 1M STRUCTURE
    # --------------------------------------------------------

    if direction == "CALL":

        structure_ok = (
            fast1 >= slow1
            and fast1
            >= previous_fast1
        )

    else:

        structure_ok = (
            fast1 <= slow1
            and fast1
            <= previous_fast1
        )

    if not structure_ok:

        diag_inc(
            "structure_fail"
        )

        reason(
            "1M structure invalid"
        )

        return None

    diag_inc(
        "structure_pass"
    )

    # --------------------------------------------------------
    # CANDLE CONFIRMATION
    # --------------------------------------------------------

    body_ratio = (
        candle_body_ratio(
            current
        )
    )

    if direction == "CALL":

        bullish_confirm = (
            bullish_candle(
                current
            )
            and body_ratio
            >= MIN_CANDLE_BODY_RATIO
        )

        close_above_previous_high = (
            current["c"]
            > previous["h"]
        )

        engulfing = (
            bullish_engulfing(
                current,
                previous,
            )
        )

        rejection_confirm = (
            bullish_rejection(
                current
            )
            and bullish_candle(
                current
            )
        )

        confirmation_ok = (
            bullish_confirm
            and (
                close_above_previous_high
                or engulfing
                or rejection_confirm
            )
        )

    else:

        bearish_confirm = (
            bearish_candle(
                current
            )
            and body_ratio
            >= MIN_CANDLE_BODY_RATIO
        )

        close_below_previous_low = (
            current["c"]
            < previous["l"]
        )

        engulfing = (
            bearish_engulfing(
                current,
                previous,
            )
        )

        rejection_confirm = (
            bearish_rejection(
                current
            )
            and bearish_candle(
                current
            )
        )

        confirmation_ok = (
            bearish_confirm
            and (
                close_below_previous_low
                or engulfing
                or rejection_confirm
            )
        )

    if not confirmation_ok:

        diag_inc(
            "confirmation_fail"
        )

        reason(
            "Confirmation invalid"
        )

        return None

    diag_inc(
        "confirmation_pass"
    )

    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------

    if direction == "CALL":

        momentum_ok = (
            current["c"]
            > previous["c"]
        )

    else:

        momentum_ok = (
            current["c"]
            < previous["c"]
        )

    if not momentum_ok:

        diag_inc(
            "momentum_fail"
        )

        reason(
            "Momentum invalid"
        )

        return None

    diag_inc(
        "momentum_pass"
    )

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    if direction == "CALL":

        rsi_ok = (
            RSI_BULL_MIN
            <= rsi_value1
            <= RSI_BULL_MAX
        )

    else:

        rsi_ok = (
            RSI_BEAR_MIN
            <= rsi_value1
            <= RSI_BEAR_MAX
        )

    if not rsi_ok:

        diag_inc(
            "rsi_fail"
        )

        reason(
            "RSI invalid"
        )

        return None

    diag_inc(
        "rsi_pass"
    )

    # --------------------------------------------------------
    # ROOM
    # --------------------------------------------------------

    if direction == "CALL":

        room_up_atr = (
            resistance
            - price
        ) / atr_value1

        room_ok = (
            room_up_atr
            >= MIN_ROOM_ATR
        )

        room_value = room_up_atr

    else:

        room_down_atr = (
            price
            - support
        ) / atr_value1

        room_ok = (
            room_down_atr
            >= MIN_ROOM_ATR
        )

        room_value = room_down_atr

    if not room_ok:

        diag_inc(
            "room_fail"
        )

        reason(
            "Room below 0.80 ATR"
        )

        return None

    diag_inc(
        "room_pass"
    )

    # --------------------------------------------------------
    # SCORE
    # --------------------------------------------------------

    score = 0

    # Trend
    score += 20

    # Structure
    score += 10

    # Zone
    score += 15

    # Rejection
    score += 20

    # Confirmation
    score += 10

    # Momentum
    score += 5

    # RSI
    score += 5

    # Room
    score += 5

    # Additional confirmation
    if (
        (
            direction == "CALL"
            and bullish_engulfing(
                current,
                previous,
            )
        )
        or
        (
            direction == "PUT"
            and bearish_engulfing(
                current,
                previous,
            )
        )
        or
        (
            direction == "CALL"
            and bullish_rejection(
                current
            )
            and bullish_candle(
                current
            )
        )
        or
        (
            direction == "PUT"
            and bearish_rejection(
                current
            )
            and bearish_candle(
                current
            )
        )
    ):
        score += 5

    if score < MIN_SCORE:

        diag_inc(
            "score_fail"
        )

        reason(
            "Score below 80"
        )

        return None

    diag_inc(
        "score_pass"
    )

    # --------------------------------------------------------
    # SIGNAL COOLDOWN
    # --------------------------------------------------------

    now = time.time()

    previous_signal = (
        last_signal_time.get(
            asset,
            0,
        )
    )

    if (
        now - previous_signal
        < 180
    ):

        reason(
            "Signal cooldown"
        )

        return None

    last_signal_time[
        asset
    ] = now

    # --------------------------------------------------------
    # SIGNAL
    # --------------------------------------------------------

    return {
        "asset": asset,
        "direction": direction,
        "score": score,
        "price": price,
        "rsi": rsi_value1,
        "adx": adx_value5,
        "atr": atr_value1,
        "support": support,
        "resistance": resistance,
        "room_atr": room_value,
        "pullback_atr": pullback_atr,
        "timestamp": datetime.now(
            timezone.utc
        ).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        ),
    }


# ============================================================
# SIGNAL FORMAT
# ============================================================

def format_signal(signal):

    direction_emoji = (
        "🟢"
        if signal["direction"]
        == "CALL"
        else "🔴"
    )

    return (
        f"{direction_emoji} ZETA V2.4 SIGNAL\n\n"
        f"Asset: {signal['asset']}\n"
        f"Direction: {signal['direction']}\n"
        f"Score: {signal['score']}\n"
        f"5M ADX: {signal['adx']:.1f}\n"
        f"1M RSI: {signal['rsi']:.1f}\n"
        f"Pullback: {signal['pullback_atr']:.2f} ATR\n"
        f"Room: {signal['room_atr']:.2f} ATR\n"
        f"Support: {signal['support']}\n"
        f"Resistance: {signal['resistance']}\n"
        f"Expiry: {EXPIRY_MINUTES}M\n"
        f"Stake: ${STAKE:.2f}\n\n"
        f"⏱ {signal['timestamp']}"
    )


# ============================================================
# DEMO TRADE
# ============================================================

def execute_trade(signal):

    global trade_count
    global total_profit

    asset = signal["asset"]
    direction = signal["direction"]

    # --------------------------------------------------------
    # Trade lock
    # --------------------------------------------------------

    now = time.time()

    previous_trade = (
        active_trade_ids.get(
            asset,
            0,
        )
    )

    if (
        now - previous_trade
        < ASSET_LOCK_SECONDS
    ):
        reason(
            "Asset trade lock"
        )
        return False

    if (
        trade_count
        >= TARGET_TRADES
    ):
        return False

    try:

        if not ensure_connection():
            return False

        action = (
            "call"
            if direction == "CALL"
            else "put"
        )

        print(
            f"Executing DEMO trade: "
            f"{asset} "
            f"{direction} "
            f"${STAKE:.2f}"
        )

        success, order_id = (
            iq.buy(
                STAKE,
                asset,
                action,
                EXPIRY_MINUTES,
            )
        )

        if not success:

            print(
                f"Trade failed: "
                f"{asset}"
            )

            telegram(
                f"⚠️ ZETA TRADE FAILED\n"
                f"{asset}\n"
                f"{direction}"
            )

            return False

        trade_count += 1

        active_trade_ids[
            asset
        ] = now

        trade_record = {
            "id": order_id,
            "asset": asset,
            "direction": direction,
            "stake": STAKE,
            "time": now,
            "status": "PENDING",
        }

        active_trade_ids[
            f"{asset}:{order_id}"
        ] = now

        recent_trades.append(
            trade_record
        )

        if len(recent_trades) > 100:
            recent_trades.pop(0)

        telegram(
            format_signal(signal)
            + "\n\n"
            + f"✅ DEMO ORDER PLACED\n"
            + f"Order ID: {order_id}\n"
            + f"Trade: {trade_count}/{TARGET_TRADES}"
        )

        print(
            f"DEMO ORDER PLACED: "
            f"{order_id}"
        )

        return True

    except Exception as e:

        print(
            f"Trade execution error "
            f"{asset}: {e}"
        )

        traceback.print_exc()

        return False


# ============================================================
# CHECK CLOSED TRADE RESULT
# ============================================================

def check_trade_results():

    global wins
    global losses
    global total_profit

    if iq is None:
        return

    for trade in list(
        recent_trades
    ):

        if (
            trade["status"]
            != "PENDING"
        ):
            continue

        order_id = trade["id"]

        try:

            result = None

            # ------------------------------------------------
            # Try check_win_v4
            # ------------------------------------------------

            try:
                result = (
                    iq.check_win_v4(
                        order_id
                    )
                )
            except Exception:
                pass

            if result is None:
                continue

            # ------------------------------------------------
            # Normalize result
            # ------------------------------------------------

            try:
                profit = float(
                    result
                )
            except Exception:
                continue

            trade["status"] = (
                "WIN"
                if profit > 0
                else "LOSS"
            )

            trade["profit"] = profit

            total_profit += profit

            if profit > 0:
                wins += 1
            else:
                losses += 1

            emoji = (
                "🟢"
                if profit > 0
                else "🔴"
            )

            telegram(
                f"{emoji} ZETA RESULT\n\n"
                f"Asset: {trade['asset']}\n"
                f"Direction: {trade['direction']}\n"
                f"Result: "
                f"{'WIN' if profit > 0 else 'LOSS'}\n"
                f"P/L: ${profit:.2f}\n\n"
                f"Wins: {wins}\n"
                f"Losses: {losses}\n"
                f"Total P/L: ${total_profit:.2f}"
            )

        except Exception:
            continue


# ============================================================
# HEARTBEAT
# ============================================================

def send_heartbeat():

    global last_status_time

    balance = None

    try:

        if iq is not None:
            balance = (
                iq.get_balance()
            )

    except Exception:
        pass

    total_results = (
        wins + losses
    )

    if total_results > 0:
        win_rate = (
            wins
            / total_results
            * 100
        )
    else:
        win_rate = 0.0

    message = (
        "🟡 ZETA V2.4 HEARTBEAT\n\n"
        f"Strategy: High Quality Trend Pullback\n"
        f"Context: 5M\n"
        f"Entry: 1M\n"
        f"Min score: {MIN_SCORE}\n"
        f"Expiry: {EXPIRY_MINUTES}M\n"
        f"Stake: ${STAKE:.2f}\n"
        f"OTC assets: {len(otc_assets)}\n\n"
        f"Orders: {trade_count}/{TARGET_TRADES}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Win rate: {win_rate:.1f}%\n"
        f"Demo P/L: ${total_profit:.2f}\n"
    )

    if balance is not None:
        try:
            message += (
                f"Balance: ${float(balance):.2f}\n"
            )
        except Exception:
            pass

    message += (
        "\n📊 DIAGNOSTICS\n"
        + diagnostics_summary()
    )

    telegram(message)

    print(
        "\n" + message
    )

    last_status_time = time.time()


# ============================================================
# SCAN ONE ASSET
# ============================================================

def scan_asset(asset):

    try:

        candles5 = get_candles_safe(
            asset,
            TF5,
            CANDLE_COUNT_5M,
        )

        if len(candles5) < 80:

            reason(
                "5M candle fetch failed"
            )

            return None

        candles1 = get_candles_safe(
            asset,
            TF1,
            CANDLE_COUNT_1M,
        )

        if len(candles1) < 80:

            reason(
                "1M candle fetch failed"
            )

            return None

        return evaluate_zeta_v2(
            asset,
            candles5,
            candles1,
        )

    except Exception as e:

        print(
            f"Scan error {asset}: {e}"
        )

        return None


# ============================================================
# SCAN ALL OTC
# ============================================================

def scan_all_assets():

    if not otc_assets:
        return

    reset_diagnostics()

    print(
        f"\nScanning "
        f"{len(otc_assets)} OTC assets..."
    )

    for asset in list(
        otc_assets
    ):

        if (
            trade_count
            >= TARGET_TRADES
        ):
            break

        try:

            signal = scan_asset(
                asset
            )

            if signal is not None:

                print(
                    "\n"
                    + format_signal(
                        signal
                    )
                )

                execute_trade(
                    signal
                )

        except Exception as e:

            print(
                f"Asset loop error "
                f"{asset}: {e}"
            )

    check_trade_results()


# ============================================================
# MAIN TRADING LOOP
# ============================================================

def run_trader():

    global last_discovery_time
    global last_status_time

    print(
        "=" * 60
    )

    print(
        "ZETA V2.4 DEMO TRADER"
    )

    print(
        "Strategy: "
        "High Quality Trend Pullback"
    )

    print(
        f"Expiry: "
        f"{EXPIRY_MINUTES} minutes"
    )

    print(
        f"Stake: "
        f"${STAKE:.2f}"
    )

    print(
        f"Target: "
        f"{TARGET_TRADES} trades"
    )

    print(
        "=" * 60
    )

    while True:

        try:

            # ------------------------------------------------
            # Target reached
            # ------------------------------------------------

            if (
                trade_count
                >= TARGET_TRADES
            ):

                print(
                    "\nTARGET REACHED"
                )

                telegram(
                    "🏁 ZETA V2.4 TARGET REACHED\n\n"
                    f"Trades: {trade_count}\n"
                    f"Wins: {wins}\n"
                    f"Losses: {losses}\n"
                    f"P/L: ${total_profit:.2f}"
                )

                break

            # ------------------------------------------------
            # Connection
            # ------------------------------------------------

            if not ensure_connection():

                print(
                    "Waiting for connection..."
                )

                time.sleep(
                    RECONNECT_INTERVAL
                )

                continue

            # ------------------------------------------------
            # OTC discovery
            # ------------------------------------------------

            if (
                not otc_assets
                or
                time.time()
                - last_discovery_time
                >= DISCOVERY_INTERVAL
            ):

                discover_otc_assets()

            # ------------------------------------------------
            # Scan
            # ------------------------------------------------

            if otc_assets:
                scan_all_assets()

            else:

                print(
                    "No OTC assets found."
                )

            # ------------------------------------------------
            # Heartbeat
            # ------------------------------------------------

            if (
                time.time()
                - last_status_time
                >= STATUS_INTERVAL
            ):

                send_heartbeat()

            time.sleep(
                SCAN_INTERVAL
            )

        except KeyboardInterrupt:

            print(
                "\nStopping ZETA V2.4..."
            )

            telegram(
                "🛑 ZETA V2.4 STOPPED"
            )

            break

        except Exception as e:

            print(
                "\nMAIN LOOP ERROR:"
            )

            print(e)

            traceback.print_exc()

            telegram(
                "⚠️ ZETA V2.4 ERROR\n"
                f"{str(e)[:500]}"
            )

            time.sleep(
                RECONNECT_INTERVAL
            )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        f"{VERSION}"
    )

    print(
        "Starting..."
    )

    if not IQ_EMAIL:
        print(
            "ERROR: IQ_EMAIL secret missing."
        )
        return

    if not IQ_PASSWORD:
        print(
            "ERROR: IQ_PASSWORD secret missing."
        )
        return

    if not connect_iq():

        print(
            "Initial connection failed."
        )

        time.sleep(
            RECONNECT_INTERVAL
        )

        if not connect_iq():
            return

    discover_otc_assets()

    if not otc_assets:

        print(
            "WARNING: No OTC assets discovered."
        )

        telegram(
            "⚠️ ZETA V2.4\n"
            "Connected, but no OTC assets were discovered."
        )

    else:

        print(
            f"Found {len(otc_assets)} OTC assets."
        )

    run_trader()


if __name__ == "__main__":
    main()
