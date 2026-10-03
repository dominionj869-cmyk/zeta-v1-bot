import os
import time
import math
import traceback
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# ZETA V2.4 — HIGH QUALITY TREND PULLBACK
# DEMO / PRACTICE ONLY
#
# Strategy settings and decision logic are unchanged.
# This version fixes:
# 1. Syntax error
# 2. Websocket/candle reconnection handling
# 3. Global connection-state handling
# ============================================================


# =========================
# ACCOUNT / TELEGRAM
# =========================

BALANCE_MODE = "PRACTICE"

IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# =========================
# TRADING SETTINGS
# =========================

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


# =========================
# STRATEGY SETTINGS
# =========================

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


# =========================
# FOCUSED ASSETS
# =========================

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
# GLOBAL STATE
# ============================================================

iq = None

connected = False
last_connection_attempt = 0

working_assets = []

last_heartbeat = time.time()

trade_count = 0
signal_count = 0

asset_last_signal = {}
asset_trade_lock = {}

last_feed_status = {}

# Diagnostic window
diagnostic_total_evaluations = 0
diagnostic_counts = {}
diagnostic_asset_counts = {}
diagnostic_latest = {}

diagnostic_adx_bins = {
    "<15": 0,
    "15-17.99": 0,
    "18-20": 0,
    ">20": 0,
}

diagnostic_zone_bins = {
    "<0.25 ATR": 0,
    "0.25-0.49 ATR": 0,
    "0.50-0.99 ATR": 0,
    ">=1.00 ATR": 0,
}

diagnostic_trend_counts = {
    "bull_trend": 0,
    "bear_trend": 0,
    "no_valid_trend": 0,
}


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(message)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            timeout=15,
        )
    except Exception as e:
        print(f"Telegram error: {e}")

    print(message)


# ============================================================
# STARTUP
# ============================================================

def send_startup():
    telegram(
        "🟢 ZETA V2.4 ONLINE\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Connection: OK\n"
        f"Account: {BALANCE_MODE}\n"
        "Strategy: High Quality Trend Pullback\n"
        "Context: 5M\n"
        "Entry: 1M\n"
        f"Expiry: {EXPIRY_MINUTES} minutes\n"
        f"Minimum score: {MIN_SCORE}\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Target: {TARGET_TRADES} demo orders\n"
        "Result tracking: MANUAL\n"
        "Auto-trading: ON\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎯 FOCUSED ASSET TEST\n"
        f"{len(FOCUSED_ASSETS)} selected OTC assets\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔎 Using real IQ Option OTC initialization...\n"
        "🔎 Deep rejection diagnostics: ON\n"
        "🛡️ Candle reconnect protection: ON"
    )


# ============================================================
# CONNECTION
# ============================================================

def create_connection():
    global iq

    try:
        print("🔌 Creating IQ Option connection...")

        new_iq = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD,
        )

        status, reason = new_iq.connect()

        if status:
            try:
                new_iq.change_balance(
                    BALANCE_MODE
                )
            except Exception as e:
                print(
                    f"Balance mode warning: {e}"
                )

            iq = new_iq

            print(
                "✅ IQ Option connection established."
            )

            try:
                balance = new_iq.get_balance()

                print(
                    f"💰 Balance: {balance:.2f}"
                )

            except Exception:
                pass

            return True

        print(
            f"❌ IQ Option connection failed: "
            f"{reason}"
        )

    except Exception as e:
        print(
            f"❌ Connection exception: {e}"
        )

    return False


def safe_disconnect():
    global iq

    try:
        if iq is not None:
            iq.close()
    except Exception:
        pass


def reconnect(force=False):
    global connected
    global last_connection_attempt

    now = time.time()

    if (
        not force
        and now - last_connection_attempt
        < RECONNECT_INTERVAL
    ):
        return False

    last_connection_attempt = now

    print(
        "🔄 Reconnecting IQ Option..."
    )

    connected = False

    safe_disconnect()

    time.sleep(3)

    for attempt in range(1, 4):

        print(
            f"🔄 Reconnect attempt "
            f"{attempt}/3..."
        )

        if create_connection():

            connected = True

            print(
                "✅ Reconnected successfully."
            )

            try:
                telegram(
                    "🔄 ZETA CONNECTION RECOVERED\n"
                    "IQ Option websocket is connected again."
                )
            except Exception:
                pass

            return True

        time.sleep(5)

    print(
        "❌ All reconnect attempts failed."
    )

    return False


def ensure_connection():
    global connected

    try:

        if iq is None:

            connected = create_connection()

            return connected

        try:
            status = iq.check_connect()
        except Exception:
            status = False

        if status:

            connected = True

            return True

    except Exception:
        pass

    connected = False

    return reconnect(force=True)


# ============================================================
# SAFE CANDLE RETRIEVAL
# ============================================================

def candles_are_valid(
    candles,
    minimum_count=80,
):
    if not candles:
        return False

    if not isinstance(candles, list):
        return False

    if len(candles) < minimum_count:
        return False

    required = [
        "open",
        "close",
        "high",
        "low",
    ]

    for candle in candles[-minimum_count:]:

        if not isinstance(candle, dict):
            return False

        for key in required:

            if key not in candle:
                return False

            try:

                value = float(
                    candle[key]
                )

                if not math.isfinite(value):
                    return False

            except Exception:
                return False

    return True


