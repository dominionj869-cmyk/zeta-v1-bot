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
# DIAGNOSTIC DEMO TRADER
# ============================================================

VERSION = "ZETA V2.4-DIAGNOSTIC"

# -----------------------------
# ACCOUNT / TRADING SETTINGS
# -----------------------------

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
SIGNAL_COOLDOWN_SECONDS = 180


# ============================================================
# STRATEGY SETTINGS — DO NOT CHANGE
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
# GLOBALS
# ============================================================

api = None

otc_assets = {}

last_signal_time = {}
last_trade_time = {}

total_trades = 0

runtime_start = time.time()
last_heartbeat = 0
last_discovery = 0
last_scan = 0


# ============================================================
# DIAGNOSTIC COUNTERS
# ============================================================

DIAG = {
    "scan_cycles": 0,
    "assets_seen": 0,
    "data_errors": 0,

    "five_min_data": 0,
    "one_min_data": 0,

    "trend_pass": 0,
    "trend_fail": 0,

    "adx_pass": 0,
    "adx_fail": 0,

    "pullback_pass": 0,
    "pullback_fail": 0,

    "zone_pass": 0,
    "zone_fail": 0,

    "rejection_pass": 0,
    "rejection_fail": 0,

    "structure_pass": 0,
    "structure_fail": 0,

    "confirmation_pass": 0,
    "confirmation_fail": 0,

    "momentum_pass": 0,
    "momentum_fail": 0,

    "rsi_pass": 0,
    "rsi_fail": 0,

    "room_pass": 0,
    "room_fail": 0,

    "score_pass": 0,
    "score_fail": 0,

    "cooldown_block": 0,
    "lock_block": 0,

    "valid_signals": 0,

    "buy_attempts": 0,
    "buy_success": 0,
    "buy_rejected": 0,

    "exceptions": 0,
}

DIAG_REASONS = {}


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM] Missing token/chat ID")
        return False

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

        response = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            timeout=15,
        )

        if response.ok:
            return True

        print("[TELEGRAM ERROR]", response.status_code, response.text)
        return False

    except Exception as e:
        print("[TELEGRAM ERROR]", e)
        return False


# ============================================================
# TIME HELPERS
# ============================================================

def now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def runtime_text():
    seconds = int(time.time() - runtime_start)

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    return f"{hours}h {minutes}m {secs}s"


def server_now():
    try:
        if api is not None:
            if hasattr(api, "time_sync"):
                ts = getattr(api.time_sync, "server_timestamp", None)
                if ts:
                    return int(ts)

            if hasattr(api, "get_server_timestamp"):
                return int(api.get_server_timestamp())

    except Exception:
        pass

    return int(time.time())


# ============================================================
# DIAGNOSTIC HELPERS
# ============================================================

def diag_inc(name, amount=1):
    DIAG[name] = DIAG.get(name, 0) + amount


def reason(reason_name):
    DIAG_REASONS[reason_name] = DIAG_REASONS.get(reason_name, 0) + 1


def reset_cycle_diagnostics():
    for key in DIAG:
        if key != "scan_cycles":
            DIAG[key] = 0

    DIAG_REASONS.clear()


def top_reasons(limit=8):
    if not DIAG_REASONS:
        return "None"

    ordered = sorted(
        DIAG_REASONS.items(),
        key=lambda x: x[1],
        reverse=True,
    )

    return "\n".join(
        f"• {name}: {count}"
        for name, count in ordered[:limit]
    )


# ============================================================
# OTC DISCOVERY
# ============================================================

def is_otc_name(name):
    if not name:
        return False

    text = str(name).upper()

    return (
        "OTC" in text
        or text.endswith("-OTC")
        or text.endswith("(OTC)")
        or text.endswith("_OTC")
    )


def clean_active_name(name):
    if not name:
        return None

    text = str(name).strip()

    return text


def register_active_id(name, active_id):
    if not name or active_id is None:
        return False

    name = clean_active_name(name)

    if not name:
        return False

    if not is_otc_name(name):
        return False

    try:
        active_id = int(active_id)
    except Exception:
        return False

    otc_assets[name] = active_id

    try:
        OP_code.ACTIVES[name] = active_id
    except Exception:
        pass

    return True


def get_raw_initialization():
    global api

    if api is None:
        return None

    try:
        result = api.get_all_init_v2()

        if result:
            return result

    except Exception as e:
        print("[INIT V2 ERROR]", e)

    try:
        result = api.get_all_init()

        if result:
            return result

    except Exception as e:
        print("[INIT ERROR]", e)

    return None


