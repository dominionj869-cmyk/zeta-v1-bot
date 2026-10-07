import os
import csv
import time
import statistics
from datetime import datetime, timezone

import requests


# ============================================================
# ZETA MOMENTUM 10 — OTCHARTS FREE API TEST
# ============================================================
#
# IMPORTANT:
# - Read-only market data.
# - No broker login.
# - No password/session/cookie.
# - No automatic trading.
# - Designed for OTCharts free API allowance.
# - ONE candle request per run.
#
# ============================================================


# -----------------------------
# CONFIG
# -----------------------------

OTCHARTS_API_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BASE_URL = "https://otcharts.com"

# Current project target.
# Change these only if your OTCharts account is using another book.
VENUE = "quotex"
TARGET_SYMBOL = "EURUSD_otc"

TIMEFRAME_SECONDS = 60

# Free-tier friendly:
# One request gets the candle history we need.
CANDLE_LIMIT = 200

HTTP_TIMEOUT = 20

LOG_FILE = "momentum_signal_log.csv"


# -----------------------------
# MOMENTUM 10 STRATEGY
# -----------------------------

MOMENTUM_PERIOD = 10
MOMENTUM_LOOKBACK = 50

EXTREME_PERCENTILE = 0.10
MIN_TURN_DISTANCE = 0.20


# -----------------------------
# HTTP SESSION
# -----------------------------

session = requests.Session()

if OTCHARTS_API_KEY:
    session.headers.update(
        {
            "Authorization": f"Bearer {OTCHARTS_API_KEY}",
            "Accept": "application/json",
            "User-Agent": "ZETA-Momentum10/1.0",
        }
    )


# ============================================================
# HELPERS
# ============================================================


def utc_now():
    return datetime.now(timezone.utc)


def timestamp_text():
    return utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")


def api_get(path, params=None):
    url = BASE_URL + path

    response = session.get(
        url,
        params=params,
        timeout=HTTP_TIMEOUT,
    )

    if response.status_code != 200:
        body = response.text.strip()

        if len(body) > 500:
            body = body[:500]

        raise RuntimeError(
            f"OTCharts HTTP {response.status_code}: {body}"
        )

    try:
        return response.json()

    except Exception as exc:
        raise RuntimeError(
            "OTCharts returned invalid JSON: "
            + str(exc)
        )


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(
            "Telegram not configured. "
            "Signal will only be printed locally.",
            flush=True,
        )
        return

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=HTTP_TIMEOUT,
        )

        if response.status_code != 200:
            print(
                "Telegram error: "
                + response.text[:500],
                flush=True,
            )
            return

        print(
            "Telegram message sent.",
            flush=True,
        )

    except Exception as exc:
        print(
            "Telegram send failed: "
            + str(exc),
            flush=True,
        )


# ============================================================
# API CHECKS
# ============================================================


def check_api_key():
    print(
        "\nChecking OTCharts API key...",
        flush=True,
    )

    if not OTCHARTS_API_KEY:
        raise RuntimeError(
            "OTCHARTS_API_KEY is missing. "
            "Add your OTCharts key to GitHub Secrets."
        )

    print(
        "API key is present.",
        flush=True,
    )


def get_usage():
    """
    /v1/usage does NOT consume the request quota.
    """

    print(
        "\nChecking OTCharts usage...",
        flush=True,
    )

    data = api_get("/v1/usage")

    print(
        "OTCharts usage response:",
        flush=True,
    )

    print(
        data,
        flush=True,
    )

    requests_info = data.get("requests", {})

    used = requests_info.get("used")
    quota = requests_info.get("quota")
    remaining = requests_info.get("remaining")

    print(
        f"Requests: used={used}, "
        f"quota={quota}, "
        f"remaining={remaining}",
        flush=True,
    )

    return data


def get_symbols():
    """
    Discover the available instruments instead of blindly
    assuming the symbol exists.
    """

    print(
        f"\nChecking {VENUE} instrument catalogue...",
        flush=True,
    )

    data = api_get(
        "/v1/symbols",
        params={
            "venue": VENUE,
        },
    )

    print(
        "Symbol catalogue received.",
        flush=True,
    )

    return data


def extract_symbol_ids(data):
    ids = []

    if isinstance(data, list):
        items = data

    elif isinstance(data, dict):
        items = (
            data.get("symbols")
            or data.get("instruments")
            or data.get("data")
            or []
        )

    else:
        items = []

    for item in items:
        if isinstance(item, str):
            ids.append(item)

        elif isinstance(item, dict):
            symbol = (
                item.get("symbol")
                or item.get("id")
            )

            if symbol:
                ids.append(str(symbol))

    return ids