def fetch_candles(
    asset,
    timeframe,
    count,
):
    global connected

    for attempt in range(1, 5):

        if not ensure_connection():

            print(
                f"⚠️ {asset} TF{timeframe}: "
                f"connection unavailable "
                f"(attempt {attempt}/4)"
            )

            time.sleep(
                min(5 * attempt, 15)
            )

            continue

        try:

            end_time = int(
                time.time()
            )

            candles = iq.get_candles(
                asset,
                timeframe,
                count,
                end_time,
            )

            if candles_are_valid(
                candles,
                minimum_count=80,
            ):
                return candles

            print(
                f"⚠️ {asset} TF{timeframe}: "
                f"invalid/insufficient candle "
                f"data "
                f"({len(candles) if candles else 0})"
            )

        except Exception as e:

            error_text = str(e)

            print(
                f"⚠️ Candle error "
                f"{asset} TF{timeframe} "
                f"attempt {attempt}/4: "
                f"{error_text}"
            )

            connected = False

            try:

                telegram(
                    "⚠️ CANDLE CONNECTION ERROR\n"
                    f"Asset: {asset}\n"
                    f"TF: {timeframe}s\n"
                    f"Attempt: {attempt}/4\n"
                    f"Error: {error_text[:250]}\n"
                    "🔄 Reconnecting..."
                )

            except Exception:
                pass

            reconnect(
                force=True
            )

        time.sleep(
            min(3 * attempt, 10)
        )

    print(
        f"❌ Unable to retrieve candles: "
        f"{asset} TF{timeframe}"
    )

    return None


# ============================================================
# OTC DISCOVERY
# ============================================================

def discover_otc_assets():
    global working_assets

    print(
        "🔎 Discovering real IQ Option "
        "OTC feeds..."
    )

    if not ensure_connection():

        print(
            "❌ Cannot discover OTC assets: "
            "no connection."
        )

        return []

    discovered = set()

    try:

        data = None

        try:

            data = iq.get_all_init_v2()

        except Exception as e:

            print(
                f"get_all_init_v2 warning: {e}"
            )

        if not data:

            try:

                data = iq.get_all_init()

            except Exception as e:

                print(
                    f"get_all_init warning: {e}"
                )

        if data:

            def recursive_find(obj):

                if isinstance(obj, dict):

                    for key, value in obj.items():

                        key_text = str(
                            key
                        ).upper()

                        if "-OTC" in key_text:

                            discovered.add(
                                key_text
                            )

                        recursive_find(
                            value
                        )

                elif isinstance(obj, list):

                    for item in obj:

                        recursive_find(
                            item
                        )

            recursive_find(data)

    except Exception as e:

        print(
            f"❌ OTC discovery error: {e}"
        )

    found_focused = []

    for asset in FOCUSED_ASSETS:

        if asset.upper() in discovered:

            found_focused.append(
                asset
            )

    working_assets = found_focused

    print(
        f"🎯 Focused OTC feeds available: "
        f"{len(working_assets)}/"
        f"{len(FOCUSED_ASSETS)}"
    )

    for asset in FOCUSED_ASSETS:

        if asset in working_assets:

            print(
                f"  ✅ {asset}"
            )

        else:

            print(
                f"  ❌ {asset}"
            )

    return working_assets


# ============================================================
# MATH / INDICATORS
# ============================================================

def closes(candles):
    return [
        float(x["close"])
        for x in candles
    ]


def highs(candles):
    return [
        float(x["high"])
        for x in candles
    ]


def lows(candles):
    return [
        float(x["low"])
        for x in candles
    ]


def opens(candles):
    return [
        float(x["open"])
        for x in candles
    ]


def ema(values, period):

    if len(values) < period:
        return None

    multiplier = 2 / (
        period + 1
    )

    result = (
        sum(values[:period])
        / period
    )

    for value in values[period:]:

        result = (
            (value - result)
            * multiplier
        ) + result

    return result


def ema_series(
    values,
    period,
):

    if len(values) < period:
        return []

    multiplier = 2 / (
        period + 1
    )

    first = (
        sum(values[:period])
        / period
    )

    result = [first]

    previous = first

    for value in values[period:]:

        current = (
            (value - previous)
            * multiplier
        ) + previous

        result.append(current)

        previous = current

    return result


def atr(
    candles,
    period=14,
):

    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(
        1,
        len(candles),
    ):

        high = float(
            candles[i]["high"]
        )

        low = float(
            candles[i]["low"]
        )

        previous_close = float(
            candles[i - 1]["close"]
        )

        tr = max(
            high - low,
            abs(
                high - previous_close
            ),
            abs(
                low - previous_close
            ),
        )

        trs.append(tr)

    if len(trs) < period:
        return None

    value = (
        sum(trs[:period])
        / period
    )

    for tr in trs[period:]:

        value = (
            (
                (value * (period - 1))
                + tr
            )
            / period
        )

    return value


