import os
import time
import math
import traceback
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# ZETA V2.4
# CANDLE / WEBSOCKET RELIABILITY BUILD
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

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

# ------------------------------------------------------------
# STRATEGY SETTINGS — UNCHANGED
# ------------------------------------------------------------

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


# ------------------------------------------------------------
# FOCUSED OTC ASSETS
# ------------------------------------------------------------

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
last_heartbeat = 0
last_discovery = 0

working_assets = []

trade_count = 0
signal_count = 0

asset_last_signal = {}
asset_trade_lock = {}

last_feed_status = {}

diagnostics = {
    "evaluations": 0,
    "signals": 0,
    "no_trend": 0,
    "adx_low": 0,
    "pullback_low": 0,
    "pullback_high": 0,
    "extension_high": 0,
    "zone_fail": 0,
    "rejection_fail": 0,
    "structure_fail": 0,
    "confirmation_fail": 0,
    "momentum_fail": 0,
    "rsi_fail": 0,
    "room_fail": 0,
    "score_fail": 0,
}

diagnostic_assets = {}

candle_failure_count = 0
websocket_recovery_count = 0


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):
    print(message)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
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


# ============================================================
# CONNECTION HELPERS
# ============================================================

def safe_disconnect():
    global iq
    global connected

    try:
        if iq is not None:
            try:
                iq.close()
            except Exception:
                pass

            try:
                iq.api.close()
            except Exception:
                pass

    except Exception:
        pass

    iq = None
    connected = False


def create_connection():
    global iq
    global connected
    global last_connection_attempt

    now = time.time()

    if now - last_connection_attempt < RECONNECT_INTERVAL:
        return False

    last_connection_attempt = now

    try:
        print("🔌 Creating IQ Option connection...")

        new_iq = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD,
        )

        status, reason = new_iq.connect()

        if not status:
            print(
                f"❌ IQ Option connection failed: {reason}"
            )
            connected = False
            return False

        try:
            new_iq.change_balance(BALANCE_MODE)
        except Exception as e:
            print(
                f"Balance mode warning: {e}"
            )

        iq = new_iq
        connected = True

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

    except Exception as e:
        connected = False

        print(
            f"❌ Connection exception: {e}"
        )

        return False