def verify_target_symbol():
    data = get_symbols()

    symbol_ids = extract_symbol_ids(data)

    print(
        f"Discovered {len(symbol_ids)} instruments.",
        flush=True,
    )

    if TARGET_SYMBOL in symbol_ids:
        print(
            f"Target symbol found: {TARGET_SYMBOL}",
            flush=True,
        )
        return True

    # Some API responses may not expose the catalogue
    # in the exact shape above. We still allow the candle
    # request to give the authoritative answer.
    print(
        f"Target symbol was not confirmed in the parsed "
        f"catalogue: {TARGET_SYMBOL}",
        flush=True,
    )

    print(
        "The candle endpoint will perform the final check.",
        flush=True,
    )

    return False


# ============================================================
# CANDLES
# ============================================================


def get_candles():
    """
    ONE historical candle request.

    We deliberately do NOT call this on a timer.
    """

    print(
        "\nDownloading one 1-minute candle batch...",
        flush=True,
    )

    print(
        f"Venue: {VENUE}",
        flush=True,
    )

    print(
        f"Symbol: {TARGET_SYMBOL}",
        flush=True,
    )

    print(
        f"Timeframe: {TIMEFRAME_SECONDS}s",
        flush=True,
    )

    print(
        f"Limit: {CANDLE_LIMIT}",
        flush=True,
    )

    data = api_get(
        "/v1/candles",
        params={
            "venue": VENUE,
            "symbol": TARGET_SYMBOL,
            "tf": TIMEFRAME_SECONDS,
            "limit": CANDLE_LIMIT,
        },
    )

    candles = data.get("candles")

    if not isinstance(candles, list):
        raise RuntimeError(
            "OTCharts candle response did not contain "
            "a valid 'candles' list."
        )

    print(
        f"Received {len(candles)} candles.",
        flush=True,
    )

    if len(candles) < MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 5:
        raise RuntimeError(
            "Not enough candles for Momentum 10. "
            f"Received {len(candles)}."
        )

    return candles


# ============================================================
# DATA NORMALIZATION
# ============================================================


def normalize_candles(raw_candles):
    result = []

    for candle in raw_candles:

        try:
            opened = float(candle["open"])
            high = float(candle["high"])
            low = float(candle["low"])
            close = float(candle["close"])
            candle_time = int(candle["time"])

        except Exception:
            continue

        result.append(
            {
                "open": opened,
                "high": high,
                "low": low,
                "close": close,
                "time": candle_time,
            }
        )

    result.sort(
        key=lambda x: x["time"]
    )

    return result


# ============================================================
# MOMENTUM
# ============================================================


def calculate_momentum_series(candles):
    closes = [
        candle["close"]
        for candle in candles
    ]

    momentum = []

    for index in range(len(closes)):

        if index < MOMENTUM_PERIOD:
            momentum.append(None)
            continue

        previous = closes[
            index - MOMENTUM_PERIOD
        ]

        current = closes[index]

        if previous == 0:
            momentum.append(None)
            continue

        value = (
            current / previous
        ) * 100.0

        momentum.append(value)

    return momentum


def percentile(values, percentile):
    if not values:
        return None

    values = sorted(values)

    if len(values) == 1:
        return values[0]

    position = (
        (len(values) - 1)
        * percentile
    )

    lower = int(position)
    upper = min(
        lower + 1,
        len(values) - 1,
    )

    fraction = position - lower

    return (
        values[lower]
        + (
            values[upper]
            - values[lower]
        )
        * fraction
    )


# ============================================================
# PRICE ACTION
# ============================================================


def bullish_rejection(candle):
    body = abs(
        candle["close"]
        - candle["open"]
    )

    lower_wick = (
        min(
            candle["open"],
            candle["close"],
        )
        - candle["low"]
    )

    upper_wick = (
        candle["high"]
        - max(
            candle["open"],
            candle["close"],
        )
    )

    if body <= 0:
        body = 0.00000001

    return (
        candle["close"]
        > candle["open"]
        and lower_wick >= body * 1.5
        and lower_wick > upper_wick
    )