def rsi(
    values,
    period=14,
):

    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(
        1,
        len(values),
    ):

        change = (
            values[i]
            - values[i - 1]
        )

        gains.append(
            max(change, 0)
        )

        losses.append(
            max(-change, 0)
        )

    avg_gain = (
        sum(gains[:period])
        / period
    )

    avg_loss = (
        sum(losses[:period])
        / period
    )

    for i in range(
        period,
        len(gains),
    ):

        avg_gain = (
            (
                (avg_gain * (period - 1))
                + gains[i]
            )
            / period
        )

        avg_loss = (
            (
                (avg_loss * (period - 1))
                + losses[i]
            )
            / period
        )

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100 - (
        100 / (1 + rs)
    )


def adx(
    candles,
    period=14,
):

    if len(candles) < (
        (period * 2) + 5
    ):
        return None

    highs_v = highs(candles)
    lows_v = lows(candles)
    closes_v = closes(candles)

    tr_values = []
    plus_dm = []
    minus_dm = []

    for i in range(
        1,
        len(candles),
    ):

        high = highs_v[i]
        low = lows_v[i]

        prev_high = highs_v[i - 1]
        prev_low = lows_v[i - 1]
        prev_close = closes_v[i - 1]

        tr = max(
            high - low,
            abs(
                high - prev_close
            ),
            abs(
                low - prev_close
            ),
        )

        up_move = (
            high - prev_high
        )

        down_move = (
            prev_low - low
        )

        plus = (
            up_move
            if (
                up_move > down_move
                and up_move > 0
            )
            else 0
        )

        minus = (
            down_move
            if (
                down_move > up_move
                and down_move > 0
            )
            else 0
        )

        tr_values.append(tr)
        plus_dm.append(plus)
        minus_dm.append(minus)

    if len(tr_values) < period:
        return None

    atr_value = sum(
        tr_values[:period]
    )

    plus_value = sum(
        plus_dm[:period]
    )

    minus_value = sum(
        minus_dm[:period]
    )

    dx_values = []

    for i in range(
        period,
        len(tr_values),
    ):

        atr_value = (
            atr_value
            - (atr_value / period)
            + tr_values[i]
        )

        plus_value = (
            plus_value
            - (plus_value / period)
            + plus_dm[i]
        )

        minus_value = (
            minus_value
            - (minus_value / period)
            + minus_dm[i]
        )

        if atr_value == 0:
            continue

        plus_di = (
            100
            * plus_value
            / atr_value
        )

        minus_di = (
            100
            * minus_value
            / atr_value
        )

        denominator = (
            plus_di
            + minus_di
        )

        if denominator == 0:
            continue

        dx = (
            100
            * abs(
                plus_di - minus_di
            )
            / denominator
        )

        dx_values.append(dx)

    if len(dx_values) < period:
        return None

    adx_value = (
        sum(dx_values[:period])
        / period
    )

    for dx in dx_values[period:]:

        adx_value = (
            (
                (adx_value * (period - 1))
                + dx
            )
            / period
        )

    return adx_value


# ============================================================
# CANDLE FUNCTIONS
# ============================================================

def candle_info(candle):

    o = float(
        candle["open"]
    )

    c = float(
        candle["close"]
    )

    h = float(
        candle["high"]
    )

    l = float(
        candle["low"]
    )

    body = abs(c - o)

    full_range = max(
        h - l,
        1e-12,
    )

    upper_wick = (
        h - max(o, c)
    )

    lower_wick = (
        min(o, c) - l
    )

    body_ratio = (
        body / full_range
    )

    bullish = c > o
    bearish = c < o

    return {
        "open": o,
        "close": c,
        "high": h,
        "low": l,
        "body": body,
        "range": full_range,
        "body_ratio": body_ratio,
        "upper_wick": upper_wick,
        "lower_wick": lower_wick,
        "bullish": bullish,
        "bearish": bearish,
    }


def bullish_engulfing(
    previous,
    current,
):

    p = candle_info(previous)
    c = candle_info(current)

    return (
        p["bearish"]
        and c["bullish"]
        and c["open"] <= p["close"]
        and c["close"] >= p["open"]
    )


def bearish_engulfing(
    previous,
    current,
):

    p = candle_info(previous)
    c = candle_info(current)

    return (
        p["bullish"]
        and c["bearish"]
        and c["open"] >= p["close"]
        and c["close"] <= p["open"]
    )


def bullish_rejection(candle):

    x = candle_info(candle)

    return (
        x["bullish"]
        and x["lower_wick"] > x["body"]
    )


def bearish_rejection(candle):

    x = candle_info(candle)

    return (
        x["bearish"]
        and x["upper_wick"] > x["body"]
    )


# ============================================================
# DIAGNOSTICS
# ============================================================

def record_rejection(
    asset,
    reason,
    detail="",
):

    global diagnostic_counts

    diagnostic_counts[reason] = (
        diagnostic_counts.get(
            reason,
            0,
        ) + 1
    )

    if asset not in diagnostic_asset_counts:

        diagnostic_asset_counts[
            asset
        ] = {}

    diagnostic_asset_counts[
        asset
    ][reason] = (
        diagnostic_asset_counts[
            asset
        ].get(reason, 0)
        + 1
    )

    diagnostic_latest[
        asset
    ] = f"{reason}: {detail}"


def record_adx(value):

    if value is None:
        return

    if value < 15:

        diagnostic_adx_bins[
            "<15"
        ] += 1

    elif value < 18:

        diagnostic_adx_bins[
            "15-17.99"
        ] += 1

    elif value <= 20:

        diagnostic_adx_bins[
            "18-20"
        ] += 1

    else:

        diagnostic_adx_bins[
            ">20"
        ] += 1