def force_reconnect(reason="Unknown"):
    global websocket_recovery_count
    global connected

    websocket_recovery_count += 1

    telegram(
        "🔄 ZETA WEBSOCKET RECOVERY\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Reason: {reason[:300]}\n"
        f"Recovery attempt: {websocket_recovery_count}\n"
        "Action: Closing broken connection\n"
        "Action: Creating a fresh IQ connection\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    safe_disconnect()

    time.sleep(2)

    connected = False

    # Bypass normal reconnect throttle here.
    global last_connection_attempt
    last_connection_attempt = 0

    ok = create_connection()

    if ok:
        telegram(
            "✅ WEBSOCKET RECOVERY SUCCESS\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Fresh IQ Option connection established.\n"
            "Candle requests can resume."
        )
    else:
        telegram(
            "⚠️ WEBSOCKET RECOVERY FAILED\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Fresh connection could not be established.\n"
            "The bot will retry later."
        )

    return ok


def ensure_connection():
    global connected

    if iq is None:
        connected = False

    if not connected:
        return create_connection()

    try:
        if hasattr(iq, "check_connect"):
            if not iq.check_connect():
                connected = False
                return create_connection()

    except Exception:
        connected = False
        return create_connection()

    return True


# ============================================================
# CANDLE VALIDATION
# ============================================================

def candles_are_valid(candles, minimum_count=20):
    if not candles:
        return False

    if not isinstance(candles, (list, tuple)):
        return False

    if len(candles) < minimum_count:
        return False

    required = [
        "open",
        "close",
        "high",
        "low",
        "from",
    ]

    valid_count = 0

    for candle in candles:

        if not isinstance(candle, dict):
            continue

        if not all(
            key in candle
            for key in required
        ):
            continue

        try:
            o = float(candle["open"])
            c = float(candle["close"])
            h = float(candle["high"])
            l = float(candle["low"])
            t = float(candle["from"])

            if not all(
                math.isfinite(x)
                for x in [o, c, h, l, t]
            ):
                continue

            if h < l:
                continue

            if h < max(o, c):
                continue

            if l > min(o, c):
                continue

            valid_count += 1

        except Exception:
            continue

    return valid_count >= minimum_count


# ============================================================
# ROBUST CANDLE FETCH
#
# THIS IS THE MAIN FIX.
# ============================================================

def fetch_candles(
    asset,
    timeframe,
    count,
    max_attempts=3,
):
    global candle_failure_count
    global connected

    for attempt in range(
        1,
        max_attempts + 1,
    ):

        try:

            if not ensure_connection():
                print(
                    f"⚠️ No connection for {asset}"
                )

                time.sleep(2)

                continue

            if iq is None:
                connected = False
                continue

            end_time = int(time.time())

            print(
                f"📡 Candle request "
                f"{asset} "
                f"TF={timeframe} "
                f"attempt={attempt}/{max_attempts}"
            )

            candles = iq.get_candles(
                asset,
                timeframe,
                count,
                end_time,
            )

            if candles_are_valid(
                candles,
                minimum_count=min(
                    count,
                    20,
                ),
            ):
                return candles

            print(
                f"⚠️ Invalid candle response "
                f"for {asset}"
            )

            # A bad/empty response can indicate that
            # the websocket is no longer usable.
            if attempt < max_attempts:

                force_reconnect(
                    f"Invalid candle response for {asset}"
                )

                time.sleep(2)

        except Exception as e:

            candle_failure_count += 1

            error_text = str(e)

            print(
                f"❌ Candle error "
                f"{asset}: {error_text}"
            )

            connected = False

            if attempt < max_attempts:

                recovered = force_reconnect(
                    f"{asset} candle request error: "
                    f"{error_text}"
                )

                if recovered:
                    time.sleep(2)
                else:
                    time.sleep(3)

            else:

                print(
                    f"⏭️ Giving up on this candle "
                    f"request: {asset}"
                )

    return None


# ============================================================
# OTC DISCOVERY
# ============================================================

def find_otc_values(obj, found=None):
    if found is None:
        found = set()

    if isinstance(obj, dict):

        for key, value in obj.items():

            if isinstance(key, str):
                if "-OTC" in key.upper():
                    found.add(key.upper())

            find_otc_values(
                value,
                found,
            )

    elif isinstance(obj, (list, tuple)):

        for item in obj:
            find_otc_values(
                item,
                found,
            )

    elif isinstance(obj, str):

        text = obj.upper()

        if "-OTC" in text:
            found.add(text)

    return found


def discover_otc_assets():
    global working_assets
    global last_discovery

    if not ensure_connection():
        return []

    try:

        discovered = set()

        try:
            data = iq.get_all_init_v2()

            discovered.update(
                find_otc_values(data)
            )

        except Exception as e:

            print(
                f"get_all_init_v2 warning: {e}"
            )

        try:
            data = iq.get_all_init()

            discovered.update(
                find_otc_values(data)
            )

        except Exception as e:

            print(
                f"get_all_init warning: {e}"
            )

        discovered = sorted(
            x for x in discovered
            if "-OTC" in x
        )

        matched = [
            asset
            for asset in FOCUSED_ASSETS
            if asset in discovered
        ]

        working_assets = matched[:MAX_OTC_ASSETS]

        last_discovery = time.time()

        telegram(
            "📡 ZETA OTC DISCOVERY\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"OTC symbols discovered: {len(discovered)}\n"
            f"Focused assets found: "
            f"{len(working_assets)}/{len(FOCUSED_ASSETS)}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(
                f"• {asset}"
                for asset in working_assets
            )
        )

        missing = [
            asset
            for asset in FOCUSED_ASSETS
            if asset not in working_assets
        ]

        if missing:

            telegram(
                "⚠️ FOCUSED FEEDS UNAVAILABLE\n"
                "━━━━━━━━━━━━━━━━━━\n"
                + "\n".join(
                    f"• {asset}"
                    for asset in missing
                )
            )

        return working_assets

    except Exception as e:

        print(
            f"❌ OTC discovery error: {e}"
        )

        return working_assets


# ============================================================
# INDICATORS
# ============================================================

def closes(candles):
    return [
        float(c["close"])
        for c in candles
    ]


def highs(candles):
    return [
        float(c["high"])
        for c in candles
    ]


def lows(candles):
    return [
        float(c["low"])
        for c in candles
    ]


def opens(candles):
    return [
        float(c["open"])
        for c in candles
    ]


def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    value = sum(
        values[:period]
    ) / period

    for price in values[period:]:
        value = (
            (price - value) * multiplier
            + value
        )

    return value