def discover_otc_from_initialization():
    global otc_assets

    otc_assets = {}

    raw = get_raw_initialization()

    if not raw:
        print("[OTC] Initialization data unavailable")
        return {}

    found = {}

    def walk(obj):
        if len(found) >= MAX_OTC_ASSETS:
            return

        if isinstance(obj, dict):

            active_id = None
            name = None

            for key in (
                "active_id",
                "activeId",
                "id",
                "asset_id",
                "instrument_id",
            ):
                if key in obj:
                    active_id = obj.get(key)
                    break

            for key in (
                "name",
                "symbol",
                "ticker",
                "active",
                "instrument",
            ):
                if key in obj and isinstance(obj.get(key), str):
                    name = obj.get(key)
                    break

            enabled = True
            suspended = False

            for key in (
                "enabled",
                "is_enabled",
                "open",
                "isOpen",
            ):
                if key in obj:
                    value = obj.get(key)

                    if value in (False, 0, "0", "false", "False"):
                        enabled = False

            for key in (
                "suspended",
                "is_suspended",
                "suspend",
            ):
                if key in obj:
                    value = obj.get(key)

                    if value in (True, 1, "1", "true", "True"):
                        suspended = True

            if (
                name
                and active_id is not None
                and enabled
                and not suspended
                and is_otc_name(name)
            ):
                try:
                    aid = int(active_id)
                    found[clean_active_name(name)] = aid
                except Exception:
                    pass

            for value in obj.values():
                walk(value)

        elif isinstance(obj, list):
            for item in obj:
                walk(item)

                if len(found) >= MAX_OTC_ASSETS:
                    break

    try:
        walk(raw)
    except Exception as e:
        print("[OTC WALK ERROR]", e)

    for name, active_id in list(found.items())[:MAX_OTC_ASSETS]:
        register_active_id(name, active_id)

    print(f"[OTC] Discovered {len(otc_assets)} OTC assets")

    return otc_assets


# ============================================================
# CANDLE NORMALIZATION
# ============================================================

def normalize_candle(c):
    if not isinstance(c, dict):
        return None

    try:
        if "open" in c:
            o = c["open"]
        else:
            o = c.get("o")

        if "close" in c:
            cl = c["close"]
        else:
            cl = c.get("c")

        if "max" in c:
            h = c["max"]
        elif "high" in c:
            h = c["high"]
        else:
            h = c.get("h")

        if "min" in c:
            l = c["min"]
        elif "low" in c:
            l = c["low"]
        else:
            l = c.get("l")

        t = c.get("from", c.get("time", c.get("t")))

        if None in (o, cl, h, l):
            return None

        return {
            "o": float(o),
            "c": float(cl),
            "h": float(h),
            "l": float(l),
            "t": int(t) if t is not None else 0,
        }

    except Exception:
        return None


def normalize_candles(data, interval):
    if not isinstance(data, list):
        return []

    result = []

    for item in data:
        c = normalize_candle(item)

        if c:
            result.append(c)

    result.sort(key=lambda x: x["t"])

    # Remove obvious duplicate timestamps.
    unique = {}

    for c in result:
        unique[c["t"]] = c

    result = list(unique.values())
    result.sort(key=lambda x: x["t"])

    # Do not use the currently forming candle.
    current_time = server_now()

    if result:
        last = result[-1]

        if last["t"] + interval > current_time:
            result = result[:-1]

    return result


# ============================================================
# CONTROLLED RECONNECT
# ============================================================

def reconnect_iq_controlled():
    global api

    print("[RECONNECT] Starting controlled reconnect...")

    old_api = api

    if old_api is not None:
        try:
            old_api.close()
        except Exception:
            pass

    time.sleep(1)

    try:
        new_api = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD,
        )

        connected, reason_text = new_api.connect()

        if not connected:
            print("[RECONNECT] Failed:", reason_text)
            return False

        try:
            new_api.change_balance(BALANCE_MODE)
        except Exception as e:
            print("[RECONNECT] Balance mode warning:", e)

        api = new_api

        print("[RECONNECT] Connected successfully")

        return True

    except Exception as e:
        print("[RECONNECT ERROR]", e)
        traceback.print_exc()

        return False


# ============================================================
# LOW LEVEL CANDLE FETCH
# ============================================================

def get_candles_safe(asset, interval, count):
    global api

    active_id = OP_code.ACTIVES.get(asset)

    if active_id is None:
        active_id = otc_assets.get(asset)

    if active_id is None:
        raise RuntimeError(
            f"No active ID registered for {asset}"
        )

    last_error = None

    for attempt in range(2):

        try:
            if api is None:
                raise RuntimeError("IQ Option API is None")

            internal_api = getattr(api, "api", None)

            if internal_api is None:
                raise RuntimeError("Internal IQ API unavailable")

            candles_obj = getattr(
                internal_api,
                "candles",
                None,
            )

            if candles_obj is None:
                raise RuntimeError("Internal candles object unavailable")

            candles_obj.candles_data = None

            end_time = server_now()

            print(
                f"[CANDLES] {asset} "
                f"{interval}s x {count} "
                f"attempt={attempt + 1}"
            )

            internal_api.getcandles(
                active_id,
                interval,
                count + 5,
                end_time,
            )

            deadline = time.time() + 10

            while time.time() < deadline:

                data = getattr(
                    candles_obj,
                    "candles_data",
                    None,
                )

                if data is not None:

                    candles = normalize_candles(
                        data,
                        interval,
                    )

                    if len(candles) >= min(80, count):
                        return candles[-count:]

                time.sleep(0.10)

            raise TimeoutError(
                f"Candle timeout: {asset} {interval}"
            )

        except Exception as e:

            last_error = e

            print(
                f"[CANDLE ERROR] "
                f"{asset} {interval}: {e}"
            )

            if attempt == 0:

                diag_inc("data_errors")
                reason("candle fetch/reconnect")

                if not reconnect_iq_controlled():
                    break

            else:
                break

    raise RuntimeError(
        f"Unable to fetch candles for {asset}: {last_error}"
    )


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    if len(values) < period:
        return [None] * len(values)

    result = [None] * len(values)

    seed = sum(values[:period]) / period

    result[period - 1] = seed

    multiplier = 2 / (period + 1)

    previous = seed

    for i in range(period, len(values)):
        current = (
            (values[i] - previous) * multiplier
            + previous
        )

        result[i] = current
        previous = current

    return result


