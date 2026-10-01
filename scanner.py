import os
import time
import traceback
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# ZETA V2.3
# HIGH QUALITY TREND PULLBACK
# IQ OPTION OTC DEMO TRADER
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

PRACTICE = True
AUTO_TRADE = True

STAKE = 1.0
EXPIRY = 2
TARGET_COMPLETED_TRADES = 50

MAX_OTC_ASSETS = 70
MAX_ACTIVE_TRADES = 3

TF5 = 300
TF1 = 60

CANDLE_COUNT = 180

MIN_SCORE = 80
MIN_ADX = 18

PULLBACK_MIN_ATR = 0.20
PULLBACK_MAX_ATR = 1.50

MAX_EXTENSION_ATR = 1.80
ZONE_TOLERANCE_ATR = 0.35
MIN_ROOM_ATR = 0.80

ASSET_LOCK_SECONDS = 300
OTC_REFRESH_SCANS = 30

SCAN_INTERVAL = 10
STATUS_INTERVAL = 300
RECONNECT_INTERVAL = 30


# ============================================================
# GLOBAL STATE
# ============================================================

iq = None

otc_assets = []

completed_trades = 0
wins = 0
losses = 0
draws = 0
net_profit = 0.0

active_trades = {}
asset_last_trade = {}

last_status_time = 0
last_otc_refresh_scan = 0

started_at = time.time()


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    try:
        requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown",
            },
            timeout=15,
        )
    except Exception:
        pass


# ============================================================
# HELPERS
# ============================================================

def now_text():
    return datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")


def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def ema(values, period):
    if len(values) < period:
        return None

    result = sum(values[:period]) / period
    multiplier = 2.0 / (period + 1)

    for value in values[period:]:
        result = (
            (value - result) * multiplier
        ) + result

    return result


def rsi(values, period=14):
    if len(values) <= period:
        return None

    gains = []
    losses_list = []

    for index in range(1, len(values)):
        change = (
            values[index] - values[index - 1]
        )

        if change > 0:
            gains.append(change)
            losses_list.append(0.0)
        else:
            gains.append(0.0)
            losses_list.append(abs(change))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses_list[:period]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    current = 100.0 - (100.0 / (1.0 + rs))

    for index in range(period, len(gains)):
        avg_gain = (
            (avg_gain * (period - 1))
            + gains[index]
        ) / period

        avg_loss = (
            (avg_loss * (period - 1))
            + losses_list[index]
        ) / period

        if avg_loss == 0:
            current = 100.0
        else:
            rs = avg_gain / avg_loss
            current = 100.0 - (
                100.0 / (1.0 + rs)
            )

    return current


def atr(candles, period=14):
    if len(candles) <= period:
        return None

    true_ranges = []

    for index in range(1, len(candles)):
        high = safe_float(
            candles[index]["max"]
        )
        low = safe_float(
            candles[index]["min"]
        )
        previous_close = safe_float(
            candles[index - 1]["close"]
        )

        true_range = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )

        true_ranges.append(true_range)

    if len(true_ranges) < period:
        return None

    value = (
        sum(true_ranges[:period]) / period
    )

    for true_range in true_ranges[period:]:
        value = (
            (value * (period - 1))
            + true_range
        ) / period

    return value


def adx(candles, period=14):
    if len(candles) < (period * 2) + 2:
        return None

    trs = []
    plus_dm = []
    minus_dm = []

    for index in range(1, len(candles)):
        high = safe_float(
            candles[index]["max"]
        )
        low = safe_float(
            candles[index]["min"]
        )

        previous_high = safe_float(
            candles[index - 1]["max"]
        )
        previous_low = safe_float(
            candles[index - 1]["min"]
        )
        previous_close = safe_float(
            candles[index - 1]["close"]
        )

        up_move = high - previous_high
        down_move = previous_low - low

        if up_move > down_move and up_move > 0:
            plus = up_move
        else:
            plus = 0.0

        if down_move > up_move and down_move > 0:
            minus = down_move
        else:
            minus = 0.0

        true_range = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )

        trs.append(true_range)
        plus_dm.append(plus)
        minus_dm.append(minus)

    if len(trs) < period * 2:
        return None

    atr_value = sum(trs[:period]) / period
    plus_value = sum(plus_dm[:period]) / period
    minus_value = sum(minus_dm[:period]) / period

    dx_values = []

    for index in range(period, len(trs)):
        atr_value = (
            (atr_value * (period - 1))
            + trs[index]
        ) / period

        plus_value = (
            (plus_value * (period - 1))
            + plus_dm[index]
        ) / period

        minus_value = (
            (minus_value * (period - 1))
            + minus_dm[index]
        ) / period

        if atr_value == 0:
            continue

        plus_di = (
            100.0 * plus_value / atr_value
        )

        minus_di = (
            100.0 * minus_value / atr_value
        )

        denominator = plus_di + minus_di

        if denominator == 0:
            continue

        dx = (
            100.0
            * abs(plus_di - minus_di)
            / denominator
        )

        dx_values.append(dx)

    if len(dx_values) < period:
        return None

    adx_value = (
        sum(dx_values[:period]) / period
    )

    for dx in dx_values[period:]:
        adx_value = (
            (adx_value * (period - 1))
            + dx
        ) / period

    return adx_value


