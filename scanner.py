import os
import time
import traceback
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code

from candlestick_patterns import evaluate_confirmation_context


# ============================================================
# ZETA V1 — DEMO / PRACTICE TRADING BOT
# ============================================================

BALANCE_MODE = "PRACTICE"

STAKE = 1.0
EXPIRY_MINUTES = 2

TIMEFRAME = 60
CANDLE_COUNT = 180

TIMEFRAME_5M = 300
CANDLE_COUNT_5M = 120

MAX_OTC_ASSETS = 60

SCAN_INTERVAL = 5
STATUS_INTERVAL = 300
RECONNECT_INTERVAL = 30


# ============================================================
# 50-TRADE TEST
# ============================================================

TEST_TRADE_TARGET = 50
MAX_ACTIVE_TRADES = 50


# ============================================================
# ZETA TREND SETTINGS
# ============================================================

FAST_LENGTH = 9
SLOW_LENGTH = 21
ATR_PERIOD = 14

NORMALIZED_DISTANCE_MIN = 0.05
USE_TREND_SLOPE = True
SIGNAL_COOLDOWN_BARS = 3


# ============================================================
# ZETA FLIP SETTINGS
# ============================================================

REQUIRE_ZETA_FLIP = False
ZETA_FLIP_MAX_AGE_BARS = 10
ZETA_REQUIRE_CLOSE_BEYOND_FAST_EMA = False
ZETA_REQUIRE_FAST_SLOPE = False
ZETA_CLOSE_VS_SLOW_TOLERANCE_ATR = 0.15


# ============================================================
# CANDLE LIBRARY
# ============================================================

CANDLE_LIBRARY_CONFIG = {}
LOG_ZETA_REJECTIONS = True


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
logged_once = {}
evaluated_candles = {}
ready_at = {}
cache_5m = {}

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

final_report_sent = False


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:

        response = requests.post(
            url,
            data=payload,
            timeout=15,
        )

        return response.ok

    except Exception:

        return False


# ============================================================
# TIME
# ============================================================

def server_now():
    return int(time.time())