def atr(candles, period):
    if len(candles) < period + 1:
        return [None] * len(candles)

    tr = [None] * len(candles)

    for i in range(1, len(candles)):
        high = candles[i]["h"]
        low = candles[i]["l"]
        previous_close = candles[i - 1]["c"]

        tr[i] = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )

    result = [None] * len(candles)

    values = [
        x for x in tr[1:]
        if x is not None
    ]

    if len(values) < period:
        return result

    first = sum(values[:period]) / period

    index = period

    result[index] = first

    previous = first

    for i in range(index + 1, len(candles)):

        current_tr = tr[i]

        if current_tr is None:
            continue

        previous = (
            (previous * (period - 1))
            + current_tr
        ) / period

        result[i] = previous

    return result


def rsi(values, period):
    result = [None] * len(values)

    if len(values) <= period:
        return result

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def calc(g, l):
        if l == 0:
            return 100.0

        rs = g / l

        return 100 - (100 / (1 + rs))

    result[period] = calc(
        avg_gain,
        avg_loss,
    )

    for i in range(period + 1, len(values)):

        avg_gain = (
            avg_gain * (period - 1)
            + gains[i - 1]
        ) / period

        avg_loss = (
            avg_loss * (period - 1)
            + losses[i - 1]
        ) / period

        result[i] = calc(
            avg_gain,
            avg_loss,
        )

    return result


def adx(candles, period):
    n = len(candles)

    adx_values = [None] * n
    plus_di = [None] * n
    minus_di = [None] * n

    if n < period * 2:
        return adx_values, plus_di, minus_di

    trs = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n

    for i in range(1, n):

        high = candles[i]["h"]
        low = candles[i]["l"]

        prev_high = candles[i - 1]["h"]
        prev_low = candles[i - 1]["l"]
        prev_close = candles[i - 1]["c"]

        trs[i] = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )

        up = high - prev_high
        down = prev_low - low

        if up > down and up > 0:
            plus_dm[i] = up

        if down > up and down > 0:
            minus_dm[i] = down

    tr_sum = sum(trs[1:period + 1])
    plus_sum = sum(plus_dm[1:period + 1])
    minus_sum = sum(minus_dm[1:period + 1])

    dx = [None] * n

    for i in range(period, n):

        if i > period:

            tr_sum = (
                tr_sum
                - (tr_sum / period)
                + trs[i]
            )

            plus_sum = (
                plus_sum
                - (plus_sum / period)
                + plus_dm[i]
            )

            minus_sum = (
                minus_sum
                - (minus_sum / period)
                + minus_dm[i]
            )

        if tr_sum <= 0:
            continue

        pdi = 100 * plus_sum / tr_sum
        mdi = 100 * minus_sum / tr_sum

        plus_di[i] = pdi
        minus_di[i] = mdi

        denominator = pdi + mdi

        if denominator > 0:
            dx[i] = (
                100
                * abs(pdi - mdi)
                / denominator
            )

    valid_dx = [
        x for x in dx
        if x is not None
    ]

    if len(valid_dx) < period:
        return adx_values, plus_di, minus_di

    first_adx = sum(valid_dx[:period]) / period

    first_index = next(
        i for i, x in enumerate(dx)
        if x is not None
    ) + period - 1

    if first_index < n:
        adx_values[first_index] = first_adx

    previous = first_adx

    for i in range(first_index + 1, n):

        if dx[i] is None:
            continue

        previous = (
            (previous * (period - 1))
            + dx[i]
        ) / period

        adx_values[i] = previous

    return adx_values, plus_di, minus_di


# ============================================================
# CANDLE / STRUCTURE HELPERS
# ============================================================

def candle_body_ratio(c):
    candle_range = c["h"] - c["l"]

    if candle_range <= 0:
        return 0.0

    return abs(c["c"] - c["o"]) / candle_range


def bullish_candle(c):
    return c["c"] > c["o"]


def bearish_candle(c):
    return c["c"] < c["o"]


def bullish_rejection(c):
    body = abs(c["c"] - c["o"])
    lower_wick = min(c["o"], c["c"]) - c["l"]
    upper_wick = c["h"] - max(c["o"], c["c"])

    return (
        lower_wick >= body
        and lower_wick > upper_wick
    )


def bearish_rejection(c):
    body = abs(c["c"] - c["o"])
    upper_wick = c["h"] - max(c["o"], c["c"])
    lower_wick = min(c["o"], c["c"]) - c["l"]

    return (
        upper_wick >= body
        and upper_wick > lower_wick
    )


def bullish_engulfing(previous, current):
    return (
        previous["c"] < previous["o"]
        and current["c"] > current["o"]
        and current["o"] <= previous["c"]
        and current["c"] >= previous["o"]
    )


def bearish_engulfing(previous, current):
    return (
        previous["c"] > previous["o"]
        and current["c"] < current["o"]
        and current["o"] >= previous["c"]
        and current["c"] <= previous["o"]
    )


