import os
import time
import math
import traceback
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# ZETA V2.4
# HIGH QUALITY TREND PULLBACK
# ============================================================
# IMPORTANT:
# This version keeps the trading strategy unchanged.
#
# Added diagnostics only:
#   1. 5M trend diagnostics
#   2. ADX distribution diagnostics
#   3. 1M zone-distance diagnostics
#
# No thresholds have been loosened.
# ============================================================


# ============================================================
# CONNECTION
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ============================================================
# ACCOUNT / TRADING SETTINGS
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


def normalize_asset_key(name):
    if not isinstance(name, str):
        return ""

    return "".join(
        ch for ch in name.upper()
        if ch.isalnum()
    )


FOCUSED_ASSET_KEYS = {
    normalize_asset_key(asset)
    for asset in FOCUSED_ASSETS
}


# ============================================================
# GLOBAL STATE
# ============================================================

iq = None

focused_working_assets = []

last_signal_time = {}
last_trade_time = {}

total_trades = 0
start_time = time.time()

last_heartbeat = 0
last_discovery = 0
last_reconnect = 0


# ============================================================
# DIAGNOSTIC STATE
# ============================================================

diagnostic_counts = {}

diagnostic_asset_counts = {}

diagnostic_latest = {}

diagnostic_measurements = {
    "adx_lt_15": 0,
    "adx_15_17_99": 0,
    "adx_18_20": 0,
    "adx_gt_20": 0,

    "zone_lt_025": 0,
    "zone_025_050": 0,
    "zone_050_100": 0,
    "zone_gt_100": 0,
}

diagnostic_trend_counts = {
    "bull_trend": 0,
    "bear_trend": 0,
    "no_trend": 0,
}

diagnostic_total_evaluations = 0


# ============================================================
# TELEGRAM
# ============================================================

def telegram_send(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    try:
        url = (
            f"https://api.telegram.org/bot"
            f"{TELEGRAM_TOKEN}/sendMessage"
        )

        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
        }

        response = requests.post(
            url,
            json=payload,
            timeout=15,
        )

        return response.ok

    except Exception:
        return False


# ============================================================
# DIAGNOSTIC HELPERS
# ============================================================

def record_rejection(asset, reason, detail=""):
    global diagnostic_counts
    global diagnostic_asset_counts
    global diagnostic_latest

    diagnostic_counts[reason] = (
        diagnostic_counts.get(reason, 0) + 1
    )

    asset_counts = diagnostic_asset_counts.setdefault(
        asset,
        {}
    )

    asset_counts[reason] = (
        asset_counts.get(reason, 0) + 1
    )

    if detail:
        diagnostic_latest[asset] = (
            f"{reason}: {detail}"
        )
    else:
        diagnostic_latest[asset] = reason


def record_adx(adx):
    if adx is None:
        return

    if adx < 15:
        diagnostic_measurements["adx_lt_15"] += 1

    elif adx < 18:
        diagnostic_measurements["adx_15_17_99"] += 1

    elif adx <= 20:
        diagnostic_measurements["adx_18_20"] += 1

    else:
        diagnostic_measurements["adx_gt_20"] += 1


def record_zone_distance(distance):
    if distance is None:
        return

    if distance < 0.25:
        diagnostic_measurements["zone_lt_025"] += 1

    elif distance < 0.50:
        diagnostic_measurements["zone_025_050"] += 1

    elif distance < 1.00:
        diagnostic_measurements["zone_050_100"] += 1

    else:
        diagnostic_measurements["zone_gt_100"] += 1


def reset_diagnostic_window():
    global diagnostic_counts
    global diagnostic_asset_counts
    global diagnostic_latest
    global diagnostic_measurements
    global diagnostic_trend_counts
    global diagnostic_total_evaluations

    diagnostic_counts = {}
    diagnostic_asset_counts = {}
    diagnostic_latest = {}

    diagnostic_measurements = {
        "adx_lt_15": 0,
        "adx_15_17_99": 0,
        "adx_18_20": 0,
        "adx_gt_20": 0,

        "zone_lt_025": 0,
        "zone_025_050": 0,
        "zone_050_100": 0,
        "zone_gt_100": 0,
    }

    diagnostic_trend_counts = {
        "bull_trend": 0,
        "bear_trend": 0,
        "no_trend": 0,
    }

    diagnostic_total_evaluations = 0


# ============================================================
# BASIC NUMERIC HELPERS
# ============================================================

def safe_float(value, default=None):
    try:
        return float(value)
    except Exception:
        return default


def candle_close(candle):
    return safe_float(
        candle.get("close", candle.get("c"))
    )


def candle_open(candle):
    return safe_float(
        candle.get("open", candle.get("o"))
    )


def candle_high(candle):
    return safe_float(
        candle.get("max", candle.get("high", candle.get("h")))
    )


