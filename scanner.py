import os
import time
import math
import traceback
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code


# ============================================================
# ZETA V2.4 — HIGH QUALITY TREND PULLBACK
# REAL IQ OPTION OTC DISCOVERY
# PRACTICE / DEMO ONLY
#
# FOCUSED ASSET TEST + REJECTION DIAGNOSTICS
#
# IMPORTANT:
# Strategy rules are unchanged.
# Diagnostics only explain why setups are rejected.
# ============================================================

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
# FOCUSED ASSETS
# ============================================================

FOCUSED_ASSETS = [
    "COFFEE-OTC",
    "USDSGD-OTC",
    "USDPLN-OTC",
    "COSMOS-OTC",
    "USDMYR-OTC",
    "EURCAD-OTC",
    "USDTRY-OTC",
    "ONDO-OTC",
    "EURUSD-OTC",
    "AUDUSD-OTC",
]


# ============================================================
# STRATEGY SETTINGS
#
# DO NOT CHANGE — SAME ZETA STRATEGY
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

api = None

otc_assets = []

last_signal_time = {}
last_trade_time = {}

total_trades = 0

start_time = time.time()

last_status_time = 0
last_discovery_time = 0
last_connection_check = 0


# ============================================================
# REJECTION DIAGNOSTICS
#
# These do NOT change strategy decisions.
# They only record why evaluate_zeta_v2() returned None.
# ============================================================

rejection_counts = {}
last_rejection_by_asset = {}

diagnostic_scan_count = 0
diagnostic_last_report = 0


def record_rejection(asset, reason):

    rejection_counts[reason] = (
        rejection_counts.get(reason, 0) + 1
    )

    last_rejection_by_asset[asset] = reason


def diagnostic_summary():

    if not rejection_counts:
        return "No rejections recorded yet."

    ordered = sorted(
        rejection_counts.items(),
        key=lambda x: x[1],
        reverse=True,
    )

    lines = []

    for reason, count in ordered[:8]:
        lines.append(
            f"• {reason}: {count}"
        )

    return "\n".join(lines)


def latest_asset_diagnostics():

    if not last_rejection_by_asset:
        return "No rejection diagnostics yet."

    lines = []

    for asset in FOCUSED_ASSETS:

        reason = last_rejection_by_asset.get(asset)

        if reason:
            lines.append(
                f"{asset}: {reason}"
            )

    if not lines:
        return "No rejection diagnostics yet."

    return "\n".join(lines[:10])


# ============================================================
# BASIC HELPERS
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

    seconds = int(
        time.time() - start_time
    )

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
# ASSET NORMALIZATION
# ============================================================

def normalize_asset_key(name):

    if not isinstance(name, str):
        return ""

    return "".join(
        ch
        for ch in name.upper()
        if ch.isalnum()
    )


FOCUSED_ASSET_KEYS = {
    normalize_asset_key(asset)
    for asset in FOCUSED_ASSETS
}


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
# WORKING IQ OPTION INITIALIZATION
# ============================================================

def get_raw_initialization():

    if api is None:
        return None

    print("\n" + "=" * 70)
    print("IQ OPTION MARKET INITIALIZATION")
    print("=" * 70)

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
# REAL OTC DISCOVERY
# ============================================================