# ============================================================
# CANDLE FUNCTIONS
# ============================================================

def candle_body(candle):
    return abs(
        safe_float(candle["close"])
        - safe_float(candle["open"])
    )


def bullish_candle(candle):
    return (
        safe_float(candle["close"])
        > safe_float(candle["open"])
    )


def bearish_candle(candle):
    return (
        safe_float(candle["close"])
        < safe_float(candle["open"])
    )


def bullish_engulfing(previous, current):
    return (
        bearish_candle(previous)
        and bullish_candle(current)
        and safe_float(current["open"])
        <= safe_float(previous["close"])
        and safe_float(current["close"])
        >= safe_float(previous["open"])
    )


def bearish_engulfing(previous, current):
    return (
        bullish_candle(previous)
        and bearish_candle(current)
        and safe_float(current["open"])
        >= safe_float(previous["close"])
        and safe_float(current["close"])
        <= safe_float(previous["open"])
    )


def bullish_pin(candle):
    open_price = safe_float(
        candle["open"]
    )
    close_price = safe_float(
        candle["close"]
    )
    high = safe_float(
        candle["max"]
    )
    low = safe_float(
        candle["min"]
    )

    body = abs(close_price - open_price)
    lower_wick = min(
        open_price,
        close_price
    ) - low

    upper_wick = high - max(
        open_price,
        close_price
    )

    return (
        lower_wick >= max(
            body * 2.0,
            0.00000001
        )
        and lower_wick > upper_wick
    )


def bearish_pin(candle):
    open_price = safe_float(
        candle["open"]
    )
    close_price = safe_float(
        candle["close"]
    )
    high = safe_float(
        candle["max"]
    )
    low = safe_float(
        candle["min"]
    )

    body = abs(close_price - open_price)

    upper_wick = high - max(
        open_price,
        close_price
    )

    lower_wick = min(
        open_price,
        close_price
    ) - low

    return (
        upper_wick >= max(
            body * 2.0,
            0.00000001
        )
        and upper_wick > lower_wick
    )


# ============================================================
# CONNECTION
# ============================================================

def connect():
    global iq

    print("Connecting to IQ Option...")

    iq = IQ_Option(
        IQ_EMAIL,
        IQ_PASSWORD
    )

    connected, reason = iq.connect()

    if not connected:
        raise RuntimeError(
            f"IQ Option connection failed: {reason}"
        )

    try:
        iq.change_balance("PRACTICE")
    except Exception:
        pass

    time.sleep(3)

    print("IQ Option connection: OK")

    telegram(
        "🟢 *ZETA V2.3 ONLINE*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Connection: OK\n"
        "Account: PRACTICE\n"
        "Strategy: High Quality Trend Pullback\n"
        "Context: 5M\n"
        "Entry: 1M\n"
        f"Expiry: {EXPIRY} minutes\n"
        f"Minimum score: {MIN_SCORE}\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Target: {TARGET_COMPLETED_TRADES} completed trades\n"
        "Auto-trading: ON"
    )


# ============================================================
# OTC DISCOVERY
# ============================================================