def candle_low(candle):
    return safe_float(
        candle.get("min", candle.get("low", candle.get("l")))
    )


def candle_time(candle):
    return safe_float(
        candle.get("from", candle.get("at", 0)),
        0
    )


# ============================================================
# CANDLE FUNCTIONS
# ============================================================

def candle_body(candle):
    o = candle_open(candle)
    c = candle_close(candle)

    if o is None or c is None:
        return 0.0

    return abs(c - o)


def candle_range(candle):
    h = candle_high(candle)
    l = candle_low(candle)

    if h is None or l is None:
        return 0.0

    return max(0.0, h - l)


def is_bullish(candle):
    o = candle_open(candle)
    c = candle_close(candle)

    return (
        o is not None
        and c is not None
        and c > o
    )


def is_bearish(candle):
    o = candle_open(candle)
    c = candle_close(candle)

    return (
        o is not None
        and c is not None
        and c < o
    )


def body_ratio(candle):
    rng = candle_range(candle)

    if rng <= 0:
        return 0.0

    return candle_body(candle) / rng


def bullish_rejection(candle):
    o = candle_open(candle)
    c = candle_close(candle)
    h = candle_high(candle)
    l = candle_low(candle)

    if None in (o, c, h, l):
        return False

    rng = h - l

    if rng <= 0:
        return False

    body = abs(c - o)
    lower_wick = min(o, c) - l

    return (
        lower_wick >= body
        and c >= l + rng * 0.60
    )


def bearish_rejection(candle):
    o = candle_open(candle)
    c = candle_close(candle)
    h = candle_high(candle)
    l = candle_low(candle)

    if None in (o, c, h, l):
        return False

    rng = h - l

    if rng <= 0:
        return False

    body = abs(c - o)
    upper_wick = h - max(o, c)

    return (
        upper_wick >= body
        and c <= l + rng * 0.40
    )


def bullish_engulfing(previous, current):
    po = candle_open(previous)
    pc = candle_close(previous)

    co = candle_open(current)
    cc = candle_close(current)

    if None in (po, pc, co, cc):
        return False

    return (
        pc < po
        and cc > co
        and co <= pc
        and cc >= po
    )


def bearish_engulfing(previous, current):
    po = candle_open(previous)
    pc = candle_close(previous)

    co = candle_open(current)
    cc = candle_close(current)

    if None in (po, pc, co, cc):
        return False

    return (
        pc > po
        and cc < co
        and co >= pc
        and cc <= po
    )


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2.0 / (period + 1.0)

    result = sum(values[:period]) / period

    for value in values[period:]:
        result = (
            (value - result) * multiplier
            + result
        )

    return result


def ema_series(values, period):
    if len(values) < period:
        return []

    multiplier = 2.0 / (period + 1.0)

    current = sum(values[:period]) / period

    output = [None] * (period - 1)
    output.append(current)

    for value in values[period:]:
        current = (
            (value - current) * multiplier
            + current
        )

        output.append(current)

    return output


def true_ranges(candles):
    output = []

    previous_close = None

    for candle in candles:
        h = candle_high(candle)
        l = candle_low(candle)

        if h is None or l is None:
            continue

        if previous_close is None:
            tr = h - l

        else:
            tr = max(
                h - l,
                abs(h - previous_close),
                abs(l - previous_close),
            )

        output.append(tr)

        c = candle_close(candle)

        if c is not None:
            previous_close = c

    return output


def atr(candles, period=14):
    trs = true_ranges(candles)

    if len(trs) < period:
        return None

    return sum(trs[-period:]) / period


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]

        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (
            (avg_gain * (period - 1) + gains[i])
            / period
        )

        avg_loss = (
            (avg_loss * (period - 1) + losses[i])
            / period
        )

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100.0 - (100.0 / (1.0 + rs))


def adx(candles, period=14):
    if len(candles) < period * 2 + 1:
        return None

    highs = [
        candle_high(c)
        for c in candles
    ]

    lows = [
        candle_low(c)
        for c in candles
    ]

    closes = [
        candle_close(c)
        for c in candles
    ]

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(candles)):
        h = highs[i]
        l = lows[i]
        ph = highs[i - 1]
        pl = lows[i - 1]
        pc = closes[i - 1]

        if None in (h, l, ph, pl, pc):
            continue

        tr = max(
            h - l,
            abs(h - pc),
            abs(l - pc),
        )

        up_move = h - ph
        down_move = pl - l

        plus = (
            up_move
            if up_move > down_move and up_move > 0
            else 0.0
        )

        minus = (
            down_move
            if down_move > up_move and down_move > 0
            else 0.0
        )

        trs.append(tr)
        plus_dm.append(plus)
        minus_dm.append(minus)

    if len(trs) < period * 2:
        return None

    atr_value = sum(trs[:period]) / period
    plus_value = sum(plus_dm[:period]) / period
    minus_value = sum(minus_dm[:period]) / period

    dx_values = []

    for i in range(period, len(trs)):
        atr_value = (
            (atr_value * (period - 1) + trs[i])
            / period
        )

        plus_value = (
            (plus_value * (period - 1) + plus_dm[i])
            / period
        )

        minus_value = (
            (minus_value * (period - 1) + minus_dm[i])
            / period
        )

        if atr_value <= 0:
            continue

        plus_di = 100.0 * plus_value / atr_value
        minus_di = 100.0 * minus_value / atr_value

        denominator = plus_di + minus_di

        if denominator <= 0:
            continue

        dx = (
            100.0
            * abs(plus_di - minus_di)
            / denominator
        )

        dx_values.append(dx)

    if len(dx_values) < period:
        return None

    return sum(dx_values[-period:]) / period


