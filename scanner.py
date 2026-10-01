import os
import time
import traceback
import requests
from datetime import datetime, timezone

from iqoptionapi.stable_api import IQ_Option


# ============================================================
# ZETA V2.2
# HIGH QUALITY TREND PULLBACK DEMO TRADER
# ============================================================

VERSION = "ZETA V2.2"

# ============================================================
# SECRETS
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


# ============================================================
# SETTINGS
# ============================================================

PRACTICE = True
AUTO_TRADE = True

STAKE = 1.0
EXPIRY_MINUTES = 2

TARGET_COMPLETED_TRADES = 50

MAX_ACTIVE_TRADES = 3
MAX_OTC_ASSETS = 70

TF_5M = 300
TF_1M = 60

CANDLES_5M = 180
CANDLES_1M = 180

MIN_SCORE = 80

EMA_FAST = 20
EMA_SLOW = 50

MIN_ADX = 18
MIN_TREND_SEPARATION_ATR = 0.12

RSI_PERIOD = 14

CALL_RSI_MIN = 45
CALL_RSI_MAX = 68

PUT_RSI_MIN = 32
PUT_RSI_MAX = 55

ATR_PERIOD = 14

PULLBACK_MIN_ATR = 0.20
PULLBACK_MAX_ATR = 1.50

MAX_ENTRY_EXTENSION_ATR = 1.80

ZONE_TOLERANCE_ATR = 0.35
SWING_LOOKBACK = 30
MIN_ZONE_TOUCHES = 2

MIN_REJECTION_WICK_RATIO = 0.35

MIN_TRIGGER_BODY_ATR = 0.15
MIN_TRIGGER_BODY_RATIO = 0.40
MAX_TRIGGER_RANGE_ATR = 1.80

MIN_ROOM_ATR = 0.80

ASSET_LOCK_SECONDS = 300

OTC_RETRY_SECONDS = 30
CONNECTION_RETRY_SECONDS = 30

STATUS_INTERVAL = 300


# ============================================================
# GLOBALS
# ============================================================

iq = None

opened_trades = {}
completed_trades = {}

asset_last_trade = {}

wins = 0
losses = 0
draws = 0
net_pl = 0.0

start_time = time.time()
last_status_time = 0

otc_assets = []


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    try:

        url = (
            f"https://api.telegram.org/"
            f"bot{TELEGRAM_TOKEN}/sendMessage"
        )

        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        response = requests.post(
            url,
            data=payload,
            timeout=15
        )

        return response.ok

    except Exception as e:

        print(
            f"Telegram error: {e}"
        )

        return False


# ============================================================
# HELPERS
# ============================================================

def safe_float(value, default=0.0):

    try:
        return float(value)

    except Exception:
        return default


def clamp(value, low, high):

    return max(
        low,
        min(high, value)
    )


def utc_now():

    return datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


# ============================================================
# SMA / EMA
# ============================================================

def sma(values, period):

    if len(values) < period:
        return None

    return sum(
        values[-period:]
    ) / period


def ema(values, period):

    if len(values) < period:
        return None

    result = sum(
        values[:period]
    ) / period

    multiplier = 2 / (period + 1)

    for value in values[period:]:

        result = (
            (value - result) *
            multiplier
        ) + result

    return result


# ============================================================
# ATR
# ============================================================

def atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(1, len(candles)):

        high = safe_float(
            candles[i]["max"]
        )

        low = safe_float(
            candles[i]["min"]
        )

        previous_close = safe_float(
            candles[i - 1]["close"]
        )

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )

        trs.append(tr)

    if len(trs) < period:
        return None

    return sum(
        trs[-period:]
    ) / period


# ============================================================
# RSI
# ============================================================

def rsi(candles, period=14):

    if len(candles) < period + 1:
        return None

    closes = [
        safe_float(c["close"])
        for c in candles
    ]

    gains = []
    losses_ = []

    for i in range(1, len(closes)):

        change = (
            closes[i] -
            closes[i - 1]
        )

        if change > 0:

            gains.append(change)
            losses_.append(0)

        else:

            gains.append(0)
            losses_.append(
                abs(change)
            )

    avg_gain = (
        sum(gains[:period]) /
        period
    )

    avg_loss = (
        sum(losses_[:period]) /
        period
    )

    for i in range(
        period,
        len(gains)
    ):

        avg_gain = (
            (
                avg_gain *
                (period - 1)
            ) +
            gains[i]
        ) / period

        avg_loss = (
            (
                avg_loss *
                (period - 1)
            ) +
            losses_[i]
        ) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss

    return 100 - (
        100 /
        (1 + rs)
    )


# ============================================================
# ADX
# ============================================================