def record_zone_distance(
    distance,
):

    if distance is None:
        return

    if distance < 0.25:

        diagnostic_zone_bins[
            "<0.25 ATR"
        ] += 1

    elif distance < 0.50:

        diagnostic_zone_bins[
            "0.25-0.49 ATR"
        ] += 1

    elif distance < 1.00:

        diagnostic_zone_bins[
            "0.50-0.99 ATR"
        ] += 1

    else:

        diagnostic_zone_bins[
            ">=1.00 ATR"
        ] += 1


def reset_diagnostic_window():

    global diagnostic_total_evaluations

    diagnostic_total_evaluations = 0

    diagnostic_counts.clear()
    diagnostic_asset_counts.clear()

    diagnostic_adx_bins.update(
        {
            "<15": 0,
            "15-17.99": 0,
            "18-20": 0,
            ">20": 0,
        }
    )

    diagnostic_zone_bins.update(
        {
            "<0.25 ATR": 0,
            "0.25-0.49 ATR": 0,
            "0.50-0.99 ATR": 0,
            ">=1.00 ATR": 0,
        }
    )

    diagnostic_trend_counts.update(
        {
            "bull_trend": 0,
            "bear_trend": 0,
            "no_valid_trend": 0,
        }
    )


def build_diagnostic_text():

    lines = []

    lines.append(
        "🔬 DEEP REJECTION DIAGNOSTICS"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        f"Evaluations: "
        f"{diagnostic_total_evaluations}"
    )

    lines.append("")

    lines.append(
        "🚧 TOP REJECTION GATES"
    )

    if diagnostic_counts:

        ordered = sorted(
            diagnostic_counts.items(),
            key=lambda x: x[1],
            reverse=True,
        )

        for reason, count in ordered[:10]:

            lines.append(
                f"• {reason}: {count}"
            )

    else:

        lines.append(
            "• No rejection recorded"
        )

    lines.append("")

    lines.append(
        "📈 5M TREND DIAGNOSTICS"
    )

    lines.append(
        f"• Bull trend: "
        f"{diagnostic_trend_counts['bull_trend']}"
    )

    lines.append(
        f"• Bear trend: "
        f"{diagnostic_trend_counts['bear_trend']}"
    )

    lines.append(
        f"• No valid trend: "
        f"{diagnostic_trend_counts['no_valid_trend']}"
    )

    lines.append("")

    lines.append(
        "📊 ADX DISTRIBUTION"
    )

    for key, value in (
        diagnostic_adx_bins.items()
    ):

        lines.append(
            f"• {key}: {value}"
        )

    lines.append("")

    lines.append(
        "📍 1M ZONE DISTANCE (ATR)"
    )

    for key, value in (
        diagnostic_zone_bins.items()
    ):

        lines.append(
            f"• {key}: {value}"
        )

    return "\n".join(lines)


def build_asset_diagnostics():

    lines = []

    lines.append(
        "🎯 LATEST ASSET REJECTIONS"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━"
    )

    if not diagnostic_latest:

        lines.append(
            "No asset diagnostics yet."
        )

        return "\n".join(lines)

    for asset in working_assets:

        detail = diagnostic_latest.get(
            asset,
            "No evaluation yet",
        )

        lines.append(
            f"• {asset}: {detail}"
        )

    return "\n".join(lines)


# ============================================================
# STRATEGY EVALUATION
# ============================================================

