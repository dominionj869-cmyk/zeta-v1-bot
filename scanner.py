import os
import time
import traceback
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# ZETA V2.2
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

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown"
            },
            timeout=15
        )
    except Exception:
        pass


# ============================================================
# HELPERS
# ============================================================

def now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    result = sum(values[:period]) / period

    for value in values[period:]:
        result = ((value - result) * multiplier) + result

    return result


def rsi(values, period=14):
    if len(values) <= period:
        return None

    gains = []
    losses_list = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        if change > 0:
            gains.append(change)
            losses_list.append(0)
        else:
            gains.append(0)
            losses_list.append(abs(change))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses_list[:period]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    current_rsi = 100 - (100 / (1 + rs))

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses_list[i]) / period

        if avg_loss == 0:
            current_rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            current_rsi = 100 - (100 / (1 + rs))

    return current_rsi


def atr(candles, period=14):
    if len(candles) <= period:
        return None

    true_ranges = []

    for i in range(1, len(candles)):
        high = safe_float(candles[i]["max"])
        low = safe_float(candles[i]["min"])
        previous_close = safe_float(candles[i - 1]["close"])

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )

        true_ranges.append(tr)

    if len(true_ranges) < period:
        return None

    value = sum(true_ranges[:period]) / period

    for tr in true_ranges[period:]:
        value = ((value * (period - 1)) + tr) / period

    return value


def adx(candles, period=14):
    if len(candles) < period * 2 + 2:
        return None

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(candles)):
        high = safe_float(candles[i]["max"])
        low = safe_float(candles[i]["min"])

        prev_high = safe_float(candles[i - 1]["max"])
        prev_low = safe_float(candles[i - 1]["min"])
        prev_close = safe_float(candles[i - 1]["close"])

        up_move = high - prev_high
        down_move = prev_low - low

        if up_move > down_move and up_move > 0:
            pdm = up_move
        else:
            pdm = 0

        if down_move > up_move and down_move > 0:
            mdm = down_move
        else:
            mdm = 0

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

        trs.append(tr)
        plus_dm.append(pdm)
        minus_dm.append(mdm)

    if len(trs) < period * 2:
        return None

    atr_value = sum(trs[:period]) / period
    plus_value = sum(plus_dm[:period]) / period
    minus_value = sum(minus_dm[:period]) / period

    dx_values = []

    for i in range(period, len(trs)):
        atr_value = ((atr_value * (period - 1)) + trs[i]) / period
        plus_value = ((plus_value * (period - 1)) + plus_dm[i]) / period
        minus_value = ((minus_value * (period - 1)) + minus_dm[i]) / period

        if atr_value == 0:
            continue

        plus_di = 100 * (plus_value / atr_value)
        minus_di = 100 * (minus_value / atr_value)

        denominator = plus_di + minus_di

        if denominator == 0:
            continue

        dx = 100 * abs(plus_di - minus_di) / denominator
        dx_values.append(dx)

    if len(dx_values) < period:
        return None

    adx_value = sum(dx_values[:period]) / period

    for dx in dx_values[period:]:
        adx_value = ((adx_value * (period - 1)) + dx) / period

    return adx_value


# ============================================================
# CANDLE FUNCTIONS
# ============================================================

def candle_body(c):
    return abs(
        safe_float(c["close"]) -
        safe_float(c["open"])
    )


def candle_range(c):
    return (
        safe_float(c["max"]) -
        safe_float(c["min"])
    )


def bullish_candle(c):
    return safe_float(c["close"]) > safe_float(c["open"])


def bearish_candle(c):
    return safe_float(c["close"]) < safe_float(c["open"])


def bullish_engulfing(previous, current):
    return (
        bearish_candle(previous)
        and bullish_candle(current)
        and safe_float(current["open"]) <= safe_float(previous["close"])
        and safe_float(current["close"]) >= safe_float(previous["open"])
    )


def bearish_engulfing(previous, current):
    return (
        bullish_candle(previous)
        and bearish_candle(current)
        and safe_float(current["open"]) >= safe_float(previous["close"])
        and safe_float(current["close"]) <= safe_float(previous["open"])
    )


def bullish_pin(c):
    o = safe_float(c["open"])
    cl = safe_float(c["close"])
    high = safe_float(c["max"])
    low = safe_float(c["min"])

    body = abs(cl - o)
    lower_wick = min(o, cl) - low
    upper_wick = high - max(o, cl)

    return (
        lower_wick >= max(body * 2, 0.00000001)
        and lower_wick > upper_wick
    )