def adx(candles, period=14):

    if len(candles) < 2 * period + 5:
        return None

    highs = [
        safe_float(c["max"])
        for c in candles
    ]

    lows = [
        safe_float(c["min"])
        for c in candles
    ]

    closes = [
        safe_float(c["close"])
        for c in candles
    ]

    tr_values = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(candles)):

        up_move = (
            highs[i] -
            highs[i - 1]
        )

        down_move = (
            lows[i - 1] -
            lows[i]
        )

        tr = max(
            highs[i] - lows[i],
            abs(
                highs[i] -
                closes[i - 1]
            ),
            abs(
                lows[i] -
                closes[i - 1]
            )
        )

        tr_values.append(tr)

        if (
            up_move > down_move and
            up_move > 0
        ):
            plus_dm.append(up_move)

        else:
            plus_dm.append(0)

        if (
            down_move > up_move and
            down_move > 0
        ):
            minus_dm.append(
                down_move
            )

        else:
            minus_dm.append(0)

    dx_values = []

    for i in range(
        period,
        len(tr_values)
    ):

        tr_sum = sum(
            tr_values[
                i - period:i
            ]
        )

        plus_sum = sum(
            plus_dm[
                i - period:i
            ]
        )

        minus_sum = sum(
            minus_dm[
                i - period:i
            ]
        )

        if tr_sum == 0:
            continue

        plus_di = (
            100 *
            plus_sum /
            tr_sum
        )

        minus_di = (
            100 *
            minus_sum /
            tr_sum
        )

        denominator = (
            plus_di +
            minus_di
        )

        if denominator == 0:
            continue

        dx = (
            100 *
            abs(
                plus_di -
                minus_di
            ) /
            denominator
        )

        dx_values.append(dx)

    if len(dx_values) < period:
        return None

    return (
        sum(dx_values[-period:]) /
        period
    )


# ============================================================
# CANDLE STRUCTURE
# ============================================================

def candle_parts(candle):

    o = safe_float(
        candle["open"]
    )

    c = safe_float(
        candle["close"]
    )

    h = safe_float(
        candle["max"]
    )

    l = safe_float(
        candle["min"]
    )

    body = abs(c - o)

    rng = max(
        h - l,
        0.00000001
    )

    upper = (
        h -
        max(o, c)
    )

    lower = (
        min(o, c) -
        l
    )

    body_ratio = (
        body / rng
    )

    return (
        o,
        c,
        h,
        l,
        body,
        rng,
        upper,
        lower,
        body_ratio
    )


# ============================================================
# CANDLE PATTERNS
# ============================================================

def bullish_engulfing(previous, current):

    po, pc, _, _, _, _, _, _, _ = (
        candle_parts(previous)
    )

    co, cc, _, _, _, _, _, _, cbr = (
        candle_parts(current)
    )

    return (
        pc < po and
        cc > co and
        co <= pc and
        cc >= po and
        cbr >= 0.45
    )


def bearish_engulfing(previous, current):

    po, pc, _, _, _, _, _, _, _ = (
        candle_parts(previous)
    )

    co, cc, _, _, _, _, _, _, cbr = (
        candle_parts(current)
    )

    return (
        pc > po and
        cc < co and
        co >= pc and
        cc <= po and
        cbr >= 0.45
    )


def hammer(candle):

    (
        o,
        c,
        h,
        l,
        body,
        rng,
        upper,
        lower,
        body_ratio
    ) = candle_parts(candle)

    return (
        lower >= body * 1.8 and
        upper <= rng * 0.30 and
        body_ratio >= 0.20
    )


def shooting_star(candle):

    (
        o,
        c,
        h,
        l,
        body,
        rng,
        upper,
        lower,
        body_ratio
    ) = candle_parts(candle)

    return (
        upper >= body * 1.8 and
        lower <= rng * 0.30 and
        body_ratio >= 0.20
    )


def bullish_pin_bar(candle):

    (
        o,
        c,
        h,
        l,
        body,
        rng,
        upper,
        lower,
        body_ratio
    ) = candle_parts(candle)

    return (
        lower >= rng * 0.45 and
        upper <= rng * 0.25
    )