def ema_series(values, period):
    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)

    current = (
        sum(values[:period]) / period
    )

    result = [current]

    for price in values[period:]:
        current = (
            (price - current)
            * multiplier
            + current
        )

        result.append(current)

    return result


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(1, len(candles)):

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
            abs(high - previous_close),
            abs(low - previous_close),
        )

        trs.append(tr)

    if len(trs) < period:
        return None

    value = (
        sum(trs[:period]) / period
    )

    for tr in trs[period:]:
        value = (
            (value * (period - 1) + tr)
            / period
        )

    return value


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):

        change = (
            values[i] - values[i - 1]
        )

        gains.append(
            max(change, 0)
        )

        losses.append(
            max(-change, 0)
        )

    avg_gain = (
        sum(gains[:period]) / period
    )

    avg_loss = (
        sum(losses[:period]) / period
    )

    for i in range(
        period,
        len(gains),
    ):

        avg_gain = (
            (avg_gain * (period - 1)
             + gains[i])
            / period
        )

        avg_loss = (
            (avg_loss * (period - 1)
             + losses[i])
            / period
        )

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100 - (
        100 / (1 + rs)
    )


def adx(candles, period=14):
    if len(candles) < (
        period * 2 + 1
    ):
        return None

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(candles)):

        high = float(
            candles[i]["high"]
        )

        low = float(
            candles[i]["low"]
        )

        prev_high = float(
            candles[i - 1]["high"]
        )

        prev_low = float(
            candles[i - 1]["low"]
        )

        prev_close = float(
            candles[i - 1]["close"]
        )

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )

        up_move = high - prev_high
        down_move = prev_low - low

        plus = (
            up_move
            if up_move > down_move
            and up_move > 0
            else 0
        )

        minus = (
            down_move
            if down_move > up_move
            and down_move > 0
            else 0
        )

        trs.append(tr)
        plus_dm.append(plus)
        minus_dm.append(minus)

    if len(trs) < period:
        return None

    atr_value = (
        sum(trs[:period]) / period
    )

    plus_value = (
        sum(plus_dm[:period]) / period
    )

    minus_value = (
        sum(minus_dm[:period]) / period
    )

    dx_values = []

    for i in range(
        period,
        len(trs),
    ):

        atr_value = (
            (
                atr_value * (period - 1)
                + trs[i]
            ) / period
        )

        plus_value = (
            (
                plus_value * (period - 1)
                + plus_dm[i]
            ) / period
        )

        minus_value = (
            (
                minus_value * (period - 1)
                + minus_dm[i]
            ) / period
        )

        if atr_value == 0:
            continue

        plus_di = (
            100 * plus_value / atr_value
        )

        minus_di = (
            100 * minus_value / atr_value
        )

        denominator = (
            plus_di + minus_di
        )

        if denominator == 0:
            continue

        dx = (
            100
            * abs(plus_di - minus_di)
            / denominator
        )

        dx_values.append(dx)

    if len(dx_values) < period:
        return None

    return (
        sum(dx_values[-period:])
        / period
    )


# ============================================================
# CANDLE PATTERNS
# ============================================================

def candle_info(candle):
    o = float(candle["open"])
    c = float(candle["close"])
    h = float(candle["high"])
    l = float(candle["low"])

    body = abs(c - o)
    rng = max(h - l, 1e-10)

    upper = h - max(o, c)
    lower = min(o, c) - l

    return {
        "open": o,
        "close": c,
        "high": h,
        "low": l,
        "body": body,
        "range": rng,
        "body_ratio": body / rng,
        "upper_wick": upper,
        "lower_wick": lower,
    }


def bullish_engulfing(previous, current):
    p = candle_info(previous)
    c = candle_info(current)

    return (
        p["close"] < p["open"]
        and c["close"] > c["open"]
        and c["open"] <= p["close"]
        and c["close"] >= p["open"]
    )


def bearish_engulfing(previous, current):
    p = candle_info(previous)
    c = candle_info(current)

    return (
        p["close"] > p["open"]
        and c["close"] < c["open"]
        and c["open"] >= p["close"]
        and c["close"] <= p["open"]
    )


def bullish_rejection(candle):
    c = candle_info(candle)

    return (
        c["lower_wick"]
        >= c["body"] * 1.2
        and c["lower_wick"]
        > c["upper_wick"]
        and c["close"] >= (
            c["low"]
            + c["range"] * 0.55
        )
    )


def bearish_rejection(candle):
    c = candle_info(candle)

    return (
        c["upper_wick"]
        >= c["body"] * 1.2
        and c["upper_wick"]
        > c["lower_wick"]
        and c["close"] <= (
            c["high"]
            - c["range"] * 0.55
        )
    )


