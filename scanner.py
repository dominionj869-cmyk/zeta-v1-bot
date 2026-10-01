import os
import time
import math
import traceback
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# ZETA V2 — HIGH QUALITY TREND PULLBACK SCANNER
# ============================================================
#
# Strategy:
#   5M = trend
#   1M = pullback + rejection + confirmation
#   Expiry = 2 minutes
#
# IMPORTANT:
#   This version does NOT treat a candlestick pattern by itself
#   as a valid trade.
#
#   A trade must pass the complete setup and score >= 80/100.
#
# ============================================================


# -----------------------------
# ENVIRONMENT
# -----------------------------

IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# -----------------------------
# ACCOUNT / EXECUTION
# -----------------------------

PRACTICE = True

# Keep this True if you want the bot to actually open PRACTICE
# trades after the signal passes all filters.
AUTO_TRADE = True

STAKE = 1.0
EXPIRY_MINUTES = 2

TEST_TARGET = 50


# -----------------------------
# MARKET
# -----------------------------

MAX_OTC_ASSETS = 60

TF_5M = 300
TF_1M = 60

CANDLES_5M = 180
CANDLES_1M = 180


# -----------------------------
# QUALITY
# -----------------------------

MIN_SCORE = 80

STRONG_SCORE = 85
VERY_STRONG_SCORE = 90


# -----------------------------
# TREND
# -----------------------------

EMA_FAST = 20
EMA_SLOW = 50

MIN_TREND_SEPARATION_ATR = 0.12

MIN_ADX = 18.0


# -----------------------------
# RSI
# -----------------------------

RSI_PERIOD = 14

CALL_RSI_MIN = 45
CALL_RSI_MAX = 68

PUT_RSI_MIN = 32
PUT_RSI_MAX = 55


# -----------------------------
# ATR
# -----------------------------

ATR_PERIOD = 14

MIN_ATR = 0.0

# Don't chase candles that have already moved too far.
MAX_ENTRY_EXTENSION_ATR = 1.80


# -----------------------------
# PULLBACK
# -----------------------------

PULLBACK_MIN_ATR = 0.20
PULLBACK_MAX_ATR = 1.50


# -----------------------------
# CANDLE QUALITY
# -----------------------------

MIN_REJECTION_WICK_RATIO = 0.35

MIN_TRIGGER_BODY_ATR = 0.15

MIN_TRIGGER_BODY_RATIO = 0.40

MAX_TRIGGER_RANGE_ATR = 1.80


# -----------------------------
# SUPPORT / RESISTANCE
# -----------------------------

SWING_LOOKBACK = 30

ZONE_TOLERANCE_ATR = 0.35

MIN_ZONE_TOUCHES = 2


# -----------------------------
# ROOM
# -----------------------------

MIN_ROOM_ATR = 0.80


# -----------------------------
# DUPLICATE PROTECTION
# -----------------------------

ASSET_LOCK_SECONDS = 300


# ============================================================
# GLOBAL STATE
# ============================================================

iq = None

asset_last_trade = {}

opened_trades = []
completed_trades = []

wins = 0
losses = 0
draws = 0

net_pl = 0.0

start_time = time.time()

last_status_time = 0

seen_trade_ids = set()


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:

        url = (
            f"https://api.telegram.org/bot"
            f"{TELEGRAM_TOKEN}/sendMessage"
        )

        requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message
            },
            timeout=15
        )

    except Exception as e:
        print("Telegram error:", e)


# ============================================================
# HELPERS
# ============================================================

def now_text():

    return datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")


def safe_float(value, default=0.0):

    try:
        value = float(value)

        if math.isfinite(value):
            return value

    except Exception:
        pass

    return default


def clamp(value, low, high):

    return max(low, min(high, value))


# ============================================================
# MATH
# ============================================================

def ema(values, period):

    if not values:
        return []

    if len(values) < period:
        return [None] * len(values)

    result = [None] * len(values)

    multiplier = 2 / (period + 1)

    initial = sum(values[:period]) / period

    result[period - 1] = initial

    for i in range(period, len(values)):

        previous = result[i - 1]

        result[i] = (
            (values[i] - previous) * multiplier
            + previous
        )

    return result


def sma(values, period):

    result = [None] * len(values)

    if len(values) < period:
        return result

    for i in range(period - 1, len(values)):

        window = values[
            i - period + 1:
            i + 1
        ]

        result[i] = sum(window) / period

    return result


def atr(candles, period=14):

    if len(candles) < period + 1:
        return [None] * len(candles)

    tr = [None]

    for i in range(1, len(candles)):

        high = candles[i]["max"]
        low = candles[i]["min"]

        previous_close = candles[i - 1]["close"]

        value = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )

        tr.append(value)

    result = [None] * len(candles)

    initial = sum(
        x for x in tr[1:period + 1]
        if x is not None
    ) / period

    result[period] = initial

    for i in range(period + 1, len(candles)):

        result[i] = (
            (result[i - 1] * (period - 1))
            + tr[i]
        ) / period

    return result