def bearish_pin_bar(candle):

    (
        o,
        c,
        h,
        l,
        body,
        rng,
        upper,
        lower,
        body_ratio
    ) = candle_parts(candle)

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

    fo, fc, _, _, _, _, _, _, fbr = (
        candle_parts(first)
    )

    _, _, _, _, _, _, _, _, sbr = (
        candle_parts(second)
    )

    to, tc, _, _, _, _, _, _, tbr = (
        candle_parts(third)
    )

    first_mid = (
        fo + fc
    ) / 2

    return (
        fc < fo and
        tc > to and
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

    fo, fc, _, _, _, _, _, _, fbr = (
        candle_parts(first)
    )

    _, _, _, _, _, _, _, _, sbr = (
        candle_parts(second)
    )

    to, tc, _, _, _, _, _, _, tbr = (
        candle_parts(third)
    )

    first_mid = (
        fo + fc
    ) / 2

    return (
        fc > fo and
        tc < to and
        fbr >= 0.45 and
        sbr <= 0.40 and
        tbr >= 0.45 and
        tc < first_mid
    )


def bullish_patterns(candles):

    patterns = []

    if morning_star(candles):
        patterns.append(
            ("Morning Star", 0.90)
        )

    if len(candles) >= 2:

        if bullish_engulfing(
            candles[-2],
            candles[-1]
        ):
            patterns.append(
                ("Bullish Engulfing", 0.88)
            )

    if hammer(candles[-1]):

        patterns.append(
            ("Hammer", 0.78)
        )

    if bullish_pin_bar(
        candles[-1]
    ):

        patterns.append(
            ("Bullish Pin Bar", 0.78)
        )

    return patterns


def bearish_patterns(candles):

    patterns = []

    if evening_star(candles):

        patterns.append(
            ("Evening Star", 0.90)
        )

    if len(candles) >= 2:

        if bearish_engulfing(
            candles[-2],
            candles[-1]
        ):
            patterns.append(
                ("Bearish Engulfing", 0.88)
            )

    if shooting_star(
        candles[-1]
    ):

        patterns.append(
            ("Shooting Star", 0.78)
        )

    if bearish_pin_bar(
        candles[-1]
    ):

        patterns.append(
            ("Bearish Pin Bar", 0.78)
        )

    return patterns


# ============================================================
# CLOSED CANDLES ONLY
# ============================================================

def remove_open_candle(
    candles,
    timeframe
):

    if not candles:
        return candles

    try:

        last = candles[-1]

        candle_start = safe_float(
            last.get("from", 0)
        )

        if (
            candle_start +
            timeframe >
            time.time()
        ):

            return candles[:-1]

    except Exception:
        pass

    return candles


# ============================================================
# SUPPORT / RESISTANCE
# ============================================================

def recent_support(
    candles,
    atr_value
):

    sample = candles[
        -SWING_LOOKBACK:
    ]

    if not sample:
        return None, 0

    lowest = min(
        safe_float(c["min"])
        for c in sample
    )

    tolerance = (
        atr_value *
        ZONE_TOLERANCE_ATR
    )

    touches = 0

    for candle in sample:

        low = safe_float(
            candle["min"]
        )

        if (
            abs(low - lowest)
            <= tolerance
        ):
            touches += 1

    return lowest, touches


def recent_resistance(
    candles,
    atr_value
):

    sample = candles[
        -SWING_LOOKBACK:
    ]

    if not sample:
        return None, 0

    highest = max(
        safe_float(c["max"])
        for c in sample
    )

    tolerance = (
        atr_value *
        ZONE_TOLERANCE_ATR
    )

    touches = 0

    for candle in sample:

        high = safe_float(
            candle["max"]
        )

        if (
            abs(high - highest)
            <= tolerance
        ):
            touches += 1

    return highest, touches


# ============================================================
# TREND
# ============================================================

def analyze_trend(candles):

    closes = [
        safe_float(c["close"])
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

    atr_value = atr(
        candles,
        ATR_PERIOD
    )

    adx_value = adx(
        candles,
        14
    )

    if (
        fast is None or
        slow is None or
        atr_value is None or
        adx_value is None or
        atr_value <= 0
    ):
        return None

    price = closes[-1]

    separation = abs(
        fast - slow
    )

    separation_atr = (
        separation /
        atr_value
    )

    if (
        fast > slow and
        price > fast and
        separation_atr >=
        MIN_TREND_SEPARATION_ATR and
        adx_value >= MIN_ADX
    ):

        direction = "CALL"

    elif (
        fast < slow and
        price < fast and
        separation_atr >=
        MIN_TREND_SEPARATION_ATR and
        adx_value >= MIN_ADX
    ):

        direction = "PUT"

    else:

        direction = None

    return {
        "direction": direction,
        "ema_fast": fast,
        "ema_slow": slow,
        "atr": atr_value,
        "adx": adx_value,
        "separation_atr": separation_atr,
        "price": price,
    }


# ============================================================
# REJECTION
# ============================================================

def rejection(
    candle,
    direction,
    atr_value
):

    (
        o,
        c,
        h,
        l,
        body,
        rng,
        upper,
        lower,
        body_ratio
    ) = candle_parts(candle)

    if rng <= 0:
        return False, 0

    if (
        rng /
        atr_value >
        MAX_TRIGGER_RANGE_ATR
    ):
        return False, 0

    if direction == "CALL":

        ratio = (
            lower /
            rng
        )

        valid = (
            ratio >=
            MIN_REJECTION_WICK_RATIO
        )

    else:

        ratio = (
            upper /
            rng
        )

        valid = (
            ratio >=
            MIN_REJECTION_WICK_RATIO
        )

    return (
        valid,
        clamp(ratio, 0, 1)
    )


# ============================================================
# CONFIRMATION
# ============================================================

def confirmation(
    rejection_candle,
    trigger_candle,
    direction,
    atr_value
):

    (
        ro,
        rc,
        rh,
        rl,
        rb,
        rr,
        ru,
        rlo,
        rbr
    ) = candle_parts(
        rejection_candle
    )

    (
        to,
        tc,
        th,
        tl,
        tb,
        tr,
        tu,
        tlo,
        tbr
    ) = candle_parts(
        trigger_candle
    )

    if (
        tr /
        atr_value >
        MAX_TRIGGER_RANGE_ATR
    ):
        return False

    if (
        tb /
        atr_value <
        MIN_TRIGGER_BODY_ATR
    ):
        return False

    if tbr < MIN_TRIGGER_BODY_RATIO:
        return False

    if direction == "CALL":

        return (
            tc > to and
            tc > rh
        )

    return (
        tc < to and
        tc < rl
    )


# ============================================================
# MOMENTUM
# ============================================================

def momentum(
    previous,
    trigger,
    direction,
    atr_value
):

    (
        _,
        pc,
        _,
        _,
        _,
        _,
        _,
        _,
        _
    ) = candle_parts(previous)

    (
        to,
        tc,
        _,
        _,
        tb,
        _,
        _,
        _,
        _
    ) = candle_parts(trigger)

    if (
        tb /
        atr_value <
        MIN_TRIGGER_BODY_ATR
    ):
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
# ANALYZE SETUP
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
        len(candles_5m) < 80 or
        len(candles_1m) < 50
    ):
        return None

    trend = analyze_trend(
        candles_5m
    )

    if not trend:
        return None

    direction = (
        trend["direction"]
    )

    if direction is None:
        return None

    atr_value = atr(
        candles_1m,
        ATR_PERIOD
    )

    rsi_value = rsi(
        candles_1m,
        RSI_PERIOD
    )

    if (
        atr_value is None or
        rsi_value is None or
        atr_value <= 0
    ):
        return None

    price = safe_float(
        candles_1m[-1]["close"]
    )

    pullback = (
        abs(
            price -
            trend["ema_fast"]
        ) /
        atr_value
    )

    if (
        pullback <
        PULLBACK_MIN_ATR or
        pullback >
        PULLBACK_MAX_ATR
    ):
        return None

    support, support_touches = (
        recent_support(
            candles_1m,
            atr_value
        )
    )

    resistance, resistance_touches = (
        recent_resistance(
            candles_1m,
            atr_value
        )
    )

    tolerance = (
        atr_value *
        ZONE_TOLERANCE_ATR
    )

    near_support = (
        support is not None and
        abs(price - support)
        <= tolerance and
        support_touches >=
        MIN_ZONE_TOUCHES
    )

    near_resistance = (
        resistance is not None and
        abs(price - resistance)
        <= tolerance and
        resistance_touches >=
        MIN_ZONE_TOUCHES
    )

    # ========================================================
    # CALL
    # ========================================================

    if direction == "CALL":

        if (
            near_support and
            near_resistance
        ):
            return None

        if not near_support:
            return None

        rejection_candle = (
            candles_1m[-2]
        )

        trigger_candle = (
            candles_1m[-1]
        )

        valid_rejection, rejection_strength = (
            rejection(
                rejection_candle,
                "CALL",
                atr_value
            )
        )

        if not valid_rejection:
            return None

        patterns = bullish_patterns(
            candles_1m[-3:]
        )

        if not patterns:
            return None

        pattern_name, pattern_quality = max(
            patterns,
            key=lambda x: x[1]
        )

        confirmed = confirmation(
            rejection_candle,
            trigger_candle,
            "CALL",
            atr_value
        )

        if not confirmed:
            return None

        immediate_momentum = momentum(
            rejection_candle,
            trigger_candle,
            "CALL",
            atr_value
        )

        if not immediate_momentum:
            return None

        if not (
            CALL_RSI_MIN <=
            rsi_value <=
            CALL_RSI_MAX
        ):
            return None

        room = (
            999.0
            if resistance is None
            else (
                resistance -
                price
            ) /
            atr_value
        )

        if room < MIN_ROOM_ATR:
            return None

        extension = (
            abs(
                price -
                trend["ema_fast"]
            ) /
            atr_value
        )

        if (
            extension >
            MAX_ENTRY_EXTENSION_ATR
        ):
            return None

        score = 0

        score += clamp(
            (
                trend["adx"] -
                MIN_ADX
            ) / 15,
            0,
            1
        ) * 20

        score += clamp(
            trend["separation_atr"]
            / 0.50,
            0,
            1
        ) * 10

        score += 15

        score += 15

        score += (
            clamp(
                rejection_strength,
                0,
                1
            ) * 20
        )

        score += 10

        score += 5

        score += 5

        score += (
            pattern_quality *
            5
        )

        score = min(
            score,
            100
        )

        if score < MIN_SCORE:
            return None

        return {
            "asset": asset,
            "direction": "CALL",
            "score": round(score, 1),
            "pattern": pattern_name,
            "rsi": round(
                rsi_value,
                2
            ),
            "adx": round(
                trend["adx"],
                2
            ),
            "pullback_atr": round(
                pullback,
                2
            ),
            "extension_atr": round(
                extension,
                2
            ),
            "room_atr": round(
                room,
                2
            ),
            "trend": "BULLISH",
        }

    # ========================================================
    # PUT
    # ========================================================

    if direction == "PUT":

        if (
            near_support and
            near_resistance
        ):
            return None

        if not near_resistance:
            return None

        rejection_candle = (
            candles_1m[-2]
        )

        trigger_candle = (
            candles_1m[-1]
        )

        valid_rejection, rejection_strength = (
            rejection(
                rejection_candle,
                "PUT",
                atr_value
            )
        )

        if not valid_rejection:
            return None

        patterns = bearish_patterns(
            candles_1m[-3:]
        )

        if not patterns:
            return None

        pattern_name, pattern_quality = max(
            patterns,
            key=lambda x: x[1]
        )

        confirmed = confirmation(
            rejection_candle,
            trigger_candle,
            "PUT",
            atr_value
        )

        if not confirmed:
            return None

        immediate_momentum = momentum(
            rejection_candle,
            trigger_candle,
            "PUT",
            atr_value
        )

        if not immediate_momentum:
            return None

        if not (
            PUT_RSI_MIN <=
            rsi_value <=
            PUT_RSI_MAX
        ):
            return None

        room = (
            999.0
            if support is None
            else (
                price -
                support
            ) /
            atr_value
        )

        if room < MIN_ROOM_ATR:
            return None

        extension = (
            abs(
                price -
                trend["ema_fast"]
            ) /
            atr_value
        )

        if (
            extension >
            MAX_ENTRY_EXTENSION_ATR
        ):
            return None

        score = 0

        score += clamp(
            (
                trend["adx"] -
                MIN_ADX
            ) / 15,
            0,
            1
        ) * 20

        score += clamp(
            trend["separation_atr"]
            / 0.50,
            0,
            1
        ) * 10

        score += 15

        score += 15

        score += (
            clamp(
                rejection_strength,
                0,
                1
            ) * 20
        )

        score += 10

        score += 5

        score += 5

        score += (
            pattern_quality *
            5
        )

        score = min(
            score,
            100
        )

        if score < MIN_SCORE:
            return None

        return {
            "asset": asset,
            "direction": "PUT",
            "score": round(score, 1),
            "pattern": pattern_name,
            "rsi": round(
                rsi_value,
                2
            ),
            "adx": round(
                trend["adx"],
                2
            ),
            "pullback_atr": round(
                pullback,
                2
            ),
            "extension_atr": round(
                extension,
                2
            ),
            "room_atr": round(
                room,
                2
            ),
            "trend": "BEARISH",
        }

    return None


# ============================================================
# CONNECT
# ============================================================

def connect():

    global iq

    print(
        "Connecting to IQ Option..."
    )

    iq = IQ_Option(
        IQ_EMAIL,
        IQ_PASSWORD
    )

    success, reason = iq.connect()

    if not success:

        raise RuntimeError(
            f"IQ Option connection failed: "
            f"{reason}"
        )

    if PRACTICE:

        iq.change_balance(
            "PRACTICE"
        )

        account = "PRACTICE"

    else:

        iq.change_balance(
            "REAL"
        )

        account = "REAL"

    print(
        "IQ Option connection: OK"
    )

    send_telegram(
        f"🟢 <b>{VERSION} ONLINE</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Connection: OK\n"
        f"Account: {account}\n"
        f"Strategy: High Quality Trend Pullback\n"
        f"Context: 5M\n"
        f"Entry: 1M\n"
        f"Expiry: {EXPIRY_MINUTES} minutes\n"
        f"Minimum score: {MIN_SCORE}\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Target: {TARGET_COMPLETED_TRADES} completed trades\n"
        f"Auto-trading: "
        f"{'ON' if AUTO_TRADE else 'OFF'}"
    )

    return True


# ============================================================
# CONNECTION TEST
# ============================================================

def connection_alive():

    try:

        if iq is None:
            return False

        # The API normally exposes this websocket object.
        websocket = getattr(
            iq,
            "websocket_client",
            None
        )

        if websocket is None:
            return True

        return True

    except Exception:

        return False


# ============================================================
# ROBUST OTC DISCOVERY
# ============================================================

def discover_otc_assets():

    print(
        "🔎 Discovering IQ Option OTC markets..."
    )

    try:

        # ----------------------------------------------------
        # PRIMARY METHOD
        # ----------------------------------------------------

        data = iq.get_all_open_time()

        if not isinstance(
            data,
            dict
        ):
            data = {}

        found = []

        # Check every market section instead
        # of depending on only one.
        for market_name, market_data in data.items():

            if not isinstance(
                market_data,
                dict
            ):
                continue

            for asset, info in market_data.items():

                if not isinstance(
                    info,
                    dict
                ):
                    continue

                name = str(asset).upper()

                if "OTC" not in name:
                    continue

                # Some IQ Option API versions
                # expose open as bool.
                if info.get("open") is True:

                    if asset not in found:

                        found.append(
                            asset
                        )

        # ----------------------------------------------------
        # FALLBACK: CHECK ACTIVE INFORMATION
        # ----------------------------------------------------

        if not found:

            try:

                init_data = (
                    iq.get_all_init_v2()
                )

                if isinstance(
                    init_data,
                    dict
                ):

                    for section in [
                        "binary",
                        "turbo",
                        "digital",
                        "forex",
                        "stocks",
                        "crypto",
                        "commodities"
                    ]:

                        section_data = (
                            init_data.get(
                                section,
                                {}
                            )
                        )

                        if not isinstance(
                            section_data,
                            dict
                        ):
                            continue

                        for asset, info in (
                            section_data.items()
                        ):

                            name = str(
                                asset
                            ).upper()

                            if (
                                "OTC"
                                not in name
                            ):
                                continue

                            if (
                                isinstance(
                                    info,
                                    dict
                                )
                            ):

                                if (
                                    info.get(
                                        "open"
                                    ) is True
                                ):

                                    if (
                                        asset
                                        not in
                                        found
                                    ):

                                        found.append(
                                            asset
                                        )

            except Exception as e:

                print(
                    f"OTC fallback error: {e}"
                )

        # ----------------------------------------------------
        # LIMIT
        # ----------------------------------------------------

        found = found[
            :MAX_OTC_ASSETS
        ]

        print(
            f"OTC markets discovered: "
            f"{len(found)}"
        )

        return found

    except Exception as e:

        print(
            f"OTC discovery error: {e}"
        )

        return []


# ============================================================
# WAIT UNTIL OTC EXISTS
# ============================================================

def wait_for_otc():

    global otc_assets

    while True:

        otc_assets = (
            discover_otc_assets()
        )

        if otc_assets:

            send_telegram(
                f"🟢 <b>OTC MARKETS FOUND</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"OTC assets: "
                f"{len(otc_assets)}\n"
                f"Scanner is starting."
            )

            return True

        send_telegram(
            f"🟡 <b>OTC DISCOVERY RETRY</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"No OTC feeds returned yet.\n"
            f"IQ connection is still active.\n"
            f"Retrying in "
            f"{OTC_RETRY_SECONDS}s."
        )

        print(
            "⚠️ 0 OTC feeds. "
            "Waiting and retrying..."
        )

        time.sleep(
            OTC_RETRY_SECONDS
        )


# ============================================================
# CANDLES
# ============================================================

def get_candles(
    asset,
    timeframe,
    count
):

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
            f"Candle error "
            f"{asset}: {e}"
        )

        return []