def support_resistance(candles, lookback=30):
    recent = candles[-lookback:]

    if not recent:
        return None, None

    support = min(x["l"] for x in recent)
    resistance = max(x["h"] for x in recent)

    return support, resistance


# ============================================================
# MAIN STRATEGY
# ============================================================

def evaluate_zeta_v2(asset, candles5, candles1):
    """
    IMPORTANT:
    This function keeps the original strategy rules.

    The only addition is diagnostic reporting.
    """

    if len(candles5) < 80:
        reason("5M insufficient candles")
        return None

    if len(candles1) < 80:
        reason("1M insufficient candles")
        return None

    diag_inc("five_min_data")
    diag_inc("one_min_data")

    closes5 = [x["c"] for x in candles5]

    ema20_5 = ema(closes5, EMA_FAST)
    ema50_5 = ema(closes5, EMA_SLOW)

    atr5 = atr(candles5, ATR_PERIOD)

    adx5, plus_di5, minus_di5 = adx(
        candles5,
        ADX_PERIOD,
    )

    i5 = len(candles5) - 1
    p5 = i5 - 1

    fast5 = ema20_5[i5]
    slow5 = ema50_5[i5]

    previous_fast5 = ema20_5[p5]
    previous_slow5 = ema50_5[p5]

    atr_value5 = atr5[i5]
    adx_value = adx5[i5]

    if None in (
        fast5,
        slow5,
        previous_fast5,
        previous_slow5,
        atr_value5,
        adx_value,
    ):
        reason("5M indicator unavailable")
        return None

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

    if not bull_trend and not bear_trend:
        diag_inc("trend_fail")
        reason("5M trend invalid")
        return None

    diag_inc("trend_pass")

    direction = "CALL" if bull_trend else "PUT"

    # --------------------------------------------------------
    # ADX
    # --------------------------------------------------------

    if adx_value < MIN_ADX:
        diag_inc("adx_fail")
        reason(f"ADX below {MIN_ADX}")
        return None

    diag_inc("adx_pass")

    # --------------------------------------------------------
    # 1M INDICATORS
    # --------------------------------------------------------

    closes1 = [x["c"] for x in candles1]

    ema20_1 = ema(closes1, EMA_FAST)
    ema50_1 = ema(closes1, EMA_SLOW)

    atr1 = atr(candles1, ATR_PERIOD)
    rsi1 = rsi(closes1, RSI_PERIOD)

    i1 = len(candles1) - 1
    p1 = i1 - 1

    fast1 = ema20_1[i1]
    slow1 = ema50_1[i1]

    previous_fast1 = ema20_1[p1]
    previous_slow1 = ema50_1[p1]

    atr_value1 = atr1[i1]
    rsi_value = rsi1[i1]

    current = candles1[i1]
    previous = candles1[p1]

    if None in (
        fast1,
        slow1,
        previous_fast1,
        previous_slow1,
        atr_value1,
        rsi_value,
    ):
        reason("1M indicator unavailable")
        return None

    if atr_value1 <= 0:
        reason("1M ATR invalid")
        return None

    # --------------------------------------------------------
    # 1M STRUCTURE
    # --------------------------------------------------------

    if direction == "CALL":
        structure_ok = (
            fast1 >= slow1
            and fast1 >= previous_fast1
        )
    else:
        structure_ok = (
            fast1 <= slow1
            and fast1 <= previous_fast1
        )

    if structure_ok:
        diag_inc("structure_pass")
    else:
        diag_inc("structure_fail")
        reason("1M structure invalid")
        return None

    # --------------------------------------------------------
    # PULLBACK
    # --------------------------------------------------------

    price = current["c"]

    distance_from_ema = abs(
        price - fast1
    ) / atr_value1

    if distance_from_ema < MIN_PULLBACK_ATR:
        diag_inc("pullback_fail")
        reason("Pullback too small")
        return None

    if distance_from_ema > MAX_PULLBACK_ATR:
        diag_inc("pullback_fail")
        reason("Pullback too large")
        return None

    diag_inc("pullback_pass")

    # --------------------------------------------------------
    # EXTENSION
    # --------------------------------------------------------

    extension = abs(
        price - fast5
    ) / atr_value5

    if extension > MAX_EXTENSION_ATR:
        reason("Price too extended")
        return None

    # --------------------------------------------------------
    # SUPPORT / RESISTANCE ZONE
    # --------------------------------------------------------

    support, resistance = support_resistance(
        candles1,
        30,
    )

    if support is None or resistance is None:
        diag_inc("zone_fail")
        reason("No support/resistance")
        return None

    zone_tolerance = (
        atr_value1 * ZONE_TOLERANCE_ATR
    )

    if direction == "CALL":
        zone_ok = (
            abs(price - support)
            <= zone_tolerance
            or
            abs(fast1 - support)
            <= zone_tolerance
        )
    else:
        zone_ok = (
            abs(price - resistance)
            <= zone_tolerance
            or
            abs(fast1 - resistance)
            <= zone_tolerance
        )

    if not zone_ok:
        diag_inc("zone_fail")
        reason(
            "CALL zone invalid"
            if direction == "CALL"
            else "PUT zone invalid"
        )
        return None

    diag_inc("zone_pass")

    # --------------------------------------------------------
    # REJECTION CANDLE
    # --------------------------------------------------------

    if direction == "CALL":
        rejection_ok = bullish_rejection(current)
    else:
        rejection_ok = bearish_rejection(current)

    if rejection_ok:
        diag_inc("rejection_pass")
    else:
        diag_inc("rejection_fail")
        reason("No rejection candle")
        return None

    # --------------------------------------------------------
    # CANDLE CONFIRMATION
    # --------------------------------------------------------

    body_ratio = candle_body_ratio(current)

    if direction == "CALL":

        confirmation_ok = (
            bullish_candle(current)
            and body_ratio >= MIN_CANDLE_BODY_RATIO
            and (
                current["c"] > previous["h"]
                or bullish_engulfing(
                    previous,
                    current,
                )
                or (
                    bullish_rejection(current)
                    and current["c"] > current["o"]
                )
            )
        )

    else:

        confirmation_ok = (
            bearish_candle(current)
            and body_ratio >= MIN_CANDLE_BODY_RATIO
            and (
                current["c"] < previous["l"]
                or bearish_engulfing(
                    previous,
                    current,
                )
                or (
                    bearish_rejection(current)
                    and current["c"] < current["o"]
                )
            )
        )

    if confirmation_ok:
        diag_inc("confirmation_pass")
    else:
        diag_inc("confirmation_fail")
        reason("Candle confirmation failed")
        return None

    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------

    if direction == "CALL":
        momentum_ok = current["c"] > previous["c"]
    else:
        momentum_ok = current["c"] < previous["c"]

    if momentum_ok:
        diag_inc("momentum_pass")
    else:
        diag_inc("momentum_fail")
        reason("Momentum failed")
        return None

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    if direction == "CALL":
        rsi_ok = (
            RSI_BULL_MIN
            <= rsi_value
            <= RSI_BULL_MAX
        )
    else:
        rsi_ok = (
            RSI_BEAR_MIN
            <= rsi_value
            <= RSI_BEAR_MAX
        )

    if rsi_ok:
        diag_inc("rsi_pass")
    else:
        diag_inc("rsi_fail")
        reason("RSI outside valid range")
        return None

    # --------------------------------------------------------
    # ROOM
    # --------------------------------------------------------

    room_up_atr = (
        (resistance - price)
        / atr_value1
    )

    room_down_atr = (
        (price - support)
        / atr_value1
    )

    if direction == "CALL":
        room_atr = room_up_atr
    else:
        room_atr = room_down_atr

    if room_atr < MIN_ROOM_ATR:
        diag_inc("room_fail")
        reason("Insufficient room")
        return None

    diag_inc("room_pass")

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

    # Engulfing OR rejection + confirmation
    extra_confirmation = (
        bullish_engulfing(
            previous,
            current,
        )
        if direction == "CALL"
        else bearish_engulfing(
            previous,
            current,
        )
    )

    if extra_confirmation or rejection_ok:
        score += 5

    if score < MIN_SCORE:
        diag_inc("score_fail")
        reason(
            f"Score below {MIN_SCORE}"
        )
        return None

    diag_inc("score_pass")
    diag_inc("valid_signals")

    return {
        "asset": asset,
        "direction": direction,
        "score": score,
        "adx": adx_value,
        "rsi": rsi_value,
        "atr": atr_value1,
        "price": price,
        "room_atr": room_atr,
        "extension_atr": extension,
        "pullback_atr": distance_from_ema,
        "body_ratio": body_ratio,
        "created_at": now_text(),
    }