# ============================================================
# SUPPORT / RESISTANCE
# ============================================================

def support_resistance(candles, lookback=30):
    usable = candles[-lookback:]

    highs = [
        candle_high(c)
        for c in usable
        if candle_high(c) is not None
    ]

    lows = [
        candle_low(c)
        for c in usable
        if candle_low(c) is not None
    ]

    if not highs or not lows:
        return None, None

    return min(lows), max(highs)


# ============================================================
# CANDLE NORMALIZATION
# ============================================================

def normalize_candle(candle):
    if not isinstance(candle, dict):
        return None

    return {
        "from": safe_float(
            candle.get(
                "from",
                candle.get("at", 0)
            ),
            0
        ),
        "open": safe_float(
            candle.get(
                "open",
                candle.get("o")
            )
        ),
        "close": safe_float(
            candle.get(
                "close",
                candle.get("c")
            )
        ),
        "max": safe_float(
            candle.get(
                "max",
                candle.get(
                    "high",
                    candle.get("h")
                )
            )
        ),
        "min": safe_float(
            candle.get(
                "min",
                candle.get(
                    "low",
                    candle.get("l")
                )
            )
        ),
    }


def normalize_candles(candles):
    output = []

    if not candles:
        return output

    for candle in candles:
        normalized = normalize_candle(candle)

        if normalized is None:
            continue

        if None in (
            normalized["open"],
            normalized["close"],
            normalized["max"],
            normalized["min"],
        ):
            continue

        output.append(normalized)

    output.sort(
        key=lambda x: x.get("from", 0)
    )

    return output


# ============================================================
# IQ OPTION CONNECTION
# ============================================================

def connect_iq():
    global iq

    try:
        if iq is not None:
            try:
                if iq.check_connect():
                    return True
            except Exception:
                pass

        iq = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD
        )

        check, reason = iq.connect()

        if not check:
            print(
                f"❌ IQ Option connection failed: {reason}"
            )

            return False

        iq.change_balance(BALANCE_MODE)

        print(
            f"🟢 IQ Option connected: {BALANCE_MODE}"
        )

        return True

    except Exception as exc:
        print(
            f"❌ Connection error: {exc}"
        )

        return False


# ============================================================
# OTC DISCOVERY
# ============================================================

def discover_focused_assets():
    global focused_working_assets

    print(
        "🔎 Using real IQ Option OTC initialization..."
    )

    if iq is None:
        return []

    discovered = set()

    try:
        try:
            init_data = iq.get_all_init_v2()

            if isinstance(init_data, dict):
                for section in init_data.values():

                    if not isinstance(section, dict):
                        continue

                    for asset_name in section.keys():
                        discovered.add(
                            normalize_asset_key(asset_name)
                        )

        except Exception:
            pass

        try:
            init_data = iq.get_all_init()

            if isinstance(init_data, dict):
                for section in init_data.values():

                    if not isinstance(section, dict):
                        continue

                    for asset_name in section.keys():
                        discovered.add(
                            normalize_asset_key(asset_name)
                        )

        except Exception:
            pass

    except Exception:
        pass

    # Also verify focused symbols directly through candle access.
    # This does not change the strategy; it only determines which
    # focused feeds are actually usable.
    working = []

    for requested_asset in FOCUSED_ASSETS:

        key = normalize_asset_key(requested_asset)

        if discovered and key not in discovered:
            # The initialization may use a slightly different
            # representation. We still test the requested symbol.
            pass

        try:
            candles = iq.get_candles(
                requested_asset,
                TF1,
                5,
                time.time()
            )

            normalized = normalize_candles(candles)

            if len(normalized) >= 2:
                working.append(requested_asset)

        except Exception:
            continue

    # Preserve requested order.
    focused_working_assets = working

    return working


# ============================================================
# FETCH CANDLES
# ============================================================

def fetch_candles(asset, timeframe, count):
    try:
        candles = iq.get_candles(
            asset,
            timeframe,
            count,
            time.time()
        )

        return normalize_candles(candles)

    except Exception as exc:
        print(
            f"⚠️ Candle error {asset}: {exc}"
        )

        return []