# ============================================================
# EXECUTE
# ============================================================

def execute_trade(
    asset,
    setup
):

    now = time.time()

    previous_trade = (
        asset_last_trade.get(
            asset,
            0
        )
    )

    if (
        now -
        previous_trade <
        ASSET_LOCK_SECONDS
    ):
        return False

    direction = (
        setup["direction"]
    )

    action = (
        "call"
        if direction == "CALL"
        else "put"
    )

    signal_id = (
        "ZETA2-"
        +
        asset.replace(
            "-",
            ""
        ).replace(
            "/",
            ""
        )
        +
        "-"
        +
        direction
        +
        "-"
        +
        str(
            int(now)
        )
    )

    send_telegram(
        f"🟢 <b>ZETA V2 QUALIFIED SIGNAL</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Asset: {asset}\n"
        f"Direction: {direction}\n"
        f"Score: {setup['score']}/100\n"
        f"Pattern: {setup['pattern']}\n"
        f"Trend: {setup['trend']}\n"
        f"RSI: {setup['rsi']}\n"
        f"ADX: {setup['adx']}\n"
        f"Pullback: "
        f"{setup['pullback_atr']} ATR\n"
        f"Room: "
        f"{setup['room_atr']} ATR\n"
        f"Extension: "
        f"{setup['extension_atr']} ATR\n"
        f"Expiry: "
        f"{EXPIRY_MINUTES}m\n"
        f"Stake: "
        f"${STAKE:.2f}\n"
        f"Signal ID: "
        f"<code>{signal_id}</code>"
    )

    if not AUTO_TRADE:

        print(
            "AUTO_TRADE OFF"
        )

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
                f"Asset: {asset}\n"
                f"Direction: {direction}\n"
                f"Signal: "
                f"<code>{signal_id}</code>"
            )

            return False

        asset_last_trade[
            asset
        ] = now

        trade_id = str(
            trade_id
        )

        opened_trades[
            trade_id
        ] = {
            "trade_id": trade_id,
            "signal_id": signal_id,
            "asset": asset,
            "direction": direction,
            "stake": STAKE,
            "score": setup["score"],
            "pattern": setup["pattern"],
            "opened_at": now,
        }

        print(
            f"🚀 TRADE OPENED "
            f"{asset} "
            f"{direction} "
            f"{trade_id}"
        )

        send_telegram(
            f"🚀 <b>DEMO TRADE OPENED</b>\n"
            f"Asset: {asset}\n"
            f"Direction: {direction}\n"
            f"Stake: ${STAKE:.2f}\n"
            f"Expiry: {EXPIRY_MINUTES}m\n"
            f"Score: {setup['score']}/100\n"
            f"Trade ID: "
            f"<code>{trade_id}</code>"
        )

        return True

    except Exception as e:

        print(
            f"Trade execution error: {e}"
        )

        send_telegram(
            f"🔴 <b>TRADE ERROR</b>\n"
            f"Asset: {asset}\n"
            f"Error: "
            f"<code>{str(e)[:400]}</code>"
        )

        return False