# ============================================================
# DIAGNOSTICS
# ============================================================

def record_rejection(asset, reason):
    if asset not in diagnostic_assets:
        diagnostic_assets[asset] = {}

    diagnostic_assets[asset][reason] = (
        diagnostic_assets[asset].get(
            reason,
            0,
        ) + 1
    )


def reset_diagnostic_window():
    global diagnostics
    global diagnostic_assets

    diagnostics = {
        key: 0
        for key in diagnostics
    }

    diagnostic_assets = {}


def build_diagnostic_text():
    return (
        "📊 ZETA REJECTION DIAGNOSTICS\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Evaluations: {diagnostics['evaluations']}\n"
        f"No valid trend: {diagnostics['no_trend']}\n"
        f"ADX < {MIN_ADX}: {diagnostics['adx_low']}\n"
        f"Pullback too small: {diagnostics['pullback_low']}\n"
        f"Pullback too large: {diagnostics['pullback_high']}\n"
        f"Extension too high: {diagnostics['extension_high']}\n"
        f"No valid zone: {diagnostics['zone_fail']}\n"
        f"No rejection candle: {diagnostics['rejection_fail']}\n"
        f"Structure fail: {diagnostics['structure_fail']}\n"
        f"Confirmation fail: {diagnostics['confirmation_fail']}\n"
        f"Momentum fail: {diagnostics['momentum_fail']}\n"
        f"RSI fail: {diagnostics['rsi_fail']}\n"
        f"Room fail: {diagnostics['room_fail']}\n"
        f"Score < {MIN_SCORE}: {diagnostics['score_fail']}\n"
        "━━━━━━━━━━━━━━━━━━"
    )


def build_asset_diagnostics():
    if not diagnostic_assets:
        return ""

    lines = [
        "📋 ASSET DIAGNOSTICS",
        "━━━━━━━━━━━━━━━━━━",
    ]

    for asset, reasons in diagnostic_assets.items():

        top = sorted(
            reasons.items(),
            key=lambda x: x[1],
            reverse=True,
        )[:3]

        if not top:
            continue

        text = ", ".join(
            f"{reason}: {count}"
            for reason, count in top
        )

        lines.append(
            f"{asset}: {text}"
        )

    return "\n".join(lines)


# ============================================================
# STRATEGY
# ============================================================