def rsi(closes, period=14):

    result = [None] * len(closes)

    if len(closes) <= period:
        return result

    gains = []
    losses_ = []

    for i in range(1, len(closes)):

        change = closes[i] - closes[i - 1]

        gains.append(max(change, 0))
        losses_.append(max(-change, 0))

    avg_gain = sum(
        gains[:period]
    ) / period

    avg_loss = sum(
        losses_[:period]
    ) / period

    if avg_loss == 0:
        result[period] = 100
    else:
        rs = avg_gain / avg_loss
        result[period] = 100 - (100 / (1 + rs))

    for i in range(period + 1, len(closes)):

        gain = gains[i - 1]
        loss = losses_[i - 1]

        avg_gain = (
            (avg_gain * (period - 1))
            + gain
        ) / period

        avg_loss = (
            (avg_loss * (period - 1))
            + loss
        ) / period

        if avg_loss == 0:
            result[i] = 100
        else:
            rs = avg_gain / avg_loss
            result[i] = 100 - (100 / (1 + rs))

    return result


def adx(candles, period=14):

    if len(candles) < period * 2:
        return [None] * len(candles)

    tr_values = [None]

    plus_dm = [None]
    minus_dm = [None]

    for i in range(1, len(candles)):

        high = candles[i]["max"]
        low = candles[i]["min"]

        prev_high = candles[i - 1]["max"]
        prev_low = candles[i - 1]["min"]
        prev_close = candles[i - 1]["close"]

        tr_value = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

        up_move = high - prev_high
        down_move = prev_low - low

        if up_move > down_move and up_move > 0:
            p_dm = up_move
        else:
            p_dm = 0

        if down_move > up_move and down_move > 0:
            m_dm = down_move
        else:
            m_dm = 0

        tr_values.append(tr_value)
        plus_dm.append(p_dm)
        minus_dm.append(m_dm)

    atr_s = [None] * len(candles)
    p_s = [None] * len(candles)
    m_s = [None] * len(candles)

    if len(candles) <= period:
        return [None] * len(candles)

    atr_s[period] = sum(
        tr_values[1:period + 1]
    )

    p_s[period] = sum(
        plus_dm[1:period + 1]
    )

    m_s[period] = sum(
        minus_dm[1:period + 1]
    )

    dx = [None] * len(candles)

    for i in range(period, len(candles)):

        if i > period:

            atr_s[i] = (
                atr_s[i - 1]
                - (atr_s[i - 1] / period)
                + tr_values[i]
            )

            p_s[i] = (
                p_s[i - 1]
                - (p_s[i - 1] / period)
                + plus_dm[i]
            )

            m_s[i] = (
                m_s[i - 1]
                - (m_s[i - 1] / period)
                + minus_dm[i]
            )

        if atr_s[i] == 0:
            continue

        plus_di = (
            100 * p_s[i] / atr_s[i]
        )

        minus_di = (
            100 * m_s[i] / atr_s[i]
        )

        denominator = plus_di + minus_di

        if denominator == 0:
            continue

        dx[i] = (
            100
            * abs(plus_di - minus_di)
            / denominator
        )

    result = [None] * len(candles)

    valid_dx = [
        x for x in dx
        if x is not None
    ]

    if len(valid_dx) < period:
        return result

    first_index = None

    for i in range(len(dx)):

        if dx[i] is not None:

            possible = [
                x for x in dx[
                    i:i + period
                ]
                if x is not None
            ]

            if len(possible) == period:
                first_index = i + period - 1
                break

    if first_index is None:
        return result

    result[first_index] = sum(
        dx[
            first_index - period + 1:
            first_index + 1
        ]
    ) / period

    for i in range(
        first_index + 1,
        len(candles)
    ):

        if dx[i] is None:
            continue

        result[i] = (
            (
                result[i - 1]
                * (period - 1)
            )
            + dx[i]
        ) / period

    return result


# ============================================================
# CANDLE HELPERS
# ============================================================

def candle_parts(c):

    high = c["max"]
    low = c["min"]
    open_ = c["open"]
    close = c["close"]

    total = max(high - low, 1e-12)

    body = abs(close - open_)

    upper = high - max(open_, close)
    lower = min(open_, close) - low

    return {
        "range": total,
        "body": body,
        "upper": upper,
        "lower": lower,
        "body_ratio": body / total,
        "bull": close > open_,
        "bear": close < open_
    }


def bullish_engulfing(previous, current):

    p = candle_parts(previous)
    c = candle_parts(current)

    return (
        p["bear"]
        and c["bull"]
        and current["open"] <= previous["close"]
        and current["close"] >= previous["open"]
        and c["body_ratio"] >= 0.45
    )


def bearish_engulfing(previous, current):

    p = candle_parts(previous)
    c = candle_parts(current)

    return (
        p["bull"]
        and c["bear"]
        and current["open"] >= previous["close"]
        and current["close"] <= previous["open"]
        and c["body_ratio"] >= 0.45
    )


def hammer(c):

    x = candle_parts(c)

    return (
        x["lower"] >= x["body"] * 1.8
        and x["upper"] <= x["range"] * 0.30
        and x["body_ratio"] >= 0.20
    )