def bearish_pin(c):
    o = safe_float(c["open"])
    cl = safe_float(c["close"])
    high = safe_float(c["max"])
    low = safe_float(c["min"])

    body = abs(cl - o)
    upper_wick = high - max(o, cl)
    lower_wick = min(o, cl) - low

    return (
        upper_wick >= max(body * 2, 0.00000001)
        and upper_wick > lower_wick
    )


# ============================================================
# CONNECTION
# ============================================================

def connect():
    global iq

    telegram(
        "🟢 *ZETA V2.2 ONLINE*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Strategy: High Quality Trend Pullback\n"
        "Context: 5M\n"
        "Entry: 1M\n"
        f"Expiry: {EXPIRY} minutes\n"
        f"Minimum score: {MIN_SCORE}\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Target: {TARGET_COMPLETED_TRADES} completed trades\n"
        "Account: PRACTICE\n"
        "Auto-trading: ON"
    )

    iq = IQ_Option(IQ_EMAIL, IQ_PASSWORD)

    check, reason = iq.connect()

    if not check:
        raise RuntimeError(f"IQ Option connection failed: {reason}")

    try:
        iq.change_balance("PRACTICE")
    except Exception:
        pass

    time.sleep(3)

    print("IQ Option connection: OK")

    telegram(
        "🟢 *IQ OPTION CONNECTION OK*\n"
        f"Time: {now_text()}"
    )

    return True


# ============================================================
# OTC DISCOVERY
# ============================================================

def discover_otc_assets():
    """
    Discovers currently open OTC instruments from IQ Option.

    The bot never invents OTC symbols.
    """

    global otc_assets

    discovered = set()

    try:
        data = iq.get_all_open_time()

        if isinstance(data, dict):

            for market_name, market_data in data.items():

                if not isinstance(market_data, dict):
                    continue

                for asset, info in market_data.items():

                    if not isinstance(info, dict):
                        continue

                    name = str(asset).upper()

                    if "OTC" not in name:
                        continue

                    is_open = info.get("open")

                    if is_open is True:
                        discovered.add(asset)

        if discovered:
            result = sorted(discovered)[:MAX_OTC_ASSETS]

            otc_assets = result

            print(f"OTC markets discovered: {len(result)}")

            return result

    except Exception as e:
        print(f"Primary OTC discovery error: {e}")

    # --------------------------------------------------------
    # FALLBACK: IQ OPTION INIT DATA
    # --------------------------------------------------------

    try:
        init_data = iq.get_all_init_v2()

        if isinstance(init_data, dict):

            def recursive_search(obj):

                if isinstance(obj, dict):

                    for key, value in obj.items():

                        key_text = str(key).upper()

                        if "OTC" in key_text:
                            discovered.add(str(key))

                        recursive_search(value)

                elif isinstance(obj, list):

                    for item in obj:
                        recursive_search(item)

            recursive_search(init_data)

    except Exception as e:
        print(f"Fallback OTC discovery error: {e}")

    result = []

    for asset in sorted(discovered):

        if "OTC" in asset.upper():

            result.append(asset)

        if len(result) >= MAX_OTC_ASSETS:
            break

    if result:
        otc_assets = result

    print(f"OTC markets discovered: {len(result)}")

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
                    "Scanning ALL discovered OTC markets."
                )

                return assets

            telegram(
                "🟡 *WAITING FOR OTC MARKETS*\n"
                "IQ Option connection is active, "
                "but no open OTC instruments are currently available.\n"
                "Retrying..."
            )

        except Exception as e:

            print(f"OTC discovery exception: {e}")

        time.sleep(RECONNECT_INTERVAL)


# ============================================================
# GET CANDLES
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

        candles = sorted(
            candles,
            key=lambda x: safe_float(x.get("from", 0))
        )

        return candles

    except Exception as e:
        print(f"Candle error {asset}: {e}")
        return None


# ============================================================
# SIGNAL ENGINE
# ============================================================