def discover_otc_assets():
    global otc_assets

    discovered = set()

    try:
        market_data = iq.get_all_open_time()

        if isinstance(market_data, dict):

            for section in market_data.values():

                if not isinstance(section, dict):
                    continue

                for asset, info in section.items():

                    if not isinstance(info, dict):
                        continue

                    asset_name = str(asset)

                    if "OTC" not in asset_name.upper():
                        continue

                    if info.get("open") is True:
                        discovered.add(asset_name)

    except Exception as error:
        print(
            "OTC primary discovery error:",
            error
        )

    if discovered:
        result = sorted(discovered)[
            :MAX_OTC_ASSETS
        ]

        otc_assets = result

        print(
            f"OTC markets discovered: "
            f"{len(result)}"
        )

        return result

    # --------------------------------------------------------
    # FALLBACK INIT DATA
    # --------------------------------------------------------

    try:
        init_data = iq.get_all_init_v2()

        def search_object(value):

            if isinstance(value, dict):

                for key, item in value.items():

                    key_text = str(key)

                    if (
                        "OTC" in key_text.upper()
                    ):
                        discovered.add(
                            key_text
                        )

                    search_object(item)

            elif isinstance(value, list):

                for item in value:
                    search_object(item)

        search_object(init_data)

    except Exception as error:
        print(
            "OTC fallback discovery error:",
            error
        )

    result = []

    for asset in sorted(discovered):

        if "OTC" in asset.upper():
            result.append(asset)

        if len(result) >= MAX_OTC_ASSETS:
            break

    if result:
        otc_assets = result

    print(
        f"OTC markets discovered: "
        f"{len(result)}"
    )

    return result


def wait_for_otc():
    global otc_assets

    while True:

        try:
            assets = discover_otc_assets()

            if assets:
                telegram(
                    "🟢 *OTC MARKETS READY*\n"
                    f"Markets discovered: {len(assets)}\n"
                    "Scanning discovered OTC markets."
                )

                return assets

        except Exception as error:
            print(
                "OTC discovery exception:",
                error
            )

        telegram(
            "🟡 *WAITING FOR OTC MARKETS*\n"
            "Connection is active, but no open "
            "OTC instruments were discovered.\n"
            "Retrying..."
        )

        time.sleep(RECONNECT_INTERVAL)


# ============================================================
# CANDLES
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
            return None

        return sorted(
            candles,
            key=lambda candle: safe_float(
                candle.get("from", 0)
            )
        )

    except Exception as error:
        print(
            f"Candle error {asset}:",
            error
        )
        return None


# ============================================================
# SIGNAL ANALYSIS
# ============================================================