def shooting_star(c):

    x = candle_parts(c)

    return (
        x["upper"] >= x["body"] * 1.8
        and x["lower"] <= x["range"] * 0.30
        and x["body_ratio"] >= 0.20
    )


def bullish_pin_bar(c):

    x = candle_parts(c)

    return (
        x["lower"] >= x["range"] * 0.45
        and x["upper"] <= x["range"] * 0.25
    )


def bearish_pin_bar(c):

    x = candle_parts(c)

    return (
        x["upper"] >= x["range"] * 0.45
        and x["lower"] <= x["range"] * 0.25
    )


def morning_star(a, b, c):

    x1 = candle_parts(a)
    x2 = candle_parts(b)
    x3 = candle_parts(c)

    midpoint = (
        a["open"] + a["close"]
    ) / 2

    return (
        x1["bear"]
        and x1["body_ratio"] >= 0.45
        and x2["body_ratio"] <= 0.40
        and x3["bull"]
        and x3["body_ratio"] >= 0.45
        and c["close"] > midpoint
    )


def evening_star(a, b, c):

    x1 = candle_parts(a)
    x2 = candle_parts(b)
    x3 = candle_parts(c)

    midpoint = (
        a["open"] + a["close"]
    ) / 2

    return (
        x1["bull"]
        and x1["body_ratio"] >= 0.45
        and x2["body_ratio"] <= 0.40
        and x3["bear"]
        and x3["body_ratio"] >= 0.45
        and c["close"] < midpoint
    )


# ============================================================
# PATTERN DETECTION
# ============================================================

def detect_bullish_patterns(candles):

    patterns = []

    if len(candles) < 4:
        return patterns

    a = candles[-4]
    b = candles[-3]
    c = candles[-2]

    if morning_star(a, b, c):

        patterns.append({
            "name": "Morning Star",
            "bias": "bullish",
            "quality": 0.90
        })

    if bullish_engulfing(b, c):

        patterns.append({
            "name": "Bullish Engulfing",
            "bias": "bullish",
            "quality": 0.88
        })

    if hammer(c):

        patterns.append({
            "name": "Hammer",
            "bias": "bullish",
            "quality": 0.78
        })

    if bullish_pin_bar(c):

        patterns.append({
            "name": "Bullish Pin Bar",
            "bias": "bullish",
            "quality": 0.78
        })

    return patterns


def detect_bearish_patterns(candles):

    patterns = []

    if len(candles) < 4:
        return patterns

    a = candles[-4]
    b = candles[-3]
    c = candles[-2]

    if evening_star(a, b, c):

        patterns.append({
            "name": "Evening Star",
            "bias": "bearish",
            "quality": 0.90
        })

    if bearish_engulfing(b, c):

        patterns.append({
            "name": "Bearish Engulfing",
            "bias": "bearish",
            "quality": 0.88
        })

    if shooting_star(c):

        patterns.append({
            "name": "Shooting Star",
            "bias": "bearish",
            "quality": 0.78
        })

    if bearish_pin_bar(c):

        patterns.append({
            "name": "Bearish Pin Bar",
            "bias": "bearish",
            "quality": 0.78
        })

    return patterns


# ============================================================
# CLOSED CANDLES
# ============================================================

def remove_open_candle(candles, timeframe):

    if not candles:
        return []

    current_time = int(time.time())

    last = candles[-1]

    candle_time = int(
        last.get("from", 0)
    )

    if (
        candle_time
        + timeframe
        > current_time
    ):
        return candles[:-1]

    return candles


# ============================================================
# ZONES
# ============================================================

def recent_support(candles, current_price, current_atr):

    lookback = candles[
        -SWING_LOOKBACK:
    ]

    if not lookback:
        return None, 0

    lows = [
        c["min"]
        for c in lookback
    ]

    candidate = min(lows)

    tolerance = (
        current_atr
        * ZONE_TOLERANCE_ATR
    )

    touches = sum(
        1
        for x in lows
        if abs(x - candidate)
        <= tolerance
    )

    return candidate, touches


def recent_resistance(candles, current_price, current_atr):

    lookback = candles[
        -SWING_LOOKBACK:
    ]

    if not lookback:
        return None, 0

    highs = [
        c["max"]
        for c in lookback
    ]

    candidate = max(highs)

    tolerance = (
        current_atr
        * ZONE_TOLERANCE_ATR
    )

    touches = sum(
        1
        for x in highs
        if abs(x - candidate)
        <= tolerance
    )

    return candidate, touches


# ============================================================
# TREND
# ============================================================