def evaluate_asset(asset):

    global diagnostic_total_evaluations

    diagnostic_total_evaluations += 1

    # --------------------------------------------------------
    # 5M candles
    # --------------------------------------------------------

    candles5 = fetch_candles(
        asset,
        TF5,
        CANDLE_COUNT_5M,
    )

    if not candles5:

        record_rejection(
            asset,
            "5M candle data unavailable",
            "get_candles failed",
        )

        return None

    if len(candles5) < 80:

        record_rejection(
            asset,
            "not enough 5M candles",
            str(len(candles5)),
        )

        return None

    # --------------------------------------------------------
    # 1M candles
    # --------------------------------------------------------

    candles1 = fetch_candles(
        asset,
        TF1,
        CANDLE_COUNT_1M,
    )

    if not candles1:

        record_rejection(
            asset,
            "1M candle data unavailable",
            "get_candles failed",
        )

        return None

    if len(candles1) < 80:

        record_rejection(
            asset,
            "not enough 1M candles",
            str(len(candles1)),
        )

        return None

    # --------------------------------------------------------
    # 5M indicators
    # --------------------------------------------------------

    close5 = closes(candles5)

    fast5 = ema(
        close5,
        EMA_FAST,
    )

    slow5 = ema(
        close5,
        EMA_SLOW,
    )

    if (
        fast5 is None
        or slow5 is None
    ):

        record_rejection(
            asset,
            "5M indicators unavailable",
        )

        return None

    fast5_series = ema_series(
        close5,
        EMA_FAST,
    )

    slow5_series = ema_series(
        close5,
        EMA_SLOW,
    )

    if (
        len(fast5_series) < 2
        or len(slow5_series) < 2
    ):

        record_rejection(
            asset,
            "5M indicator history unavailable",
        )

        return None

    previous_fast5 = (
        fast5_series[-2]
    )

    previous_slow5 = (
        slow5_series[-2]
    )

    atr5 = atr(
        candles5,
        ATR_PERIOD,
    )

    adx5 = adx(
        candles5,
        ADX_PERIOD,
    )

    # --------------------------------------------------------
    # 5M TREND
    # --------------------------------------------------------

    bull_trend = (
        fast5 > slow5
        and previous_fast5 >= previous_slow5
        and fast5 > previous_fast5
        and slow5 >= previous_slow5
    )

    bear_trend = (
        fast5 < slow5
        and previous_fast5 <= previous_slow5
        and fast5 < previous_fast5
        and slow5 <= previous_slow5
    )

    if bull_trend:

        diagnostic_trend_counts[
            "bull_trend"
        ] += 1

    elif bear_trend:

        diagnostic_trend_counts[
            "bear_trend"
        ] += 1

    else:

        diagnostic_trend_counts[
            "no_valid_trend"
        ] += 1

        record_rejection(
            asset,
            "no valid 5M trend",
            f"EMA20={fast5:.6f} "
            f"EMA50={slow5:.6f}",
        )

        return None

    # --------------------------------------------------------
    # ADX
    # --------------------------------------------------------

    record_adx(adx5)

    if adx5 is None:

        record_rejection(
            asset,
            "ADX unavailable",
        )

        return None

    if adx5 < MIN_ADX:

        record_rejection(
            asset,
            "ADX below minimum",
            f"{adx5:.1f} < {MIN_ADX}",
        )

        return None

    # --------------------------------------------------------
    # 1M indicators
    # --------------------------------------------------------

    close1 = closes(candles1)

    fast1 = ema(
        close1,
        EMA_FAST,
    )

    slow1 = ema(
        close1,
        EMA_SLOW,
    )

    atr1 = atr(
        candles1,
        ATR_PERIOD,
    )

    rsi1 = rsi(
        close1,
        RSI_PERIOD,
    )

    if (
        fast1 is None
        or slow1 is None
        or atr1 is None
        or rsi1 is None
        or atr1 <= 0
    ):

        record_rejection(
            asset,
            "1M indicators unavailable",
        )

        return None

    previous_fast1_series = (
        ema_series(
            close1,
            EMA_FAST,
        )
    )

    if len(
        previous_fast1_series
    ) < 2:

        record_rejection(
            asset,
            "1M EMA history unavailable",
        )

        return None

    previous_fast1 = (
        previous_fast1_series[-2]
    )

    # --------------------------------------------------------
    # Current closed candle
    # --------------------------------------------------------

    current = candles1[-2]
    previous = candles1[-3]

    info = candle_info(current)

    price = info["close"]

    previous_close = float(
        previous["close"]
    )

    previous_high = float(
        previous["high"]
    )

    previous_low = float(
        previous["low"]
    )

    # --------------------------------------------------------
    # Pullback
    # --------------------------------------------------------

    distance_from_fast = abs(
        price - fast1
    )

    pullback_atr = (
        distance_from_fast / atr1
    )

    if pullback_atr < MIN_PULLBACK_ATR:

        record_rejection(
            asset,
            "pullback too small",
            f"{pullback_atr:.2f} ATR",
        )

        return None

    if pullback_atr > MAX_EXTENSION_ATR:

        record_rejection(
            asset,
            "extension too large",
            f"{pullback_atr:.2f} "
            f"> {MAX_EXTENSION_ATR}",
        )

        return None

    if pullback_atr > MAX_PULLBACK_ATR:

        record_rejection(
            asset,
            "pullback too large",
            f"{pullback_atr:.2f} "
            f"> {MAX_PULLBACK_ATR}",
        )

        return None

    # --------------------------------------------------------
    # Support / resistance
    # --------------------------------------------------------

    lookback = 30

    recent = candles1[
        -(lookback + 2):-2
    ]

    if len(recent) < lookback:

        record_rejection(
            asset,
            "insufficient S/R history",
        )

        return None

    support = min(
        float(c["low"])
        for c in recent
    )

    resistance = max(
        float(c["high"])
        for c in recent
    )

    zone_tolerance = (
        atr1
        * ZONE_TOLERANCE_ATR
    )

    distance_support = abs(
        price - support
    )

    distance_resistance = abs(
        resistance - price
    )

    distance_fast = abs(
        price - fast1
    )

    if bull_trend:

        zone_distance = min(
            distance_support,
            distance_fast,
        )

    else:

        zone_distance = min(
            distance_resistance,
            distance_fast,
        )

    record_zone_distance(
        zone_distance / atr1
    )

    near_support = (
        distance_support
        <= zone_tolerance
    )

    near_resistance = (
        distance_resistance
        <= zone_tolerance
    )

    near_fast = (
        distance_fast
        <= zone_tolerance
    )

    if bull_trend:

        valid_zone = (
            near_support
            or near_fast
        )

    else:

        valid_zone = (
            near_resistance
            or near_fast
        )

    if not valid_zone:

        record_rejection(
            asset,
            (
                "CALL: no valid zone"
                if bull_trend
                else "PUT: no valid zone"
            ),
            (
                f"support="
                f"{distance_support / atr1:.2f} ATR "
                f"EMA="
                f"{distance_fast / atr1:.2f} ATR"
                if bull_trend
                else
                f"resistance="
                f"{distance_resistance / atr1:.2f} ATR "
                f"EMA="
                f"{distance_fast / atr1:.2f} ATR"
            ),
        )

        return None

    # --------------------------------------------------------
    # Rejection candle
    # --------------------------------------------------------

    if bull_trend:

        rejection = (
            bullish_rejection(current)
        )

        if not rejection:

            record_rejection(
                asset,
                "CALL: no rejection candle",
            )

            return None

    else:

        rejection = (
            bearish_rejection(current)
        )

        if not rejection:

            record_rejection(
                asset,
                "PUT: no rejection candle",
            )

            return None

    # --------------------------------------------------------
    # 1M structure
    # --------------------------------------------------------

    if bull_trend:

        structure = (
            fast1 >= slow1
            and fast1 >= previous_fast1
        )

        if not structure:

            record_rejection(
                asset,
                "CALL: 1M structure invalid",
            )

            return None

    else:

        structure = (
            fast1 <= slow1
            and fast1 <= previous_fast1
        )

        if not structure:

            record_rejection(
                asset,
                "PUT: 1M structure invalid",
            )

            return None

    # --------------------------------------------------------
    # Candle confirmation
    # --------------------------------------------------------

    body_ratio = info[
        "body_ratio"
    ]

    bullish_confirm = (
        info["bullish"]
        and body_ratio
        >= MIN_CANDLE_BODY_RATIO
        and (
            price > previous_high
            or bullish_engulfing(
                previous,
                current,
            )
            or (
                bullish_rejection(
                    current
                )
                and info["bullish"]
            )
        )
    )

    bearish_confirm = (
        info["bearish"]
        and body_ratio
        >= MIN_CANDLE_BODY_RATIO
        and (
            price < previous_low
            or bearish_engulfing(
                previous,
                current,
            )
            or (
                bearish_rejection(
                    current
                )
                and info["bearish"]
            )
        )
    )

    if (
        bull_trend
        and not bullish_confirm
    ):

        record_rejection(
            asset,
            "CALL: candle confirmation failed",
            f"body={body_ratio:.2f}",
        )

        return None

    if (
        bear_trend
        and not bearish_confirm
    ):

        record_rejection(
            asset,
            "PUT: candle confirmation failed",
            f"body={body_ratio:.2f}",
        )

        return None

    # --------------------------------------------------------
    # Momentum
    # --------------------------------------------------------

    if bull_trend:

        momentum = (
            price > previous_close
        )

    else:

        momentum = (
            price < previous_close
        )

    if not momentum:

        record_rejection(
            asset,
            (
                "CALL: momentum failed"
                if bull_trend
                else "PUT: momentum failed"
            ),
        )

        return None

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    if bull_trend:

        rsi_valid = (
            RSI_BULL_MIN
            <= rsi1
            <= RSI_BULL_MAX
        )

    else:

        rsi_valid = (
            RSI_BEAR_MIN
            <= rsi1
            <= RSI_BEAR_MAX
        )

    # --------------------------------------------------------
    # Room
    # --------------------------------------------------------

    room_up_atr = (
        (resistance - price)
        / atr1
    )

    room_down_atr = (
        (price - support)
        / atr1
    )

    if bull_trend:

        room_atr = room_up_atr

    else:

        room_atr = room_down_atr

    if room_atr < MIN_ROOM_ATR:

        record_rejection(
            asset,
            (
                "CALL: insufficient room"
                if bull_trend
                else "PUT: insufficient room"
            ),
            f"{room_atr:.2f} ATR",
        )

        return None

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

    # Candle confirmation
    score += 10

    # Momentum
    score += 5

    # RSI
    if rsi_valid:
        score += 5

    # Room
    score += 5

    # Engulfing OR rejection + confirmation
    if bull_trend:

        extra_confirm = (
            bullish_engulfing(
                previous,
                current,
            )
            or (
                bullish_rejection(
                    current
                )
                and bullish_confirm
            )
        )

    else:

        extra_confirm = (
            bearish_engulfing(
                previous,
                current,
            )
            or (
                bearish_rejection(
                    current
                )
                and bearish_confirm
            )
        )

    if extra_confirm:
        score += 5

    # --------------------------------------------------------
    # FINAL SCORE
    # --------------------------------------------------------

    if score < MIN_SCORE:

        record_rejection(
            asset,
            "score below minimum",
            f"{score} < {MIN_SCORE}",
        )

        return None

    direction = (
        "CALL"
        if bull_trend
        else "PUT"
    )

    signal = {
        "asset": asset,
        "direction": direction,
        "score": score,
        "adx": adx5,
        "rsi": rsi1,
        "atr": atr1,
        "room_atr": room_atr,
        "pullback_atr": pullback_atr,
        "price": price,
        "timestamp": int(
            time.time()
        ),
    }

    return signal