def analyze_asset(asset):

    candles5 = get_candles(
        asset,
        TF5,
        CANDLE_COUNT
    )

    if not candles5 or len(candles5) < 80:
        return None

    candles1 = get_candles(
        asset,
        TF1,
        CANDLE_COUNT
    )

    if not candles1 or len(candles1) < 80:
        return None

    # Ignore currently forming candles.
    candles5 = candles5[:-1]
    candles1 = candles1[:-1]

    closes5 = [
        safe_float(candle["close"])
        for candle in candles5
    ]

    closes1 = [
        safe_float(candle["close"])
        for candle in candles1
    ]

    ema20_5 = ema(closes5, 20)
    ema50_5 = ema(closes5, 50)

    ema20_1 = ema(closes1, 20)
    ema50_1 = ema(closes1, 50)

    atr5 = atr(candles5, 14)
    atr1 = atr(candles1, 14)

    rsi1 = rsi(closes1, 14)
    adx5 = adx(candles5, 14)

    if any(
        value is None
        for value in (
            ema20_5,
            ema50_5,
            ema20_1,
            ema50_1,
            atr5,
            atr1,
            rsi1,
            adx5,
        )
    ):
        return None

    if adx5 < MIN_ADX:
        return None

    latest5 = candles5[-1]
    latest1 = candles1[-1]
    previous1 = candles1[-2]

    close5 = safe_float(
        latest5["close"]
    )

    close1 = safe_float(
        latest1["close"]
    )

    # --------------------------------------------------------
    # 5M TREND
    # --------------------------------------------------------

    if (
        ema20_5 > ema50_5
        and close5 > ema20_5
    ):
        direction = "CALL"

    elif (
        ema20_5 < ema50_5
        and close5 < ema20_5
    ):
        direction = "PUT"

    else:
        return None

    score = 20
    reasons = ["strong 5M trend"]

    # --------------------------------------------------------
    # 1M EMA STRUCTURE
    # --------------------------------------------------------

    if direction == "CALL":

        if ema20_1 > ema50_1:
            score += 10
            reasons.append("1M EMA bullish")

    else:

        if ema20_1 < ema50_1:
            score += 10
            reasons.append("1M EMA bearish")

    # --------------------------------------------------------
    # PULLBACK
    # --------------------------------------------------------

    distance = abs(
        close1 - ema20_1
    )

    distance_atr = (
        distance / atr1
        if atr1 > 0
        else 999
    )

    if not (
        PULLBACK_MIN_ATR
        <= distance_atr
        <= PULLBACK_MAX_ATR
    ):
        return None

    score += 15
    reasons.append("valid pullback")

    # --------------------------------------------------------
    # ZONE
    # --------------------------------------------------------

    recent = candles1[-30:]

    highs = [
        safe_float(candle["max"])
        for candle in recent
    ]

    lows = [
        safe_float(candle["min"])
        for candle in recent
    ]

    resistance = max(highs)
    support = min(lows)

    if direction == "CALL":

        zone_distance = abs(
            close1 - support
        )

        if (
            zone_distance
            > atr1 * ZONE_TOLERANCE_ATR
        ):
            return None

        score += 15
        reasons.append("support zone")

    else:

        zone_distance = abs(
            resistance - close1
        )

        if (
            zone_distance
            > atr1 * ZONE_TOLERANCE_ATR
        ):
            return None

        score += 15
        reasons.append("resistance zone")

    # --------------------------------------------------------
    # REJECTION
    # --------------------------------------------------------

    if direction == "CALL":

        rejection = (
            bullish_pin(latest1)
            or bullish_engulfing(
                previous1,
                latest1
            )
        )

        if not rejection:
            return None

        score += 20
        reasons.append(
            "bullish rejection"
        )

    else:

        rejection = (
            bearish_pin(latest1)
            or bearish_engulfing(
                previous1,
                latest1
            )
        )

        if not rejection:
            return None

        score += 20
        reasons.append(
            "bearish rejection"
        )

    # --------------------------------------------------------
    # CONFIRMATION
    # --------------------------------------------------------

    if direction == "CALL":

        if not bullish_candle(latest1):
            return None

        score += 10
        reasons.append(
            "bullish confirmation"
        )

    else:

        if not bearish_candle(latest1):
            return None

        score += 10
        reasons.append(
            "bearish confirmation"
        )

    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------

    body = candle_body(latest1)

    if (
        atr1 > 0
        and body >= atr1 * 0.20
    ):
        score += 5
        reasons.append("momentum")

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    if not (
        35 <= rsi1 <= 65
    ):
        return None

    score += 5
    reasons.append("RSI supportive")

    # --------------------------------------------------------
    # ROOM
    # --------------------------------------------------------

    if direction == "CALL":
        room = resistance - close1
    else:
        room = close1 - support

    room_atr = (
        room / atr1
        if atr1 > 0
        else 0
    )

    if room_atr < MIN_ROOM_ATR:
        return None

    score += 5
    reasons.append("sufficient room")

    # --------------------------------------------------------
    # EXTENSION PROTECTION
    # --------------------------------------------------------

    extension_atr = (
        abs(close5 - ema20_5) / atr5
        if atr5 > 0
        else 999
    )

    if extension_atr > MAX_EXTENSION_ATR:
        return None

    # --------------------------------------------------------
    # FINAL SCORE
    # --------------------------------------------------------

    if score < MIN_SCORE:
        return None

    return {
        "asset": asset,
        "direction": direction,
        "score": score,
        "rsi": round(rsi1, 2),
        "adx": round(adx5, 2),
        "room_atr": round(
            room_atr,
            2
        ),
        "extension_atr": round(
            extension_atr,
            2
        ),
        "reasons": reasons,
    }


# ============================================================
# TRADE EXECUTION
# ============================================================