def analyze_5m_trend(candles):

    if len(candles) < EMA_SLOW + 10:
        return None

    closes = [
        c["close"]
        for c in candles
    ]

    fast = ema(
        closes,
        EMA_FAST
    )

    slow = ema(
        closes,
        EMA_SLOW
    )

    atr_values = atr(
        candles,
        ATR_PERIOD
    )

    adx_values = adx(
        candles,
        14
    )

    i = len(candles) - 1

    current = closes[i]

    ema_fast = fast[i]
    ema_slow = slow[i]

    current_atr = atr_values[i]
    current_adx = adx_values[i]

    if any(
        x is None
        for x in [
            ema_fast,
            ema_slow,
            current_atr,
            current_adx
        ]
    ):
        return None

    separation = abs(
        ema_fast - ema_slow
    ) / max(current_atr, 1e-12)

    bullish = (
        ema_fast > ema_slow
        and current > ema_fast
        and separation >= MIN_TREND_SEPARATION_ATR
        and current_adx >= MIN_ADX
    )

    bearish = (
        ema_fast < ema_slow
        and current < ema_fast
        and separation >= MIN_TREND_SEPARATION_ATR
        and current_adx >= MIN_ADX
    )

    if bullish:
        direction = "CALL"

    elif bearish:
        direction = "PUT"

    else:
        direction = None

    return {
        "direction": direction,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "atr": current_atr,
        "adx": current_adx,
        "separation": separation
    }


# ============================================================
# PULLBACK
# ============================================================

def calculate_pullback(
    candles,
    trend,
    direction
):

    price = candles[-1]["close"]

    current_atr = trend["atr"]

    if direction == "CALL":

        distance = (
            price
            - trend["ema_fast"]
        )

    else:

        distance = (
            trend["ema_fast"]
            - price
        )

    distance_atr = (
        distance
        / max(current_atr, 1e-12)
    )

    return distance_atr


# ============================================================
# REJECTION
# ============================================================

def rejection_score(
    candle,
    direction,
    atr_value
):

    x = candle_parts(candle)

    range_atr = (
        x["range"]
        / max(atr_value, 1e-12)
    )

    if range_atr <= 0:
        return False, 0

    if range_atr > MAX_TRIGGER_RANGE_ATR:
        return False, 0

    if direction == "CALL":

        wick_ratio = (
            x["lower"]
            / x["range"]
        )

        valid = (
            x["lower"]
            >= x["range"]
            * MIN_REJECTION_WICK_RATIO
            and wick_ratio >= 0.35
        )

    else:

        wick_ratio = (
            x["upper"]
            / x["range"]
        )

        valid = (
            x["upper"]
            >= x["range"]
            * MIN_REJECTION_WICK_RATIO
            and wick_ratio >= 0.35
        )

    if not valid:
        return False, 0

    return True, clamp(
        wick_ratio,
        0,
        1
    )


# ============================================================
# CONFIRMATION
# ============================================================

def confirmation_candle(
    candles,
    direction,
    atr_value
):

    if len(candles) < 3:
        return False

    rejection = candles[-3]
    trigger = candles[-2]

    r = candle_parts(rejection)
    t = candle_parts(trigger)

    trigger_range_atr = (
        t["range"]
        / max(atr_value, 1e-12)
    )

    trigger_body_atr = (
        t["body"]
        / max(atr_value, 1e-12)
    )

    if (
        trigger_range_atr
        > MAX_TRIGGER_RANGE_ATR
    ):
        return False

    if (
        trigger_body_atr
        < MIN_TRIGGER_BODY_ATR
    ):
        return False

    if (
        t["body_ratio"]
        < MIN_TRIGGER_BODY_RATIO
    ):
        return False

    if direction == "CALL":

        return (
            t["bull"]
            and trigger["close"]
            > rejection["high"]
        )

    return (
        t["bear"]
        and trigger["close"]
        < rejection["low"]
    )


# ============================================================
# MOMENTUM
# ============================================================

def momentum_confirmation(
    candles,
    direction,
    atr_value
):

    if len(candles) < 3:
        return False

    previous = candles[-3]
    current = candles[-2]

    p = candle_parts(previous)
    c = candle_parts(current)

    if direction == "CALL":

        return (
            c["bull"]
            and current["close"]
            > previous["close"]
            and c["body"]
            >= atr_value
            * MIN_TRIGGER_BODY_ATR
        )

    return (
        c["bear"]
        and current["close"]
        < previous["close"]
        and c["body"]
        >= atr_value
        * MIN_TRIGGER_BODY_ATR
    )


# ============================================================
# SCORE
# ============================================================