# ============================================================
# SIGNAL ID
# ============================================================

def make_signal_id(signal):

    now = datetime.now(
        timezone.utc
    ).strftime("%H%M%S")

    return (
        f"{signal['asset'].replace('-OTC', '')}"
        f"-{signal['direction']}"
        f"-{now}"
    )


# ============================================================
# TRADE EXECUTION
# ============================================================

def execute_trade(signal):

    global trade_count
    global signal_count
    global connected

    asset = signal["asset"]

    now = time.time()

    # --------------------------------------------------------
    # Signal cooldown
    # --------------------------------------------------------

    previous_signal = (
        asset_last_signal.get(
            asset,
            0,
        )
    )

    if (
        now - previous_signal
        < 180
    ):
        return False

    # --------------------------------------------------------
    # Trade lock
    # --------------------------------------------------------

    previous_trade = (
        asset_trade_lock.get(
            asset,
            0,
        )
    )

    if (
        now - previous_trade
        < ASSET_LOCK_SECONDS
    ):
        return False

    signal_id = make_signal_id(
        signal
    )

    try:

        if not ensure_connection():

            print(
                f"⚠️ Cannot execute "
                f"{signal_id}: "
                "connection unavailable"
            )

            return False

        action = (
            signal["direction"].lower()
        )

        print(
            "🚨 SIGNAL\n"
            f"ID: {signal_id}\n"
            f"Asset: {asset}\n"
            f"Direction: "
            f"{action.upper()}\n"
            f"Score: {signal['score']}\n"
            f"ADX: {signal['adx']:.1f}\n"
            f"RSI: {signal['rsi']:.1f}\n"
            f"Room: "
            f"{signal['room_atr']:.2f} ATR"
        )

        telegram(
            "🚨 ZETA V2.4 SIGNAL\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"ID: {signal_id}\n"
            f"Asset: {asset}\n"
            f"Direction: "
            f"{action.upper()}\n"
            f"Score: {signal['score']}\n"
            f"ADX: {signal['adx']:.1f}\n"
            f"RSI: {signal['rsi']:.1f}\n"
            f"Room: "
            f"{signal['room_atr']:.2f} ATR\n"
            f"Expiry: {EXPIRY_MINUTES} min\n"
            f"Stake: ${STAKE:.2f}\n"
            "━━━━━━━━━━━━━━━━━━"
        )

        status, order_id = iq.buy(
            STAKE,
            asset,
            action,
            EXPIRY_MINUTES,
        )

        if status:

            trade_count += 1
            signal_count += 1

            asset_last_signal[
                asset
            ] = now

            asset_trade_lock[
                asset
            ] = now

            telegram(
                "✅ DEMO ORDER EXECUTED\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"ID: {signal_id}\n"
                f"Asset: {asset}\n"
                f"Direction: "
                f"{action.upper()}\n"
                f"Order ID: {order_id}\n"
                f"Stake: ${STAKE:.2f}\n"
                f"Expiry: "
                f"{EXPIRY_MINUTES} min\n"
                f"Trades: "
                f"{trade_count}/{TARGET_TRADES}\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Result tracking: MANUAL"
            )

            return True

        telegram(
            "❌ DEMO ORDER FAILED\n"
            f"ID: {signal_id}\n"
            f"Asset: {asset}\n"
            f"Direction: "
            f"{action.upper()}\n"
            f"Response: {order_id}"
        )

    except Exception as e:

        error_text = str(e)

        print(
            f"❌ Trade execution error: "
            f"{error_text}"
        )

        connected = False

        telegram(
            "⚠️ ORDER ERROR\n"
            f"Asset: {asset}\n"
            f"Error: {error_text[:300]}"
        )

    return False