def place_trade(signal):

    global active_trades

    asset = signal["asset"]
    direction = signal["direction"]

    if asset in active_trades:
        return False

    if len(active_trades) >= MAX_ACTIVE_TRADES:
        return False

    last_trade = asset_last_trade.get(
        asset,
        0
    )

    if (
        time.time() - last_trade
        < ASSET_LOCK_SECONDS
    ):
        return False

    action = (
        "call"
        if direction == "CALL"
        else "put"
    )

    signal_id = (
        f"ZETA-"
        f"{asset.replace('-', '')}-"
        f"{direction}-"
        f"{int(time.time())}"
    )

    telegram(
        "🟡 *ZETA DEMO SIGNAL*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Asset: `{asset}`\n"
        f"Direction: *{direction}*\n"
        f"Score: *{signal['score']}*\n"
        f"RSI: {signal['rsi']}\n"
        f"ADX: {signal['adx']}\n"
        f"Room: {signal['room_atr']} ATR\n"
        f"Extension: {signal['extension_atr']} ATR\n"
        f"Expiry: {EXPIRY} minutes\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Signal ID: `{signal_id}`"
    )

    if not AUTO_TRADE:
        return False

    try:

        success, order_id = iq.buy(
            STAKE,
            asset,
            action,
            EXPIRY
        )

    except Exception as error:

        print(
            f"Order error {asset}:",
            error
        )

        telegram(
            "🔴 *ORDER ERROR*\n"
            f"Asset: `{asset}`\n"
            f"Error: `{str(error)[:300]}`"
        )

        return False

    if not success:

        telegram(
            "🔴 *ORDER FAILED*\n"
            f"Asset: `{asset}`\n"
            f"Direction: {direction}\n"
            f"Signal ID: `{signal_id}`"
        )

        return False

    active_trades[asset] = {
        "asset": asset,
        "direction": direction,
        "score": signal["score"],
        "signal_id": signal_id,
        "order_id": order_id,
        "stake": STAKE,
        "opened_at": time.time(),
    }

    asset_last_trade[asset] = time.time()

    telegram(
        "🚀 *ZETA DEMO TRADE OPENED*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Asset: `{asset}`\n"
        f"Direction: *{direction}*\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Expiry: {EXPIRY} minutes\n"
        f"Score: {signal['score']}\n"
        f"Signal ID: `{signal_id}`"
    )

    return True


# ============================================================
# RESULT TRACKING
# ============================================================

def check_trade_result(trade):

    global completed_trades
    global wins
    global losses
    global draws
    global net_profit

    try:

        result = iq.check_win_v4(
            trade["order_id"]
        )

        if result is None:
            return False

        result = safe_float(result)

        if result > 0:
            outcome = "WIN"
            wins += 1

        elif result < 0:
            outcome = "LOSS"
            losses += 1

        else:
            outcome = "DRAW"
            draws += 1

        completed_trades += 1
        net_profit += result

        win_rate = (
            wins / completed_trades
        ) * 100

        telegram(
            "🟢 *ZETA RESULT*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Asset: `{trade['asset']}`\n"
            f"Direction: {trade['direction']}\n"
            f"Outcome: *{outcome}*\n"
            f"Result: ${result:+.2f}\n"
            f"Completed: "
            f"{completed_trades}/"
            f"{TARGET_COMPLETED_TRADES}\n"
            f"Wins: {wins}\n"
            f"Losses: {losses}\n"
            f"Draws: {draws}\n"
            f"Win rate: {win_rate:.2f}%\n"
            f"Net P/L: ${net_profit:+.2f}\n"
            f"Signal ID: `{trade['signal_id']}`"
        )

        return True

    except Exception as error:

        print(
            "Result check error:",
            error
        )

        return False


def process_active_trades():

    finished = []

    for asset, trade in list(
        active_trades.items()
    ):

        elapsed = (
            time.time()
            - trade["opened_at"]
        )

        if elapsed < (
            EXPIRY * 60 + 10
        ):
            continue

        if check_trade_result(trade):
            finished.append(asset)

    for asset in finished:
        active_trades.pop(
            asset,
            None
        )


# ============================================================
# STATUS
# ============================================================

def send_status():

    global last_status_time

    current_time = time.time()

    if (
        current_time - last_status_time
        < STATUS_INTERVAL
    ):
        return

    last_status_time = current_time

    if completed_trades:
        win_rate = (
            wins / completed_trades
        ) * 100
    else:
        win_rate = 0.0

    runtime = int(
        current_time - started_at
    )

    hours = runtime // 3600
    minutes = (
        runtime % 3600
    ) // 60

    telegram(
        "🟡 *ZETA V2.3 STATUS*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"OTC markets: {len(otc_assets)}\n"
        f"Completed: "
        f"{completed_trades}/"
        f"{TARGET_COMPLETED_TRADES}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Draws: {draws}\n"
        f"Win rate: {win_rate:.2f}%\n"
        f"Net demo P/L: ${net_profit:+.2f}\n"
        f"Active trades: "
        f"{len(active_trades)}/"
        f"{MAX_ACTIVE_TRADES}\n"
        f"Runtime: {hours}h {minutes}m\n"
        "Account: PRACTICE\n"
        "Auto-trading: ON"
    )