def utc_text(timestamp=None):

    if timestamp is None:
        timestamp = server_now()

    return datetime.fromtimestamp(
        timestamp,
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")


# ============================================================
# CONNECTION
# ============================================================

def connect_iq():

    global api

    if not IQ_EMAIL or not IQ_PASSWORD:

        raise RuntimeError(
            "IQ_EMAIL or IQ_PASSWORD is missing."
        )

    print(
        "Connecting to IQ Option...",
        flush=True
    )

    api = IQ_Option(
        IQ_EMAIL,
        IQ_PASSWORD
    )

    connected, reason = api.connect()

    if not connected:

        raise RuntimeError(
            f"IQ Option connection failed: {reason}"
        )

    print(
        "IQ Option connection: OK",
        flush=True
    )

    try:

        api.change_balance(
            BALANCE_MODE
        )

    except Exception:

        pass

    # --------------------------------------------------------
    # REFRESH ACTIVE-ID TABLE
    # --------------------------------------------------------

    try:

        if hasattr(
            api,
            "update_ACTIVES_OPCODE"
        ):

            api.update_ACTIVES_OPCODE()

            print(
                "IQ Option active-ID table refreshed.",
                flush=True
            )

    except Exception as exc:

        print(
            f"Active-ID refresh warning: {exc}",
            flush=True
        )

    return True


# ============================================================
# CONNECTION CHECK
# ============================================================

def ensure_connection():

    global api
    global last_connection_check

    now = server_now()

    if now - last_connection_check < RECONNECT_INTERVAL:
        return True

    last_connection_check = now

    try:

        if api is None:
            return connect_iq()

        if api.check_connect():
            return True

    except Exception:
        pass

    print(
        "Connection lost. Reconnecting...",
        flush=True
    )

    try:

        connected, reason = api.connect()

        if connected:

            print(
                "Reconnected to IQ Option.",
                flush=True
            )

            try:

                api.change_balance(
                    BALANCE_MODE
                )

            except Exception:

                pass

            try:

                if hasattr(
                    api,
                    "update_ACTIVES_OPCODE"
                ):

                    api.update_ACTIVES_OPCODE()

            except Exception:

                pass

            return True

        print(
            f"Reconnect failed: {reason}",
            flush=True
        )

    except Exception as exc:

        print(
            f"Reconnect error: {exc}",
            flush=True
        )

    return False


# ============================================================
# ACTIVE REGISTRATION
# ============================================================

def register_active(
    asset,
    active_id=None
):

    # Use ID supplied directly by IQ Option
    if active_id is not None:

        try:

            active_id = int(
                active_id
            )

            OP_code.ACTIVES[
                asset
            ] = active_id

            return active_id

        except Exception:

            pass

    # Otherwise use existing table
    try:

        active_id = OP_code.ACTIVES.get(
            asset
        )

        if active_id is not None:

            return active_id

    except Exception:

        pass

    return None


# ============================================================
# RECURSIVE OTC EXTRACTION
# ============================================================

def extract_otc_assets(
    obj,
    output=None
):

    if output is None:
        output = set()

    if isinstance(
        obj,
        dict
    ):

        for key, value in obj.items():

            key_text = str(
                key
            )

            if (
                "-OTC"
                in key_text.upper()
                or key_text.upper().endswith("OTC")
            ):

                output.add(
                    key_text
                )

            extract_otc_assets(
                value,
                output
            )

    elif isinstance(
        obj,
        list
    ):

        for item in obj:

            extract_otc_assets(
                item,
                output
            )

    return output


# ============================================================
# OTC DISCOVERY
# ============================================================

def discover_otc_assets():

    global otc_assets
    global last_discovery_time

    print(
        "Discovering IQ Option OTC instruments...",
        flush=True
    )

    # asset name -> real active ID
    discovered = {}

    # ========================================================
    # METHOD 1
    # DIRECT INITIALIZATION DATA
    # ========================================================

    try:

        data = None

        try:

            data = api.get_all_init_v2()

        except Exception as exc:

            print(
                f"get_all_init_v2 warning: {exc}",
                flush=True
            )

        if not data:

            try:

                data = api.get_all_init()

            except Exception as exc:

                print(
                    f"get_all_init warning: {exc}",
                    flush=True
                )

        if data:

            root = data

            if (
                isinstance(
                    data,
                    dict
                )
                and isinstance(
                    data.get("result"),
                    dict
                )
            ):

                root = data[
                    "result"
                ]

            for option_type in (
                "binary",
                "turbo"
            ):

                section = root.get(
                    option_type,
                    {}
                )

                if not isinstance(
                    section,
                    dict
                ):

                    continue

                # Some IQ Option responses contain
                # the active records here.
                actives = section.get(
                    "actives",
                    {}
                )

                if isinstance(
                    actives,
                    dict
                ):

                    for active_id, info in actives.items():

                        if not isinstance(
                            info,
                            dict
                        ):

                            continue

                        raw_name = info.get(
                            "name",
                            ""
                        )

                        if not raw_name:
                            continue

                        asset_name = str(
                            raw_name
                        ).strip()

                        # Remove possible numeric prefix
                        if "." in asset_name:

                            parts = asset_name.split(
                                ".",
                                1
                            )

                            if len(parts) == 2:

                                asset_name = parts[1]

                        asset_name = asset_name.strip()

                        if (
                            "-OTC"
                            not in asset_name.upper()
                        ):

                            continue

                        enabled = info.get(
                            "enabled",
                            True
                        )

                        suspended = info.get(
                            "is_suspended",
                            False
                        )

                        if enabled is False:
                            continue

                        if suspended is True:
                            continue

                        try:

                            numeric_id = int(
                                active_id
                            )

                        except Exception:

                            continue

                        discovered[
                            asset_name
                        ] = numeric_id

            # ------------------------------------------------
            # FALLBACK RECURSIVE DISCOVERY
            # ------------------------------------------------

            if not discovered:

                names = extract_otc_assets(
                    root
                )

                for asset_name in names:

                    try:

                        existing_id = (
                            OP_code.ACTIVES.get(
                                asset_name
                            )
                        )

                    except Exception:

                        existing_id = None

                    if existing_id is not None:

                        discovered[
                            asset_name
                        ] = int(
                            existing_id
                        )

    except Exception as exc:

        print(
            f"OTC initialization discovery error: "
            f"{exc}",
            flush=True
        )

        traceback.print_exc()

    # ========================================================
    # METHOD 2
    # OPEN-TIME DATA
    # ========================================================

    if not discovered:

        try:

            open_time = (
                api.get_all_open_time()
            )

            if isinstance(
                open_time,
                dict
            ):

                for option_type in (
                    "binary",
                    "turbo"
                ):

                    section = open_time.get(
                        option_type,
                        {}
                    )

                    if not isinstance(
                        section,
                        dict
                    ):

                        continue

                    for asset, info in section.items():

                        asset_name = str(
                            asset
                        ).strip()

                        if (
                            "-OTC"
                            not in asset_name.upper()
                        ):

                            continue

                        if not isinstance(
                            info,
                            dict
                        ):

                            continue

                        if info.get(
                            "open"
                        ) is not True:

                            continue

                        try:

                            active_id = (
                                OP_code.ACTIVES.get(
                                    asset_name
                                )
                            )

                        except Exception:

                            active_id = None

                        if active_id is not None:

                            discovered[
                                asset_name
                            ] = int(
                                active_id
                            )

        except Exception as exc:

            print(
                f"Open-time discovery warning: "
                f"{exc}",
                flush=True
            )

    # ========================================================
    # FINAL CLEANUP
    # ========================================================

    cleaned = []

    for asset, active_id in discovered.items():

        if not isinstance(
            asset,
            str
        ):

            continue

        asset = asset.strip()

        if not asset:
            continue

        if (
            "-OTC"
            not in asset.upper()
        ):

            continue

        registered_id = register_active(
            asset,
            active_id
        )

        if registered_id is None:

            print(
                f"Could not register OTC asset: "
                f"{asset}",
                flush=True
            )

            continue

        cleaned.append(
            asset
        )

    cleaned = sorted(
        list(
            set(cleaned)
        )
    )

    if MAX_OTC_ASSETS:

        cleaned = cleaned[
            :MAX_OTC_ASSETS
        ]

    otc_assets = cleaned

    last_discovery_time = server_now()

    # ========================================================
    # REPORT
    # ========================================================

    print(
        f"OTC instruments discovered: "
        f"{len(otc_assets)}",
        flush=True
    )

    if otc_assets:

        print(
            "OTC assets:",
            ", ".join(
                otc_assets[:20]
            ),
            flush=True
        )

        print(
            "OTC active IDs:",
            ", ".join(
                f"{asset}="
                f"{OP_code.ACTIVES.get(asset)}"
                for asset in otc_assets[:10]
            ),
            flush=True
        )

    else:

        print(
            "⚠️ No usable OTC binary/turbo "
            "instruments were discovered.",
            flush=True
        )

    return otc_assets


# ============================================================
# CANDLE NORMALIZATION
# ============================================================

def normalize_candle(candle):

    if not isinstance(
        candle,
        dict
    ):

        return None

    try:

        return {
            "from": int(
                candle.get(
                    "from",
                    candle.get(
                        "at",
                        0
                    )
                )
            ),

            "open": float(
                candle.get(
                    "open",
                    candle.get(
                        "open_price",
                        0
                    )
                )
            ),

            "close": float(
                candle.get(
                    "close",
                    candle.get(
                        "close_price",
                        0
                    )
                )
            ),

            "high": float(
                candle.get(
                    "max",
                    candle.get(
                        "high",
                        candle.get(
                            "max_price",
                            0
                        )
                    )
                )
            ),

            "low": float(
                candle.get(
                    "min",
                    candle.get(
                        "low",
                        candle.get(
                            "min_price",
                            0
                        )
                    )
                )
            ),

            "volume": float(
                candle.get(
                    "volume",
                    0
                )
            ),
        }

    except Exception:

        return None


# ============================================================
# CLOSED CANDLES ONLY
# ============================================================

def get_closed_candles(
    candles,
    timeframe
):

    if not candles:
        return []

    normalized = []

    for candle in candles:

        item = normalize_candle(
            candle
        )

        if item is None:
            continue

        if item["from"] <= 0:
            continue

        if (
            item["high"] <= 0
            or item["low"] <= 0
        ):

            continue

        normalized.append(
            item
        )

    normalized.sort(
        key=lambda x: x["from"]
    )

    now = server_now()

    closed = [
        candle
        for candle in normalized
        if candle["from"] + timeframe <= now
    ]

    unique = []

    seen = set()

    for candle in closed:

        timestamp = candle["from"]

        if timestamp in seen:
            continue

        seen.add(
            timestamp
        )

        unique.append(
            candle
        )

    return unique


# ============================================================
# SAFE CANDLE REQUEST
# ============================================================

def get_candles_safe(
    asset,
    timeframe,
    count
):

    try:

        candles = api.get_candles(
            asset,
            timeframe,
            count,
            server_now()
        )

        if not candles:
            return []

        return get_closed_candles(
            candles,
            timeframe
        )

    except Exception as exc:

        key = (
            f"candles:"
            f"{asset}:"
            f"{timeframe}"
        )

        if key not in logged_once:

            logged_once[key] = True

            print(
                f"Candle error {asset} "
                f"{timeframe}s: {exc}",
                flush=True
            )

        return []


# ============================================================
# 5M CACHE
# ============================================================

def get_5m_candles(asset):

    bucket = (
        server_now()
        // TIMEFRAME_5M
    )

    cached = cache_5m.get(
        asset
    )

    if (
        cached
        and cached[0] == bucket
    ):

        return cached[1]

    candles = get_candles_safe(
        asset,
        TIMEFRAME_5M,
        CANDLE_COUNT_5M
    )

    if candles:

        cache_5m[asset] = (
            bucket,
            candles
        )

    return candles


# ============================================================
# EMA
# ============================================================

def ema(
    values,
    period
):

    if not values:
        return []

    if len(values) < period:

        return [
            None
            for _ in values
        ]

    multiplier = (
        2.0
        / (period + 1)
    )

    result = [
        None
        for _ in values
    ]

    seed = (
        sum(values[:period])
        / period
    )

    result[
        period - 1
    ] = seed

    previous = seed

    for i in range(
        period,
        len(values)
    ):

        current = (
            values[i]
            * multiplier
            + previous
            * (1 - multiplier)
        )

        result[i] = current

        previous = current

    return result


# ============================================================
# WILDER ATR
# ============================================================

def atr_wilder(
    candles,
    period=14
):

    if len(candles) < period + 1:

        return [
            None
            for _ in candles
        ]

    tr = [
        None
        for _ in candles
    ]

    for i in range(
        1,
        len(candles)
    ):

        high = candles[i]["high"]
        low = candles[i]["low"]
        previous_close = (
            candles[i - 1]["close"]
        )

        tr[i] = max(
            high - low,
            abs(
                high
                - previous_close
            ),
            abs(
                low
                - previous_close
            )
        )

    result = [
        None
        for _ in candles
    ]

    first_values = [
        tr[i]
        for i in range(
            1,
            period + 1
        )
        if tr[i] is not None
    ]

    if len(first_values) < period:

        return result

    first_atr = (
        sum(first_values)
        / period
    )

    result[period] = first_atr

    previous_atr = first_atr

    for i in range(
        period + 1,
        len(candles)
    ):

        if tr[i] is None:
            continue

        current_atr = (
            (
                previous_atr
                * (period - 1)
            )
            + tr[i]
        ) / period

        result[i] = current_atr

        previous_atr = current_atr

    return result


# ============================================================
# ZETA STATE
# ============================================================

def calculate_zeta_state(
    candles
):

    if len(candles) < 60:
        return None

    closes = [
        candle["close"]
        for candle in candles
    ]

    ema_fast = ema(
        closes,
        FAST_LENGTH
    )

    ema_slow = ema(
        closes,
        SLOW_LENGTH
    )

    atr_values = atr_wilder(
        candles,
        ATR_PERIOD
    )

    i = len(candles) - 1

    fast = ema_fast[i]
    slow = ema_slow[i]
    current_atr = atr_values[i]

    if (
        fast is None
        or slow is None
        or current_atr is None
        or current_atr <= 0
    ):

        return None

    close = closes[i]

    if i < 3:
        return None

    fast_slope = (
        fast
        - ema_fast[i - 3]
        if ema_fast[i - 3] is not None
        else 0
    )

    slow_slope = (
        slow
        - ema_slow[i - 3]
        if ema_slow[i - 3] is not None
        else 0
    )

    normalized_distance = (
        abs(
            fast - slow
        )
        / current_atr
    )

    bullish = (
        fast > slow
        and fast_slope > 0
        and slow_slope > 0
        and normalized_distance
        >= NORMALIZED_DISTANCE_MIN
    )

    bearish = (
        fast < slow
        and fast_slope < 0
        and slow_slope < 0
        and normalized_distance
        >= NORMALIZED_DISTANCE_MIN
    )

    if bullish:

        direction = "CALL"

    elif bearish:

        direction = "PUT"

    else:

        direction = None

    return {
        "direction": direction,
        "ema_fast": fast,
        "ema_slow": slow,
        "atr": current_atr,
        "fast_slope": fast_slope,
        "slow_slope": slow_slope,
        "normalized_distance": normalized_distance,
        "close": close,
        "candle_time": candles[i]["from"],
    }


# ============================================================
# ZETA ENTRY EVALUATION
# ============================================================

def evaluate_zeta(
    asset,
    candles
):

    global evaluated_candles
    global last_signal_time

    if len(candles) < 60:
        return None

    candle = candles[-1]

    candle_time = candle["from"]

    last_evaluated = (
        evaluated_candles.get(
            asset
        )
    )

    if last_evaluated == candle_time:
        return None

    evaluated_candles[
        asset
    ] = candle_time

    state = calculate_zeta_state(
        candles
    )

    if not state:
        return None

    direction = state[
        "direction"
    ]

    if direction is None:

        if LOG_ZETA_REJECTIONS:

            print(
                f"[NO TRADE] {asset} "
                f"ZETA trend not aligned.",
                flush=True
            )

        return None

    current_atr = state["atr"]

    if direction == "CALL":

        if not (
            state["ema_fast"]
            > state["ema_slow"]
        ):

            return None

        if state["slow_slope"] <= 0:
            return None

        if (
            candle["close"]
            <
            state["ema_slow"]
            - (
                ZETA_CLOSE_VS_SLOW_TOLERANCE_ATR
                * current_atr
            )
        ):

            return None

        if (
            ZETA_REQUIRE_CLOSE_BEYOND_FAST_EMA
            and candle["close"]
            <= state["ema_fast"]
        ):

            return None

        if (
            ZETA_REQUIRE_FAST_SLOPE
            and state["fast_slope"] <= 0
        ):

            return None

    else:

        if not (
            state["ema_fast"]
            < state["ema_slow"]
        ):

            return None

        if state["slow_slope"] >= 0:
            return None

        if (
            candle["close"]
            >
            state["ema_slow"]
            + (
                ZETA_CLOSE_VS_SLOW_TOLERANCE_ATR
                * current_atr
            )
        ):

            return None

        if (
            ZETA_REQUIRE_CLOSE_BEYOND_FAST_EMA
            and candle["close"]
            >= state["ema_fast"]
        ):

            return None

        if (
            ZETA_REQUIRE_FAST_SLOPE
            and state["fast_slope"] >= 0
        ):

            return None

    previous_signal_time = (
        last_signal_time.get(
            asset
        )
    )

    if previous_signal_time is not None:

        bars_since = (
            candle_time
            - previous_signal_time
        ) / TIMEFRAME

        if (
            bars_since
            < SIGNAL_COOLDOWN_BARS
        ):

            return None

    candles_5m = get_5m_candles(
        asset
    )

    if len(candles_5m) < 30:

        if LOG_ZETA_REJECTIONS:

            print(
                f"[NO TRADE] {asset} "
                f"not enough 5M candles.",
                flush=True
            )

        return None

    try:

        decision = (
            evaluate_confirmation_context(
                direction=direction,
                candles_1m=candles,
                candles_5m=candles_5m,
                atr_1m=current_atr,
                now_ts=server_now(),
                cfg=CANDLE_LIBRARY_CONFIG,
                timeframe=TIMEFRAME,
                timeframe_5m=TIMEFRAME_5M,
            )
        )

    except Exception as exc:

        print(
            f"Candle library error "
            f"{asset}: {exc}",
            flush=True
        )

        traceback.print_exc()

        return None

    if not decision:
        return None

    if not decision.get(
        "approved",
        False
    ):

        if LOG_ZETA_REJECTIONS:

            reasons = decision.get(
                "reasons",
                []
            )

            print(
                f"[NO TRADE] {asset} "
                f"{direction} | "
                f"{'; '.join(map(str, reasons))}",
                flush=True
            )

        return None

    patterns = decision.get(
        "patterns",
        []
    )

    if not patterns:

        print(
            f"[NO TRADE] {asset} "
            f"approved without pattern.",
            flush=True
        )

        return None

    quality = float(
        decision.get(
            "quality",
            0
        ) or 0
    )

    if quality < 0.45:

        print(
            f"[NO TRADE] {asset} "
            f"pattern quality too low: "
            f"{quality:.2f}",
            flush=True
        )

        return None

    last_signal_time[
        asset
    ] = candle_time

    signal_id = (
        f"ZETA-"
        f"{asset.replace('/', '')}"
        f"-{direction}-"
        f"{candle_time}"
    )

    return {
        "signal_id": signal_id,
        "asset": asset,
        "direction": direction,
        "price": candle["close"],
        "ema_fast": state["ema_fast"],
        "ema_slow": state["ema_slow"],
        "atr": current_atr,
        "normalized_distance": state[
            "normalized_distance"
        ],
        "candle_time": candle_time,
        "timestamp": server_now(),

        "confirmation": decision.get(
            "confirmation_text",
            ""
        ),

        "confirmation_quality": quality,

        "structure_5m": decision.get(
            "structure_5m",
            ""
        ),

        "alignment_1m": decision.get(
            "alignment_1m",
            ""
        ),

        "zone": decision.get(
            "zone"
        ),

        "zone_source": decision.get(
            "zone_source",
            ""
        ),

        "pullback": decision.get(
            "pullback"
        ),

        "room": decision.get(
            "room"
        ),

        "timing": decision.get(
            "timing"
        ),

        "all_detected": decision.get(
            "all_detected",
            patterns
        ),

        "approval_reason": decision.get(
            "approval_reason",
            ""
        ),
    }


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def format_signal(signal):

    patterns = signal.get(
        "all_detected",
        []
    )

    if isinstance(
        patterns,
        list
    ):

        pattern_text = ", ".join(
            str(x)
            for x in patterns
        )

    else:

        pattern_text = str(
            patterns
        )

    return (
        "🟢 <b>ZETA V1 QUALIFIED SIGNAL</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>Asset:</b> {signal['asset']}\n"
        f"<b>Direction:</b> {signal['direction']}\n"
        f"<b>Expiry:</b> {EXPIRY_MINUTES} minutes\n"
        f"<b>Entry:</b> {signal['price']}\n"
        f"<b>EMA {FAST_LENGTH}:</b> "
        f"{signal['ema_fast']:.8f}\n"
        f"<b>EMA {SLOW_LENGTH}:</b> "
        f"{signal['ema_slow']:.8f}\n"
        f"<b>ATR:</b> "
        f"{signal['atr']:.8f}\n"
        f"<b>Distance:</b> "
        f"{signal['normalized_distance']:.2f} ATR\n"
        f"<b>Patterns:</b> {pattern_text}\n"
        f"<b>Quality:</b> "
        f"{signal['confirmation_quality']:.2f}\n"
        f"<b>5M Structure:</b> "
        f"{signal['structure_5m']}\n"
        f"<b>Zone:</b> "
        f"{signal['zone_source']}\n"
        f"<b>Confirmation:</b> "
        f"{signal['confirmation']}\n"
        f"<b>Signal ID:</b> "
        f"<code>{signal['signal_id']}</code>\n"
        "━━━━━━━━━━━━━━━━━━"
    )


# ============================================================
# TRADE EXECUTION
# ============================================================

def execute_demo_trade(signal):

    global total_trades

    if (
        total_trades
        >= TEST_TRADE_TARGET
    ):

        print(
            "50-trade target reached. "
            "No new trade.",
            flush=True
        )

        return False

    if (
        len(active_trades)
        >= MAX_ACTIVE_TRADES
    ):

        print(
            "Maximum active trade limit reached.",
            flush=True
        )

        return False

    asset = signal["asset"]

    direction = (
        "call"
        if signal["direction"] == "CALL"
        else "put"
    )

    try:

        active_id = (
            OP_code.ACTIVES.get(
                asset
            )
        )

        if active_id is None:

            print(
                f"Active ID unavailable for {asset}",
                flush=True
            )

            return False

    except Exception:

        print(
            f"Could not resolve active ID for {asset}",
            flush=True
        )

        return False

    print(
        f"Sending DEMO order: "
        f"{asset} "
        f"{signal['direction']}",
        flush=True
    )

    try:

        result = api.buy(
            STAKE,
            asset,
            direction,
            EXPIRY_MINUTES
        )

    except Exception as exc:

        print(
            f"Order error {asset}: {exc}",
            flush=True
        )

        send_telegram(
            "🔴 <b>ORDER ERROR</b>\n"
            f"Asset: {asset}\n"
            f"Direction: {signal['direction']}\n"
            f"Error: {exc}"
        )

        return False

    success = False
    trade_id = None

    if isinstance(
        result,
        tuple
    ):

        if len(result) >= 1:
            success = bool(
                result[0]
            )

        if len(result) >= 2:
            trade_id = result[1]

    else:

        success = bool(
            result
        )

    if not success:

        print(
            f"❌ DEMO order rejected: "
            f"{asset} "
            f"{signal['direction']}",
            flush=True
        )

        send_telegram(
            "🔴 <b>DEMO ORDER REJECTED</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Asset: {asset}\n"
            f"Direction: {signal['direction']}\n"
            f"Stake: ${STAKE:.2f}\n"
            f"Time: {utc_text()}\n"
            "━━━━━━━━━━━━━━━━━━"
        )

        return False

    if trade_id is None:

        print(
            f"⚠️ Order accepted but "
            f"no trade ID returned: {asset}",
            flush=True
        )

        send_telegram(
            "⚠️ <b>ORDER ACCEPTED "
            "BUT UNTRACKED</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Asset: {asset}\n"
            f"Direction: {signal['direction']}\n"
            "No trade ID was returned.\n"
            "This trade is NOT counted "
            "toward the 50-trade sample.\n"
            "━━━━━━━━━━━━━━━━━━"
        )

        return False

    total_trades += 1

    active_trades[
        str(trade_id)
    ] = {
        "trade_id": str(trade_id),
        "asset": asset,
        "direction": signal["direction"],
        "stake": STAKE,
        "signal_id": signal["signal_id"],
        "opened_at": server_now(),
        "candle_time": signal["candle_time"],
        "pattern": signal.get(
            "all_detected",
            []
        ),
    }

    print(
        f"🚀 DEMO TRADE OPENED\n"
        f"Asset: {asset}\n"
        f"Direction: {signal['direction']}\n"
        f"Trade ID: {trade_id}\n"
        f"Trade #{total_trades}/"
        f"{TEST_TRADE_TARGET}",
        flush=True
    )

    send_telegram(
        "🚀 <b>ZETA V1 DEMO TRADE OPENED</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>Trade:</b> "
        f"#{total_trades}/"
        f"{TEST_TRADE_TARGET}\n"
        f"<b>Asset:</b> {asset}\n"
        f"<b>Direction:</b> "
        f"{signal['direction']}\n"
        f"<b>Stake:</b> ${STAKE:.2f}\n"
        f"<b>Expiry:</b> "
        f"{EXPIRY_MINUTES} minutes\n"
        f"<b>Trade ID:</b> "
        f"<code>{trade_id}</code>\n"
        f"<b>Signal ID:</b> "
        f"<code>{signal['signal_id']}</code>\n"
        f"<b>Patterns:</b> "
        f"{signal.get('all_detected', [])}\n"
        f"<b>Quality:</b> "
        f"{signal.get('confirmation_quality', 0):.2f}\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    return True


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

    except Exception:

        return None

    if result is None:
        return None

    try:

        profit = float(
            result
        )

    except Exception:

        return None

    if profit > 0:

        outcome = "WIN"
        wins += 1

    elif profit < 0:

        outcome = "LOSS"
        losses += 1

    else:

        outcome = "DRAW"
        draws += 1

    total_profit += profit

    return {
        "outcome": outcome,
        "profit": profit,
    }


# ============================================================
# UPDATE ACTIVE TRADES
# ============================================================

def update_active_trades():

    if not active_trades:
        return

    completed = []

    for (
        trade_id,
        trade
    ) in list(
        active_trades.items()
    ):

        result = check_trade_result(
            trade
        )

        if result is None:
            continue

        completed.append(
            trade_id
        )

        print(
            f"📊 TRADE RESULT | "
            f"{trade['asset']} | "
            f"{trade['direction']} | "
            f"{result['outcome']} | "
            f"{result['profit']:+.2f}",
            flush=True
        )

        send_telegram(
            "📊 <b>ZETA V1 TRADE RESULT</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"<b>Trade:</b> "
            f"{total_completed_trades()}/"
            f"{TEST_TRADE_TARGET}\n"
            f"<b>Asset:</b> "
            f"{trade['asset']}\n"
            f"<b>Direction:</b> "
            f"{trade['direction']}\n"
            f"<b>Result:</b> "
            f"{result['outcome']}\n"
            f"<b>P/L:</b> "
            f"${result['profit']:+.2f}\n"
            f"<b>Wins:</b> {wins}\n"
            f"<b>Losses:</b> {losses}\n"
            f"<b>Draws:</b> {draws}\n"
            f"<b>Net:</b> "
            f"${total_profit:+.2f}\n"
            "━━━━━━━━━━━━━━━━━━"
        )

    for trade_id in completed:

        active_trades.pop(
            trade_id,
            None
        )


# ============================================================
# COMPLETED TRADES
# ============================================================

def total_completed_trades():

    return (
        wins
        + losses
        + draws
    )


# ============================================================
# WIN RATE
# ============================================================

def win_rate():

    completed = (
        total_completed_trades()
    )

    if completed <= 0:
        return 0.0

    return (
        wins
        / completed
        * 100.0
    )


# ============================================================
# HEARTBEAT
# ============================================================

def send_status():

    elapsed = int(
        time.time()
        - start_time
    )

    hours = elapsed // 3600

    minutes = (
        elapsed % 3600
    ) // 60

    seconds = elapsed % 60

    completed = (
        total_completed_trades()
    )

    message = (
        "🟡 <b>ZETA V1 STATUS</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>OTC Assets:</b> "
        f"{len(otc_assets)}\n"
        f"<b>Opened:</b> "
        f"{total_trades}/"
        f"{TEST_TRADE_TARGET}\n"
        f"<b>Completed:</b> "
        f"{completed}/"
        f"{TEST_TRADE_TARGET}\n"
        f"<b>Wins:</b> {wins}\n"
        f"<b>Losses:</b> {losses}\n"
        f"<b>Draws:</b> {draws}\n"
        f"<b>Win Rate:</b> "
        f"{win_rate():.2f}%\n"
        f"<b>Net Demo P/L:</b> "
        f"${total_profit:+.2f}\n"
        f"<b>Active:</b> "
        f"{len(active_trades)}\n"
        f"<b>Runtime:</b> "
        f"{hours:02d}:"
        f"{minutes:02d}:"
        f"{seconds:02d}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>Mode:</b> "
        f"{BALANCE_MODE}\n"
        f"<b>Stake:</b> "
        f"${STAKE:.2f}\n"
        f"<b>Expiry:</b> "
        f"{EXPIRY_MINUTES} minutes\n"
        "<b>5M Trend:</b> ON\n"
        "<b>1M Entry:</b> ON\n"
        "<b>Candle Confirmation:</b> ON\n"
        "<b>Auto-trading:</b> ON\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    send_telegram(
        message
    )


# ============================================================
# FINAL REPORT
# ============================================================

def send_final_report():

    global final_report_sent

    if final_report_sent:
        return

    completed = (
        total_completed_trades()
    )

    if (
        total_trades
        < TEST_TRADE_TARGET
    ):

        return

    if active_trades:
        return

    final_report_sent = True

    message = (
        "🏁 <b>ZETA V1 — 50 TRADE TEST COMPLETE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>Total tracked trades:</b> "
        f"{completed}\n"
        f"<b>Wins:</b> {wins}\n"
        f"<b>Losses:</b> {losses}\n"
        f"<b>Draws:</b> {draws}\n"
        f"<b>Win rate:</b> "
        f"{win_rate():.2f}%\n"
        f"<b>Net demo P/L:</b> "
        f"${total_profit:+.2f}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Strategy:</b> ZETA V1\n"
        "<b>Trend:</b> 5M\n"
        "<b>Entry:</b> 1M\n"
        f"<b>Expiry:</b> "
        f"{EXPIRY_MINUTES} minutes\n"
        "<b>Candle library:</b> ACTIVE\n"
        "<b>Mode:</b> PRACTICE\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "No further trades will be opened "
        "for this test."
    )

    print(
        "\n"
        "========================================\n"
        "50-TRADE TEST COMPLETE\n"
        "========================================",
        flush=True
    )

    print(
        f"Trades: {completed}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Draws: {draws}\n"
        f"Win rate: {win_rate():.2f}%\n"
        f"Net P/L: "
        f"${total_profit:+.2f}",
        flush=True
    )

    print(
        "========================================",
        flush=True
    )

    send_telegram(
        message
    )


# ============================================================
# SCAN ONE ASSET
# ============================================================

def scan_asset(asset):

    if (
        total_trades
        >= TEST_TRADE_TARGET
    ):

        return None

    if (
        server_now()
        < ready_at.get(
            asset,
            0
        )
    ):

        return None

    candles = get_candles_safe(
        asset,
        TIMEFRAME,
        CANDLE_COUNT
    )

    if len(candles) < 60:
        return None

    ready_at[
        asset
    ] = (
        candles[-1]["from"]
        + TIMEFRAME
    )

    signal = evaluate_zeta(
        asset,
        candles
    )

    if not signal:
        return None

    print(
        "\n"
        "========================================\n"
        "QUALIFIED SETUP\n"
        "========================================",
        flush=True
    )

    print(
        f"Asset: {signal['asset']}\n"
        f"Direction: {signal['direction']}\n"
        f"Patterns: {signal['all_detected']}\n"
        f"Quality: "
        f"{signal['confirmation_quality']:.2f}\n"
        f"5M Structure: "
        f"{signal['structure_5m']}\n"
        f"Zone Source: "
        f"{signal['zone_source']}\n"
        f"Reason: "
        f"{signal['approval_reason']}",
        flush=True
    )

    print(
        "========================================",
        flush=True
    )

    send_telegram(
        format_signal(signal)
    )

    execute_demo_trade(
        signal
    )

    return signal


# ============================================================
# NO TRADE STATUS
# ============================================================

def send_no_trade_status():

    completed = (
        total_completed_trades()
    )

    send_telegram(
        "🔵 <b>ZETA V1 SCANNING</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>OTC assets:</b> "
        f"{len(otc_assets)}\n"
        f"<b>Trades opened:</b> "
        f"{total_trades}/"
        f"{TEST_TRADE_TARGET}\n"
        f"<b>Completed:</b> "
        f"{completed}/"
        f"{TEST_TRADE_TARGET}\n"
        f"<b>Wins:</b> {wins}\n"
        f"<b>Losses:</b> {losses}\n"
        f"<b>Win rate:</b> "
        f"{win_rate():.2f}%\n"
        f"<b>Net P/L:</b> "
        f"${total_profit:+.2f}\n"
        f"<b>Active:</b> "
        f"{len(active_trades)}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>5M:</b> Trend + Structure\n"
        "<b>1M:</b> Pullback + S/R + Candle\n"
        "<b>Status:</b> NO TRADE\n"
        "━━━━━━━━━━━━━━━━━━"
    )


# ============================================================
# MAIN LOOP
# ============================================================

def main():

    global last_status_time
    global last_discovery_time

    print(
        "\n"
        "==================================================\n"
        "ZETA V1 BOT STARTING\n"
        "==================================================",
        flush=True
    )

    print(
        f"Mode: {BALANCE_MODE}",
        flush=True
    )

    print(
        f"Stake: ${STAKE:.2f}",
        flush=True
    )

    print(
        f"Expiry: "
        f"{EXPIRY_MINUTES} minutes",
        flush=True
    )

    print(
        "Trend timeframe: 5M",
        flush=True
    )

    print(
        "Entry timeframe: 1M",
        flush=True
    )

    print(
        "Candle confirmation: ACTIVE",
        flush=True
    )

    print(
        "Auto-trading: ON",
        flush=True
    )

    print(
        f"50-trade target: "
        f"{TEST_TRADE_TARGET}",
        flush=True
    )

    print(
        "==================================================",
        flush=True
    )

    # --------------------------------------------------------
    # CONNECT
    # --------------------------------------------------------

    while True:

        try:

            if connect_iq():
                break

        except Exception as exc:

            print(
                f"Initial connection error: "
                f"{exc}",
                flush=True
            )

            time.sleep(
                RECONNECT_INTERVAL
            )

    # --------------------------------------------------------
    # DISCOVER OTC
    # --------------------------------------------------------

    while not otc_assets:

        try:

            discover_otc_assets()

        except Exception as exc:

            print(
                f"Discovery error: "
                f"{exc}",
                flush=True
            )

        if not otc_assets:

            print(
                "No OTC assets discovered. "
                "Retrying...",
                flush=True
            )

            time.sleep(
                RECONNECT_INTERVAL
            )

    send_telegram(
        "🟢 <b>ZETA V1 BOT ONLINE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>OTC assets:</b> "
        f"{len(otc_assets)}\n"
        "<b>Feed:</b> REAL IQ Option OTC\n"
        "<b>Trend:</b> 5M\n"
        "<b>Entry:</b> 1M\n"
        f"<b>Expiry:</b> "
        f"{EXPIRY_MINUTES} minutes\n"
        "<b>Candle library:</b> ACTIVE\n"
        "<b>Mode:</b> PRACTICE\n"
        "<b>Auto-trading:</b> ON\n"
        f"<b>Test target:</b> "
        f"{TEST_TRADE_TARGET} trades\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Scanning for qualified "
        "trend-pullback setups."
    )

    last_status_time = server_now()

    # --------------------------------------------------------
    # CONTINUOUS LOOP
    # --------------------------------------------------------

    while True:

        try:

            if not ensure_connection():

                time.sleep(
                    RECONNECT_INTERVAL
                )

                continue

            # CHECK EXISTING TRADES

            update_active_trades()

            # FINISH AFTER 50 TRADES

            if (
                total_trades
                >= TEST_TRADE_TARGET
                and not active_trades
            ):

                send_final_report()

                print(
                    "50-trade test complete. "
                    "Stopping scanner.",
                    flush=True
                )

                break

            # REFRESH OTC EVERY 30 MINUTES

            if (
                server_now()
                - last_discovery_time
                >= 1800
            ):

                try:

                    discover_otc_assets()

                except Exception as exc:

                    print(
                        f"Refresh discovery error: "
                        f"{exc}",
                        flush=True
                    )

            # OPEN NEW TRADES

            if (
                total_trades
                < TEST_TRADE_TARGET
            ):

                assets_snapshot = list(
                    otc_assets
                )

                for asset in assets_snapshot:

                    if (
                        total_trades
                        >= TEST_TRADE_TARGET
                    ):

                        break

                    try:

                        scan_asset(
                            asset
                        )

                    except Exception as exc:

                        print(
                            f"Scan error "
                            f"{asset}: {exc}",
                            flush=True
                        )

                        if LOG_ZETA_REJECTIONS:

                            traceback.print_exc()

                    time.sleep(
                        0.15
                    )

            # HEARTBEAT

            now = server_now()

            if (
                now
                - last_status_time
                >= STATUS_INTERVAL
            ):

                send_status()

                last_status_time = now

            time.sleep(
                SCAN_INTERVAL
            )

        except KeyboardInterrupt:

            print(
                "Scanner stopped manually.",
                flush=True
            )

            break

        except Exception as exc:

            print(
                f"MAIN LOOP ERROR: "
                f"{exc}",
                flush=True
            )

            traceback.print_exc()

            time.sleep(
                RECONNECT_INTERVAL
            )

    # --------------------------------------------------------
    # FINAL LOCAL OUTPUT
    # --------------------------------------------------------

    print(
        "\n"
        "==================================================",
        flush=True
    )

    print(
        "ZETA V1 TEST FINISHED",
        flush=True
    )

    print(
        f"Opened trades: "
        f"{total_trades}",
        flush=True
    )

    print(
        f"Completed trades: "
        f"{total_completed_trades()}",
        flush=True
    )

    print(
        f"Wins: {wins}",
        flush=True
    )

    print(
        f"Losses: {losses}",
        flush=True
    )

    print(
        f"Draws: {draws}",
        flush=True
    )

    print(
        f"Win rate: "
        f"{win_rate():.2f}%",
        flush=True
    )

    print(
        f"Net demo P/L: "
        f"${total_profit:+.2f}",
        flush=True
    )

    print(
        "==================================================",
        flush=True
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