def bearish_rejection(candle):
    body = abs(
        candle["close"]
        - candle["open"]
    )

    upper_wick = (
        candle["high"]
        - max(
            candle["open"],
            candle["close"],
        )
    )

    lower_wick = (
        min(
            candle["open"],
            candle["close"],
        )
        - candle["low"]
    )

    if body <= 0:
        body = 0.00000001

    return (
        candle["close"]
        < candle["open"]
        and upper_wick >= body * 1.5
        and upper_wick > lower_wick
    )


def bullish_engulfing(previous, current):
    return (
        previous["close"]
        < previous["open"]
        and current["close"]
        > current["open"]
        and current["open"]
        <= previous["close"]
        and current["close"]
        >= previous["open"]
    )


def bearish_engulfing(previous, current):
    return (
        previous["close"]
        > previous["open"]
        and current["close"]
        < current["open"]
        and current["open"]
        >= previous["close"]
        and current["close"]
        <= previous["open"]
    )


# ============================================================
# SIGNAL ENGINE
# ============================================================


def analyze_market(candles):
    candles = normalize_candles(candles)

    momentum = calculate_momentum_series(
        candles
    )

    latest_index = len(candles) - 1

    if latest_index < 2:
        return None

    current_momentum = momentum[
        latest_index
    ]

    previous_momentum = momentum[
        latest_index - 1
    ]

    if (
        current_momentum is None
        or previous_momentum is None
    ):
        return None

    start = max(
        0,
        latest_index - MOMENTUM_LOOKBACK,
    )

    historical_momentum = [
        value
        for value in momentum[start:latest_index]
        if value is not None
    ]

    if len(historical_momentum) < 20:
        return None

    low_extreme = percentile(
        historical_momentum,
        EXTREME_PERCENTILE,
    )

    high_extreme = percentile(
        historical_momentum,
        1.0 - EXTREME_PERCENTILE,
    )

    current_candle = candles[
        latest_index
    ]

    previous_candle = candles[
        latest_index - 1
    ]

    # ----------------------------------------
    # CALL
    # ----------------------------------------

    call_extreme = (
        previous_momentum
        <= low_extreme
    )

    call_turn = (
        current_momentum
        > previous_momentum
        and (
            current_momentum
            - previous_momentum
        )
        >= MIN_TURN_DISTANCE
    )

    call_price_action = (
        bullish_rejection(
            current_candle
        )
        or bullish_engulfing(
            previous_candle,
            current_candle,
        )
    )

    if (
        call_extreme
        and call_turn
        and call_price_action
    ):
        return {
            "direction": "CALL",
            "momentum": current_momentum,
            "previous_momentum": previous_momentum,
            "extreme": low_extreme,
            "candle": current_candle,
            "reason": (
                "Momentum 10 reached a "
                "lower extreme, turned upward, "
                "and received bullish price-action "
                "confirmation."
            ),
        }

    # ----------------------------------------
    # PUT
    # ----------------------------------------

    put_extreme = (
        previous_momentum
        >= high_extreme
    )

    put_turn = (
        current_momentum
        < previous_momentum
        and (
            previous_momentum
            - current_momentum
        )
        >= MIN_TURN_DISTANCE
    )

    put_price_action = (
        bearish_rejection(
            current_candle
        )
        or bearish_engulfing(
            previous_candle,
            current_candle,
        )
    )

    if (
        put_extreme
        and put_turn
        and put_price_action
    ):
        return {
            "direction": "PUT",
            "momentum": current_momentum,
            "previous_momentum": previous_momentum,
            "extreme": high_extreme,
            "candle": current_candle,
            "reason": (
                "Momentum 10 reached a "
                "higher extreme, turned downward, "
                "and received bearish price-action "
                "confirmation."
            ),
        }

    return None


# ============================================================
# LOGGING
# ============================================================


def write_log(signal):
    file_exists = os.path.exists(
        LOG_FILE
    )

    with open(
        LOG_FILE,
        "a",
        newline="",
        encoding="utf-8",
    ) as file:

        writer = csv.writer(file)

        if not file_exists:
            writer.writerow(
                [
                    "timestamp",
                    "signal_id",
                    "venue",
                    "symbol",
                    "direction",
                    "price",
                    "momentum",
                    "previous_momentum",
                    "extreme",
                    "reason",
                ]
            )

        writer.writerow(
            [
                signal["timestamp"],
                signal["signal_id"],
                signal["venue"],
                signal["symbol"],
                signal["direction"],
                signal["price"],
                signal["momentum"],
                signal["previous_momentum"],
                signal["extreme"],
                signal["reason"],
            ]
        )