def score_setup(
    direction,
    trend,
    pullback_atr,
    zone_valid,
    zone_touches,
    rejection_valid,
    confirmation_valid,
    momentum_valid,
    room_valid,
    patterns
):

    score = 0

    reasons = []

    # --------------------------------
    # 5M trend: 20
    # --------------------------------

    if trend["adx"] >= 25:
        score += 20
        reasons.append("strong ADX")
    elif trend["adx"] >= 22:
        score += 18
        reasons.append("good ADX")
    elif trend["adx"] >= MIN_ADX:
        score += 15
        reasons.append("acceptable ADX")

    # --------------------------------
    # EMA structure: 10
    # --------------------------------

    if trend["separation"] >= 0.30:
        score += 10
        reasons.append("strong EMA separation")

    elif trend["separation"] >= 0.20:
        score += 8
        reasons.append("good EMA separation")

    elif trend["separation"] >= MIN_TREND_SEPARATION_ATR:
        score += 6
        reasons.append("EMA trend structure")

    # --------------------------------
    # Pullback: 15
    # --------------------------------

    if (
        PULLBACK_MIN_ATR
        <= pullback_atr
        <= PULLBACK_MAX_ATR
    ):

        score += 15
        reasons.append("valid pullback")

    # --------------------------------
    # Zone: 15
    # --------------------------------

    if zone_valid:

        if zone_touches >= 3:
            score += 15
            reasons.append("strong zone")

        elif zone_touches >= 2:
            score += 12
            reasons.append("confirmed zone")

    # --------------------------------
    # Rejection: 20
    # --------------------------------

    if rejection_valid:

        score += 20
        reasons.append("fresh rejection")

    # --------------------------------
    # Confirmation: 10
    # --------------------------------

    if confirmation_valid:

        score += 10
        reasons.append("1M confirmation")

    # --------------------------------
    # Momentum: 5
    # --------------------------------

    if momentum_valid:

        score += 5
        reasons.append("immediate momentum")

    # --------------------------------
    # Room: 5
    # --------------------------------

    if room_valid:

        score += 5
        reasons.append("enough room")

    # --------------------------------
    # Pattern bonus
    # --------------------------------

    if patterns:

        best = max(
            p["quality"]
            for p in patterns
        )

        if best >= 0.90:
            score += 5
            reasons.append("strong candle pattern")

        elif best >= 0.80:
            score += 3
            reasons.append("good candle pattern")

    return min(score, 100), reasons


# ============================================================
# SETUP ANALYSIS
# ============================================================

def analyze_asset(
    asset,
    candles_5m,
    candles_1m
):

    candles_5m = remove_open_candle(
        candles_5m,
        TF_5M
    )

    candles_1m = remove_open_candle(
        candles_1m,
        TF_1M
    )

    if (
        len(candles_5m)
        < EMA_SLOW + 10
        or len(candles_1m)
        < 50
    ):
        return None

    trend = analyze_5m_trend(
        candles_5m
    )

    if not trend:
        return None

    direction = trend["direction"]

    if not direction:
        return None

    price = candles_1m[-1]["close"]

    current_atr = trend["atr"]

    # --------------------------------
    # Pullback
    # --------------------------------

    pullback_atr = calculate_pullback(
        candles_1m,
        trend,
        direction
    )

    if not (
        PULLBACK_MIN_ATR
        <= pullback_atr
        <= PULLBACK_MAX_ATR
    ):
        return None

    # --------------------------------
    # Zones
    # --------------------------------

    support, support_touches = (
        recent_support(
            candles_1m,
            price,
            current_atr
        )
    )

    resistance, resistance_touches = (
        recent_resistance(
            candles_1m,
            price,
            current_atr
        )
    )

    tolerance = (
        current_atr
        * ZONE_TOLERANCE_ATR
    )

    if direction == "CALL":

        zone_valid = (
            support is not None
            and abs(price - support)
            <= tolerance
            and support_touches
            >= MIN_ZONE_TOUCHES
        )

        zone_touches = support_touches

        # Do not accept ambiguous location.
        if (
            resistance is not None
            and abs(price - resistance)
            <= tolerance
            and resistance_touches
            >= MIN_ZONE_TOUCHES
        ):
            return None

    else:

        zone_valid = (
            resistance is not None
            and abs(price - resistance)
            <= tolerance
            and resistance_touches
            >= MIN_ZONE_TOUCHES
        )

        zone_touches = resistance_touches

        if (
            support is not None
            and abs(price - support)
            <= tolerance
            and support_touches
            >= MIN_ZONE_TOUCHES
        ):
            return None

    if not zone_valid:
        return None

    # --------------------------------
    # Fresh rejection candle
    # --------------------------------

    rejection = candles_1m[-3]

    rejection_valid, rejection_strength = (
        rejection_score(
            rejection,
            direction,
            current_atr
        )
    )

    if not rejection_valid:
        return None

    # --------------------------------
    # Candlestick patterns
    # --------------------------------

    if direction == "CALL":

        patterns = detect_bullish_patterns(
            candles_1m
        )

    else:

        patterns = detect_bearish_patterns(
            candles_1m
        )

    if not patterns:
        return None

    # --------------------------------
    # Confirmation
    # --------------------------------

    confirmation_valid = (
        confirmation_candle(
            candles_1m,
            direction,
            current_atr
        )
    )

    if not confirmation_valid:
        return None

    # --------------------------------
    # Momentum
    # --------------------------------

    momentum_valid = (
        momentum_confirmation(
            candles_1m,
            direction,
            current_atr
        )
    )

    if not momentum_valid:
        return None

    # --------------------------------
    # Room to opposing level
    # --------------------------------

    if direction == "CALL":

        if resistance is None:
            room_valid = True
        else:
            room = (
                resistance - price
            )

            room_valid = (
                room
                / max(current_atr, 1e-12)
                >= MIN_ROOM_ATR
            )

    else:

        if support is None:
            room_valid = True
        else:
            room = (
                price - support
            )

            room_valid = (
                room
                / max(current_atr, 1e-12)
                >= MIN_ROOM_ATR
            )

    if not room_valid:
        return None

    # --------------------------------
    # RSI
    # --------------------------------

    closes_1m = [
        c["close"]
        for c in candles_1m
    ]

    rsi_values = rsi(
        closes_1m,
        RSI_PERIOD
    )

    current_rsi = rsi_values[-2]

    if current_rsi is None:
        return None

    if direction == "CALL":

        if not (
            CALL_RSI_MIN
            <= current_rsi
            <= CALL_RSI_MAX
        ):
            return None

    else:

        if not (
            PUT_RSI_MIN
            <= current_rsi
            <= PUT_RSI_MAX
        ):
            return None

    # --------------------------------
    # Extension protection
    # --------------------------------

    extension = abs(
        price - trend["ema_fast"]
    ) / max(
        current_atr,
        1e-12
    )

    if extension > MAX_ENTRY_EXTENSION_ATR:
        return None

    # --------------------------------
    # SCORE
    # --------------------------------

    score, reasons = score_setup(
        direction=direction,
        trend=trend,
        pullback_atr=pullback_atr,
        zone_valid=zone_valid,
        zone_touches=zone_touches,
        rejection_valid=rejection_valid,
        confirmation_valid=confirmation_valid,
        momentum_valid=momentum_valid,
        room_valid=room_valid,
        patterns=patterns
    )

    # --------------------------------
    # HARD MINIMUM
    # --------------------------------

    if score < MIN_SCORE:
        return None

    return {
        "asset": asset,
        "direction": direction,
        "score": score,
        "price": price,
        "ema_fast": trend["ema_fast"],
        "ema_slow": trend["ema_slow"],
        "atr": current_atr,
        "adx": trend["adx"],
        "rsi": current_rsi,
        "pullback_atr": pullback_atr,
        "extension_atr": extension,
        "patterns": patterns,
        "reasons": reasons,
        "support": support,
        "resistance": resistance,
        "zone_touches": zone_touches
    }


