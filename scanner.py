import os
import time
import math
import traceback
import requests
from datetime import datetime, timezone

from iqoptionapi.stable_api import IQ_Option


# ============================================================
# ZETA V2 - HIGH QUALITY TREND PULLBACK SCANNER
# ============================================================

VERSION = "ZETA V2.1"

# -----------------------------
# ENVIRONMENT
# -----------------------------
IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


# -----------------------------
# ACCOUNT / EXECUTION
# -----------------------------
PRACTICE = True
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
# SCORE
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
MIN_ADX = 18


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

MAX_ENTRY_EXTENSION_ATR = 1.80


# -----------------------------
# PULLBACK
# -----------------------------
PULLBACK_MIN_ATR = 0.20
PULLBACK_MAX_ATR = 1.50


# -----------------------------
# REJECTION
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

MIN_ROOM_ATR = 0.80


# -----------------------------
# LOCK
# -----------------------------
ASSET_LOCK_SECONDS = 300


# -----------------------------
# GLOBAL STATE
# -----------------------------
iq = None

asset_last_trade = {}

opened_trades = {}
completed_trades = {}

wins = 0
losses = 0
draws = 0
net_pl = 0.0

start_time = time.time()

last_status_time = 0

seen_trade_ids = set()


# ============================================================
# BASIC HELPERS
# ============================================================

def now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def clamp(value, low, high):
    return max(low, min(high, value))


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

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
            timeout=15
        )

        return response.ok

    except Exception as e:
        print(f"Telegram error: {e}")
        return False


# ============================================================
# MATH
# ============================================================

def sma(values, period):
    if len(values) < period:
        return None

    return sum(values[-period:]) / period


def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    result = sum(values[:period]) / period

    for value in values[period:]:
        result = (value - result) * multiplier + result

    return result


def true_ranges(candles):
    trs = []

    for i, candle in enumerate(candles):
        high = safe_float(candle["max"])
        low = safe_float(candle["min"])

        if i == 0:
            previous_close = safe_float(candle["close"])
        else:
            previous_close = safe_float(candles[i - 1]["close"])

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )

        trs.append(tr)

    return trs


def atr(candles, period=14):
    trs = true_ranges(candles)

    if len(trs) < period:
        return None

    return sum(trs[-period:]) / period


def rsi(candles, period=14):
    closes = [safe_float(c["close"]) for c in candles]

    if len(closes) < period + 1:
        return None

    gains = []
    losses_ = []

    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]

        if change > 0:
            gains.append(change)
            losses_.append(0)
        else:
            gains.append(0)
            losses_.append(abs(change))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses_[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses_[i]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def adx(candles, period=14):
    if len(candles) < period * 2 + 1:
        return None

    highs = [safe_float(c["max"]) for c in candles]
    lows = [safe_float(c["min"]) for c in candles]
    closes = [safe_float(c["close"]) for c in candles]

    tr_values = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(candles)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]

        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )

        tr_values.append(tr)

        if up_move > down_move and up_move > 0:
            plus_dm.append(up_move)
        else:
            plus_dm.append(0)

        if down_move > up_move and down_move > 0:
            minus_dm.append(down_move)
        else:
            minus_dm.append(0)

    if len(tr_values) < period:
        return None

    dx_values = []

    for i in range(period, len(tr_values)):
        tr_sum = sum(tr_values[i - period:i])
        plus_sum = sum(plus_dm[i - period:i])
        minus_sum = sum(minus_dm[i - period:i])

        if tr_sum == 0:
            continue

        plus_di = 100 * plus_sum / tr_sum
        minus_di = 100 * minus_sum / tr_sum

        denominator = plus_di + minus_di

        if denominator == 0:
            continue

        dx = 100 * abs(plus_di - minus_di) / denominator

        dx_values.append(dx)

    if len(dx_values) < period:
        return None

    return sum(dx_values[-period:]) / period


# ============================================================
# CANDLE HELPERS
# ============================================================

def candle_parts(candle):
    o = safe_float(candle["open"])
    c = safe_float(candle["close"])
    h = safe_float(candle["max"])
    l = safe_float(candle["min"])

    body = abs(c - o)
    rng = max(h - l, 0.00000001)

    upper = h - max(o, c)
    lower = min(o, c) - l

    body_ratio = body / rng

    return o, c, h, l, body, rng, upper, lower, body_ratio


def bullish_engulfing(previous, current):
    po, pc, ph, pl, pb, pr, pu, plow, pbr = candle_parts(previous)
    co, cc, ch, cl, cb, cr, cu, clow, cbr = candle_parts(current)

    previous_bear = pc < po
    current_bull = cc > co

    engulf = (
        co <= pc and
        cc >= po
    )

    return (
        previous_bear and
        current_bull and
        engulf and
        cbr >= 0.45
    )