# ============================================================
# RESULT
# ============================================================

def check_trade(
    trade_id,
    trade
):

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

        result = safe_float(
            result
        )

        if result > 0:

            outcome = "WIN"
            wins += 1
            emoji = "🟢"

        elif result < 0:

            outcome = "LOSS"
            losses += 1
            emoji = "🔴"

        else:

            outcome = "DRAW"
            draws += 1
            emoji = "🟡"

        net_pl += result

        completed_trades[
            trade_id
        ] = {
            **trade,
            "result": result,
            "outcome": outcome,
            "completed_at": time.time(),
        }

        completed = (
            len(completed_trades)
        )

        if wins + losses:

            win_rate = (
                wins /
                (wins + losses)
            ) * 100

        else:

            win_rate = 0

        send_telegram(
            f"{emoji} <b>{outcome}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Asset: {trade['asset']}\n"
            f"Direction: "
            f"{trade['direction']}\n"
            f"Score: "
            f"{trade['score']}/100\n"
            f"Pattern: "
            f"{trade['pattern']}\n"
            f"Result: "
            f"${result:.2f}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Wins: {wins}\n"
            f"Losses: {losses}\n"
            f"Draws: {draws}\n"
            f"Win rate: "
            f"{win_rate:.2f}%\n"
            f"Completed: "
            f"{completed}/"
            f"{TARGET_COMPLETED_TRADES}\n"
            f"Net P/L: "
            f"${net_pl:.2f}"
        )

        return True

    except Exception as e:

        print(
            f"Result error "
            f"{trade_id}: {e}"
        )

        return False