# ============================================================
# SIGNAL FORMAT
# ============================================================

def format_signal(signal):
    direction = signal["direction"]

    arrow = "🟢 CALL / UP" if direction == "CALL" else "🔴 PUT / DOWN"

    return (
        "🎯 ZETA V2.4 VALID SIGNAL\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💱 Asset: {signal['asset']}\n"
        f"📌 Direction: {arrow}\n"
        f"⭐ Score: {signal['score']}/100\n"
        f"📐 ADX: {signal['adx']:.1f}\n"
        f"📉 RSI: {signal['rsi']:.1f}\n"
        f"📏 Pullback: {signal['pullback_atr']:.2f} ATR\n"
        f"🚪 Room: {signal['room_atr']:.2f} ATR\n"
        f"📊 Extension: {signal['extension_atr']:.2f} ATR\n"
        f"🕯 Body: {signal['body_ratio']:.2f}\n"
        f"💵 Price: {signal['price']}\n"
        f"⏱ Expiry: {EXPIRY_MINUTES} minutes\n"
        f"🕒 {signal['created_at']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ PRACTICE/DEMO ONLY"
    )


# ============================================================
# EXECUTE DEMO TRADE
# ============================================================

def execute_demo_trade(signal):
    global total_trades

    asset = signal["asset"]
    direction = signal["direction"]

    active_id = OP_code.ACTIVES.get(asset)

    if active_id is None:
        active_id = otc_assets.get(asset)

    if active_id is None:
        print(
            f"[BUY BLOCKED] No active ID for {asset}"
        )

        reason("BUY blocked: no active ID")
        diag_inc("buy_rejected")

        send_telegram(
            "❌ ZETA BUY BLOCKED\n"
            f"Asset: {asset}\n"
            "Reason: No IQ Option active ID"
        )

        return False

    option_type = (
        "call"
        if direction == "CALL"
        else "put"
    )

    diag_inc("buy_attempts")

    send_telegram(
        "🚨 ZETA DEMO ORDER ATTEMPT\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Asset: {asset}\n"
        f"Direction: {direction}\n"
        f"Type sent to IQ Option: {option_type}\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Expiry: {EXPIRY_MINUTES} minutes\n"
        f"Score: {signal['score']}/100\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    print(
        f"[BUY ATTEMPT] "
        f"{asset} {option_type} "
        f"${STAKE:.2f} "
        f"{EXPIRY_MINUTES}m"
    )

    try:

        result = api.buy(
            STAKE,
            asset,
            option_type,
            EXPIRY_MINUTES,
        )

        print(
            "[BUY RAW RESULT]",
            repr(result)
        )

        success = False
        order_id = None
        rejection_text = None

        if isinstance(result, tuple):

            if len(result) >= 1:
                success = bool(result[0])

            if len(result) >= 2:
                order_id = result[1]

        elif isinstance(result, list):

            if len(result) >= 1:
                success = bool(result[0])

            if len(result) >= 2:
                order_id = result[1]

        elif isinstance(result, bool):

            success = result

        else:

            success = bool(result)

        if success:

            diag_inc("buy_success")
            total_trades += 1

            last_trade_time[asset] = time.time()

            print(
                f"[BUY SUCCESS] "
                f"{asset} "
                f"order_id={order_id}"
            )

            send_telegram(
                "✅ ZETA DEMO ORDER ACCEPTED\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"Asset: {asset}\n"
                f"Direction: {direction}\n"
                f"Stake: ${STAKE:.2f}\n"
                f"Expiry: {EXPIRY_MINUTES} minutes\n"
                f"Order ID: {order_id}\n"
                f"Orders: {total_trades}/{TARGET_TRADES}\n"
                "━━━━━━━━━━━━━━━━━━"
            )

            return True

        diag_inc("buy_rejected")

        rejection_text = repr(result)

        print(
            f"[BUY REJECTED] "
            f"{asset}: {rejection_text}"
        )

        send_telegram(
            "❌ ZETA DEMO ORDER REJECTED\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Asset: {asset}\n"
            f"Direction: {direction}\n"
            f"Raw response: {rejection_text}\n"
            "━━━━━━━━━━━━━━━━━━"
        )

        return False

    except Exception as e:

        diag_inc("buy_rejected")
        diag_inc("exceptions")

        print(
            f"[BUY EXCEPTION] "
            f"{asset}: {e}"
        )

        traceback.print_exc()

        send_telegram(
            "❌ ZETA BUY EXCEPTION\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Asset: {asset}\n"
            f"Direction: {direction}\n"
            f"Error: {e}\n"
            "━━━━━━━━━━━━━━━━━━"
        )

        return False