# ============================================================
# DEEP DIAGNOSTIC REPORTING
# ============================================================

def top_rejections(limit=8):
    if not diagnostic_counts:
        return []

    return sorted(
        diagnostic_counts.items(),
        key=lambda x: x[1],
        reverse=True
    )[:limit]


def build_diagnostic_text():
    global diagnostic_total_evaluations

    lines = []

    lines.append(
        f"**Evaluations:** {diagnostic_total_evaluations}"
    )

    lines.append("")
    lines.append("**Main rejection gates:**")

    for reason, count in top_rejections(8):
        lines.append(
            f"• {reason}: {count}"
        )

    lines.append("")
    lines.append("**5M TREND DIAGNOSTICS**")

    lines.append(
        f"• Bull trend reached: "
        f"{diagnostic_trend_counts['bull_trend']}"
    )

    lines.append(
        f"• Bear trend reached: "
        f"{diagnostic_trend_counts['bear_trend']}"
    )

    lines.append(
        f"• No valid trend: "
        f"{diagnostic_trend_counts['no_trend']}"
    )

    lines.append("")
    lines.append("**ADX DISTRIBUTION**")

    lines.append(
        f"• <15: "
        f"{diagnostic_measurements['adx_lt_15']}"
    )

    lines.append(
        f"• 15–17.99: "
        f"{diagnostic_measurements['adx_15_17_99']}"
    )

    lines.append(
        f"• 18–20: "
        f"{diagnostic_measurements['adx_18_20']}"
    )

    lines.append(
        f"• >20: "
        f"{diagnostic_measurements['adx_gt_20']}"
    )

    lines.append("")
    lines.append("**1M ZONE DISTANCE (ATR)**")

    lines.append(
        f"• <0.25 ATR: "
        f"{diagnostic_measurements['zone_lt_025']}"
    )

    lines.append(
        f"• 0.25–0.49 ATR: "
        f"{diagnostic_measurements['zone_025_050']}"
    )

    lines.append(
        f"• 0.50–0.99 ATR: "
        f"{diagnostic_measurements['zone_050_100']}"
    )

    lines.append(
        f"• ≥1.00 ATR: "
        f"{diagnostic_measurements['zone_gt_100']}"
    )

    return "\n".join(lines)


# ============================================================
# ASSET DIAGNOSTICS
# ============================================================

def build_asset_diagnostics():
    lines = []

    for asset in focused_working_assets:

        latest = diagnostic_latest.get(
            asset,
            "No evaluation yet"
        )

        lines.append(
            f"{asset}: {latest}"
        )

    return "\n".join(lines)


# ============================================================
# HEARTBEAT
# ============================================================

def send_heartbeat():
    global last_heartbeat

    runtime_seconds = int(
        time.time() - start_time
    )

    hours = runtime_seconds // 3600
    minutes = (
        runtime_seconds % 3600
    ) // 60

    try:
        balance = iq.get_balance()
    except Exception:
        balance = 0.0

    diagnostics = build_diagnostic_text()
    asset_diagnostics = build_asset_diagnostics()

    message = (
        "🟡 **ZETA V2 HEARTBEAT**\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "**Status:** ONLINE\n"
        f"**Focused OTC feeds:** "
        f"{len(focused_working_assets)}\n"
        f"**Demo orders opened:** "
        f"{total_trades}/{TARGET_TRADES}\n"
        f"**Runtime:** {hours}h {minutes}m\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "**Account:** PRACTICE\n"
        "**Auto-trading:** ON\n"
        f"**Stake:** ${STAKE:.2f}\n"
        f"**Expiry:** {EXPIRY_MINUTES} minutes\n"
        f"**Minimum score:** {MIN_SCORE}\n"
        f"**Demo balance:** ${balance:.2f}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎯 **FOCUSED ASSET TEST**\n"
        "COFFEE • USDSGD • USDPLN\n"
        "COSMOS • USDMYR • EURCAD\n"
        "USDTRY • ONDO • EURUSD • AUDUSD\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔎 **DEEP REJECTION DIAGNOSTICS**\n"
        f"{diagnostics}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📍 **LATEST BY ASSET**\n"
        f"{asset_diagnostics}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📋 **Results are tracked manually in IQ Option.**\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🟢 **ZETA V2 SCANNING SELECTED OTC MARKETS**"
    )

    telegram_send(message)

    last_heartbeat = time.time()


# ============================================================
# STRATEGY
# ============================================================