# ============================================================
# SCANNER
# ============================================================

def scanner_loop():

    global otc_assets
    global last_otc_refresh_scan

    scan_number = 0

    while (
        completed_trades
        < TARGET_COMPLETED_TRADES
    ):

        scan_number += 1

        try:

            # Refresh OTC list periodically.
            if (
                not otc_assets
                or (
                    scan_number
                    - last_otc_refresh_scan
                    >= OTC_REFRESH_SCANS
                )
            ):

                fresh_assets = (
                    discover_otc_assets()
                )

                if fresh_assets:

                    otc_assets = (
                        fresh_assets
                    )

                    last_otc_refresh_scan = (
                        scan_number
                    )

                elif not otc_assets:

                    print(
                        "No OTC markets found. "
                        "Waiting..."
                    )

                    time.sleep(
                        RECONNECT_INTERVAL
                    )

                    continue

            # Check trades already open.
            process_active_trades()

            if (
                completed_trades
                >= TARGET_COMPLETED_TRADES
            ):
                break

            # Scan for new trades.
            if (
                len(active_trades)
                < MAX_ACTIVE_TRADES
            ):

                for asset in otc_assets:

                    if (
                        completed_trades
                        >= TARGET_COMPLETED_TRADES
                    ):
                        break

                    if (
                        len(active_trades)
                        >= MAX_ACTIVE_TRADES
                    ):
                        break

                    if asset in active_trades:
                        continue

                    last_trade = (
                        asset_last_trade.get(
                            asset,
                            0
                        )
                    )

                    if (
                        time.time()
                        - last_trade
                        < ASSET_LOCK_SECONDS
                    ):
                        continue

                    signal = analyze_asset(
                        asset
                    )

                    if signal:

                        print(
                            f"SIGNAL: {asset} "
                            f"{signal['direction']} "
                            f"score="
                            f"{signal['score']}"
                        )

                        place_trade(
                            signal
                        )

            send_status()

            time.sleep(
                SCAN_INTERVAL
            )

        except KeyboardInterrupt:
            raise

        except Exception as error:

            print(
                "Scanner loop error:",
                error
            )

            traceback.print_exc()

            telegram(
                "🔴 *ZETA LOOP ERROR*\n"
                f"`{str(error)[:400]}`\n"
                "Continuing..."
            )

            time.sleep(
                RECONNECT_INTERVAL
            )


# ============================================================
# FINAL REPORT
# ============================================================

def finish():

    if completed_trades:
        win_rate = (
            wins / completed_trades
        ) * 100
    else:
        win_rate = 0.0

    telegram(
        "🏁 *ZETA V2.3 TEST COMPLETE*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Completed: {completed_trades}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Draws: {draws}\n"
        f"Win rate: {win_rate:.2f}%\n"
        f"Net demo P/L: ${net_profit:+.2f}\n"
        f"Target: {TARGET_COMPLETED_TRADES}"
    )

    print(
        "ZETA V2.3 TEST COMPLETE"
    )
    print(
        f"Completed: {completed_trades}"
    )
    print(
        f"Wins: {wins}"
    )
    print(
        f"Losses: {losses}"
    )
    print(
        f"Draws: {draws}"
    )
    print(
        f"Win rate: {win_rate:.2f}%"
    )
    print(
        f"Net P/L: ${net_profit:+.2f}"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    required = {
        "IQ_EMAIL": IQ_EMAIL,
        "IQ_PASSWORD": IQ_PASSWORD,
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    }

    for name, value in required.items():

        if not value:
            raise RuntimeError(
                f"{name} secret is missing."
            )

    print(
        "========================================"
    )
    print(
        "ZETA V2.3"
    )
    print(
        "IQ OPTION OTC DEMO BOT"
    )
    print(
        "========================================"
    )

    while (
        completed_trades
        < TARGET_COMPLETED_TRADES
    ):

        try:

            connect()

            wait_for_otc()

            scanner_loop()

            break

        except KeyboardInterrupt:

            print(
                "Bot stopped."
            )

            break

        except Exception as error:

            print(
                "MAIN ERROR:",
                error
            )

            traceback.print_exc()

            telegram(
                "🔴 *ZETA MAIN ERROR*\n"
                f"`{str(error)[:400]}`\n"
                "Retrying..."
            )

            time.sleep(
                RECONNECT_INTERVAL
            )

    finish()


if __name__ == "__main__":
    main()