# ============================================================
# CHECK OPEN TRADES
# ============================================================

def check_open_trades():

    finished = []

    for trade_id, trade in list(
        opened_trades.items()
    ):

        opened_at = trade[
            "opened_at"
        ]

        minimum_wait = (
            EXPIRY_MINUTES *
            60
        ) + 15

        if (
            time.time() -
            opened_at <
            minimum_wait
        ):
            continue

        if check_trade(
            trade_id,
            trade
        ):

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

def send_status(
    force=False
):

    global last_status_time

    now = time.time()

    if (
        not force and
        now -
        last_status_time <
        STATUS_INTERVAL
    ):
        return

    last_status_time = now

    completed = (
        len(completed_trades)
    )

    if wins + losses:

        win_rate = (
            wins /
            (wins + losses)
        ) * 100

    else:

        win_rate = 0

    runtime = int(
        now -
        start_time
    )

    hours = (
        runtime //
        3600
    )

    minutes = (
        runtime %
        3600
    ) // 60

    send_telegram(
        f"🟡 <b>{VERSION} STATUS</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"OTC assets: "
        f"{len(otc_assets)}\n"
        f"Completed: "
        f"{completed}/"
        f"{TARGET_COMPLETED_TRADES}\n"
        f"Active: "
        f"{len(opened_trades)}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Draws: {draws}\n"
        f"Win rate: "
        f"{win_rate:.2f}%\n"
        f"Net P/L: "
        f"${net_pl:.2f}\n"
        f"Runtime: "
        f"{hours}h {minutes}m\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Account: "
        f"{'PRACTICE' if PRACTICE else 'REAL'}\n"
        f"Stake: "
        f"${STAKE:.2f}\n"
        f"Expiry: "
        f"{EXPIRY_MINUTES}m"
    )