def evaluate_zeta_v2(
    asset,
    candles_5m,
    candles_1m
):
    global diagnostic_total_evaluations

    diagnostic_total_evaluations += 1

    # --------------------------------------------------------
    # DATA CHECKS
    # --------------------------------------------------------

    if len(candles_5m) < 80:
        record_rejection(
            asset,
            "Insufficient 5M data",
            f"{len(candles_5m)} candles"
        )
        return None

    if len(candles_1m) < 80:
        record_rejection(
            asset,
            "Insufficient 1M data",
            f"{len(candles_1m)} candles"
        )
        return None

    # --------------------------------------------------------
    # 5M INDICATORS
    # --------------------------------------------------------

    closes5 = [
        candle_close(c)
        for c in candles_5m
    ]

    if any(v is None for v in closes5):
        record_rejection(
            asset,
            "Invalid 5M data"
        )
        return None

    ema_fast5_series = ema_series(
        closes5,
        EMA_FAST
    )

    ema_slow5_series = ema_series(
        closes5,
        EMA_SLOW
    )

    if len(ema_fast5_series) < 2:
        record_rejection(
            asset,
            "5M EMA unavailable"
        )
        return None

    if len(ema_slow5_series) < 2:
        record_rejection(
            asset,
            "5M EMA unavailable"
        )
        return None

    fast5 = ema_fast5_series[-1]
    previous_fast5 = ema_fast5_series[-2]

    slow5 = ema_slow5_series[-1]
    previous_slow5 = ema_slow5_series[-2]

    atr5 = atr(
        candles_5m,
        ATR_PERIOD
    )

    adx5 = adx(
        candles_5m,
        ADX_PERIOD
    )

    if any(
        value is None
        for value in (
            fast5,
            previous_fast5,
            slow5,
            previous_slow5,
            atr5,
            adx5,
        )
    ):
        record_rejection(
            asset,
            "5M indicator unavailable"
        )
        return None

    # --------------------------------------------------------
    # 5M TREND
    # EXACT ORIGINAL TREND LOGIC
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
        diagnostic_trend_counts["bull_trend"] += 1

    elif bear_trend:
        diagnostic_trend_counts["bear_trend"] += 1

    else:
        diagnostic_trend_counts["no_trend"] += 1

        trend_gap = 0.0

        if slow5:
            trend_gap = (
                abs(fast5 - slow5)
                / abs(slow5)
                * 100.0
            )

        record_rejection(
            asset,
            "No valid 5M trend",
            f"EMA20={fast5:.6f} EMA50={slow5:.6f} "
            f"gap={trend_gap:.3f}%"
        )

        return None

    # --------------------------------------------------------
    # ADX
    # --------------------------------------------------------

    record_adx(adx5)

    if adx5 < MIN_ADX:
        record_rejection(
            asset,
            "ADX below 18",
            f"{adx5:.1f} < {MIN_ADX}"
        )
        return None

    # --------------------------------------------------------
    # 1M INDICATORS
    # --------------------------------------------------------

    closes1 = [
        candle_close(c)
        for c in candles_1m
    ]

    if any(v is None for v in closes1):
        record_rejection(
            asset,
            "Invalid 1M data"
        )
        return None

    ema_fast1_series = ema_series(
        closes1,
        EMA_FAST
    )

    ema_slow1_series = ema_series(
        closes1,
        EMA_SLOW
    )

    if len(ema_fast1_series) < 2:
        record_rejection(
            asset,
            "1M EMA unavailable"
        )
        return None

    if len(ema_slow1_series) < 2:
        record_rejection(
            asset,
            "1M EMA unavailable"
        )
        return None

    fast1 = ema_fast1_series[-1]
    previous_fast1 = ema_fast1_series[-2]

    slow1 = ema_slow1_series[-1]
    previous_slow1 = ema_slow1_series[-2]

    atr1 = atr(
        candles_1m,
        ATR_PERIOD
    )

    rsi1 = rsi(
        closes1,
        RSI_PERIOD
    )

    if any(
        value is None
        for value in (
            fast1,
            previous_fast1,
            slow1,
            previous_slow1,
            atr1,
            rsi1,
        )
    ):
        record_rejection(
            asset,
            "1M indicator unavailable"
        )
        return None

    if atr1 <= 0:
        record_rejection(
            asset,
            "Invalid 1M ATR"
        )
        return None

    # --------------------------------------------------------
    # CURRENT / PREVIOUS CANDLE
    # --------------------------------------------------------

    current = candles_1m[-1]
    previous = candles_1m[-2]

    price = candle_close(current)

    if price is None:
        record_rejection(
            asset,
            "Invalid current price"
        )
        return None

    # --------------------------------------------------------
    # PULLBACK / EXTENSION
    # EXACT ORIGINAL LIMITS
    # --------------------------------------------------------

    distance_from_fast = abs(
        price - fast1
    )

    pullback_atr = (
        distance_from_fast / atr1
    )

    extension_from_fast = (
        distance_from_fast / atr1
    )

    if pullback_atr < MIN_PULLBACK_ATR:
        record_rejection(
            asset,
            "Pullback too small",
            f"{pullback_atr:.2f} < {MIN_PULLBACK_ATR}"
        )
        return None

    if extension_from_fast > MAX_EXTENSION_ATR:
        record_rejection(
            asset,
            "Extension too high",
            f"{extension_from_fast:.2f} > {MAX_EXTENSION_ATR}"
        )
        return None

    if pullback_atr > MAX_PULLBACK_ATR:
        record_rejection(
            asset,
            "Pullback too large",
            f"{pullback_atr:.2f} > {MAX_PULLBACK_ATR}"
        )
        return None

    # --------------------------------------------------------
    # SUPPORT / RESISTANCE
    # --------------------------------------------------------

    support, resistance = support_resistance(
        candles_1m,
        lookback=30
    )

    if support is None or resistance is None:
        record_rejection(
            asset,
            "Support/resistance unavailable"
        )
        return None

    zone_tolerance = (
        atr1 * ZONE_TOLERANCE_ATR
    )

    support_distance_atr = (
        abs(price - support) / atr1
    )

    resistance_distance_atr = (
        abs(price - resistance) / atr1
    )

    # Diagnostic measurement:
    # For the active direction we measure distance to the
    # relevant zone before applying the exact existing zone test.

    if bull_trend:
        record_zone_distance(
            support_distance_atr
        )

    elif bear_trend:
        record_zone_distance(
            resistance_distance_atr
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
    # CANDLE CONDITIONS
    # --------------------------------------------------------

    bull_reject = bullish_rejection(
        current
    )

    bear_reject = bearish_rejection(
        current
    )

    bull_structure = (
        fast1 >= slow1
        and fast1 >= previous_fast1
    )

    bear_structure = (
        fast1 <= slow1
        and fast1 <= previous_fast1
    )

    bull_confirm = (
        is_bullish(current)
        and body_ratio(current)
        >= MIN_CANDLE_BODY_RATIO
        and (
            candle_close(current)
            > candle_high(previous)
            or bullish_engulfing(
                previous,
                current
            )
            or (
                bull_reject
                and candle_close(current)
                > candle_open(current)
            )
        )
    )

    bear_confirm = (
        is_bearish(current)
        and body_ratio(current)
        >= MIN_CANDLE_BODY_RATIO
        and (
            candle_close(current)
            < candle_low(previous)
            or bearish_engulfing(
                previous,
                current
            )
            or (
                bear_reject
                and candle_close(current)
                < candle_open(current)
            )
        )
    )

    bullish_momentum = (
        candle_close(current)
        > candle_close(previous)
    )

    bearish_momentum = (
        candle_close(current)
        < candle_close(previous)
    )

    # --------------------------------------------------------
    # ROOM
    # --------------------------------------------------------

    room_up_atr = (
        resistance - price
    ) / atr1

    room_down_atr = (
        price - support
    ) / atr1

    # --------------------------------------------------------
    # SCORE
    # EXACT ORIGINAL SCORE LOGIC
    # --------------------------------------------------------

    score = 0
    direction = None

    if bull_trend:

        score += 20

        if bull_structure:
            score += 10

        if (
            near_support
            or abs(price - fast1)
            <= zone_tolerance
        ):
            score += 15

        if bull_reject:
            score += 20

        if bull_confirm:
            score += 10

        if bullish_momentum:
            score += 5

        if (
            RSI_BULL_MIN
            <= rsi1
            <= RSI_BULL_MAX
        ):
            score += 5

        if room_up_atr >= MIN_ROOM_ATR:
            score += 5

        if (
            bullish_engulfing(
                previous,
                current
            )
            or (
                bull_reject
                and bull_confirm
            )
        ):
            score += 5

        valid_zone = (
            near_support
            or abs(price - fast1)
            <= zone_tolerance
        )

        if not valid_zone:
            record_rejection(
                asset,
                "CALL: no valid zone",
                f"support distance={support_distance_atr:.2f} ATR"
            )
            return None

        if not bull_reject:
            record_rejection(
                asset,
                "CALL: no rejection candle"
            )
            return None

        if not bull_confirm:
            record_rejection(
                asset,
                "CALL: candle confirmation failed"
            )
            return None

        if not bullish_momentum:
            record_rejection(
                asset,
                "CALL: momentum failed"
            )
            return None

        if room_up_atr < MIN_ROOM_ATR:
            record_rejection(
                asset,
                "CALL: insufficient room",
                f"{room_up_atr:.2f} ATR < {MIN_ROOM_ATR}"
            )
            return None

        if not (
            RSI_BULL_MIN
            <= rsi1
            <= RSI_BULL_MAX
        ):
            record_rejection(
                asset,
                "CALL: RSI failed",
                f"{rsi1:.1f}"
            )
            return None

        direction = "call"

    elif bear_trend:

        score += 20

        if bear_structure:
            score += 10

        if (
            near_resistance
            or abs(price - fast1)
            <= zone_tolerance
        ):
            score += 15

        if bear_reject:
            score += 20

        if bear_confirm:
            score += 10

        if bearish_momentum:
            score += 5

        if (
            RSI_BEAR_MIN
            <= rsi1
            <= RSI_BEAR_MAX
        ):
            score += 5

        if room_down_atr >= MIN_ROOM_ATR:
            score += 5

        if (
            bearish_engulfing(
                previous,
                current
            )
            or (
                bear_reject
                and bear_confirm
            )
        ):
            score += 5

        valid_zone = (
            near_resistance
            or abs(price - fast1)
            <= zone_tolerance
        )

        if not valid_zone:
            record_rejection(
                asset,
                "PUT: no valid zone",
                f"resistance distance={resistance_distance_atr:.2f} ATR"
            )
            return None

        if not bear_reject:
            record_rejection(
                asset,
                "PUT: no rejection candle"
            )
            return None

        if not bear_confirm:
            record_rejection(
                asset,
                "PUT: candle confirmation failed"
            )
            return None

        if not bearish_momentum:
            record_rejection(
                asset,
                "PUT: momentum failed"
            )
            return None

        if room_down_atr < MIN_ROOM_ATR:
            record_rejection(
                asset,
                "PUT: insufficient room",
                f"{room_down_atr:.2f} ATR < {MIN_ROOM_ATR}"
            )
            return None

        if not (
            RSI_BEAR_MIN
            <= rsi1
            <= RSI_BEAR_MAX
        ):
            record_rejection(
                asset,
                "PUT: RSI failed",
                f"{rsi1:.1f}"
            )
            return None

        direction = "put"

    # --------------------------------------------------------
    # FINAL SCORE
    # --------------------------------------------------------

    if direction is None:
        record_rejection(
            asset,
            "No direction"
        )
        return None

    if score < MIN_SCORE:
        record_rejection(
            asset,
            "Score below minimum",
            f"{score} < {MIN_SCORE}"
        )
        return None

    # --------------------------------------------------------
    # SIGNAL COOLDOWN
    # --------------------------------------------------------

    now = time.time()

    previous_signal = last_signal_time.get(
        asset,
        0
    )

    if now - previous_signal < 180:
        record_rejection(
            asset,
            "Signal cooldown"
        )
        return None

    # --------------------------------------------------------
    # TRADE LOCK
    # --------------------------------------------------------

    previous_trade = last_trade_time.get(
        asset,
        0
    )

    if now - previous_trade < ASSET_LOCK_SECONDS:
        record_rejection(
            asset,
            "Trade lock"
        )
        return None

    # --------------------------------------------------------
    # SIGNAL PASSED
    # --------------------------------------------------------

    last_signal_time[asset] = now

    signal_id = (
        f"{normalize_asset_key(asset)}-"
        f"{direction.upper()}-"
        f"{int(now)}"
    )

    return {
        "signal_id": signal_id,
        "asset": asset,
        "direction": direction,
        "score": score,
        "rsi": rsi1,
        "adx": adx5,
        "pullback_atr": pullback_atr,
        "room_atr": (
            room_up_atr
            if direction == "call"
            else room_down_atr
        ),
        "price": price,
        "timestamp": now,
    }


# ============================================================
# EXECUTE DEMO TRADE
# ============================================================

def execute_trade(signal):
    global total_trades

    asset = signal["asset"]
    direction = signal["direction"]

    try:
        action = (
            "call"
            if direction == "call"
            else "put"
        )

        check, order_id = iq.buy(
            STAKE,
            asset,
            action,
            EXPIRY_MINUTES
        )

        if not check:
            telegram_send(
                "🔴 **TRADE FAILED**\n"
                f"Asset: `{asset}`\n"
                f"Direction: `{direction.upper()}`\n"
                f"Score: `{signal['score']}`"
            )

            return False

        total_trades += 1

        last_trade_time[asset] = time.time()

        telegram_send(
            "🟢 **ZETA V2 DEMO TRADE OPENED**\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"**Asset:** {asset}\n"
            f"**Direction:** {direction.upper()}\n"
            f"**Score:** {signal['score']}\n"
            f"**ADX:** {signal['adx']:.1f}\n"
            f"**RSI:** {signal['rsi']:.1f}\n"
            f"**Pullback:** "
            f"{signal['pullback_atr']:.2f} ATR\n"
            f"**Room:** "
            f"{signal['room_atr']:.2f} ATR\n"
            f"**Stake:** ${STAKE:.2f}\n"
            f"**Expiry:** {EXPIRY_MINUTES} minutes\n"
            f"**Signal ID:** `{signal['signal_id']}`\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📋 Result tracking: MANUAL"
        )

        return True

    except Exception as exc:
        print(
            f"❌ Trade execution error: {exc}"
        )

        return False


# ============================================================
# STARTUP MESSAGE
# ============================================================

def send_startup():
    message = (
        "🟢 **ZETA V2.4 ONLINE**\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "**Connection:** OK\n"
        "**Account:** PRACTICE\n"
        "**Strategy:** High Quality Trend Pullback\n"
        "**Context:** 5M\n"
        "**Entry:** 1M\n"
        "**Expiry:** 2 minutes\n"
        "**Minimum score:** 80\n"
        "**Stake:** $1.00\n"
        "**Target:** 50 demo orders\n"
        "**Result tracking:** MANUAL\n"
        "**Auto-trading:** ON\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎯 **FOCUSED ASSET TEST**\n"
        "10 selected OTC assets\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔎 Using real IQ Option OTC initialization...\n"
        "🔎 Deep rejection diagnostics: ON"
    )

    telegram_send(message)


def send_feed_status():
    assets = (
        ", ".join(focused_working_assets)
        if focused_working_assets
        else "NONE"
    )

    message = (
        "🟢 **FOCUSED OTC FEEDS READY**\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"**Working focused feeds:** "
        f"{len(focused_working_assets)}/10\n"
        "**Data:** REAL IQ OPTION CANDLES\n"
        "**Context:** 5M\n"
        "**Entry:** 1M\n"
        "**Expiry:** 2 minutes\n"
        "**Result tracking:** MANUAL\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"**Assets:** {assets}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔎 **Deep diagnostics:** ON"
    )

    telegram_send(message)


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    global last_discovery
    global last_reconnect

    if not IQ_EMAIL or not IQ_PASSWORD:
        telegram_send(
            "🔴 **ZETA V2 ERROR**\n"
            "IQ Option credentials are missing."
        )

        return

    if not connect_iq():
        telegram_send(
            "🔴 **ZETA V2 ERROR**\n"
            "Could not connect to IQ Option."
        )

        return

    send_startup()

    working = discover_focused_assets()

    if not working:
        telegram_send(
            "🔴 **NO FOCUSED OTC FEEDS FOUND**\n"
            "The bot will keep trying to reconnect."
        )

    else:
        send_feed_status()

    last_discovery = time.time()
    last_reconnect = time.time()

    while True:

        try:

            # ------------------------------------------------
            # TARGET CHECK
            # ------------------------------------------------

            if total_trades >= TARGET_TRADES:

                telegram_send(
                    "🏁 **ZETA V2 TARGET REACHED**\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"**Demo orders:** "
                    f"{total_trades}/{TARGET_TRADES}\n"
                    "**Bot status:** STOPPED\n"
                    "📋 Results remain manually tracked."
                )

                break

            # ------------------------------------------------
            # CONNECTION CHECK
            # ------------------------------------------------

            try:
                connected = iq.check_connect()
            except Exception:
                connected = False

            if not connected:

                now = time.time()

                if (
                    now - last_reconnect
                    >= RECONNECT_INTERVAL
                ):
                    print(
                        "🔄 Reconnecting to IQ Option..."
                    )

                    connect_iq()

                    last_reconnect = now

                time.sleep(5)

                continue

            # ------------------------------------------------
            # REDISCOVERY
            # ------------------------------------------------

            now = time.time()

            if (
                now - last_discovery
                >= DISCOVERY_INTERVAL
            ):

                try:
                    working = discover_focused_assets()

                    if working:
                        send_feed_status()

                except Exception as exc:
                    print(
                        f"⚠️ Discovery error: {exc}"
                    )

                last_discovery = now

            # ------------------------------------------------
            # SCAN FOCUSED ASSETS
            # ------------------------------------------------

            for asset in list(
                focused_working_assets
            ):

                if total_trades >= TARGET_TRADES:
                    break

                try:

                    candles_5m = fetch_candles(
                        asset,
                        TF5,
                        CANDLE_COUNT_5M
                    )

                    candles_1m = fetch_candles(
                        asset,
                        TF1,
                        CANDLE_COUNT_1M
                    )

                    signal = evaluate_zeta_v2(
                        asset,
                        candles_5m,
                        candles_1m
                    )

                    if signal is not None:

                        print(
                            f"🟢 SIGNAL "
                            f"{signal['asset']} "
                            f"{signal['direction'].upper()} "
                            f"score={signal['score']}"
                        )

                        execute_trade(signal)

                except Exception as exc:

                    print(
                        f"⚠️ Scan error "
                        f"{asset}: {exc}"
                    )

                    traceback.print_exc()

            # ------------------------------------------------
            # HEARTBEAT
            # ------------------------------------------------

            if (
                time.time() - last_heartbeat
                >= STATUS_INTERVAL
            ):

                send_heartbeat()

                # Keep latest rejection per asset visible,
                # but reset numerical counters for the next
                # diagnostic window.
                reset_diagnostic_window()

            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:

            print(
                "🛑 ZETA V2 stopped manually."
            )

            break

        except Exception as exc:

            print(
                f"❌ Main loop error: {exc}"
            )

            traceback.print_exc()

            time.sleep(10)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