def discover_otc_from_initialization(data):

    print("\n" + "=" * 70)
    print("REAL IQ OPTION OTC DISCOVERY")
    print("=" * 70)

    if not isinstance(data, dict):

        print(
            "[DISCOVERY] Unexpected root type:",
            type(data).__name__,
        )

        return []

    found = []
    seen = set()

    def walk(node, market_type="unknown"):

        if isinstance(node, dict):

            if "actives" in node:

                actives = node.get("actives")

                if isinstance(actives, dict):

                    for active_id, active in actives.items():

                        if not isinstance(active, dict):
                            continue

                        raw_name = active.get("name")

                        name = clean_active_name(
                            raw_name
                        )

                        if not is_otc_name(name):
                            continue

                        enabled = bool(
                            active.get(
                                "enabled",
                                True,
                            )
                        )

                        suspended = bool(
                            active.get(
                                "is_suspended",
                                False,
                            )
                        )

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
                            normalize_asset_key(name),
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

            for key, value in node.items():

                child_market = market_type

                if key in (
                    "binary",
                    "turbo",
                    "digital",
                    "cfd",
                    "forex",
                    "crypto",
                    "stocks",
                    "commodities",
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

                walk(
                    item,
                    market_type,
                )

    root = data

    if isinstance(
        data.get("result"),
        dict,
    ):

        root = data["result"]

    walk(root)

    found.sort(
        key=lambda item: (
            item["asset"],
            item["market_type"],
            item["active_id"],
        )
    )

    print(
        "TOTAL REAL OTC DISCOVERED:",
        len(found),
    )

    return found


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

            completed.append(candle)

    return completed


# ============================================================
# SAFE CANDLE FETCH
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

        candles = normalize_candles(raw)

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

def ema(values, period):

    if len(values) < period:
        return [None] * len(values)

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
# ATR
# ============================================================

def atr_values(
    candles,
    period=14,
):

    if len(candles) < period + 1:
        return [None] * len(candles)

    trs = [None] * len(candles)

    for i in range(
        1,
        len(candles),
    ):

        high = candles[i]["high"]
        low = candles[i]["low"]

        previous_close = candles[
            i - 1
        ]["close"]

        trs[i] = max(
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

    seed = [
        x
        for x in trs
        if x is not None
    ]

    if len(seed) < period:
        return result

    current = (
        sum(seed[:period])
        / period
    )

    result[period] = current

    for i in range(
        period + 1,
        len(candles),
    ):

        if trs[i] is None:
            continue

        current = (
            (
                current
                * (period - 1)
            )
            + trs[i]
        ) / period

        result[i] = current

    return result


# ============================================================
# RSI
# ============================================================

def rsi_values(
    closes,
    period=14,
):

    result = [None] * len(closes)

    if len(closes) <= period:
        return result

    gains = []
    losses_list = []

    for i in range(
        1,
        len(closes),
    ):

        change = (
            closes[i]
            - closes[i - 1]
        )

        gains.append(
            max(change, 0.0)
        )

        losses_list.append(
            max(-change, 0.0)
        )

    avg_gain = (
        sum(gains[:period])
        / period
    )

    avg_loss = (
        sum(losses_list[:period])
        / period
    )

    index = period

    if avg_loss == 0:

        result[index] = 100.0

    else:

        rs = avg_gain / avg_loss

        result[index] = (
            100.0
            - (
                100.0
                / (1.0 + rs)
            )
        )

    for j in range(
        period,
        len(gains),
    ):

        avg_gain = (
            (
                avg_gain
                * (period - 1)
            )
            + gains[j]
        ) / period

        avg_loss = (
            (
                avg_loss
                * (period - 1)
            )
            + losses_list[j]
        ) / period

        index = j + 1

        if avg_loss == 0:

            result[index] = 100.0

        else:

            rs = (
                avg_gain
                / avg_loss
            )

            result[index] = (
                100.0
                - (
                    100.0
                    / (1.0 + rs)
                )
            )

    return result


# ============================================================
# ADX
# ============================================================

def adx_values(
    candles,
    period=14,
):

    length = len(candles)

    result = [None] * length

    if length < (
        period * 2
        + 2
    ):
        return result

    tr = [0.0] * length
    plus_dm = [0.0] * length
    minus_dm = [0.0] * length

    for i in range(
        1,
        length,
    ):

        high = candles[i]["high"]
        low = candles[i]["low"]

        prev_high = candles[
            i - 1
        ]["high"]

        prev_low = candles[
            i - 1
        ]["low"]

        prev_close = candles[
            i - 1
        ]["close"]

        tr[i] = max(
            high - low,
            abs(
                high
                - prev_close
            ),
            abs(
                low
                - prev_close
            ),
        )

        up_move = (
            high
            - prev_high
        )

        down_move = (
            prev_low
            - low
        )

        if (
            up_move > down_move
            and up_move > 0
        ):

            plus_dm[i] = up_move

        if (
            down_move > up_move
            and down_move > 0
        ):

            minus_dm[i] = down_move

    atr = sum(
        tr[1:period + 1]
    ) / period

    plus = sum(
        plus_dm[1:period + 1]
    ) / period

    minus = sum(
        minus_dm[1:period + 1]
    ) / period

    dx_values = []

    for i in range(
        period + 1,
        length,
    ):

        atr = (
            (
                atr
                * (period - 1)
            )
            + tr[i]
        ) / period

        plus = (
            (
                plus
                * (period - 1)
            )
            + plus_dm[i]
        ) / period

        minus = (
            (
                minus
                * (period - 1)
            )
            + minus_dm[i]
        ) / period

        if atr <= 0:
            continue

        plus_di = (
            100.0
            * plus
            / atr
        )

        minus_di = (
            100.0
            * minus
            / atr
        )

        denominator = (
            plus_di
            + minus_di
        )

        if denominator <= 0:
            continue

        dx = (
            100.0
            * abs(
                plus_di
                - minus_di
            )
            / denominator
        )

        dx_values.append(dx)

        if len(dx_values) >= period:

            if result[i - 1] is None:

                adx = (
                    sum(
                        dx_values[
                            :period
                        ]
                    )
                    / period
                )

            else:

                adx = (
                    (
                        result[i - 1]
                        * (period - 1)
                    )
                    + dx
                ) / period

            result[i] = adx

    return result


# ============================================================
# CANDLE HELPERS
# ============================================================

def candle_body(candle):

    return abs(
        candle["close"]
        - candle["open"]
    )


def candle_range(candle):

    return (
        candle["high"]
        - candle["low"]
    )


def is_bullish(candle):

    return (
        candle["close"]
        > candle["open"]
    )


def is_bearish(candle):

    return (
        candle["close"]
        < candle["open"]
    )


def body_ratio(candle):

    rng = candle_range(candle)

    if rng <= 0:
        return 0.0

    return (
        candle_body(candle)
        / rng
    )


def bullish_rejection(candle):

    rng = candle_range(candle)

    if rng <= 0:
        return False

    lower_wick = (
        min(
            candle["open"],
            candle["close"],
        )
        - candle["low"]
    )

    body = candle_body(candle)

    return (
        lower_wick
        >= body * 1.0
        and candle["close"]
        >= (
            candle["low"]
            + rng * 0.55
        )
    )


def bearish_rejection(candle):

    rng = candle_range(candle)

    if rng <= 0:
        return False

    upper_wick = (
        candle["high"]
        - max(
            candle["open"],
            candle["close"],
        )
    )

    body = candle_body(candle)

    return (
        upper_wick
        >= body * 1.0
        and candle["close"]
        <= (
            candle["high"]
            - rng * 0.55
        )
    )


def bullish_engulfing(
    previous,
    current,
):

    return (
        is_bearish(previous)
        and is_bullish(current)
        and current["open"]
        <= previous["close"]
        and current["close"]
        >= previous["open"]
    )


def bearish_engulfing(
    previous,
    current,
):

    return (
        is_bullish(previous)
        and is_bearish(current)
        and current["open"]
        >= previous["close"]
        and current["close"]
        <= previous["open"]
    )


# ============================================================
# SUPPORT / RESISTANCE
# ============================================================

def recent_support(
    candles,
    lookback=30,
):

    subset = candles[-lookback:]

    if not subset:
        return None

    return min(
        c["low"]
        for c in subset
    )


def recent_resistance(
    candles,
    lookback=30,
):

    subset = candles[-lookback:]

    if not subset:
        return None

    return max(
        c["high"]
        for c in subset
    )


# ============================================================
# ZETA V2 EVALUATION
#
# SAME STRATEGY.
#
# Every rejection now gets recorded.
# ============================================================

def evaluate_zeta_v2(
    asset,
    candles_5m,
    candles_1m,
):

    if len(candles_5m) < 80:

        record_rejection(
            asset,
            "5M candles < 80",
        )

        return None

    if len(candles_1m) < 80:

        record_rejection(
            asset,
            "1M candles < 80",
        )

        return None

    # --------------------------------------------------------
    # 5M DATA
    # --------------------------------------------------------

    closes_5m = [
        c["close"]
        for c in candles_5m
    ]

    ema20_5m = ema(
        closes_5m,
        EMA_FAST,
    )

    ema50_5m = ema(
        closes_5m,
        EMA_SLOW,
    )

    atr_5m = atr_values(
        candles_5m,
        ATR_PERIOD,
    )

    adx_5m = adx_values(
        candles_5m,
        ADX_PERIOD,
    )

    i5 = len(candles_5m) - 1
    p5 = i5 - 1

    if p5 < 1:

        record_rejection(
            asset,
            "5M index invalid",
        )

        return None

    values = (
        ema20_5m[i5],
        ema50_5m[i5],
        ema20_5m[p5],
        ema50_5m[p5],
        atr_5m[i5],
        adx_5m[i5],
    )

    if any(
        x is None
        for x in values
    ):

        record_rejection(
            asset,
            "5M indicators unavailable",
        )

        return None

    fast5 = ema20_5m[i5]
    slow5 = ema50_5m[i5]

    previous_fast5 = ema20_5m[p5]
    previous_slow5 = ema50_5m[p5]

    current_atr5 = atr_5m[i5]
    current_adx = adx_5m[i5]

    if current_atr5 <= 0:

        record_rejection(
            asset,
            "5M ATR <= 0",
        )

        return None

    # --------------------------------------------------------
    # TREND
    # --------------------------------------------------------

    bullish_trend = (
        fast5 > slow5
        and previous_fast5
        >= previous_slow5
        and fast5 > previous_fast5
        and slow5 >= previous_slow5
    )

    bearish_trend = (
        fast5 < slow5
        and previous_fast5
        <= previous_slow5
        and fast5 < previous_fast5
        and slow5 <= previous_slow5
    )

    if not (
        bullish_trend
        or bearish_trend
    ):

        record_rejection(
            asset,
            "No valid 5M trend",
        )

        return None

    # --------------------------------------------------------
    # ADX
    # --------------------------------------------------------

    if current_adx < MIN_ADX:

        record_rejection(
            asset,
            f"ADX {current_adx:.1f} < {MIN_ADX}",
        )

        return None

    # --------------------------------------------------------
    # 1M INDICATORS
    # --------------------------------------------------------

    closes_1m = [
        c["close"]
        for c in candles_1m
    ]

    ema20_1m = ema(
        closes_1m,
        EMA_FAST,
    )

    ema50_1m = ema(
        closes_1m,
        EMA_SLOW,
    )

    atr_1m = atr_values(
        candles_1m,
        ATR_PERIOD,
    )

    rsi_1m = rsi_values(
        closes_1m,
        RSI_PERIOD,
    )

    i1 = len(candles_1m) - 1
    p1 = i1 - 1

    if p1 < 1:

        record_rejection(
            asset,
            "1M index invalid",
        )

        return None

    values_1m = (
        ema20_1m[i1],
        ema50_1m[i1],
        ema20_1m[p1],
        ema50_1m[p1],
        atr_1m[i1],
        rsi_1m[i1],
    )

    if any(
        x is None
        for x in values_1m
    ):

        record_rejection(
            asset,
            "1M indicators unavailable",
        )

        return None

    fast1 = ema20_1m[i1]
    slow1 = ema50_1m[i1]

    previous_fast1 = ema20_1m[p1]
    previous_slow1 = ema50_1m[p1]

    current_atr1 = atr_1m[i1]
    current_rsi = rsi_1m[i1]

    if current_atr1 <= 0:

        record_rejection(
            asset,
            "1M ATR <= 0",
        )

        return None

    current = candles_1m[i1]
    previous = candles_1m[p1]

    price = current["close"]

    # --------------------------------------------------------
    # PULLBACK
    # --------------------------------------------------------

    distance_from_fast = abs(
        price - fast1
    )

    pullback_atr = (
        distance_from_fast
        / current_atr1
    )

    extension_from_fast = (
        distance_from_fast
        / current_atr1
    )

    if (
        pullback_atr
        < MIN_PULLBACK_ATR
    ):

        record_rejection(
            asset,
            f"Pullback {pullback_atr:.2f} < {MIN_PULLBACK_ATR}",
        )

        return None

    if (
        extension_from_fast
        > MAX_EXTENSION_ATR
    ):

        record_rejection(
            asset,
            f"Extension {extension_from_fast:.2f} > {MAX_EXTENSION_ATR}",
        )

        return None

    if (
        pullback_atr
        > MAX_PULLBACK_ATR
    ):

        record_rejection(
            asset,
            f"Pullback {pullback_atr:.2f} > {MAX_PULLBACK_ATR}",
        )

        return None

    # --------------------------------------------------------
    # SUPPORT / RESISTANCE
    # --------------------------------------------------------

    support = recent_support(
        candles_1m,
        30,
    )

    resistance = recent_resistance(
        candles_1m,
        30,
    )

    if support is None:

        record_rejection(
            asset,
            "No support",
        )

        return None

    if resistance is None:

        record_rejection(
            asset,
            "No resistance",
        )

        return None

    # --------------------------------------------------------
    # ZONE
    # --------------------------------------------------------

    zone_tolerance = (
        current_atr1
        * ZONE_TOLERANCE_ATR
    )

    near_support = (
        abs(price - support)
        <= zone_tolerance
    )

    near_resistance = (
        abs(price - resistance)
        <= zone_tolerance
    )

    # --------------------------------------------------------
    # REJECTION CANDLE
    # --------------------------------------------------------

    bull_rejection = (
        bullish_rejection(current)
    )

    bear_rejection = (
        bearish_rejection(current)
    )

    # --------------------------------------------------------
    # 1M EMA STRUCTURE
    # --------------------------------------------------------

    bullish_1m_structure = (
        fast1 >= slow1
        and fast1 >= previous_fast1
    )

    bearish_1m_structure = (
        fast1 <= slow1
        and fast1 <= previous_fast1
    )

    # --------------------------------------------------------
    # CANDLE CONFIRMATION
    # SAME ORIGINAL LOGIC
    # --------------------------------------------------------

    bull_confirm = (
        is_bullish(current)
        and (
            body_ratio(current)
            >= MIN_CANDLE_BODY_RATIO
        )
        and (
            current["close"]
            > previous["high"]
            or bullish_engulfing(
                previous,
                current,
            )
            or (
                bull_rejection
                and current["close"]
                > current["open"]
            )
        )
    )

    bear_confirm = (
        is_bearish(current)
        and (
            body_ratio(current)
            >= MIN_CANDLE_BODY_RATIO
        )
        and (
            current["close"]
            < previous["low"]
            or bearish_engulfing(
                previous,
                current,
            )
            or (
                bear_rejection
                and current["close"]
                < current["open"]
            )
        )
    )

    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------

    momentum_bull = (
        current["close"]
        > previous["close"]
    )

    momentum_bear = (
        current["close"]
        < previous["close"]
    )

    # --------------------------------------------------------
    # ROOM
    # --------------------------------------------------------

    room_up = (
        resistance - price
    )

    room_down = (
        price - support
    )

    room_up_atr = (
        room_up / current_atr1
        if current_atr1 > 0
        else 0
    )

    room_down_atr = (
        room_down / current_atr1
        if current_atr1 > 0
        else 0
    )

    # --------------------------------------------------------
    # SCORE
    # --------------------------------------------------------

    score = 0

    direction = None

    reasons = []

    # ========================================================
    # CALL
    # ========================================================

    if bullish_trend:

        score += 20

        reasons.append(
            "5M bullish trend"
        )

        if bullish_1m_structure:

            score += 10

            reasons.append(
                "1M EMA structure"
            )

        if (
            near_support
            or abs(
                price - fast1
            )
            <= zone_tolerance
        ):

            score += 15

            reasons.append(
                "pullback zone"
            )

        if bull_rejection:

            score += 20

            reasons.append(
                "bullish rejection"
            )

        if bull_confirm:

            score += 10

            reasons.append(
                "1M confirmation"
            )

        if momentum_bull:

            score += 5

            reasons.append(
                "bullish momentum"
            )

        if (
            current_rsi
            >= RSI_BULL_MIN
            and current_rsi
            <= RSI_BULL_MAX
        ):

            score += 5

            reasons.append(
                "RSI valid"
            )

        if (
            room_up_atr
            >= MIN_ROOM_ATR
        ):

            score += 5

            reasons.append(
                "room available"
            )

        if (
            bullish_engulfing(
                previous,
                current,
            )
            or (
                bull_rejection
                and bull_confirm
            )
        ):

            score += 5

        valid_zone = (
            near_support
            or abs(
                price - fast1
            )
            <= zone_tolerance
        )

        # ----------------------------------------------------
        # FINAL CALL QUALIFICATION
        # SAME ORIGINAL CONDITIONS
        # ----------------------------------------------------

        if not valid_zone:

            record_rejection(
                asset,
                "CALL: no valid zone",
            )

            return None

        if not bull_rejection:

            record_rejection(
                asset,
                "CALL: no rejection candle",
            )

            return None

        if not bull_confirm:

            record_rejection(
                asset,
                "CALL: candle confirmation failed",
            )

            return None

        if not momentum_bull:

            record_rejection(
                asset,
                "CALL: momentum failed",
            )

            return None

        if room_up_atr < MIN_ROOM_ATR:

            record_rejection(
                asset,
                f"CALL: room {room_up_atr:.2f} < {MIN_ROOM_ATR}",
            )

            return None

        direction = "CALL"

        # ----------------------------------------------------
        # SCORE FILTER
        # ----------------------------------------------------

        if score < MIN_SCORE:

            record_rejection(
                asset,
                f"CALL: score {score} < {MIN_SCORE}",
            )

            return None

    # ========================================================
    # PUT
    # ========================================================

    elif bearish_trend:

        score += 20

        reasons.append(
            "5M bearish trend"
        )

        if bearish_1m_structure:

            score += 10

            reasons.append(
                "1M EMA structure"
            )

        if (
            near_resistance
            or abs(
                price - fast1
            )
            <= zone_tolerance
        ):

            score += 15

            reasons.append(
                "pullback zone"
            )

        if bear_rejection:

            score += 20

            reasons.append(
                "bearish rejection"
            )

        if bear_confirm:

            score += 10

            reasons.append(
                "1M confirmation"
            )

        if momentum_bear:

            score += 5

            reasons.append(
                "bearish momentum"
            )

        if (
            current_rsi
            >= RSI_BEAR_MIN
            and current_rsi
            <= RSI_BEAR_MAX
        ):

            score += 5

            reasons.append(
                "RSI valid"
            )

        if (
            room_down_atr
            >= MIN_ROOM_ATR
        ):

            score += 5

            reasons.append(
                "room available"
            )

        if (
            bearish_engulfing(
                previous,
                current,
            )
            or (
                bear_rejection
                and bear_confirm
            )
        ):

            score += 5

        valid_zone = (
            near_resistance
            or abs(
                price - fast1
            )
            <= zone_tolerance
        )

        # ----------------------------------------------------
        # FINAL PUT QUALIFICATION
        # SAME ORIGINAL CONDITIONS
        # ----------------------------------------------------

        if not valid_zone:

            record_rejection(
                asset,
                "PUT: no valid zone",
            )

            return None

        if not bear_rejection:

            record_rejection(
                asset,
                "PUT: no rejection candle",
            )

            return None

        if not bear_confirm:

            record_rejection(
                asset,
                "PUT: candle confirmation failed",
            )

            return None

        if not momentum_bear:

            record_rejection(
                asset,
                "PUT: momentum failed",
            )

            return None

        if room_down_atr < MIN_ROOM_ATR:

            record_rejection(
                asset,
                f"PUT: room {room_down_atr:.2f} < {MIN_ROOM_ATR}",
            )

            return None

        direction = "PUT"

        # ----------------------------------------------------
        # SCORE FILTER
        # ----------------------------------------------------

        if score < MIN_SCORE:

            record_rejection(
                asset,
                f"PUT: score {score} < {MIN_SCORE}",
            )

            return None

    # --------------------------------------------------------
    # SAFETY
    # --------------------------------------------------------

    if direction is None:

        record_rejection(
            asset,
            "No valid direction",
        )

        return None

    # --------------------------------------------------------
    # COOLDOWN
    # --------------------------------------------------------

    candle_time = current["from"]

    previous_signal_time = (
        last_signal_time.get(
            asset,
            0,
        )
    )

    if previous_signal_time:

        if (
            candle_time
            - previous_signal_time
            < 180
        ):

            record_rejection(
                asset,
                "Signal cooldown",
            )

            return None

    previous_trade_time = (
        last_trade_time.get(
            asset,
            0,
        )
    )

    if previous_trade_time:

        if (
            time.time()
            - previous_trade_time
            < ASSET_LOCK_SECONDS
        ):

            record_rejection(
                asset,
                "Trade lock active",
            )

            return None

    # --------------------------------------------------------
    # SIGNAL ID
    # --------------------------------------------------------

    signal_id = (
        "ZETA2-"
        + asset.replace(
            "-",
            "",
        )
        + "-"
        + direction
        + "-"
        + datetime.now(
            timezone.utc
        ).strftime(
            "%H%M%S"
        )
    )

    last_signal_time[
        asset
    ] = candle_time

    # --------------------------------------------------------
    # SUCCESS DIAGNOSTIC
    # --------------------------------------------------------

    print(
        f"[QUALIFIED] {asset} "
        f"{direction} "
        f"score={score}"
    )

    return {
        "signal_id": signal_id,
        "asset": asset,
        "direction": direction,
        "score": score,
        "price": price,
        "ema20_5m": fast5,
        "ema50_5m": slow5,
        "ema20_1m": fast1,
        "ema50_1m": slow1,
        "atr_1m": current_atr1,
        "adx_5m": current_adx,
        "rsi_1m": current_rsi,
        "room_up_atr": room_up_atr,
        "room_down_atr": room_down_atr,
        "pullback_atr": pullback_atr,
        "reasons": reasons,
        "timestamp": now_utc(),
    }


# ============================================================
# SIGNAL FORMAT
# ============================================================

def format_signal(signal):

    direction = signal["direction"]

    emoji = (
        "🟢"
        if direction == "CALL"
        else "🔴"
    )

    reasons = ", ".join(
        signal["reasons"]
    )

    return (
        f"{emoji} *ZETA V2 SIGNAL*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Asset:* {signal['asset']}\n"
        f"*Direction:* *{direction}*\n"
        f"*Score:* *{signal['score']}/100*\n"
        "*Context:* 5M\n"
        "*Entry:* 1M\n"
        f"*Expiry:* {EXPIRY_MINUTES} minutes\n"
        f"*ADX 5M:* {signal['adx_5m']:.2f}\n"
        f"*RSI 1M:* {signal['rsi_1m']:.2f}\n"
        f"*Pullback:* {signal['pullback_atr']:.2f} ATR\n"
        f"*Room UP:* {signal['room_up_atr']:.2f} ATR\n"
        f"*Room DOWN:* {signal['room_down_atr']:.2f} ATR\n"
        f"*Price:* {signal['price']:.8f}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Confirmations:* {reasons}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Signal ID:* {signal['signal_id']}\n"
        f"*Time:* {signal['timestamp']}\n"
        "🤖 *ZETA V2 — PRACTICE AUTO TRADE*"
    )


# ============================================================
# EXECUTE PRACTICE TRADE
# ============================================================

def execute_demo_trade(signal):

    global total_trades

    if api is None:
        return False

    asset = signal["asset"]

    direction = signal["direction"]

    option_type = (
        "call"
        if direction == "CALL"
        else "put"
    )

    active_id = OP_code.ACTIVES.get(
        asset
    )

    if active_id is None:

        print(
            "[TRADE ERROR] "
            "No active ID:",
            asset,
        )

        send_telegram(
            "🔴 *ZETA V2 TRADE ERROR*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"*Asset:* {asset}\n"
            "*Reason:* No real IQ Option "
            "active ID is mapped."
        )

        return False

    print("\n" + "=" * 70)
    print("[ZETA V2 AUTO EXECUTION]")
    print("Asset:", asset)
    print("Active ID:", active_id)
    print("Direction:", direction)
    print("Score:", signal["score"])
    print("Stake:", STAKE)
    print("Expiry:", EXPIRY_MINUTES)
    print("Account:", BALANCE_MODE)
    print("=" * 70)

    send_telegram(
        "🟡 *ZETA V2 SENDING DEMO ORDER*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Asset:* {asset}\n"
        f"*Active ID:* {active_id}\n"
        f"*Direction:* {direction}\n"
        f"*Score:* {signal['score']}/100\n"
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
            "🔴 *ZETA V2 TRADE ERROR*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"*Asset:* {asset}\n"
            f"*Direction:* {direction}\n"
            f"*Error:* {str(e)[:700]}"
        )

        return False

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

        success = bool(result)

    if not success:

        print(
            "[BUY FAILED]",
            repr(result),
        )

        send_telegram(
            "🔴 *ZETA V2 TRADE REJECTED*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"*Asset:* {asset}\n"
            f"*Direction:* {direction}\n"
            f"*Score:* {signal['score']}/100\n"
            f"*Signal ID:* {signal['signal_id']}\n"
            f"*IQ Option response:* "
            f"{str(result)[:700]}"
        )

        return False

    total_trades += 1

    last_trade_time[
        asset
    ] = time.time()

    if trade_id is None:

        send_telegram(
            "🟡 *ZETA V2 ORDER ACCEPTED*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"*Trade number:* {total_trades}/{TARGET_TRADES}\n"
            f"*Asset:* {asset}\n"
            f"*Direction:* {direction}\n"
            f"*Stake:* ${STAKE:.2f}\n"
            f"*Expiry:* {EXPIRY_MINUTES} minutes\n"
            f"*Signal ID:* {signal['signal_id']}\n"
            "*Trade ID:* NOT RETURNED\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📋 *Track result manually in IQ Option.*"
        )

        return True

    trade_id = str(trade_id)

    print(
        "[BUY SUCCESS]",
        trade_id,
    )

    send_telegram(
        "🚀 *ZETA V2 DEMO TRADE OPENED*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Trade #:* {total_trades}/{TARGET_TRADES}\n"
        f"*Asset:* {asset}\n"
        f"*Direction:* *{direction}*\n"
        f"*Score:* *{signal['score']}/100*\n"
        f"*Stake:* ${STAKE:.2f}\n"
        f"*Expiry:* *{EXPIRY_MINUTES} minutes*\n"
        f"*Trade ID:* {trade_id}\n"
        f"*Signal ID:* {signal['signal_id']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🤖 *PRACTICE / DEMO ONLY*\n"
        "📋 *Track the result manually in IQ Option.*"
    )

    return True


# ============================================================
# TEST REAL OTC CANDLE FEEDS
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
            TF1,
            10,
        )

        if len(candles) >= 5:

            working.append(item)

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
        "\nWORKING FOCUSED OTC CANDLE FEEDS:",
        len(working),
    )

    return working