def analyze_asset(asset):

    candles5 = get_candles(asset, TF5, CANDLE_COUNT)

    if not candles5 or len(candles5) < 80:
        return None

    candles1 = get_candles(asset, TF1, CANDLE_COUNT)

    if not candles1 or len(candles1) < 80:
        return None

    # --------------------------------------------------------
    # CLOSED CANDLES ONLY
    # --------------------------------------------------------

    candles5 = candles5[:-1]
    candles1 = candles1[:-1]

    closes5 = [
        safe_float(c["close"])
        for c in candles5
    ]

    closes1 = [
        safe_float(c["close"])
        for c in candles1
    ]

    ema20_5 = ema(closes5, 20)
    ema50_5 = ema(closes5, 50)

    ema20_1 = ema(closes1, 20)
    ema50_1 = ema(closes1, 50)

    atr5 = atr(candles5, 14)
    atr1 = atr(candles1, 14)

    rsi1 = rsi(closes1, 14)
    adx5 = adx(candles5, 14)

    if None in (
        ema20_5,
        ema50_5,
        ema20_1,
        ema50_1,
        atr5,
        atr1,
        rsi1,
        adx5
    ):
        return None

    if adx5 < MIN_ADX:
        return None

    latest5 = candles5[-1]
    latest1 = candles1[-1]

    close5 = safe_float(latest5["close"])
    close1 = safe_float(latest1["close"])

    # --------------------------------------------------------
    # TREND
    # --------------------------------------------------------

    if ema20_5 > ema50_5 and close5 > ema20_5:
        trend = "CALL"
    elif ema20_5 < ema50_5 and close5 < ema20_5:
        trend = "PUT"
    else:
        return None

    score = 0
    reasons = []

    # Strong 5M trend
    score += 20
    reasons.append("strong 5M trend")

    # EMA structure
    if trend == "CALL":
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

    distance_from_ema = abs(close1 - ema20_1)

    distance_atr = (
        distance_from_ema / atr1
        if atr1 > 0
        else 999
    )

    if (
        PULLBACK_MIN_ATR
        <= distance_atr
        <= PULLBACK_MAX_ATR
    ):
        score += 15
        reasons.append("valid pullback")
    else:
        return None

    # --------------------------------------------------------
    # SUPPORT / RESISTANCE ZONE
    # --------------------------------------------------------

    recent = candles1[-30:]

    highs = [
        safe_float(c["max"])
        for c in recent
    ]

    lows = [
        safe_float(c["min"])
        for c in recent
    ]

    resistance = max(highs)
    support = min(lows)

    if trend == "CALL":

        zone_distance = abs(close1 - support)

        if zone_distance <= atr1 * ZONE_TOLERANCE_ATR:
            score += 15
            reasons.append("support zone")
        else:
            return None

    else:

        zone_distance = abs(resistance - close1)

        if zone_distance <= atr1 * ZONE_TOLERANCE_ATR:
            score += 15
            reasons.append("resistance zone")
        else:
            return None

    # --------------------------------------------------------
    # FRESH REJECTION
    # --------------------------------------------------------

    previous1 = candles1[-2]

    if trend == "CALL":

        rejection = (
            bullish_pin(latest1)
            or bullish_engulfing(previous1, latest1)
        )

        if rejection:
            score += 20
            reasons.append("bullish rejection")
        else:
            return None

    else:

        rejection = (
            bearish_pin(latest1)
            or bearish_engulfing(previous1, latest1)
        )

        if rejection:
            score += 20
            reasons.append("bearish rejection")
        else:
            return None

    # --------------------------------------------------------
    # CLOSED CANDLE CONFIRMATION
    # --------------------------------------------------------

    if trend == "CALL" and bullish_candle(latest1):
        score += 10
        reasons.append("bullish confirmation")

    elif trend == "PUT" and bearish_candle(latest1):
        score += 10
        reasons.append("bearish confirmation")

    else:
        return None

    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------

    momentum = candle_body(latest1)

    if atr1 > 0 and momentum >= atr1 * 0.20:
        score += 5
        reasons.append("momentum")

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    if trend == "CALL":

        if 35 <= rsi1 <= 65:
            score += 5
            reasons.append("RSI supportive")
        else:
            return None

    else:

        if 35 <= rsi1 <= 65:
            score += 5
            reasons.append("RSI supportive")
        else:
            return None

    # --------------------------------------------------------
    # ROOM
    # --------------------------------------------------------

    if trend == "CALL":

        room = resistance - close1

    else:

        room = close1 - support

    room_atr = (
        room / atr1
        if atr1 > 0
        else 0
    )

    if room_atr >= MIN_ROOM_ATR:
        score += 5
        reasons.append("sufficient room")
    else:
        return None

    # --------------------------------------------------------
    # EXTENSION PROTECTION
    # --------------------------------------------------------

    extension = (
        abs(close5 - ema20_5) / atr5
        if atr5 > 0
        else 999
    )

    if extension > MAX_EXTENSION_ATR:
        return None

    # --------------------------------------------------------
    # FINAL SCORE
    # --------------------------------------------------------

    if score < MIN_SCORE:
        return None

    return {
        "asset": asset,
        "direction": trend,
        "score": score,
        "rsi": round(rsi1, 2),
        "adx": round(adx5, 2),
        "room_atr": round(room_atr, 2),
        "extension_atr": round(extension, 2),
        "reasons": reasons,
    }