# ============================================================
# FINAL REPORT
# ============================================================

def final_report():

    completed = (
        len(completed_trades)
    )

    if wins + losses:

        win_rate = (
            wins /
            (wins + losses)
        ) * 100

    else:

        win_rate = 0

    send_telegram(
        f"🏁 <b>ZETA V2.2 TEST COMPLETE</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Completed: {completed}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Draws: {draws}\n"
        f"Win rate: "
        f"{win_rate:.2f}%\n"
        f"Net demo P/L: "
        f"${net_pl:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Expiry: {EXPIRY_MINUTES}m\n"
        f"Account: PRACTICE"
    )


# ============================================================
# MAIN SCANNER
# ============================================================

def scanner_loop():

    global otc_assets

    send_telegram(
        f"🔵 <b>{VERSION} SCANNER STARTING</b>\n"
        f"Waiting for OTC markets..."
    )

    # --------------------------------------------------------
    # THIS NO LONGER CRASHES WHEN OTC = 0
    # --------------------------------------------------------

    wait_for_otc()

    send_status(
        force=True
    )

    scan_counter = 0

    while True:

        try:

            # ------------------------------------------------
            # CHECK RESULTS
            # ------------------------------------------------

            check_open_trades()

            # ------------------------------------------------
            # STOP ONLY AFTER COMPLETED TARGET
            # ------------------------------------------------

            if (
                len(completed_trades) >=
                TARGET_COMPLETED_TRADES
            ):

                final_report()

                print(
                    "🏁 50 completed trades reached."
                )

                return

            # ------------------------------------------------
            # REFRESH OTC PERIODICALLY
            # ------------------------------------------------

            scan_counter += 1

            if scan_counter % 30 == 0:

                new_assets = (
                    discover_otc_assets()
                )

                if new_assets:

                    otc_assets = (
                        new_assets
                    )

                    print(
                        f"OTC refresh: "
                        f"{len(otc_assets)}"
                    )

                else:

                    print(
                        "⚠️ OTC refresh "
                        "returned 0. "
                        "Keeping previous "
                        "working list."
                    )

            # ------------------------------------------------
            # IF LIST IS EMPTY, RETRY
            # ------------------------------------------------

            if not otc_assets:

                wait_for_otc()

                continue

            # ------------------------------------------------
            # MAX ACTIVE TRADES
            # ------------------------------------------------

            if (
                len(opened_trades) >=
                MAX_ACTIVE_TRADES
            ):

                send_status()

                time.sleep(5)

                continue

            # ------------------------------------------------
            # SCAN OTC
            # ------------------------------------------------

            for asset in list(
                otc_assets
            ):

                if (
                    len(opened_trades) >=
                    MAX_ACTIVE_TRADES
                ):
                    break

                if (
                    len(completed_trades) >=
                    TARGET_COMPLETED_TRADES
                ):
                    break

                try:

                    candles_5m = (
                        get_candles(
                            asset,
                            TF_5M,
                            CANDLES_5M
                        )
                    )

                    candles_1m = (
                        get_candles(
                            asset,
                            TF_1M,
                            CANDLES_1M
                        )
                    )

                    if (
                        not candles_5m or
                        not candles_1m
                    ):
                        continue

                    setup = (
                        analyze_asset(
                            asset,
                            candles_5m,
                            candles_1m
                        )
                    )

                    if not setup:
                        continue

                    print(
                        f"QUALIFIED | "
                        f"{asset} | "
                        f"{setup['direction']} | "
                        f"score={setup['score']} | "
                        f"{setup['pattern']}"
                    )

                    execute_trade(
                        asset,
                        setup
                    )

                    time.sleep(1)

                except Exception as e:

                    print(
                        f"Asset error "
                        f"{asset}: {e}"
                    )

            send_status()

            time.sleep(5)

        except KeyboardInterrupt:

            print(
                "Scanner stopped."
            )

            return

        except Exception as e:

            # IMPORTANT:
            # Do not crash the whole bot.
            print(
                f"⚠️ Scanner loop error: "
                f"{e}"
            )

            traceback.print_exc()

            send_telegram(
                f"⚠️ <b>SCANNER RECOVERING</b>\n"
                f"Error: "
                f"<code>{str(e)[:400]}</code>\n"
                f"Retrying..."
            )

            time.sleep(
                CONNECTION_RETRY_SECONDS
            )

            # Try reconnecting.
            try:

                connect()

            except Exception as reconnect_error:

                print(
                    f"Reconnect failed: "
                    f"{reconnect_error}"
                )

                time.sleep(
                    CONNECTION_RETRY_SECONDS
                )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "========================================"
    )

    print(
        f"       {VERSION}"
    )

    print(
        "       IQ OPTION OTC DEMO BOT"
    )

    print(
        "========================================"
    )

    # --------------------------------------------------------
    # CHECK SECRETS
    # --------------------------------------------------------

    missing = []

    if not IQ_EMAIL:
        missing.append(
            "IQ_EMAIL"
        )

    if not IQ_PASSWORD:
        missing.append(
            "IQ_PASSWORD"
        )

    if not TELEGRAM_TOKEN:
        missing.append(
            "TELEGRAM_TOKEN"
        )

    if not TELEGRAM_CHAT_ID:
        missing.append(
            "TELEGRAM_CHAT_ID"
        )

    if missing:

        error = (
            "Missing secrets: "
            +
            ", ".join(missing)
        )

        print(
            f"❌ {error}"
        )

        return

    # --------------------------------------------------------
    # CONNECT WITH RETRIES
    # --------------------------------------------------------

    while True:

        try:

            connect()

            break

        except Exception as e:

            print(
                f"❌ Connection failed: "
                f"{e}"
            )

            send_telegram(
                f"🟡 <b>IQ OPTION CONNECTION RETRY</b>\n"
                f"Error: "
                f"<code>{str(e)[:400]}</code>\n"
                f"Retrying in "
                f"{CONNECTION_RETRY_SECONDS}s."
            )

            time.sleep(
                CONNECTION_RETRY_SECONDS
            )

    # --------------------------------------------------------
    # START FOREVER-STYLE LOOP
    # --------------------------------------------------------

    while True:

        try:

            scanner_loop()

            # scanner_loop normally only
            # returns after target reached.
            if (
                len(completed_trades) >=
                TARGET_COMPLETED_TRADES
            ):
                break

            # Otherwise restart it.
            time.sleep(5)

        except KeyboardInterrupt:

            print(
                "Bot stopped."
            )

            break

        except Exception as e:

            print(
                f"Fatal main error: "
                f"{e}"
            )

            traceback.print_exc()

            send_telegram(
                f"⚠️ <b>BOT RECOVERING</b>\n"
                f"Error: "
                f"<code>{str(e)[:500]}</code>\n"
                f"Restarting..."
            )

            time.sleep(
                CONNECTION_RETRY_SECONDS
            )

            try:

                connect()

            except Exception as reconnect_error:

                print(
                    f"Reconnect failed: "
                    f"{reconnect_error}"
                )

                time.sleep(
                    CONNECTION_RETRY_SECONDS
                )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()n()