def bearish_engulfing(previous, current):
    po, pc, ph, pl, pb, pr, pu, plow, pbr = candle_parts(previous)
    co, cc, ch, cl, cb, cr, cu, clow, cbr = candle_parts(current)

    previous_bull = pc > po
    current_bear = cc < co

    engulf = (
        co >= pc and
        cc <= po
    )

    return (
        previous_bull and
        current_bear and
        engulf and
        cbr >= 0.45
    )


def hammer(candle):
    o, c, h, l, body, rng, upper, lower, body_ratio = candle_parts(candle)

    return (
        lower >= body * 1.8 and
        upper <= rng * 0.30 and
        body_ratio >= 0.20
    )


def shooting_star(candle):
    o, c, h, l, body, rng, upper, lower, body_ratio = candle_parts(candle)

    return (
        upper >= body * 1.8 and
        lower <= rng * 0.30 and
        body_ratio >= 0.20
    )


def bullish_pin_bar(candle):
    o, c, h, l, body, rng, upper, lower, body_ratio = candle_parts(candle)

    return (
        lower >= rng * 0.45 and
        upper <= rng * 0.25
    )


def bearish_pin_bar(candle):
    o, c, h, l, body, rng, upper, lower, body_ratio = candle_parts(candle)

    return (
        upper >= rng * 0.45 and
        lower <= rng * 0.25
    )


def morning_star(candles):
    if len(candles) < 3:
        return False

    first = candles[-3]
    second = candles[-2]
    third = candles[-1]

    fo, fc, fh, fl, fb, fr, fu, flo, fbr = candle_parts(first)
    so, sc, sh, sl, sb, sr, su, slo, sbr = candle_parts(second)
    to, tc, th, tl, tb, tr, tu, tlo, tbr = candle_parts(third)

    first_bear = fc < fo
    third_bull = tc > to

    first_mid = (fo + fc) / 2

    return (
        first_bear and
        third_bull and
        fbr >= 0.45 and
        sbr <= 0.40 and
        tbr >= 0.45 and
        tc > first_mid
    )


def evening_star(candles):
    if len(candles) < 3:
        return False

    first = candles[-3]
    second = candles[-2]
    third = candles[-1]

    fo, fc, fh, fl, fb, fr, fu, flo, fbr = candle_parts(first)
    so, sc, sh, sl, sb, sr, su, slo, sbr = candle_parts(second)
    to, tc, th, tl, tb, tr, tu, tlo, tbr = candle_parts(third)

    first_bull = fc > fo
    third_bear = tc < to

    first_mid = (fo + fc) / 2

    return (
        first_bull and
        third_bear and
        fbr >= 0.45 and
        sbr <= 0.40 and
        tbr >= 0.45 and
        tc < first_mid
    )


def detect_bullish_patterns(candles):
    patterns = []

    if len(candles) >= 3 and morning_star(candles):
        patterns.append(("Morning Star", 0.90))

    if len(candles) >= 2 and bullish_engulfing(candles[-2], candles[-1]):
        patterns.append(("Bullish Engulfing", 0.88))

    if hammer(candles[-1]):
        patterns.append(("Hammer", 0.78))

    if bullish_pin_bar(candles[-1]):
        patterns.append(("Bullish Pin Bar", 0.78))

    return patterns


def detect_bearish_patterns(candles):
    patterns = []

    if len(candles) >= 3 and evening_star(candles):
        patterns.append(("Evening Star", 0.90))

    if len(candles) >= 2 and bearish_engulfing(candles[-2], candles[-1]):
        patterns.append(("Bearish Engulfing", 0.88))

    if shooting_star(candles[-1]):
        patterns.append(("Shooting Star", 0.78))

    if bearish_pin_bar(candles[-1]):
        patterns.append(("Bearish Pin Bar", 0.78))

    return patterns


# ============================================================
# REMOVE OPEN CANDLE
# ============================================================

def remove_open_candle(candles, timeframe):
    if not candles:
        return candles

    current_time = time.time()

    last = candles[-1]

    candle_start = safe_float(last.get("from", 0))

    if candle_start + timeframe > current_time:
        return candles[:-1]

    return candles


# ============================================================
# SUPPORT / RESISTANCE
# ============================================================

def recent_support(candles, atr_value):
    if not candles or not atr_value:
        return None, 0

    sample = candles[-SWING_LOOKBACK:]

    lowest = min(
        safe_float(c["min"])
        for c in sample
    )

    tolerance = atr_value * ZONE_TOLERANCE_ATR

    touches = 0

    for candle in sample:
        low = safe_float(candle["min"])

        if abs(low - lowest) <= tolerance:
            touches += 1

    return lowest, touches