# ============================================================
# FILTER DISCOVERED OTC TO FOCUSED ASSETS
# ============================================================

def filter_focused_assets(discovered):

    print("\n" + "=" * 70)
    print("FILTERING TO FOCUSED ZETA ASSET GROUP")
    print("=" * 70)

    selected = []

    for item in discovered:

        asset = item["asset"]

        key = normalize_asset_key(
            asset
        )

        if key in FOCUSED_ASSET_KEYS:
            selected.append(item)

    selected_by_key = {
        normalize_asset_key(
            item["asset"]
        ): item
        for item in selected
    }

    ordered = []

    for requested in FOCUSED_ASSETS:

        key = normalize_asset_key(
            requested
        )

        item = selected_by_key.get(
            key
        )

        if item is not None:
            ordered.append(item)

    print("")
    print(
        "FOCUSED ASSETS REQUESTED:",
        len(FOCUSED_ASSETS),
    )

    print(
        "FOCUSED ASSETS DISCOVERED:",
        len(ordered),
    )

    for requested in FOCUSED_ASSETS:

        key = normalize_asset_key(
            requested
        )

        matched = selected_by_key.get(
            key
        )

        if matched:

            print(
                "[FOCUSED OK]",
                requested,
                "->",
                matched["asset"],
                "ID=",
                matched["active_id"],
            )

        else:

            print(
                "[FOCUSED NOT FOUND]",
                requested,
            )

    return ordered