# ============================================================
# SIGNAL MESSAGE
# ============================================================


def create_signal(signal):
    unix_time = int(
        time.time()
    )

    signal_id = (
        "M10-"
        + TARGET_SYMBOL
        + "-"
        + signal["direction"]
        + "-"
        + str(unix_time)
    )

    signal["signal_id"] = signal_id
    signal["timestamp"] = timestamp_text()
    signal["venue"] = VENUE
    signal["symbol"] = TARGET_SYMBOL

    candle = signal["candle"]

    signal["price"] = candle["close"]

    return signal


def send_signal(signal):
    message = (
        "🚨 ZETA MOMENTUM 10 SIGNAL\n\n"
        f"📊 Asset: {signal['symbol']}\n"
        f"🏦 Venue: {signal['venue']}\n"
        f"🎯 Direction: {signal['direction']}\n"
        f"💰 Price: {signal['price']}\n"
        f"📈 Momentum: {signal['momentum']:.4f}\n"
        f"📉 Previous: {signal['previous_momentum']:.4f}\n"
        f"⚠️ Extreme: {signal['extreme']:.4f}\n\n"
        f"🆔 Signal ID: {signal['signal_id']}\n"
        f"⏱️ Generated: {signal['timestamp']}\n\n"
        "🤖 Automatic trading: OFF\n\n"
        f"Reason: {signal['reason']}"
    )

    print(
        "\n" + message + "\n",
        flush=True,
    )

    send_telegram(message)


# ============================================================
# MAIN
# ============================================================


def main():

    print(
        "\n"
        "============================================\n"
        "🚀 ZETA MOMENTUM 10\n"
        "OTCHARTS FREE API TEST\n"
        "============================================\n"
        f"Venue: {VENUE}\n"
        f"Asset: {TARGET_SYMBOL}\n"
        "Timeframe: 1M\n"
        "Strategy: Momentum 10 Extreme-Reversal\n"
        "Automatic trading: OFF\n"
        "============================================\n",
        flush=True,
    )

    # ----------------------------------------
    # 1. Check key
    # ----------------------------------------

    check_api_key()

    # ----------------------------------------
    # 2. Usage check
    # This does NOT consume quota.
    # ----------------------------------------

    usage = get_usage()

    # ----------------------------------------
    # 3. Discover symbol
    # ----------------------------------------

    verify_target_symbol()

    # ----------------------------------------
    # 4. Get candles
    #
    # This is the main quota-consuming request.
    # We call it ONLY ONCE.
    # ----------------------------------------

    candles = get_candles()

    # ----------------------------------------
    # 5. Analyze
    # ----------------------------------------

    print(
        "\nAnalyzing Momentum 10 setup...",
        flush=True,
    )

    signal = analyze_market(
        candles
    )

    # ----------------------------------------
    # 6. Result
    # ----------------------------------------

    if signal is None:

        print(
            "\n"
            "🟡 NO TRADE\n"
            "The latest completed candle did not "
            "meet all Momentum 10 conditions.\n",
            flush=True,
        )

        send_telegram(
            "🟡 ZETA Momentum 10\n\n"
            "NO TRADE\n\n"
            f"Asset: {TARGET_SYMBOL}\n"
            f"Venue: {VENUE}\n"
            "Reason: No valid extreme-reversal "
            "setup on the downloaded candle batch.\n\n"
            "Automatic trading: OFF"
        )

    else:

        signal = create_signal(
            signal
        )

        write_log(
            signal
        )

        send_signal(
            signal
        )

    # ----------------------------------------
    # 7. Final usage display
    # ----------------------------------------

    print(
        "\nChecking final API usage...",
        flush=True,
    )

    final_usage = get_usage()

    print(
        "\n============================================",
        flush=True,
    )

    print(
        "✅ MOMENTUM 10 RUN COMPLETE",
        flush=True,
    )

    print(
        "One candle request was used for analysis.",
        flush=True,
    )

    print(
        "No live stream was opened.",
        flush=True,
    )

    print(
        "No broker credentials were used.",
        flush=True,
    )

    print(
        "No automatic trade was placed.",
        flush=True,
    )

    print(
        "============================================\n",
        flush=True,
    )


# ============================================================
# ENTRY POINT
# ============================================================


if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:

        print(
            "\nBot stopped.",
            flush=True,
        )

    except Exception as exc:

        print(
            "\n❌ FATAL ERROR",
            flush=True,
        )

        print(
            str(exc),
            flush=True,
        )

        raise