def recent_resistance(candles, atr_value):
    if not candles or not atr_value:
        return None, 0

    sample = candles[-SWING_LOOKBACK:]

    highest = max(
        safe_float(c["max"])
        for c in sample
    )

    tolerance = atr_value * ZONE_TOLERANCE_ATR

    touches = 0

    for candle in sample:
        high = safe_float(candle["max"])

        if abs(high - highest) <= tolerance:
            touches += 1

    return highest, touches


# ============================================================
# 5M TREND
# ============================================================

def analyze_5m_trend(candles):
    if len(candles) < EMA_SLOW + 20:
        return None

    closes = [
        safe_float(c["close"])
        for c in candles
    ]

    ema_fast = ema(closes, EMA_FAST)
    ema_slow = ema(closes, EMA_SLOW)
    atr_value = atr(candles, ATR_PERIOD)
    adx_value = adx(candles, 14)

    if (
        ema_fast is None or
        ema_slow is None or
        atr_value is None or
        adx_value is None or
        atr_value <= 0
    ):
        return None

    price = closes[-1]

    separation = abs(ema_fast - ema_slow)

    separation_atr = separation / atr_value

    bullish = (
        ema_fast > ema_slow and
        price > ema_fast and
        separation_atr >= MIN_TREND_SEPARATION_ATR and
        adx_value >= MIN_ADX
    )

    bearish = (
        ema_fast < ema_slow and
        price < ema_fast and
        separation_atr >= MIN_TREND_SEPARATION_ATR and
        adx_value >= MIN_ADX
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
        "atr": atr_value,
        "adx": adx_value,
        "separation_atr": separation_atr,
        "price": price,
    }


# ============================================================
# PULLBACK
# ============================================================

def calculate_pullback(price, ema_fast, atr_value):
    if not atr_value or atr_value <= 0:
        return None

    return abs(price - ema_fast) / atr_value


# ============================================================
# REJECTION
# ============================================================

def rejection_score(candle, direction, atr_value):
    o, c, h, l, body, rng, upper, lower, body_ratio = candle_parts(candle)

    if not atr_value or atr_value <= 0:
        return False, 0.0

    range_atr = rng / atr_value

    if range_atr > MAX_TRIGGER_RANGE_ATR:
        return False, 0.0

    if direction == "CALL":
        wick_ratio = lower / rng

        valid = (
            wick_ratio >= MIN_REJECTION_WICK_RATIO and
            c >= o
        )

        strength = clamp(
            wick_ratio,
            0,
            1
        )

    else:
        wick_ratio = upper / rng

        valid = (
            wick_ratio >= MIN_REJECTION_WICK_RATIO and
            c <= o
        )

        strength = clamp(
            wick_ratio,
            0,
            1
        )

    return valid, strength


# ============================================================
# CONFIRMATION
# ============================================================

def confirmation_candle(
    rejection_candle,
    trigger_candle,
    direction,
    atr_value
):
    ro, rc, rh, rl, rb, rr, ru, rlo, rbr = candle_parts(
        rejection_candle
    )

    to, tc, th, tl, tb, tr, tu, tlo, tbr = candle_parts(
        trigger_candle
    )

    if not atr_value or atr_value <= 0:
        return False

    trigger_range_atr = tr / atr_value
    trigger_body_atr = tb / atr_value

    if trigger_range_atr > MAX_TRIGGER_RANGE_ATR:
        return False

    if trigger_body_atr < MIN_TRIGGER_BODY_ATR:
        return False

    if tbr < MIN_TRIGGER_BODY_RATIO:
        return False

    if direction == "CALL":

        return (
            tc > to and
            tc > rh
        )

    else:

        return (
            tc < to and
            tc < rl
        )


# ============================================================
# MOMENTUM
# ============================================================

def momentum_confirmation(
    previous_candle,
    trigger_candle,
    direction,
    atr_value
):
    po, pc, ph, pl, pb, pr, pu, plo, pbr = candle_parts(
        previous_candle
    )

    to, tc, th, tl, tb, tr, tu, tlo, tbr = candle_parts(
        trigger_candle
    )

    if not atr_value or atr_value <= 0:
        return False

    body_atr = tb / atr_value

    if body_atr < MIN_TRIGGER_BODY_ATR:
        return False

    if direction == "CALL":
        return (
            tc > to and
            tc > pc
        )

    return (
        tc < to and
        tc < pc
    )