# ============================================================
# FEED TEST
# ============================================================

def test_candle_access(asset):
    try:

        candles = get_candles_safe(
            asset,
            TF1,
            10,
        )

        if len(candles) >= 5:
            return True

        return False

    except Exception as e:

        print(
            f"[FEED TEST FAILED] "
            f"{asset}: {e}"
        )

        return False


# ============================================================
# REFRESH OTC ASSETS
# ============================================================

def refresh_otc_assets():
    global otc_assets

    print(
        "[OTC] Refreshing real IQ Option OTC markets..."
    )

    discovered = discover_otc_from_initialization()

    if not discovered:
        print(
            "[OTC] No OTC assets discovered"
        )

        return False

    working = {}

    checked = 0

    for asset, active_id in discovered.items():

        if checked >= MAX_OTC_ASSETS:
            break

        checked += 1

        try:

            if test_candle_access(asset):
                working[asset] = active_id

                print(
                    f"[OTC WORKING] {asset}"
                )

        except Exception as e:

            print(
                f"[OTC FAILED] "
                f"{asset}: {e}"
            )

    otc_assets = working

    for asset, active_id in working.items():

        try:
            OP_code.ACTIVES[asset] = active_id
        except Exception:
            pass

    print(
        f"[OTC] Working feeds: "
        f"{len(otc_assets)}"
    )

    return len(otc_assets) > 0


# ============================================================
# CONNECTION CHECK
# ============================================================

def connection_is_alive():
    global api

    if api is None:
        return False

    try:

        if hasattr(api, "check_connect"):

            if not api.check_connect():
                return False

        return True

    except Exception:

        return False


# ============================================================
# DIAGNOSTIC TELEGRAM REPORT
# ============================================================