def evaluate_asset(asset):

    diagnostics["evaluations"] += 1

    candles5 = fetch_candles(
        asset,
        TF5,
        CANDLE_COUNT_5M,
    )

    if not candles5:
        record_rejection(
            asset,
            "5M candle feed unavailable",
        )
        return None

    candles1 = fetch_candles(
        asset,
        TF1,
        CANDLE_COUNT_1M,
    )

    if not candles1:
        record_rejection(
            asset,
            "1M candle feed unavailable",
        )
        return None

    if len(candles5) < 80:
        record_rejection(
            asset,
            "5M insufficient candles",
        )
        return None

    if len(candles1) < 80:
        record_rejection(
            asset,
            "1M insufficient candles",
        )
        return None

    # --------------------------------------------------------
    # 5M TREND
    # --------------------------------------------------------

    close5 = closes(candles5)

    ema_fast_series5 = ema_series(
        close5,
        EMA_FAST,
    )

    ema_slow_series5 = ema_series(
        close5,
        EMA_SLOW,
    )

    atr5 = atr(
        candles5,
        ATR_PERIOD,
    )

    adx5 = adx(
        candles5,
        ADX_PERIOD,
    )

    if (
        not ema_fast_series5
        or not ema_slow_series5
        or atr5 is None
        or adx5 is None
    ):
        record_rejection(
            asset,
            "5M indicators unavailable",
        )
        return None

    fast5 = ema_fast_series5[-1]
    previous_fast5 = ema_fast_series5[-2]

    slow5 = ema_slow_series5[-1]
    previous_slow5 = ema_slow_series5[-2]

    bullish_trend = (
        fast5 > slow5
        and previous_fast5 >= previous_slow5
        and fast5 > previous_fast5
        and slow5 >= previous_slow5
    )

    bearish_trend = (
        fast5 < slow5
        and previous_fast5 <= previous_slow5
        and fast5 < previous_fast5
        and slow5 <= previous_slow5
    )

    if not (
        bullish_trend
        or bearish_trend
    ):
        diagnostics["no_trend"] += 1

        record_rejection(
            asset,
            "No valid 5M trend",
        )

        return None

    if adx5 < MIN_ADX:

        diagnostics["adx_low"] += 1

        record_rejection(
            asset,
            f"ADX {adx5:.1f} < {MIN_ADX}",
        )

        return None

    # --------------------------------------------------------
    # 1M INDICATORS
    # --------------------------------------------------------

    close1 = closes(candles1)

    ema_fast_series1 = ema_series(
        close1,
        EMA_FAST,
    )

    ema_slow_series1 = ema_series(
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
        not ema_fast_series1
        or not ema_slow_series1
        or atr1 is None
        or rsi1 is None
    ):
        record_rejection(
            asset,
            "1M indicators unavailable",
        )
        return None

    if atr1 <= 0:
        record_rejection(
            asset,
            "ATR invalid",
        )
        return None

    # --------------------------------------------------------
    # CLOSED CANDLES ONLY
    # --------------------------------------------------------

    current = candles1[-2]
    previous = candles1[-3]

    current_info = candle_info(current)
    previous_info = candle_info(previous)

    price = current_info["close"]

    fast1 = ema_fast_series1[-2]
    previous_fast1 = ema_fast_series1[-3]

    slow1 = ema_slow_series1[-2]
    previous_slow1 = ema_slow_series1[-3]

    # --------------------------------------------------------
    # PULLBACK
    # --------------------------------------------------------

    distance_from_fast = abs(
        price - fast1
    )

    pullback_atr = (
        distance_from_fast / atr1
    )

    if pullback_atr < MIN_PULLBACK_ATR:

        diagnostics["pullback_low"] += 1

        record_rejection(
            asset,
            f"Pullback {pullback_atr:.2f} "
            f"< {MIN_PULLBACK_ATR}",
        )

        return None

    if pullback_atr > MAX_EXTENSION_ATR:

        diagnostics["extension_high"] += 1

        record_rejection(
            asset,
            f"Extension {pullback_atr:.2f} "
            f"> {MAX_EXTENSION_ATR}",
        )

        return None

    if pullback_atr > MAX_PULLBACK_ATR:

        diagnostics["pullback_high"] += 1

        record_rejection(
            asset,
            f"Pullback {pullback_atr:.2f} "
            f"> {MAX_PULLBACK_ATR}",
        )

        return None

    # --------------------------------------------------------
    # SUPPORT / RESISTANCE
    # --------------------------------------------------------

    sr_candles = candles1[-32:-2]

    if len(sr_candles) < 10:
        record_rejection(
            asset,
            "Insufficient S/R candles",
        )
        return None

    resistance = max(
        float(c["high"])
        for c in sr_candles
    )

    support = min(
        float(c["low"])
        for c in sr_candles
    )

    tolerance = (
        atr1 * ZONE_TOLERANCE_ATR
    )

    near_support = (
        abs(price - support)
        <= tolerance
    )

    near_resistance = (
        abs(price - resistance)
        <= tolerance
    )

    if bullish_trend:

        valid_zone = (
            near_support
            or abs(price - fast1)
            <= tolerance
        )

    else:

        valid_zone = (
            near_resistance
            or abs(price - fast1)
            <= tolerance
        )

    if not valid_zone:

        diagnostics["zone_fail"] += 1

        record_rejection(
            asset,
            "No valid zone",
        )

        return None

    # --------------------------------------------------------
    # REJECTION
    # --------------------------------------------------------

    bull_rejection = bullish_rejection(
        current
    )

    bear_rejection = bearish_rejection(
        current
    )

    if bullish_trend:

        valid_rejection = bull_rejection

    else:

        valid_rejection = bear_rejection

    if not valid_rejection:

        diagnostics["rejection_fail"] += 1

        record_rejection(
            asset,
            "No rejection candle",
        )

        return None

    # --------------------------------------------------------
    # 1M STRUCTURE
    # --------------------------------------------------------

    bull_structure = (
        fast1 >= slow1
        and fast1 >= previous_fast1
    )

    bear_structure = (
        fast1 <= slow1
        and fast1 <= previous_fast1
    )

    if bullish_trend:

        valid_structure = bull_structure

    else:

        valid_structure = bear_structure

    if not valid_structure:

        diagnostics["structure_fail"] += 1

        record_rejection(
            asset,
            "1M structure fail",
        )

        return None

    # --------------------------------------------------------
    # CANDLE CONFIRMATION
    # --------------------------------------------------------

    body_ratio = (
        current_info["body_ratio"]
    )

    bull_engulf = bullish_engulfing(
        previous,
        current,
    )

    bear_engulf = bearish_engulfing(
        previous,
        current,
    )

    bull_confirmation = (
        current_info["close"]
        > current_info["open"]
        and body_ratio >= MIN_CANDLE_BODY_RATIO
        and (
            current_info["close"]
            > previous_info["high"]
            or bull_engulf
            or (
                bull_rejection
                and current_info["close"]
                > current_info["open"]
            )
        )
    )

    bear_confirmation = (
        current_info["close"]
        < current_info["open"]
        and body_ratio >= MIN_CANDLE_BODY_RATIO
        and (
            current_info["close"]
            < previous_info["low"]
            or bear_engulf
            or (
                bear_rejection
                and current_info["close"]
                < current_info["open"]
            )
        )
    )

    if bullish_trend:

        valid_confirmation = (
            bull_confirmation
        )

    else:

        valid_confirmation = (
            bear_confirmation
        )

    if not valid_confirmation:

        diagnostics["confirmation_fail"] += 1

        record_rejection(
            asset,
            "Candle confirmation fail",
        )

        return None

    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------

    if bullish_trend:

        momentum = (
            current_info["close"]
            > previous_info["close"]
        )

    else:

        momentum = (
            current_info["close"]
            < previous_info["close"]
        )

    if not momentum:

        diagnostics["momentum_fail"] += 1

        record_rejection(
            asset,
            "Momentum fail",
        )

        return None

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    if bullish_trend:

        valid_rsi = (
            RSI_BULL_MIN
            <= rsi1
            <= RSI_BULL_MAX
        )

    else:

        valid_rsi = (
            RSI_BEAR_MIN
            <= rsi1
            <= RSI_BEAR_MAX
        )

    if not valid_rsi:

        diagnostics["rsi_fail"] += 1

        record_rejection(
            asset,
            f"RSI {rsi1:.1f} outside range",
        )

        return None

    # --------------------------------------------------------
    # ROOM
    # --------------------------------------------------------

    room_up_atr = (
        resistance - price
    ) / atr1

    room_down_atr = (
        price - support
    ) / atr1

    if bullish_trend:

        room_atr = room_up_atr

    else:

        room_atr = room_down_atr

    if room_atr < MIN_ROOM_ATR:

        diagnostics["room_fail"] += 1

        record_rejection(
            asset,
            f"Room {room_atr:.2f} "
            f"< {MIN_ROOM_ATR}",
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
    score += 5

    # Room
    score += 5

    # Strong candle combination
    if (
        bull_engulf
        or bear_engulf
        or (
            valid_rejection
            and valid_confirmation
        )
    ):
        score += 5

    if score < MIN_SCORE:

        diagnostics["score_fail"] += 1

        record_rejection(
            asset,
            f"Score {score} < {MIN_SCORE}",
        )

        return None

    # --------------------------------------------------------
    # FINAL SIGNAL
    # --------------------------------------------------------

    direction = (
        "CALL"
        if bullish_trend
        else "PUT"
    )

    diagnostics["signals"] += 1

    return {
        "asset": asset,
        "direction": direction,
        "score": score,
        "adx": adx5,
        "rsi": rsi1,
        "atr": atr1,
        "price": price,
        "room_atr": room_atr,
        "pullback_atr": pullback_atr,
        "timestamp": datetime.now(
            timezone.utc
        ).isoformat(),
    }


# ============================================================
# TRADE EXECUTION
# ============================================================

def execute_trade(signal):

    global trade_count
    global signal_count

    asset = signal["asset"]
    direction = signal["direction"]

    now = time.time()

    # 180-second signal cooldown
    last_signal = asset_last_signal.get(
        asset,
        0,
    )

    if (
        now - last_signal
        < 180
    ):
        return False

    # 300-second trade lock
    last_trade = asset_trade_lock.get(
        asset,
        0,
    )

    if (
        now - last_trade
        < ASSET_LOCK_SECONDS
    ):
        return False

    if not ensure_connection():
        telegram(
            "⚠️ TRADE BLOCKED\n"
            f"Asset: {asset}\n"
            "Reason: IQ connection unavailable."
        )
        return False

    signal_count += 1

    signal_id = (
        f"{asset.split('-')[0]}-"
        f"{direction}-"
        f"{int(time.time())}"
    )

    asset_last_signal[asset] = now
    asset_trade_lock[asset] = now

    telegram(
        "🎯 ZETA V2.4 SIGNAL\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"ID: {signal_id}\n"
        f"Asset: {asset}\n"
        f"Direction: {direction}\n"
        f"Score: {signal['score']}\n"
        f"ADX: {signal['adx']:.1f}\n"
        f"RSI: {signal['rsi']:.1f}\n"
        f"Pullback: {signal['pullback_atr']:.2f} ATR\n"
        f"Room: {signal['room_atr']:.2f} ATR\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Expiry: {EXPIRY_MINUTES}M\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    try:

        action = (
            "call"
            if direction == "CALL"
            else "put"
        )

        success, order_id = iq.buy(
            STAKE,
            asset,
            action,
            EXPIRY_MINUTES,
        )

        if success:

            trade_count += 1

            telegram(
                "✅ DEMO TRADE EXECUTED\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"ID: {signal_id}\n"
                f"Asset: {asset}\n"
                f"Direction: {direction}\n"
                f"Order: {order_id}\n"
                f"Trade: {trade_count}/{TARGET_TRADES}\n"
                "Account: PRACTICE\n"
                "━━━━━━━━━━━━━━━━━━"
            )

            return True

        telegram(
            "❌ DEMO TRADE FAILED\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"ID: {signal_id}\n"
            f"Asset: {asset}\n"
            "IQ Option rejected the order."
        )

        return False

    except Exception as e:

        connected = False

        telegram(
            "❌ TRADE EXECUTION ERROR\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Asset: {asset}\n"
            f"Error: {str(e)[:400]}\n"
            "Connection will be recovered."
        )

        return False


# ============================================================
# STATUS
# ============================================================

def send_feed_status():

    if not working_assets:
        telegram(
            "⚠️ ZETA FEED STATUS\n"
            "No focused OTC feeds currently available."
        )
        return

    telegram(
        "📡 ZETA FEED STATUS\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Working feeds: {len(working_assets)}\n"
        f"Focused feeds: {len(FOCUSED_ASSETS)}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(
            f"🟢 {asset}"
            for asset in working_assets
        )
    )


def send_heartbeat():

    balance_text = "N/A"

    try:
        if iq is not None:
            balance = iq.get_balance()
            balance_text = (
                f"${balance:.2f}"
            )
    except Exception:
        pass

    telegram(
        "💓 ZETA V2.4 HEARTBEAT\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Connection: "
        f"{'🟢' if connected else '🔴'}\n"
        f"OTC feeds: {len(working_assets)}\n"
        f"Signals: {signal_count}\n"
        f"Executed: {trade_count}/{TARGET_TRADES}\n"
        f"Balance: {balance_text}\n"
        f"Candle failures: {candle_failure_count}\n"
        f"WS recoveries: {websocket_recovery_count}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        + (
            "🔎 Scanning for qualified signals..."
            if connected
            else
            "⏳ Recovering connection..."
        )
    )