# ============================================================
# IQ OPTION CONNECTION
# ============================================================

def connect():

    global iq

    print("Connecting to IQ Option...")

    iq = IQ_Option(
        IQ_EMAIL,
        IQ_PASSWORD
    )

    iq.connect()

    if not iq.check_connect():

        raise RuntimeError(
            "IQ Option connection failed."
        )

    try:

        if PRACTICE:
            iq.change_balance("PRACTICE")
        else:
            iq.change_balance("REAL")

    except Exception:
        pass

    print("IQ Option connection OK.")

    telegram(
        "🟢 ZETA V2 BOT ONLINE\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Expiry: {EXPIRY_MINUTES} minutes\n"
        "5M Trend: ON\n"
        "1M Pullback: ON\n"
        "Fresh Rejection: ON\n"
        "Momentum Confirmation: ON\n"
        "Quality Minimum: 80/100\n"
        "Mode: PRACTICE\n"
        "Auto-trading: ON\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Scanning for HIGH QUALITY setups."
    )


# ============================================================
# OTC DISCOVERY
# ============================================================

def discover_otc_assets():

    try:

        data = iq.get_all_open_time()

    except Exception as e:

        print(
            "OTC discovery error:",
            e
        )

        return []

    assets = []

    for market_type in [
        "binary",
        "turbo",
        "digital"
    ]:

        section = data.get(
            market_type,
            {}
        )

        for asset, info in section.items():

            if not isinstance(info, dict):
                continue

            if not info.get(
                "open",
                False
            ):
                continue

            name = str(asset).upper()

            if "OTC" not in name:
                continue

            if asset not in assets:

                assets.append(asset)

            if len(assets) >= MAX_OTC_ASSETS:
                return assets

    return assets


# ============================================================
# CANDLE FETCH
# ============================================================

def get_candles(
    asset,
    timeframe,
    count
):

    try:

        data = iq.get_candles(
            asset,
            timeframe,
            count,
            time.time()
        )

        candles = []

        for c in data:

            candles.append({
                "from": int(
                    c.get("from", 0)
                ),
                "open": safe_float(
                    c.get("open")
                ),
                "close": safe_float(
                    c.get("close")
                ),
                "min": safe_float(
                    c.get("min")
                ),
                "max": safe_float(
                    c.get("max")
                ),
                "volume": safe_float(
                    c.get("volume")
                )
            })

        candles.sort(
            key=lambda x: x["from"]
        )

        return candles

    except Exception as e:

        print(
            f"Candle error {asset}:",
            e
        )

        return []


# ============================================================
# TRADE EXECUTION
# ============================================================