# ============================================================
# FEED STATUS
# ============================================================

def send_feed_status():

    if not working_assets:

        telegram(
            "⚠️ ZETA OTC FEED STATUS\n"
            "No focused OTC feeds currently "
            "available."
        )

        return

    lines = [
        "📡 ZETA OTC FEED STATUS",
        "━━━━━━━━━━━━━━━━━━",
        (
            "Working focused feeds: "
            f"{len(working_assets)}/"
            f"{len(FOCUSED_ASSETS)}"
        ),
        "",
    ]

    for asset in FOCUSED_ASSETS:

        if asset in working_assets:

            lines.append(
                f"✅ {asset}"
            )

        else:

            lines.append(
                f"❌ {asset}"
            )

    telegram(
        "\n".join(lines)
    )


# ============================================================
# HEARTBEAT
# ============================================================

def send_heartbeat():

    try:

        balance = iq.get_balance()

        balance_text = (
            f"${balance:.2f}"
        )

    except Exception:

        balance_text = "Unavailable"

    text = (
        "💓 ZETA V2.4 HEARTBEAT\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Connection: "
        f"{'OK' if connected else 'RECONNECTING'}\n"
        f"Account: {BALANCE_MODE}\n"
        f"Balance: {balance_text}\n"
        f"Working feeds: "
        f"{len(working_assets)}/"
        f"{len(FOCUSED_ASSETS)}\n"
        f"Demo orders: "
        f"{trade_count}/"
        f"{TARGET_TRADES}\n"
        f"Signals: {signal_count}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        + build_diagnostic_text()
        + "\n━━━━━━━━━━━━━━━━━━\n"
        + build_asset_diagnostics()
    )

    telegram(text)


# ============================================================
# MAIN SCANNER LOOP
# ============================================================