# ============================================================
# ROOM TO OPPOSING LEVEL
# ============================================================

def calculate_room(
    price,
    direction,
    support,
    resistance,
    atr_value
):
    if not atr_value or atr_value <= 0:
        return 0.0

    if direction == "CALL":

        if resistance is None:
            return 999.0

        return (resistance - price) / atr_value

    else:

        if support is None:
            return 999.0

        return (price - support) / atr_value


# ============================================================
# SCORE
# ============================================================

def score_setup(
    direction,
    trend,
    pullback_atr,
    zone_valid,
    rejection_strength,
    confirmation,
    momentum,
    room_atr,
    pattern_quality
):
    score = 0.0

    # ADX / trend strength: 20
    adx_value = trend["adx"]

    adx_score = clamp(
        (adx_value - MIN_ADX) / 15,
        0,
        1
    ) * 20

    score += adx_score

    # EMA separation: 10
    separation_score = clamp(
        trend["separation_atr"] / 0.50,
        0,
        1
    ) * 10

    score += separation_score

    # Pullback: 15
    if (
        PULLBACK_MIN_ATR <=
        pullback_atr <=
        PULLBACK_MAX_ATR
    ):
        if pullback_atr <= 0.80:
            score += 15
        else:
            score += 12

    # Zone: 15
    if zone_valid:
        score += 15

    # Rejection: 20
    score += clamp(
        rejection_strength,
        0,
        1
    ) * 20

    # Confirmation: 10
    if confirmation:
        score += 10

    # Immediate momentum: 5
    if momentum:
        score += 5

    # Room: 5
    if room_atr >= MIN_ROOM_ATR:
        score += 5

    # Pattern quality: up to 5
    if pattern_quality:
        score += clamp(
            pattern_quality,
            0,
            1
        ) * 5

    return min(score, 100)


# ============================================================
# ANALYZE ASSET
# ============================================================