# ============================================================
# PLACE TRADE
# ============================================================

def place_trade(signal):

    global active_trades

    asset = signal["asset"]
    direction = signal["direction"]

    if asset in active_trades:
        return False

    if len(active_trades) >= MAX_ACTIVE_TRADES:
        return False

    last_trade = asset_last_trade.get(asset, 0)

    if time.time() - last_trade < ASSET_LOCK_SECONDS:
        return False

    try:

        action = "call" if direction == "CALL" else "put"

        signal_id = (
            f"ZETA-{asset.replace('-', '')}-"
            f"{direction}-{int(time.time())}"
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

        success, order_id = iq.buy(
            STAKE,
            asset,
            action,
            EXPIRY
        )

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
            "expiry": EXPIRY,
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

    except Exception as e:

        print(f"Order error {asset}: {e}")

        telegram(
            "🔴 *ORDER ERROR*\n"
            f"Asset: `{asset}`\n"
            f"Error: `{str(e)[:300]}`"
        )

        return False


# ============================================================
# CHECK RESULTS
# ============================================================

def check_trade_result(trade):

    global completed_trades
    global wins
    global losses
    global draws
    global net_profit

    order_id = trade["order_id"]

    try:

        result = iq.check_win_v4(order_id)

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

        asset = trade["asset"]

        telegram(
            (
                "🟢 *ZETA RESULT*\n"
                if outcome == "WIN"
                else
                "🔴 *ZETA RESULT*\n"
                if outcome == "LOSS"
                else
                "⚪ *ZETA RESULT*\n"
            )
            +
            "━━━━━━━━━━━━━━━━━━\n"
            f"Asset: `{asset}`\n"
            f"Direction: {trade['direction']}\n"
            f"Outcome: *{outcome}*\n"
            f"Result: ${result:+.2f}\n"
            f"Completed: {completed_trades}/{TARGET_COMPLETED_TRADES}\n"
            f"Wins: {wins}\n"
            f"Losses: {losses}\n"
            f"Draws: {draws}\n"
            f"Win rate: {((wins / completed_trades) * 100):.2f}%\n"
            f"Net P/L: ${net_profit:+.2f}\n"
            f"Signal ID: `{trade['signal_id']}`"
        )

        return True

    except Exception as e:

        print(
            f"Result check error "
            f"{trade['asset']}: {e}"
        )

        return False


def process_active_trades():

    finished_assets = []

    for asset, trade in list(active_trades.items()):

        opened_at = trade["opened_at"]

        # Give IQ Option enough time to close the binary.
        if time.time() < opened_at + (EXPIRY * 60) + 10:
            continue

        if check_trade_result(trade):
            finished_assets.append(asset)

    for asset in finished_assets:
        active_trades.pop(asset, None)


# ============================================================
# STATUS
# ============================================================

def send_status():

    global last_status_time

    if time.time() - last_status_time < STATUS_INTERVAL:
        return

    last_status_time = time.time()

    if completed_trades > 0:
        win_rate = (
            wins / completed_trades
        ) * 100
    else:
        win_rate = 0

    runtime_seconds = int(time.time() - started_at)

    hours = runtime_seconds // 3600
    minutes = (runtime_seconds % 3600) // 60

    telegram(
        "🟡 *ZETA V2.2 STATUS*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"OTC markets: {len(otc_assets)}\n"
        f"Completed: {completed_trades}/{TARGET_COMPLETED_TRADES}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Draws: {draws}\n"
        f"Win rate: {win_rate:.2f}%\n"
        f"Net demo P/L: ${net_profit:+.2f}\n"
        f"Active trades: {len(active_trades)}/{MAX_ACTIVE_TRADES}\n"
        f"Runtime: {hours}h {minutes}m\n"
        "Account: PRACTICE\n"
        "Auto-trading: ON"
    )


# ============================================================
# SCANNER LOOP
# ============================================================

def scanner_loop():

    global otc_assets
    global last_otc_refresh_scan

    scan_number = 0

    while completed_trades < TARGET_COMPLETED_TRADES:

        scan_number += 1

        try:

            # ------------------------------------------------
            # REFRESH OTC LIST
            # ------------------------------------------------

            if (
                not otc_assets
                or scan_number - last_otc_refresh_scan
                >= OTC_REFRESH_SCANS
            ):

                new_assets = discover_otc_assets()

                if new_assets:

                    otc_assets = new_assets
                    last_otc_refresh_scan = scan_number

                    print(
                        f"OTC assets: {len(otc_assets)}"
                    )

                elif not otc_assets:

                    print(
                        "No OTC assets found. "
                        "Waiting..."
                    )

                    time.sleep(RECONNECT_INTERVAL)
                    continue

                else:

                    print(
                        "OTC refresh returned 0. "
                        "Keeping previous list."
                    )

            # ------------------------------------------------
            # CHECK EXISTING TRADES
            # ------------------------------------------------

            process_active_trades()

            if completed_trades >= TARGET_COMPLETED_TRADES:
                break

            # ------------------------------------------------
            # SCAN ASSETS
            # ------------------------------------------------

            if len(active_trades) < MAX_ACTIVE_TRADES:

                for asset in otc_assets:

                    if completed_trades >= TARGET_COMPLETED_TRADES:
                        break

                    if len(active_trades) >= MAX_ACTIVE_TRADES:
                        break

                    if asset in active_trades:
                        continue

                    last_trade = asset_last_trade.get(
                        asset,
                        0
                    )

                    if (
                        time.time() - last_trade
                        < ASSET_LOCK_SECONDS
                    ):
                        continue

                    signal = analyze_asset(asset)

                    if signal:

                        print(
                            f"SIGNAL {asset} "
                            f"{signal['direction']} "
                            f"score={signal['score']}"
                        )

                        place_trade(signal)

            send_status()

            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:

            raise

        except Exception as e:

            print(
                "Scanner loop error:",
                str(e)
            )

            traceback.print_exc()

            telegram(
                "🔴 *ZETA LOOP ERROR*\n"
                f"`{str(e)[:400]}`\n"
                "Bot will continue/reconnect."
            )

            time.sleep(RECONNECT_INTERVAL)


# ============================================================
# FINISH
# ============================================================

def finish():

    if completed_trades > 0:
        win_rate = (
            wins / completed_trades
        ) * 100
    else:
        win_rate = 0

    telegram(
        "🏁 *ZETA V2.2 TEST COMPLETE*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Completed trades: {completed_trades}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Draws: {draws}\n"
        f"Win rate: {win_rate:.2f}%\n"
        f"Net demo P/L: ${net_profit:+.2f}\n"
        f"Target: {TARGET_COMPLETED_TRADES} trades"
    )

    print()
    print("========================================")
    print("ZETA V2.2 TEST COMPLETE")
    print("========================================")
    print(f"Completed: {completed_trades}")
    print(f"Wins:      {wins}")
    print(f"Losses:    {losses}")
    print(f"Draws:     {draws}")
    print(f"Win rate:  {win_rate:.2f}%")
    print(f"Net P/L:   ${net_profit:+.2f}")
    print("========================================")


# ============================================================
# MAIN
# ============================================================

def main():

    if not IQ_EMAIL:
        raise RuntimeError("IQ_EMAIL secret is missing.")

    if not IQ_PASSWORD:
        raise RuntimeError("IQ_PASSWORD secret is missing.")

    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN secret is missing.")

    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID secret is missing.")

    print("========================================")
    print("ZETA V2.2")
    print("IQ OPTION OTC DEMO BOT")
    print("========================================")

    while True:

        try:

            connect()

            assets = wait_for_otc()

            if not assets:
                time.sleep(RECONNECT_INTERVAL)
                continue

            scanner_loop()

            break

        except KeyboardInterrupt:

            print("Bot stopped.")
            break

        except Exception as e:

            print("MAIN ERROR:", e)
            traceback.print_exc()

            telegram(
                "🔴 *ZETA CONNECTION/MAIN ERROR*\n"
                f"`{str(e)[:400]}`\n"
                "Retrying automatically..."
            )

            time.sleep(RECONNECT_INTERVAL)

    finish()


# ============================================================
# CORRECT ENTRY POINT
# ============================================================

 if __name__ == "__main__":
    main()