def send_diagnostic_report():
    print("\n========== DIAGNOSTIC REPORT ==========")

    print(
        f"Scan cycles: {DIAG['scan_cycles']}"
    )

    print(
        f"Assets: {DIAG['assets_seen']}"
    )

    print(
        f"Valid signals: {DIAG['valid_signals']}"
    )

    print(
        f"Buy attempts: {DIAG['buy_attempts']}"
    )

    print(
        f"Buy success: {DIAG['buy_success']}"
    )

    print(
        f"Buy rejected: {DIAG['buy_rejected']}"
    )

    print("\nTop rejection reasons:")

    print(top_reasons(12))

    print("========================================\n")

    message = (
        "🔎 ZETA V2.4 DIAGNOSTIC\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Runtime: {runtime_text()}\n"
        f"OTC feeds: {len(otc_assets)}\n"
        f"Orders: {total_trades}/{TARGET_TRADES}\n"
        "\n"
        "📊 STRATEGY PIPELINE\n"
        f"Assets scanned: {DIAG['assets_seen']}\n"
        f"5M trend passed: {DIAG['trend_pass']}\n"
        f"ADX passed: {DIAG['adx_pass']}\n"
        f"Pullback passed: {DIAG['pullback_pass']}\n"
        f"Zone passed: {DIAG['zone_pass']}\n"
        f"Rejection passed: {DIAG['rejection_pass']}\n"
        f"Structure passed: {DIAG['structure_pass']}\n"
        f"Confirmation passed: {DIAG['confirmation_pass']}\n"
        f"Momentum passed: {DIAG['momentum_pass']}\n"
        f"RSI passed: {DIAG['rsi_pass']}\n"
        f"Room passed: {DIAG['room_pass']}\n"
        f"Score >= {MIN_SCORE}: {DIAG['score_pass']}\n"
        "\n"
        "🚦 EXECUTION\n"
        f"Valid signals: {DIAG['valid_signals']}\n"
        f"Buy attempts: {DIAG['buy_attempts']}\n"
        f"Buy accepted: {DIAG['buy_success']}\n"
        f"Buy rejected: {DIAG['buy_rejected']}\n"
        "\n"
        "❌ TOP BLOCKERS\n"
        f"{top_reasons(7)}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Strategy filters unchanged."
    )

    send_telegram(message)


# ============================================================
# HEARTBEAT
# ============================================================

def send_heartbeat():
    message = (
        "🟡 ZETA V2.4 HEARTBEAT\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Status: ONLINE\n"
        f"Runtime: {runtime_text()}\n"
        f"OTC feeds: {len(otc_assets)}\n"
        f"Demo orders: {total_trades}/{TARGET_TRADES}\n"
        f"Account: {BALANCE_MODE}\n"
        "Auto-trading: ON\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Expiry: {EXPIRY_MINUTES} minutes\n"
        f"Minimum score: {MIN_SCORE}\n"
        "Strategy: High Quality Trend Pullback\n"
        "Context: 5M\n"
        "Entry: 1M\n"
        "\n"
        f"Valid signals: {DIAG['valid_signals']}\n"
        f"Buy attempts: {DIAG['buy_attempts']}\n"
        f"Buy accepted: {DIAG['buy_success']}\n"
        f"Buy rejected: {DIAG['buy_rejected']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🟢 SCANNING REAL IQ OPTION OTC MARKETS"
    )

    send_telegram(message)


# ============================================================
# CONNECT
# ============================================================

def connect_iq():
    global api

    print("[IQ] Connecting...")

    try:

        api = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD,
        )

        connected, reason_text = api.connect()

        if not connected:

            print(
                "[IQ] Connection failed:",
                reason_text,
            )

            return False

        try:
            api.change_balance(
                BALANCE_MODE
            )
        except Exception as e:

            print(
                "[IQ] Balance mode warning:",
                e,
            )

        print(
            "[IQ] Connected successfully"
        )

        return True

    except Exception as e:

        print(
            "[IQ CONNECT ERROR]",
            e,
        )

        traceback.print_exc()

        return False


# ============================================================
# TARGET CHECK
# ============================================================

def target_reached():
    return total_trades >= TARGET_TRADES


# ============================================================
# MAIN TRADER
# ============================================================