def execute_trade(setup):

    asset = setup["asset"]

    direction = setup["direction"]

    if asset in asset_last_trade:

        elapsed = (
            time.time()
            - asset_last_trade[asset]
        )

        if elapsed < ASSET_LOCK_SECONDS:

            return None

    signal_id = (
        f"ZETA2-"
        f"{asset.replace('-', '')}-"
        f"{direction}-"
        f"{int(time.time())}"
    )

    pattern_names = ", ".join(
        p["name"]
        for p in setup["patterns"]
    )

    grade = (
        "VERY STRONG"
        if setup["score"] >= VERY_STRONG_SCORE
        else
        "STRONG"
        if setup["score"] >= STRONG_SCORE
        else
        "QUALIFIED"
    )

    message = (
        f"🟢 ZETA V2 {grade} SIGNAL\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Asset: {asset}\n"
        f"Direction: {direction}\n"
        f"Expiry: {EXPIRY_MINUTES} minutes\n"
        f"Entry: {setup['price']}\n"
        f"Quality: {setup['score']}/100\n"
        f"5M ADX: {setup['adx']:.2f}\n"
        f"RSI: {setup['rsi']:.2f}\n"
        f"Pullback: {setup['pullback_atr']:.2f} ATR\n"
        f"Extension: {setup['extension_atr']:.2f} ATR\n"
        f"Patterns: {pattern_names}\n"
        f"Zone touches: {setup['zone_touches']}\n"
        f"Reason: {', '.join(setup['reasons'])}\n"
        f"Signal ID: {signal_id}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

    print(message)

    telegram(message)

    if not AUTO_TRADE:
        return None

    try:

        action = (
            "call"
            if direction == "CALL"
            else
            "put"
        )

        success, trade_id = iq.buy(
            STAKE,
            asset,
            action,
            EXPIRY_MINUTES
        )

        if not success:

            telegram(
                f"❌ ZETA V2 ORDER FAILED\n"
                f"Asset: {asset}\n"
                f"Direction: {direction}\n"
                f"Signal: {signal_id}"
            )

            return None

        asset_last_trade[asset] = time.time()

        trade = {
            "signal_id": signal_id,
            "trade_id": trade_id,
            "asset": asset,
            "direction": direction,
            "stake": STAKE,
            "score": setup["score"],
            "patterns": pattern_names,
            "opened_at": time.time()
        }

        opened_trades.append(trade)

        telegram(
            f"🚀 ZETA V2 DEMO TRADE OPENED\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Trade: #{len(opened_trades)}/{TEST_TARGET}\n"
            f"Asset: {asset}\n"
            f"Direction: {direction}\n"
            f"Stake: ${STAKE:.2f}\n"
            f"Expiry: {EXPIRY_MINUTES} minutes\n"
            f"Quality: {setup['score']}/100\n"
            f"Trade ID: {trade_id}\n"
            f"Signal ID: {signal_id}\n"
            f"━━━━━━━━━━━━━━━━━━"
        )

        return trade

    except Exception as e:

        print(
            "Trade execution error:",
            e
        )

        telegram(
            f"❌ ZETA V2 EXECUTION ERROR\n"
            f"{asset} {direction}\n"
            f"{e}"
        )

        return None


# ============================================================
# RESULT TRACKING
# ============================================================

def check_trade_result(trade):

    global wins
    global losses
    global draws
    global net_pl

    trade_id = trade["trade_id"]

    if trade_id in seen_trade_ids:
        return True

    try:

        result = iq.check_win_v4(
            trade_id
        )

    except Exception as e:

        print(
            "Result error:",
            e
        )

        return False

    if result is None:
        return False

    try:
        result = float(result)
    except Exception:
        return False

    seen_trade_ids.add(trade_id)

    if result > 0:

        outcome = "WIN"
        wins += 1

    elif result < 0:

        outcome = "LOSS"
        losses += 1

    else:

        outcome = "DRAW"
        draws += 1

    net_pl += result

    trade["result"] = result
    trade["outcome"] = outcome

    completed_trades.append(
        trade
    )

    win_rate = (
        wins / len(completed_trades) * 100
        if completed_trades
        else 0
    )

    telegram(
        f"{'🟢' if outcome == 'WIN' else '🔴' if outcome == 'LOSS' else '🟡'} "
        f"ZETA V2 {outcome}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Asset: {trade['asset']}\n"
        f"Direction: {trade['direction']}\n"
        f"Quality: {trade['score']}/100\n"
        f"Result: ${result:+.2f}\n"
        f"Completed: {len(completed_trades)}/{TEST_TARGET}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Draws: {draws}\n"
        f"Win rate: {win_rate:.2f}%\n"
        f"Net P/L: ${net_pl:+.2f}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

    return True


# ============================================================
# STATUS
# ============================================================

def send_status():

    global last_status_time

    if time.time() - last_status_time < 300:
        return

    last_status_time = time.time()

    completed = len(
        completed_trades
    )

    active = (
        len(opened_trades)
        - completed
    )

    win_rate = (
        wins / completed * 100
        if completed
        else 0
    )

    runtime = int(
        time.time() - start_time
    )

    hours = runtime // 3600

    minutes = (
        runtime % 3600
    ) // 60

    seconds = runtime % 60

    message = (
        f"🟡 ZETA V2 STATUS\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Opened: {len(opened_trades)}/{TEST_TARGET}\n"
        f"Completed: {completed}/{TEST_TARGET}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Draws: {draws}\n"
        f"Win Rate: {win_rate:.2f}%\n"
        f"Net Demo P/L: ${net_pl:+.2f}\n"
        f"Active: {active}\n"
        f"Runtime: {hours:02d}:{minutes:02d}:{seconds:02d}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Quality minimum: {MIN_SCORE}/100\n"
        f"Expiry: {EXPIRY_MINUTES} minutes\n"
        f"5M Trend: ON\n"
        f"1M Pullback: ON\n"
        f"Fresh Rejection: ON\n"
        f"Momentum: ON\n"
        f"Room Filter: ON\n"
        f"Mode: PRACTICE"
    )

    telegram(message)


# ============================================================
# MAIN SCANNER
# ============================================================

def scanner_loop():

    global opened_trades

    assets = discover_otc_assets()

    print(
        f"OTC assets discovered: "
        f"{len(assets)}"
    )

    telegram(
        f"🔵 ZETA V2 SCANNER STARTED\n"
        f"OTC assets: {len(assets)}\n"
        f"Target: {TEST_TARGET} completed trades\n"
        f"Minimum quality: {MIN_SCORE}/100\n"
        f"Expiry: {EXPIRY_MINUTES} minutes"
    )

    last_discovery = time.time()

    while True:

        try:

            # --------------------------------
            # Stop after 50 completed trades
            # --------------------------------

            if len(completed_trades) >= TEST_TARGET:

                win_rate = (
                    wins
                    / len(completed_trades)
                    * 100
                )

                telegram(
                    f"🏁 ZETA V2 TEST COMPLETE\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"Trades: {len(completed_trades)}\n"
                    f"Wins: {wins}\n"
                    f"Losses: {losses}\n"
                    f"Draws: {draws}\n"
                    f"Win Rate: {win_rate:.2f}%\n"
                    f"Net P/L: ${net_pl:+.2f}\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )

                print(
                    "50 completed trades reached."
                )

                break

            # --------------------------------
            # Refresh OTC list
            # --------------------------------

            if (
                time.time()
                - last_discovery
                > 600
            ):

                assets = discover_otc_assets()

                last_discovery = time.time()

                print(
                    f"OTC assets refreshed: "
                    f"{len(assets)}"
                )

            # --------------------------------
            # Check active results
            # --------------------------------

            for trade in list(
                opened_trades
            ):

                if (
                    trade
                    not in completed_trades
                ):

                    age = (
                        time.time()
                        - trade["opened_at"]
                    )

                    # Wait slightly beyond
                    # the 2-minute expiry.
                    if age >= (
                        EXPIRY_MINUTES
                        * 60
                        + 8
                    ):

                        check_trade_result(
                            trade
                        )

            # --------------------------------
            # Don't open too many simultaneous
            # positions.
            # --------------------------------

            active_count = (
                len(opened_trades)
                - len(completed_trades)
            )

            if active_count >= 3:

                send_status()

                time.sleep(5)

                continue

            # --------------------------------
            # Scan
            # --------------------------------

            for asset in assets:

                if (
                    len(completed_trades)
                    >= TEST_TARGET
                ):
                    break

                active_count = (
                    len(opened_trades)
                    - len(completed_trades)
                )

                if active_count >= 3:
                    break

                try:

                    candles_5m = get_candles(
                        asset,
                        TF_5M,
                        CANDLES_5M
                    )

                    candles_1m = get_candles(
                        asset,
                        TF_1M,
                        CANDLES_1M
                    )

                    if not candles_5m or not candles_1m:
                        continue

                    setup = analyze_asset(
                        asset,
                        candles_5m,
                        candles_1m
                    )

                    if setup is None:
                        continue

                    # --------------------------------
                    # Final hard quality check
                    # --------------------------------

                    if setup["score"] < MIN_SCORE:
                        continue

                    execute_trade(
                        setup
                    )

                    # Don't immediately scan
                    # the same asset again.
                    time.sleep(0.5)

                except Exception as e:

                    print(
                        f"Scan error "
                        f"{asset}: {e}"
                    )

            send_status()

            time.sleep(5)

        except KeyboardInterrupt:

            print(
                "Scanner stopped."
            )

            break

        except Exception:

            traceback.print_exc()

            time.sleep(10)


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":

    print(
        "========================================"
    )

    print(
        "       ZETA V2 HIGH QUALITY SCANNER"
    )

    print(
        "========================================"
    )

    print(
        f"Expiry: {EXPIRY_MINUTES} minutes"
    )

    print(
        f"Minimum quality: {MIN_SCORE}/100"
    )

    print(
        f"Practice: {PRACTICE}"
    )

    print(
        f"Auto trade: {AUTO_TRADE}"
    )

    print(
        "========================================"
    )

    if not IQ_EMAIL or not IQ_PASSWORD:

        raise RuntimeError(
            "IQ_EMAIL and IQ_PASSWORD "
            "environment variables are required."
        )

    connect()

    scanner_loop()