def send_startup():

    telegram(
        "🟢 ZETA V2.4 DEMO TRADER ONLINE\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Strategy: High Quality Trend Pullback\n"
        "Context: 5M\n"
        "Entry: 1M\n"
        f"Expiry: {EXPIRY_MINUTES}M\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Minimum score: {MIN_SCORE}\n"
        f"Target trades: {TARGET_TRADES}\n"
        "Account: PRACTICE\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Candle/WebSocket reliability mode: ON"
    )


# ============================================================
# MAIN RUN LOOP
# ============================================================

def run():

    global last_heartbeat
    global last_discovery

    telegram(
        "🚀 Starting ZETA V2.4..."
    )

    if not create_connection():

        telegram(
            "🔴 ZETA STARTUP CONNECTION FAILED\n"
            "The bot will continue attempting recovery."
        )

    send_startup()

    # --------------------------------------------------------
    # INITIAL DISCOVERY
    # --------------------------------------------------------

    discover_otc_assets()

    send_feed_status()

    # --------------------------------------------------------
    # CANDLE VERIFICATION
    # --------------------------------------------------------

    candle_verified = False

    if working_assets:

        telegram(
            "🧪 ZETA CANDLE VERIFICATION STARTING\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Feeds to test: {len(working_assets)}\n"
            "Timeframe: 1M\n"
            "Required candles: 100\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "The bot will test the available feeds."
        )

        for index, asset in enumerate(
            working_assets,
            start=1,
        ):

            telegram(
                "🧪 TESTING CANDLE FEED\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"Feed: {index}/{len(working_assets)}\n"
                f"Asset: {asset}\n"
                "Timeframe: 1M\n"
                "Requesting 100 candles..."
            )

            print(
                f"🧪 Testing candle feed: {asset}"
            )

            test_candles = fetch_candles(
                asset,
                TF1,
                100,
            )

            if test_candles:

                candle_verified = True

                telegram(
                    "✅ CANDLE FEED VERIFIED\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"Asset: {asset}\n"
                    f"Candles received: "
                    f"{len(test_candles)}\n"
                    "WebSocket candle layer is working.\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "➡️ Moving to normal scanner mode."
                )

                break

            else:

                telegram(
                    "⚠️ CANDLE TEST FAILED\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"Asset: {asset}\n"
                    "No valid candle data received.\n"
                    "The recovery system will continue.\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "➡️ Moving to the next available feed."
                )

    if not candle_verified:

        telegram(
            "⚠️ ZETA CANDLE FEED NOT VERIFIED\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "No candle feed passed verification.\n"
            "The bot will NOT change the strategy.\n"
            "It will keep recovering/rechecking the connection."
        )

    else:

        telegram(
            "🟢 ZETA SCANNER ACTIVE\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Feeds: {len(working_assets)}\n"
            "Strategy: High Quality Trend Pullback\n"
            "5M trend → 1M pullback → 1M confirmation\n"
            "🔎 Scanning for qualified signals..."
        )

    last_heartbeat = time.time()
    last_discovery = time.time()

    # --------------------------------------------------------
    # NORMAL LOOP
    # --------------------------------------------------------

    while True:

        try:

            now = time.time()

            # ----------------------------------------------
            # TARGET CHECK
            # ----------------------------------------------

            if trade_count >= TARGET_TRADES:

                telegram(
                    "🏁 ZETA 50-TRADE TARGET REACHED\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"Executed trades: {trade_count}\n"
                    "The demo test is complete.\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "Bot stopping."
                )

                break

            # ----------------------------------------------
            # CONNECTION
            # ----------------------------------------------

            if not ensure_connection():

                print(
                    "⚠️ Connection unavailable."
                )

                time.sleep(
                    SCAN_INTERVAL
                )

                continue

            # ----------------------------------------------
            # PERIODIC DISCOVERY
            # ----------------------------------------------

            if (
                now - last_discovery
                >= DISCOVERY_INTERVAL
            ):

                telegram(
                    "🔄 Scheduled OTC discovery..."
                )

                discover_otc_assets()

                send_feed_status()

            # ----------------------------------------------
            # HEARTBEAT
            # ----------------------------------------------

            if (
                now - last_heartbeat
                >= STATUS_INTERVAL
            ):

                send_heartbeat()

                last_heartbeat = now

            # ----------------------------------------------
            # NO FEEDS
            # ----------------------------------------------

            if not working_assets:

                telegram(
                    "⚠️ No working focused OTC feeds.\n"
                    "Retrying discovery..."
                )

                discover_otc_assets()

                time.sleep(
                    SCAN_INTERVAL
                )

                continue

            # ----------------------------------------------
            # SCAN ASSETS
            # ----------------------------------------------

            for asset in list(
                working_assets
            ):

                if trade_count >= TARGET_TRADES:
                    break

                try:

                    signal = evaluate_asset(
                        asset
                    )

                    if signal:

                        execute_trade(
                            signal
                        )

                except Exception as e:

                    print(
                        f"⚠️ Evaluation error "
                        f"{asset}: {e}"
                    )

                    record_rejection(
                        asset,
                        f"Evaluation error: "
                        f"{str(e)[:100]}",
                    )

                time.sleep(
                    0.2
                )

            time.sleep(
                SCAN_INTERVAL
            )

        except KeyboardInterrupt:

            telegram(
                "🛑 ZETA STOPPED MANUALLY"
            )

            break

        except Exception as e:

            print(
                f"🔥 Main loop error: {e}"
            )

            traceback.print_exc()

            telegram(
                "⚠️ ZETA MAIN LOOP ERROR\n"
                f"{str(e)[:500]}\n"
                "The bot will attempt to recover."
            )

            connected = False

            time.sleep(5)


# ============================================================
# MAIN
# ============================================================

def main():
    run()


if __name__ == "__main__":

    try:

        main()

    except Exception as e:

        print(
            f"🔥 FATAL ERROR: {e}"
        )

        traceback.print_exc()

        telegram(
            "🔴 ZETA V2.4 FATAL ERROR\n"
            f"{str(e)[:500]}"
        )