def analyze_asset(asset, candles_5m, candles_1m):

    candles_5m = remove_open_candle(
        candles_5m,
        TF_5M
    )

    candles_1m = remove_open_candle(
        candles_1m,
        TF_1M
    )

    if (
        len(candles_5m) < 80 or
        len(candles_1m) < 50
    ):
        return None

    trend = analyze_5m_trend(candles_5m)

    if not trend:
        return None

    direction = trend["direction"]

    if direction is None:
        return None

    atr_1m = atr(
        candles_1m,
        ATR_PERIOD
    )

    rsi_1m = rsi(
        candles_1m,
        RSI_PERIOD
    )

    if (
        atr_1m is None or
        rsi_1m is None or
        atr_1m <= 0
    ):
        return None

    price = safe_float(
        candles_1m[-1]["close"]
    )

    pullback_atr = calculate_pullback(
        price,
        trend["ema_fast"],
        atr_1m
    )

    if pullback_atr is None:
        return None

    if (
        pullback_atr <
        PULLBACK_MIN_ATR or
        pullback_atr >
        PULLBACK_MAX_ATR
    ):
        return None

    support, support_touches = recent_support(
        candles_1m,
        atr_1m
    )

    resistance, resistance_touches = recent_resistance(
        candles_1m,
        atr_1m
    )

    tolerance = atr_1m * ZONE_TOLERANCE_ATR

    near_support = (
        support is not None and
        abs(price - support) <= tolerance and
        support_touches >= MIN_ZONE_TOUCHES
    )

    near_resistance = (
        resistance is not None and
        abs(price - resistance) <= tolerance and
        resistance_touches >= MIN_ZONE_TOUCHES
    )

    # --------------------------------------------------------
    # CALL SETUP
    # --------------------------------------------------------

    if direction == "CALL":

        # Reject ambiguous zone
        if near_support and near_resistance:
            return None

        if not near_support:
            return None

        zone_valid = True

        rejection_candle = candles_1m[-2]
        trigger_candle = candles_1m[-1]

        valid_rejection, rejection_strength = rejection_score(
            rejection_candle,
            "CALL",
            atr_1m
        )

        if not valid_rejection:
            return None

        patterns = detect_bullish_patterns(
            candles_1m[-3:]
        )

        if not patterns:
            return None

        pattern_name, pattern_quality = max(
            patterns,
            key=lambda x: x[1]
        )

        confirmed = confirmation_candle(
            rejection_candle,
            trigger_candle,
            "CALL",
            atr_1m
        )

        if not confirmed:
            return None

        momentum = momentum_confirmation(
            candles_1m[-2],
            candles_1m[-1],
            "CALL",
            atr_1m
        )

        if not momentum:
            return None

        room_atr = calculate_room(
            price,
            "CALL",
            support,
            resistance,
            atr_1m
        )

        if room_atr < MIN_ROOM_ATR:
            return None

        if not (
            CALL_RSI_MIN <=
            rsi_1m <=
            CALL_RSI_MAX
        ):
            return None

        extension_atr = abs(
            price - trend["ema_fast"]
        ) / atr_1m

        if extension_atr > MAX_ENTRY_EXTENSION_ATR:
            return None

        score = score_setup(
            "CALL",
            trend,
            pullback_atr,
            zone_valid,
            rejection_strength,
            confirmed,
            momentum,
            room_atr,
            pattern_quality
        )

        if score < MIN_SCORE:
            return None

        return {
            "asset": asset,
            "direction": "CALL",
            "score": round(score, 1),
            "pattern": pattern_name,
            "pattern_quality": round(pattern_quality, 2),
            "rsi": round(rsi_1m, 2),
            "adx": round(trend["adx"], 2),
            "pullback_atr": round(pullback_atr, 2),
            "extension_atr": round(extension_atr, 2),
            "room_atr": round(room_atr, 2),
            "support": support,
            "support_touches": support_touches,
            "resistance": resistance,
            "resistance_touches": resistance_touches,
            "trend": "BULLISH",
        }

    # --------------------------------------------------------
    # PUT SETUP
    # --------------------------------------------------------

    if direction == "PUT":

        # Reject ambiguous zone
        if near_support and near_resistance:
            return None

        if not near_resistance:
            return None

        zone_valid = True

        rejection_candle = candles_1m[-2]
        trigger_candle = candles_1m[-1]

        valid_rejection, rejection_strength = rejection_score(
            rejection_candle,
            "PUT",
            atr_1m
        )

        if not valid_rejection:
            return None

        patterns = detect_bearish_patterns(
            candles_1m[-3:]
        )

        if not patterns:
            return None

        pattern_name, pattern_quality = max(
            patterns,
            key=lambda x: x[1]
        )

        confirmed = confirmation_candle(
            rejection_candle,
            trigger_candle,
            "PUT",
            atr_1m
        )

        if not confirmed:
            return None

        momentum = momentum_confirmation(
            candles_1m[-2],
            candles_1m[-1],
            "PUT",
            atr_1m
        )

        if not momentum:
            return None

        room_atr = calculate_room(
            price,
            "PUT",
            support,
            resistance,
            atr_1m
        )

        if room_atr < MIN_ROOM_ATR:
            return None

        if not (
            PUT_RSI_MIN <=
            rsi_1m <=
            PUT_RSI_MAX
        ):
            return None

        extension_atr = abs(
            price - trend["ema_fast"]
        ) / atr_1m

        if extension_atr > MAX_ENTRY_EXTENSION_ATR:
            return None

        score = score_setup(
            "PUT",
            trend,
            pullback_atr,
            zone_valid,
            rejection_strength,
            confirmed,
            momentum,
            room_atr,
            pattern_quality
        )

        if score < MIN_SCORE:
            return None

        return {
            "asset": asset,
            "direction": "PUT",
            "score": round(score, 1),
            "pattern": pattern_name,
            "pattern_quality": round(pattern_quality, 2),
            "rsi": round(rsi_1m, 2),
            "adx": round(trend["adx"], 2),
            "pullback_atr": round(pullback_atr, 2),
            "extension_atr": round(extension_atr, 2),
            "room_atr": round(room_atr, 2),
            "support": support,
            "support_touches": support_touches,
            "resistance": resistance,
            "resistance_touches": resistance_touches,
            "trend": "BEARISH",
        }

    return None


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

    success, reason = iq.connect()

    if not success:
        raise RuntimeError(
            f"IQ Option connection failed: {reason}"
        )

    if PRACTICE:
        iq.change_balance("PRACTICE")
        balance_type = "PRACTICE"
    else:
        iq.change_balance("REAL")
        balance_type = "REAL"

    print("IQ Option connection: OK")

    send_telegram(
        f"🟢 <b>{VERSION} ONLINE</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Connection: OK\n"
        f"Account: {balance_type}\n"
        f"Strategy: High Quality Trend Pullback\n"
        f"Context: 5M\n"
        f"Entry: 1M\n"
        f"Expiry: {EXPIRY_MINUTES} minutes\n"
        f"Minimum score: {MIN_SCORE}\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Target: {TEST_TARGET} completed trades\n"
        f"Auto-trading: {'ON' if AUTO_TRADE else 'OFF'}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

    return True


# ============================================================
# OTC DISCOVERY
# ============================================================

def discover_otc_assets():

    try:
        open_time = iq.get_all_open_time()
    except Exception as e:
        print(f"OTC discovery error: {e}")
        return []

    assets = []

    for market_type in ["binary", "turbo", "digital"]:

        market = open_time.get(
            market_type,
            {}
        )

        if not isinstance(market, dict):
            continue

        for asset, info in market.items():

            if len(assets) >= MAX_OTC_ASSETS:
                break

            if "OTC" not in str(asset).upper():
                continue

            if not isinstance(info, dict):
                continue

            if info.get("open") is not True:
                continue

            if asset not in assets:
                assets.append(asset)

    return assets


# ============================================================
# CANDLE DATA
# ============================================================

def get_candles(asset, timeframe, count):

    try:
        candles = iq.get_candles(
            asset,
            timeframe,
            count,
            time.time()
        )

        if not candles:
            return []

        return candles

    except Exception as e:
        print(
            f"Candle error {asset} "
            f"{timeframe}s: {e}"
        )

        return []


# ============================================================
# TRADE EXECUTION
# ============================================================

def execute_trade(asset, setup):

    global asset_last_trade

    now = time.time()

    last_trade = asset_last_trade.get(
        asset,
        0
    )

    if now - last_trade < ASSET_LOCK_SECONDS:
        return False

    direction = setup["direction"]

    action = direction.lower()

    signal_id = (
        f"ZETA2-"
        f"{asset.replace('-', '').replace('/', '')}-"
        f"{direction}-"
        f"{int(now)}"
    )

    message = (
        f"🟢 <b>ZETA V2 QUALIFIED SIGNAL</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>Asset:</b> {asset}\n"
        f"<b>Direction:</b> {direction}\n"
        f"<b>Score:</b> {setup['score']}/100\n"
        f"<b>Pattern:</b> {setup['pattern']}\n"
        f"<b>Trend:</b> {setup['trend']}\n"
        f"<b>RSI:</b> {setup['rsi']}\n"
        f"<b>ADX:</b> {setup['adx']}\n"
        f"<b>Pullback:</b> {setup['pullback_atr']} ATR\n"
        f"<b>Room:</b> {setup['room_atr']} ATR\n"
        f"<b>Extension:</b> {setup['extension_atr']} ATR\n"
        f"<b>Expiry:</b> {EXPIRY_MINUTES}m\n"
        f"<b>Stake:</b> ${STAKE:.2f}\n"
        f"<b>Signal ID:</b> <code>{signal_id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

    send_telegram(message)

    print(
        f"[SIGNAL] {asset} "
        f"{direction} "
        f"score={setup['score']} "
        f"pattern={setup['pattern']}"
    )

    if not AUTO_TRADE:
        return False

    try:

        success, trade_id = iq.buy(
            STAKE,
            asset,
            action,
            EXPIRY_MINUTES
        )

        if not success:
            send_telegram(
                f"🔴 <b>ORDER FAILED</b>\n"
                f"{asset} {direction}\n"
                f"Signal: <code>{signal_id}</code>"
            )

            print(
                f"[ORDER FAILED] "
                f"{asset} {direction}"
            )

            return False

        asset_last_trade[asset] = now

        opened_trades[str(trade_id)] = {
            "trade_id": str(trade_id),
            "signal_id": signal_id,
            "asset": asset,
            "direction": direction,
            "stake": STAKE,
            "score": setup["score"],
            "pattern": setup["pattern"],
            "opened_at": now,
        }

        print(
            f"[TRADE OPENED] "
            f"{asset} {direction} "
            f"ID={trade_id}"
        )

        send_telegram(
            f"🚀 <b>DEMO TRADE OPENED</b>\n"
            f"Asset: {asset}\n"
            f"Direction: {direction}\n"
            f"Stake: ${STAKE:.2f}\n"
            f"Expiry: {EXPIRY_MINUTES}m\n"
            f"Score: {setup['score']}/100\n"
            f"Trade ID: <code>{trade_id}</code>"
        )

        return True

    except Exception as e:

        print(
            f"[ORDER ERROR] "
            f"{asset}: {e}"
        )

        send_telegram(
            f"🔴 <b>ORDER ERROR</b>\n"
            f"Asset: {asset}\n"
            f"Error: <code>{str(e)[:400]}</code>"
        )

        return False


# ============================================================
# TRADE RESULT
# ============================================================

def check_trade_result(trade_id, trade):

    global wins
    global losses
    global draws
    global net_pl

    try:

        result = iq.check_win_v4(
            trade_id
        )

        if result is None:
            return False

        result = safe_float(result)

        if result == 0:
            outcome = "DRAW"
            draws += 1

        elif result > 0:
            outcome = "WIN"
            wins += 1

        else:
            outcome = "LOSS"
            losses += 1

        net_pl += result

        completed_trades[str(trade_id)] = {
            **trade,
            "result": result,
            "outcome": outcome,
            "completed_at": time.time(),
        }

        if outcome == "WIN":
            emoji = "🟢"
        elif outcome == "LOSS":
            emoji = "🔴"
        else:
            emoji = "🟡"

        send_telegram(
            f"{emoji} <b>TRADE {outcome}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Asset: {trade['asset']}\n"
            f"Direction: {trade['direction']}\n"
            f"Score: {trade['score']}/100\n"
            f"Pattern: {trade['pattern']}\n"
            f"Result: ${result:.2f}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Wins: {wins}\n"
            f"Losses: {losses}\n"
            f"Draws: {draws}\n"
            f"Completed: {len(completed_trades)}/{TEST_TARGET}\n"
            f"Net P/L: ${net_pl:.2f}"
        )

        print(
            f"[RESULT] "
            f"{trade['asset']} "
            f"{trade['direction']} "
            f"{outcome} "
            f"${result:.2f}"
        )

        return True

    except Exception as e:

        print(
            f"Result check error "
            f"{trade_id}: {e}"
        )

        return False


# ============================================================
# CHECK ALL OPEN TRADES
# ============================================================

def check_open_trades():

    finished = []

    for trade_id, trade in list(
        opened_trades.items()
    ):

        if trade_id in seen_trade_ids:
            finished.append(trade_id)
            continue

        opened_at = trade.get(
            "opened_at",
            time.time()
        )

        # Give IQ Option enough time to settle.
        if (
            time.time() -
            opened_at <
            (EXPIRY_MINUTES * 60) + 10
        ):
            continue

        completed = check_trade_result(
            trade_id,
            trade
        )

        if completed:
            seen_trade_ids.add(
                trade_id
            )

            finished.append(
                trade_id
            )

    for trade_id in finished:
        opened_trades.pop(
            trade_id,
            None
        )


# ============================================================
# STATUS
# ============================================================

def send_status(force=False):

    global last_status_time

    now = time.time()

    if (
        not force and
        now - last_status_time < 300
    ):
        return

    last_status_time = now

    completed = len(
        completed_trades
    )

    active = len(
        opened_trades
    )

    total = wins + losses + draws

    if wins + losses > 0:
        win_rate = (
            wins /
            (wins + losses)
        ) * 100
    else:
        win_rate = 0.0

    runtime = int(
        now - start_time
    )

    hours = runtime // 3600
    minutes = (
        runtime % 3600
    ) // 60

    message = (
        f"🟡 <b>{VERSION} STATUS</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Completed: {completed}/{TEST_TARGET}\n"
        f"Active: {active}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Draws: {draws}\n"
        f"Win rate: {win_rate:.2f}%\n"
        f"Net demo P/L: ${net_pl:.2f}\n"
        f"Runtime: {hours}h {minutes}m\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Account: {'PRACTICE' if PRACTICE else 'REAL'}\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Expiry: {EXPIRY_MINUTES}m\n"
        f"Auto-trading: {'ON' if AUTO_TRADE else 'OFF'}"
    )

    send_telegram(message)

    print(
        f"[STATUS] "
        f"{completed}/{TEST_TARGET} "
        f"W={wins} "
        f"L={losses} "
        f"D={draws} "
        f"P/L=${net_pl:.2f}"
    )


# ============================================================
# FINAL REPORT
# ============================================================

def send_final_report():

    total = wins + losses + draws

    if wins + losses > 0:
        win_rate = (
            wins /
            (wins + losses)
        ) * 100
    else:
        win_rate = 0.0

    message = (
        f"🏁 <b>{VERSION} TEST COMPLETE</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Completed trades: {total}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Draws: {draws}\n"
        f"Win rate: {win_rate:.2f}%\n"
        f"Net demo P/L: ${net_pl:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Expiry: {EXPIRY_MINUTES}m\n"
        f"Mode: {'PRACTICE' if PRACTICE else 'REAL'}"
    )

    send_telegram(message)

    print(message)


# ============================================================
# SCANNER LOOP
# ============================================================

def scanner_loop():

    global start_time

    start_time = time.time()

    print(
        f"🚀 {VERSION} scanner started"
    )

    assets = discover_otc_assets()

    if not assets:
        raise RuntimeError(
            "No currently open OTC instruments were discovered."
        )

    print(
        f"OTC markets discovered: "
        f"{len(assets)}"
    )

    send_telegram(
        f"🔵 <b>{VERSION} SCANNER STARTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"OTC assets: {len(assets)}\n"
        f"Context: 5M\n"
        f"Entry: 1M\n"
        f"Expiry: {EXPIRY_MINUTES}m\n"
        f"Minimum score: {MIN_SCORE}\n"
        f"Target: {TEST_TARGET} completed trades\n"
        f"Mode: {'PRACTICE' if PRACTICE else 'REAL'}"
    )

    send_status(force=True)

    scan_counter = 0

    while True:

        try:

            # ------------------------------------------------
            # CHECK COMPLETED TRADES
            # ------------------------------------------------

            check_open_trades()

            completed_count = len(
                completed_trades
            )

            # ------------------------------------------------
            # STOP AFTER 50 COMPLETED TRADES
            # ------------------------------------------------

            if completed_count >= TEST_TARGET:

                send_final_report()

                print(
                    "🏁 Test target reached."
                )

                break

            # ------------------------------------------------
            # LIMIT ACTIVE POSITIONS
            # ------------------------------------------------

            if len(opened_trades) >= 3:

                send_status()

                time.sleep(5)

                continue

            # ------------------------------------------------
            # REFRESH OTC ASSETS
            # ------------------------------------------------

            if scan_counter % 60 == 0:

                discovered = (
                    discover_otc_assets()
                )

                if discovered:
                    assets = discovered

                    print(
                        f"OTC assets refreshed: "
                        f"{len(assets)}"
                    )

            scan_counter += 1

            # ------------------------------------------------
            # SCAN ASSETS
            # ------------------------------------------------

            for asset in assets:

                if len(opened_trades) >= 3:
                    break

                if (
                    len(completed_trades) >=
                    TEST_TARGET
                ):
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

                    if (
                        not candles_5m or
                        not candles_1m
                    ):
                        continue

                    setup = analyze_asset(
                        asset,
                        candles_5m,
                        candles_1m
                    )

                    if not setup:
                        continue

                    print(
                        f"QUALIFIED: "
                        f"{asset} "
                        f"{setup['direction']} "
                        f"score={setup['score']} "
                        f"pattern={setup['pattern']}"
                    )

                    execute_trade(
                        asset,
                        setup
                    )

                    time.sleep(1)

                except Exception as e:

                    print(
                        f"Asset scan error "
                        f"{asset}: {e}"
                    )

            # ------------------------------------------------
            # STATUS
            # ------------------------------------------------

            send_status()

            time.sleep(5)

        except KeyboardInterrupt:

            print(
                "🛑 Scanner stopped."
            )

            send_telegram(
                f"🛑 <b>{VERSION} STOPPED</b>\n"
                f"Completed: "
                f"{len(completed_trades)}\n"
                f"Wins: {wins}\n"
                f"Losses: {losses}\n"
                f"Net P/L: ${net_pl:.2f}"
            )

            break

        except Exception as e:

            print(
                f"Scanner loop error: {e}"
            )

            traceback.print_exc()

            send_telegram(
                f"⚠️ <b>SCANNER ERROR</b>\n"
                f"<code>{str(e)[:500]}</code>\n"
                f"Scanner will continue."
            )

            time.sleep(10)


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "========================================"
    )

    print(
        f"      {VERSION}"
    )

    print(
        "      IQ OPTION OTC DEMO SCANNER"
    )

    print(
        "========================================"
    )

    # --------------------------------------------------------
    # CHECK SECRETS
    # --------------------------------------------------------

    missing = []

    if not IQ_EMAIL:
        missing.append("IQ_EMAIL")

    if not IQ_PASSWORD:
        missing.append("IQ_PASSWORD")

    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")

    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:

        error = (
            "Missing GitHub Secrets: "
            + ", ".join(missing)
        )

        print(
            f"❌ {error}"
        )

        return

    # --------------------------------------------------------
    # CONNECT
    # --------------------------------------------------------

    try:

        connect()

    except Exception as e:

        print(
            f"❌ Startup connection error: {e}"
        )

        traceback.print_exc()

        send_telegram(
            f"🔴 <b>{VERSION} STARTUP FAILED</b>\n"
            f"<code>{str(e)[:500]}</code>"
        )

        return

    # --------------------------------------------------------
    # START SCANNER
    # --------------------------------------------------------

    try:

        scanner_loop()

    except Exception as e:

        print(
            f"❌ Fatal scanner error: {e}"
        )

        traceback.print_exc()

        send_telegram(
            f"🔴 <b>{VERSION} CRASHED</b>\n"
            f"<code>{str(e)[:500]}</code>"
        )


# ============================================================
# REQUIRED FOR main.py
# ============================================================

if __name__ == "__main__":
    main()