def run_trader():

    global last_heartbeat
    global last_discovery
    global last_scan

    if not IQ_EMAIL or not IQ_PASSWORD:

        print(
            "[FATAL] IQ_EMAIL / IQ_PASSWORD missing"
        )

        send_telegram(
            "❌ ZETA FATAL ERROR\n"
            "IQ_EMAIL or IQ_PASSWORD is missing."
        )

        return

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:

        print(
            "[WARNING] Telegram credentials missing"
        )

    # --------------------------------------------------------
    # INITIAL CONNECTION
    # --------------------------------------------------------

    connected = connect_iq()

    if not connected:

        send_telegram(
            "🔴 ZETA V2.4 FAILED TO CONNECT"
        )

        return

    send_telegram(
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
        "Diagnostics: ON\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    # --------------------------------------------------------
    # INITIAL OTC DISCOVERY
    # --------------------------------------------------------

    while not otc_assets:

        print(
            "[STARTUP] Discovering real OTC feeds..."
        )

        if not connection_is_alive():

            print(
                "[STARTUP] Connection lost"
            )

            if not reconnect_iq_controlled():

                time.sleep(
                    RECONNECT_INTERVAL
                )

                continue

        refresh_otc_assets()

        if not otc_assets:

            print(
                "[STARTUP] No working OTC feeds yet"
            )

            time.sleep(
                RECONNECT_INTERVAL
            )

    send_telegram(
        "🟢 REAL OTC FEEDS READY\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Working OTC feeds: {len(otc_assets)}\n"
        "Data: REAL IQ OPTION CANDLES\n"
        "Context: 5M\n"
        "Entry: 1M\n"
        f"Expiry: {EXPIRY_MINUTES} minutes\n"
        "Result tracking: MANUAL\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "ZETA V2.4 scanning started."
    )

    last_discovery = time.time()

    # --------------------------------------------------------
    # CONTINUOUS LOOP
    # --------------------------------------------------------

    while True:

        try:

            if target_reached():

                send_telegram(
                    "🏁 ZETA TARGET REACHED\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"Demo orders opened: {total_trades}\n"
                    f"Target: {TARGET_TRADES}\n"
                    "Bot stopped."
                )

                break

            # ------------------------------------------------
            # CONNECTION
            # ------------------------------------------------

            if not connection_is_alive():

                send_telegram(
                    "🔄 ZETA CONNECTION LOST\n"
                    "Attempting controlled reconnect..."
                )

                if not reconnect_iq_controlled():

                    time.sleep(
                        RECONNECT_INTERVAL
                    )

                    continue

                send_telegram(
                    "🟢 ZETA RECONNECTED\n"
                    "Scanning resumed."
                )

            # ------------------------------------------------
            # OTC REFRESH
            # ------------------------------------------------

            if (
                time.time()
                - last_discovery
                >= DISCOVERY_INTERVAL
            ):

                print(
                    "[OTC] Scheduled discovery refresh"
                )

                refresh_otc_assets()

                last_discovery = time.time()

            # ------------------------------------------------
            # HEARTBEAT
            # ------------------------------------------------

            if (
                time.time()
                - last_heartbeat
                >= STATUS_INTERVAL
            ):

                send_heartbeat()

                send_diagnostic_report()

                last_heartbeat = time.time()

            # ------------------------------------------------
            # SCAN
            # ------------------------------------------------

            if (
                time.time()
                - last_scan
                < SCAN_INTERVAL
            ):

                time.sleep(0.5)
                continue

            last_scan = time.time()

            DIAG["scan_cycles"] += 1

            print(
                "\n"
                "========================================"
            )

            print(
                f"[SCAN CYCLE {DIAG['scan_cycles']}] "
                f"{now_text()}"
            )

            print(
                f"OTC feeds: {len(otc_assets)}"
            )

            print(
                "========================================"
            )

            for asset in list(
                otc_assets.keys()
            ):

                if target_reached():
                    break

                DIAG["assets_seen"] += 1

                # --------------------------------------------
                # TRADE LOCK
                # --------------------------------------------

                if asset in last_trade_time:

                    elapsed = (
                        time.time()
                        - last_trade_time[asset]
                    )

                    if elapsed < ASSET_LOCK_SECONDS:

                        diag_inc("lock_block")

                        continue

                # --------------------------------------------
                # SIGNAL COOLDOWN
                # --------------------------------------------

                if asset in last_signal_time:

                    elapsed = (
                        time.time()
                        - last_signal_time[asset]
                    )

                    if elapsed < SIGNAL_COOLDOWN_SECONDS:

                        diag_inc("cooldown_block")

                        continue

                try:

                    # ----------------------------------------
                    # 5M DATA
                    # ----------------------------------------

                    candles5 = get_candles_safe(
                        asset,
                        TF5,
                        CANDLE_COUNT_5M,
                    )

                    # ----------------------------------------
                    # 1M DATA
                    # ----------------------------------------

                    candles1 = get_candles_safe(
                        asset,
                        TF1,
                        CANDLE_COUNT_1M,
                    )

                    if len(candles5) < 80:

                        reason(
                            "5M insufficient candles"
                        )

                        continue

                    if len(candles1) < 80:

                        reason(
                            "1M insufficient candles"
                        )

                        continue

                    # ----------------------------------------
                    # STRATEGY
                    # ----------------------------------------

                    signal = evaluate_zeta_v2(
                        asset,
                        candles5,
                        candles1,
                    )

                    if signal is None:
                        continue

                    # ----------------------------------------
                    # VALID SIGNAL
                    # ----------------------------------------

                    last_signal_time[
                        asset
                    ] = time.time()

                    print(
                        "\n"
                        + format_signal(signal)
                    )

                    send_telegram(
                        format_signal(signal)
                    )

                    # ----------------------------------------
                    # EXECUTE DEMO ORDER
                    # ----------------------------------------

                    execute_demo_trade(
                        signal
                    )

                    # ----------------------------------------
                    # STOP AFTER TARGET
                    # ----------------------------------------

                    if target_reached():
                        break

                except Exception as e:

                    diag_inc("exceptions")

                    print(
                        f"[ASSET ERROR] "
                        f"{asset}: {e}"
                    )

                    traceback.print_exc()

                    continue

            print(
                f"[SCAN COMPLETE] "
                f"Cycle={DIAG['scan_cycles']} "
                f"Assets={DIAG['assets_seen']} "
                f"Valid={DIAG['valid_signals']} "
                f"BuyAttempts={DIAG['buy_attempts']} "
                f"Accepted={DIAG['buy_success']} "
                f"Rejected={DIAG['buy_rejected']}"
            )

            time.sleep(
                SCAN_INTERVAL
            )

        except KeyboardInterrupt:

            print(
                "[STOP] Keyboard interrupt"
            )

            break

        except Exception as e:

            diag_inc("exceptions")

            print(
                "[MAIN LOOP ERROR]",
                e,
            )

            traceback.print_exc()

            time.sleep(
                RECONNECT_INTERVAL
            )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "========================================"
    )

    print(
        f"{VERSION}"
    )

    print(
        "HIGH QUALITY TREND PULLBACK"
    )

    print(
        "PRACTICE / DEMO ONLY"
    )

    print(
        "========================================"
    )

    run_trader()

if __name__ == "__main__":
    main()