def run():

    global connected
    global last_heartbeat
    global working_assets

    # --------------------------------------------------------
    # Initial connection
    # --------------------------------------------------------

    telegram(
        "🔌 ZETA V2.4 starting "
        "IQ Option connection..."
    )

    if not create_connection():

        telegram(
            "🔴 ZETA STARTUP FAILED\n"
            "Could not connect to IQ Option."
        )

        return

    connected = True

    send_startup()

    # --------------------------------------------------------
    # Initial OTC discovery
    # --------------------------------------------------------

    working_assets = (
        discover_otc_assets()
    )

    send_feed_status()

    if not working_assets:

        telegram(
            "⚠️ ZETA STARTED BUT NO "
            "FOCUSED OTC FEEDS ARE "
            "CURRENTLY AVAILABLE.\n"
            "Will continue checking."
        )

    # --------------------------------------------------------
    # Verify candle retrieval
    # --------------------------------------------------------

    candle_verified = False

    for asset in working_assets:

        print(
            f"🧪 Testing candle feed: "
            f"{asset}"
        )

        test_candles = fetch_candles(
            asset,
            TF1,
            100,
        )

        if test_candles:

            candle_verified = True

            print(
                f"✅ Candle feed verified: "
                f"{asset} "
                f"({len(test_candles)} candles)"
            )

            telegram(
                "✅ IQ OPTION CANDLE FEED VERIFIED\n"
                f"Asset: {asset}\n"
                f"1M candles received: "
                f"{len(test_candles)}\n"
                "Scanner entering normal mode."
            )

            break

        else:

            print(
                f"⚠️ Candle verification "
                f"failed: {asset}"
            )

    if not candle_verified:

        telegram(
            "⚠️ ZETA CANDLE FEED NOT VERIFIED\n"
            "The bot will keep reconnecting "
            "and checking instead of "
            "silently hanging."
        )

    # --------------------------------------------------------
    # Main loop
    # --------------------------------------------------------

    last_discovery = time.time()

    while True:

        try:

            # -----------------------------------------------
            # Target reached
            # -----------------------------------------------

            if (
                trade_count
                >= TARGET_TRADES
            ):

                telegram(
                    "🏁 ZETA V2.4 TARGET REACHED\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"Demo orders: "
                    f"{trade_count}\n"
                    f"Target: "
                    f"{TARGET_TRADES}\n"
                    "Bot stopping normally."
                )

                break

            # -----------------------------------------------
            # Keep connection alive
            # -----------------------------------------------

            if not ensure_connection():

                print(
                    "⚠️ Connection unavailable. "
                    "Waiting before retry."
                )

                time.sleep(
                    RECONNECT_INTERVAL
                )

                continue

            # -----------------------------------------------
            # Periodic OTC rediscovery
            # -----------------------------------------------

            now = time.time()

            if (
                now - last_discovery
                >= DISCOVERY_INTERVAL
            ):

                print(
                    "🔎 Running scheduled "
                    "OTC discovery..."
                )

                old_assets = set(
                    working_assets
                )

                working_assets = (
                    discover_otc_assets()
                )

                new_assets = (
                    set(working_assets)
                    - old_assets
                )

                lost_assets = (
                    old_assets
                    - set(working_assets)
                )

                if (
                    new_assets
                    or lost_assets
                ):

                    send_feed_status()

                last_discovery = now

            # -----------------------------------------------
            # Heartbeat
            # -----------------------------------------------

            if (
                now - last_heartbeat
                >= STATUS_INTERVAL
            ):

                send_heartbeat()

                last_heartbeat = now

                reset_diagnostic_window()

            # -----------------------------------------------
            # Scan focused assets
            # -----------------------------------------------

            if not working_assets:

                time.sleep(
                    SCAN_INTERVAL
                )

                continue

            for asset in list(
                working_assets
            ):

                if (
                    trade_count
                    >= TARGET_TRADES
                ):
                    break

                try:

                    signal = (
                        evaluate_asset(
                            asset
                        )
                    )

                    if signal:

                        print(
                            f"🎯 QUALIFIED SIGNAL: "
                            f"{asset} "
                            f"{signal['direction']} "
                            f"score="
                            f"{signal['score']}"
                        )

                        execute_trade(
                            signal
                        )

                except Exception as e:

                    print(
                        f"⚠️ Evaluation error "
                        f"{asset}: {e}"
                    )

                    traceback.print_exc()

                time.sleep(0.5)

            # -----------------------------------------------
            # Scan interval
            # -----------------------------------------------

            time.sleep(
                SCAN_INTERVAL
            )

        except KeyboardInterrupt:

            telegram(
                "🛑 ZETA V2.4 stopped manually."
            )

            break

        except Exception as e:

            print(
                f"🔥 MAIN LOOP ERROR: {e}"
            )

            traceback.print_exc()

            telegram(
                "⚠️ ZETA MAIN LOOP ERROR\n"
                f"{str(e)[:300]}\n"
                "Attempting recovery..."
            )

            connected = False

            time.sleep(5)

            reconnect(
                force=True
            )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        run()

    except Exception as e:

        print(
            f"🔥 FATAL ERROR: {e}"
        )

        traceback.print_exc()

        telegram(
            "🔴 ZETA V2.4 FATAL ERROR\n"
            f"{str(e)[:500]}"
        )