# ============================================================
# DISCOVER + FILTER + TEST OTC
# ============================================================

def refresh_otc_assets():

    global otc_assets
    global last_discovery_time

    raw_data = get_raw_initialization()

    if not raw_data:

        print(
            "[OTC] Initialization returned no data."
        )

        return False

    discovered = (
        discover_otc_from_initialization(
            raw_data
        )
    )

    if not discovered:

        print(
            "[OTC] 0 real OTC assets."
        )

        return False

    focused = filter_focused_assets(
        discovered
    )

    if not focused:

        print(
            "[OTC] None of the focused assets "
            "were found."
        )

        return False

    working = test_candle_access(
        focused
    )

    if working:

        otc_assets = working

        last_discovery_time = time.time()

        print(
            "\n[OTC] FOCUSED FEEDS READY:",
            len(otc_assets),
        )

        return True

    print(
        "[OTC] Focused assets were discovered, "
        "but candle feeds did not respond."
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

    last_connection_check = time.time()

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

    message = (
        "🟡 *ZETA V2 HEARTBEAT*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "*Status:* ONLINE\n"
        f"*Focused OTC feeds:* {len(otc_assets)}\n"
        f"*Demo orders opened:* "
        f"{total_trades}/{TARGET_TRADES}\n"
        f"*Runtime:* {runtime_string()}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Account:* {BALANCE_MODE}\n"
        "*Auto-trading:* ON\n"
        f"*Stake:* ${STAKE:.2f}\n"
        f"*Expiry:* {EXPIRY_MINUTES} minutes\n"
        f"*Minimum score:* {MIN_SCORE}\n"
    )

    if balance is not None:

        message += (
            f"*Demo balance:* "
            f"${safe_float(balance):.2f}\n"
        )

    message += (
        "━━━━━━━━━━━━━━━━━━\n"
        "🎯 *FOCUSED ASSET TEST*\n"
        "COFFEE • USDSGD • USDPLN\n"
        "COSMOS • USDMYR • EURCAD\n"
        "USDTRY • ONDO • EURUSD • AUDUSD\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔎 *REJECTION DIAGNOSTICS*\n"
        f"*Total rejected evaluations:* "
        f"{sum(rejection_counts.values())}\n"
        f"{diagnostic_summary()}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📍 *LATEST BY ASSET*\n"
        f"{latest_asset_diagnostics()}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📋 *Results are tracked manually "
        "in IQ Option.*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🟢 *ZETA V2 SCANNING SELECTED OTC MARKETS*"
    )

    send_telegram(message)


# ============================================================
# CONNECT IQ OPTION
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

    connected, reason = api.connect()

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
# FINISH CHECK
# ============================================================

def target_reached():

    return (
        total_trades
        >= TARGET_TRADES
    )


# ============================================================
# MAIN TRADER
# ============================================================

def run_trader():

    global last_status_time
    global last_discovery_time
    global diagnostic_scan_count

    if not IQ_EMAIL or not IQ_PASSWORD:

        message = (
            "🔴 *ZETA V2*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "IQ_EMAIL or IQ_PASSWORD "
            "is missing from GitHub Secrets."
        )

        print(message)
        send_telegram(message)

        return

    # --------------------------------------------------------
    # CONNECT
    # --------------------------------------------------------

    while not connect_iq():

        send_telegram(
            "🔴 *ZETA V2 CONNECTION FAILED*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Retrying IQ Option connection "
            "in 30 seconds..."
        )

        time.sleep(
            RECONNECT_INTERVAL
        )

    print("\n" + "=" * 70)
    print("🟢 ZETA V2.4 DEMO TRADER ONLINE")
    print("=" * 70)

    send_telegram(
        "🟢 *ZETA V2.4 ONLINE*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "*Connection:* OK\n"
        f"*Account:* {BALANCE_MODE}\n"
        "*Strategy:* High Quality Trend Pullback\n"
        "*Context:* 5M\n"
        "*Entry:* 1M\n"
        f"*Expiry:* {EXPIRY_MINUTES} minutes\n"
        f"*Minimum score:* {MIN_SCORE}\n"
        f"*Stake:* ${STAKE:.2f}\n"
        f"*Target:* {TARGET_TRADES} demo orders\n"
        "*Result tracking:* MANUAL\n"
        "*Auto-trading:* ON\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎯 *FOCUSED ASSET TEST*\n"
        "10 selected OTC assets\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔎 Using real IQ Option OTC initialization...\n"
        "🔎 Rejection diagnostics: ON"
    )

    # --------------------------------------------------------
    # INITIAL OTC DISCOVERY
    # --------------------------------------------------------

    while not otc_assets:

        refresh_success = (
            refresh_otc_assets()
        )

        if refresh_success:

            selected_names = ", ".join(
                item["asset"]
                for item in otc_assets
            )

            send_telegram(
                "🟢 *FOCUSED OTC FEEDS READY*\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"*Working focused feeds:* "
                f"{len(otc_assets)}/"
                f"{len(FOCUSED_ASSETS)}\n"
                "*Data:* REAL IQ OPTION CANDLES\n"
                "*Context:* 5M\n"
                "*Entry:* 1M\n"
                f"*Expiry:* {EXPIRY_MINUTES} minutes\n"
                "*Result tracking:* MANUAL\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"*Assets:* {selected_names}\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "🔎 *Rejection diagnostics:* ON\n"
                "ZETA V2 focused scanning started."
            )

            break

        send_telegram(
            "🟡 *WAITING FOR FOCUSED OTC MARKETS*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "IQ Option connection is active,\n"
            "but the selected assets have not\n"
            "yet produced working candle feeds.\n"
            "Retrying real IQ Option initialization..."
        )

        time.sleep(
            RECONNECT_INTERVAL
        )

    last_status_time = time.time()

    last_scan_cycle = 0

    # --------------------------------------------------------
    # CONTINUOUS LOOP
    # --------------------------------------------------------

    while True:

        try:

            if target_reached():

                send_telegram(
                    "🏁 *ZETA V2 50-TRADE TEST REACHED*\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"*Demo orders opened:* "
                    f"{total_trades}\n"
                    "*Results:* Tracked manually in IQ Option\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "The 50-order Practice test is complete."
                )

                break

            current_time = time.time()

            # ------------------------------------------------
            # CONNECTION
            # ------------------------------------------------

            if not connection_is_alive():

                print(
                    "[CONNECTION] Lost."
                )

                send_telegram(
                    "🔴 *ZETA V2 CONNECTION LOST*\n"
                    "━━━━━━━━━━━━━━━━━━\n"
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
            # OTC REFRESH
            # ------------------------------------------------

            if (
                not otc_assets
                or (
                    current_time
                    - last_discovery_time
                    >= DISCOVERY_INTERVAL
                )
            ):

                print(
                    "\n[OTC] Refreshing "
                    "real IQ Option focused OTC markets..."
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

                last_scan_cycle = current_time

                if not otc_assets:

                    print(
                        "[SCAN] No working focused OTC feeds."
                    )

                else:

                    diagnostic_scan_count += 1

                    print(
                        "\n"
                        + "-" * 70
                    )

                    print(
                        "[ZETA V2 FOCUSED SCAN]",
                        now_utc(),
                    )

                    print(
                        "Focused OTC feeds:",
                        len(otc_assets),
                    )

                    print(
                        "Demo orders opened:",
                        total_trades,
                    )

                    for item in list(
                        otc_assets
                    ):

                        if target_reached():
                            break

                        asset = item["asset"]

                        try:

                            candles_5m = (
                                get_candles_safe(
                                    asset,
                                    TF5,
                                    CANDLE_COUNT_5M,
                                )
                            )

                            if len(
                                candles_5m
                            ) < 80:

                                record_rejection(
                                    asset,
                                    "5M candle data unavailable",
                                )

                                continue

                            candles_1m = (
                                get_candles_safe(
                                    asset,
                                    TF1,
                                    CANDLE_COUNT_1M,
                                )
                            )

                            if len(
                                candles_1m
                            ) < 80:

                                record_rejection(
                                    asset,
                                    "1M candle data unavailable",
                                )

                                continue

                            signal = (
                                evaluate_zeta_v2(
                                    asset,
                                    candles_5m,
                                    candles_1m,
                                )
                            )

                            if signal is None:

                                reason = (
                                    last_rejection_by_asset.get(
                                        asset,
                                        "Unknown rejection",
                                    )
                                )

                                print(
                                    f"[REJECTED] "
                                    f"{asset} -> "
                                    f"{reason}"
                                )

                                continue

                            print(
                                "\n[ZETA V2 SIGNAL]",
                                asset,
                                signal["direction"],
                                "SCORE=",
                                signal["score"],
                            )

                            send_telegram(
                                format_signal(
                                    signal
                                )
                            )

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

                            record_rejection(
                                asset,
                                "Scanner exception",
                            )

                    print(
                        "[ZETA V2 FOCUSED SCAN COMPLETE]",
                        now_utc(),
                    )

                    print(
                        "[DIAGNOSTICS]"
                    )

                    print(
                        diagnostic_summary()
                    )

            # ------------------------------------------------
            # HEARTBEAT
            # ------------------------------------------------

            if (
                time.time()
                - last_status_time
                >= STATUS_INTERVAL
            ):

                last_status_time = time.time()

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

    print("=" * 70)
    print("ZETA V2.4 — IQ OPTION OTC DEMO TRADER")
    print("=" * 70)

    print(
        "Started:",
        now_utc(),
    )

    print(
        "Strategy: High Quality Trend Pullback"
    )

    print(
        "Context: 5M"
    )

    print(
        "Entry: 1M"
    )

    print(
        "Expiry:",
        EXPIRY_MINUTES,
        "minutes"
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
        "Minimum score:",
        MIN_SCORE
    )

    print(
        "Target:",
        TARGET_TRADES,
        "demo orders"
    )

    print(
        "Focused assets:",
        len(FOCUSED_ASSETS)
    )

    for asset in FOCUSED_ASSETS:

        print(
            " -",
            asset,
        )

    print(
        "Result tracking: MANUAL"
    )

    print(
        "Automatic trading: ENABLED"
    )

    print(
        "Rejection diagnostics: ENABLED"
    )

    print("=" * 70)

    try:

        run_trader()

    except Exception as e:

        print(
            "\n[FATAL ERROR]",
            repr(e),
        )

        traceback.print_exc()

        send_telegram(
            "🔴 *ZETA V2 FATAL ERROR*\n"
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
            "\nZETA V2 stopped."
        )


# ============================================================
# IMPORTANT — DO NOT CHANGE THIS
# ============================================================

if __name__ == "__main__":
    main()
