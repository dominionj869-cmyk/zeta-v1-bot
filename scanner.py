import os
import csv
import time
from datetime import datetime, timezone

import requests


# ============================================================
# ZETA MOMENTUM 10 — OTCHARTS FREE API
# ============================================================

OTCHARTS_API_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BASE_URL = "https://otcharts.com"

# IMPORTANT: Free plan opens the OTC book.
VENUE = "otc"

TARGET_SYMBOL = "EURUSD_otc"

TIMEFRAME_SECONDS = 60
CANDLE_LIMIT = 200

MOMENTUM_PERIOD = 10
MOMENTUM_LOOKBACK = 50
EXTREME_PERCENTILE = 0.10
MIN_TURN_DISTANCE = 0.20

HTTP_TIMEOUT = 20
LOG_FILE = "momentum_signal_log.csv"

session = requests.Session()

if OTCHARTS_API_KEY:
    session.headers.update({
        "Authorization": f"Bearer {OTCHARTS_API_KEY}",
        "Accept": "application/json",
        "User-Agent": "ZETA-Momentum10/1.0",
    })


# ============================================================
# HELPERS
# ============================================================

def now_text():
    return datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def api_get(path, params=None):
    response = session.get(
        BASE_URL + path,
        params=params,
        timeout=HTTP_TIMEOUT,
    )

    if response.status_code != 200:
        body = response.text[:500]
        raise RuntimeError(
            f"OTCharts HTTP {response.status_code}: {body}"
        )

    return response.json()


def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured.")
        return

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )

    try:
        r = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            timeout=HTTP_TIMEOUT,
        )

        if r.status_code != 200:
            print(
                "Telegram error:",
                r.text[:300],
                flush=True,
            )

    except Exception as exc:
        print(
            "Telegram error:",
            str(exc),
            flush=True,
        )


# ============================================================
# USAGE
# ============================================================

def check_usage():
    data = api_get("/v1/usage")

    print("OTCharts usage:")
    print(data, flush=True)

    requests_data = data.get("requests", {})

    used = requests_data.get("used")
    quota = requests_data.get("quota")
    remaining = requests_data.get("remaining")

    print(
        f"Requests: {used}/{quota} "
        f"({remaining} remaining)",
        flush=True,
    )

    if remaining is not None and remaining <= 0:
        raise RuntimeError(
            "OTCharts API request allowance is exhausted."
        )

    return data


# ============================================================
# SYMBOL DISCOVERY
# ============================================================

def get_otc_symbols():
    print(
        "\nChecking OTC instruments...",
        flush=True,
    )

    data = api_get(
        "/v1/symbols",
        params={
            "venue": VENUE,
        },
    )

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

    symbols = []

    for item in items:
        if isinstance(item, str):
            symbols.append(item)

        elif isinstance(item, dict):
            symbol = (
                item.get("symbol")
                or item.get("id")
                or item.get("name")
            )

            if symbol:
                symbols.append(str(symbol))

    print(
        f"Found {len(symbols)} OTC instruments.",
        flush=True,
    )

    return symbols


def find_eurusd(symbols):
    if TARGET_SYMBOL in symbols:
        return TARGET_SYMBOL

    candidates = []

    for symbol in symbols:
        clean = symbol.lower().replace("/", "").replace("-", "")

        if "eurusd" in clean:
            candidates.append(symbol)

    if candidates:
        print(
            "EUR/USD OTC candidates:",
            candidates,
            flush=True,
        )

        return candidates[0]

    return None


# ============================================================
# CANDLES
# ============================================================

def get_candles(symbol):
    print(
        f"\nDownloading candles for {symbol}...",
        flush=True,
    )

    data = api_get(
        "/v1/candles",
        params={
            "venue": VENUE,
            "symbol": symbol,
            "tf": TIMEFRAME_SECONDS,
            "limit": CANDLE_LIMIT,
        },
    )

    candles = data.get("candles")

    if not isinstance(candles, list):
        raise RuntimeError(
            "OTCharts returned no valid candle list."
        )

    print(
        f"Received {len(candles)} candles.",
        flush=True,
    )

    return candles


def normalize(candles):
    result = []

    for c in candles:
        try:
            result.append({
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "time": int(c["time"]),
            })
        except Exception:
            continue

    result.sort(key=lambda x: x["time"])

    return result


# ============================================================
# MOMENTUM
# ============================================================

def momentum_series(candles):
    closes = [c["close"] for c in candles]

    result = []

    for i in range(len(closes)):
        if i < MOMENTUM_PERIOD:
            result.append(None)
            continue

        previous = closes[i - MOMENTUM_PERIOD]

        if previous == 0:
            result.append(None)
            continue

        result.append(
            (closes[i] / previous) * 100.0
        )

    return result


def percentile(values, p):
    values = sorted(values)

    if not values:
        return None

    if len(values) == 1:
        return values[0]

    position = (len(values) - 1) * p

    lower = int(position)
    upper = min(
        lower + 1,
        len(values) - 1,
    )

    fraction = position - lower

    return (
        values[lower]
        + (
            values[upper] - values[lower]
        ) * fraction
    )


# ============================================================
# PRICE ACTION
# ============================================================

def bullish_rejection(c):
    body = abs(c["close"] - c["open"])

    if body == 0:
        body = 0.00000001

    lower = (
        min(c["open"], c["close"])
        - c["low"]
    )

    upper = (
        c["high"]
        - max(c["open"], c["close"])
    )

    return (
        c["close"] > c["open"]
        and lower >= body * 1.5
        and lower > upper
    )


def bearish_rejection(c):
    body = abs(c["close"] - c["open"])

    if body == 0:
        body = 0.00000001

    upper = (
        c["high"]
        - max(c["open"], c["close"])
    )

    lower = (
        min(c["open"], c["close"])
        - c["low"]
    )

    return (
        c["close"] < c["open"]
        and upper >= body * 1.5
        and upper > lower
    )


def bullish_engulfing(previous, current):
    return (
        previous["close"] < previous["open"]
        and current["close"] > current["open"]
        and current["open"] <= previous["close"]
        and current["close"] >= previous["open"]
    )


def bearish_engulfing(previous, current):
    return (
        previous["close"] > previous["open"]
        and current["close"] < current["open"]
        and current["open"] >= previous["close"]
        and current["close"] <= previous["open"]
    )


# ============================================================
# STRATEGY
# ============================================================

def analyze(candles):
    candles = normalize(candles)

    if len(candles) < 70:
        raise RuntimeError(
            f"Only {len(candles)} valid candles received."
        )

    momentum = momentum_series(candles)

    i = len(candles) - 1

    current = momentum[i]
    previous = momentum[i - 1]

    if current is None or previous is None:
        return None

    start = max(
        0,
        i - MOMENTUM_LOOKBACK,
    )

    history = [
        x
        for x in momentum[start:i]
        if x is not None
    ]

    if len(history) < 20:
        return None

    low = percentile(
        history,
        EXTREME_PERCENTILE,
    )

    high = percentile(
        history,
        1.0 - EXTREME_PERCENTILE,
    )

    current_candle = candles[i]
    previous_candle = candles[i - 1]

    # CALL
    if (
        previous <= low
        and current > previous
        and current - previous >= MIN_TURN_DISTANCE
        and (
            bullish_rejection(current_candle)
            or bullish_engulfing(
                previous_candle,
                current_candle,
            )
        )
    ):
        return {
            "direction": "CALL",
            "momentum": current,
            "previous": previous,
            "extreme": low,
            "price": current_candle["close"],
            "reason": "Lower momentum extreme + bullish turn + bullish confirmation.",
        }

    # PUT
    if (
        previous >= high
        and current < previous
        and previous - current >= MIN_TURN_DISTANCE
        and (
            bearish_rejection(current_candle)
            or bearish_engulfing(
                previous_candle,
                current_candle,
            )
        )
    ):
        return {
            "direction": "PUT",
            "momentum": current,
            "previous": previous,
            "extreme": high,
            "price": current_candle["close"],
            "reason": "Upper momentum extreme + bearish turn + bearish confirmation.",
        }

    return None


# ============================================================
# LOG
# ============================================================

def save_signal(signal, symbol):
    exists = os.path.exists(LOG_FILE)

    with open(
        LOG_FILE,
        "a",
        newline="",
        encoding="utf-8",
    ) as f:

        writer = csv.writer(f)

        if not exists:
            writer.writerow([
                "timestamp",
                "signal_id",
                "venue",
                "symbol",
                "direction",
                "price",
                "momentum",
                "previous",
                "extreme",
                "reason",
            ])

        writer.writerow([
            signal["timestamp"],
            signal["signal_id"],
            VENUE,
            symbol,
            signal["direction"],
            signal["price"],
            signal["momentum"],
            signal["previous"],
            signal["extreme"],
            signal["reason"],
        ])


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "\n"
        "============================================\n"
        "🚀 ZETA MOMENTUM 10\n"
        "OTCHARTS FREE API\n"
        "============================================\n"
        f"Book: {VENUE}\n"
        f"Target: {TARGET_SYMBOL}\n"
        "Timeframe: 1M\n"
        "Automatic trading: OFF\n"
        "============================================\n",
        flush=True,
    )

    if not OTCHARTS_API_KEY:
        raise RuntimeError(
            "OTCHARTS_API_KEY is missing."
        )

    print(
        "\nAPI key is present.",
        flush=True,
    )

    # Usage endpoint does not consume quota.
    check_usage()

    # Discover OTC symbols.
    symbols = get_otc_symbols()

    symbol = find_eurusd(symbols)

    if not symbol:
        print(
            "\nAvailable symbols:",
            symbols[:100],
            flush=True,
        )

        raise RuntimeError(
            "EUR/USD OTC symbol was not found "
            "in the OTC book."
        )

    print(
        f"\n✅ Using symbol: {symbol}",
        flush=True,
    )

    # One candle request only.
    candles = get_candles(symbol)

    signal = analyze(candles)

    if signal is None:

        message = (
            "🟡 ZETA MOMENTUM 10\n\n"
            "NO TRADE\n\n"
            f"Asset: {symbol}\n"
            f"Book: {VENUE}\n"
            "Reason: No valid Momentum 10 "
            "extreme-reversal setup.\n\n"
            "Automatic trading: OFF"
        )

        print(
            "\n" + message,
            flush=True,
        )

        telegram(message)

    else:

        signal_id = (
            "M10-"
            + symbol.replace("/", "")
            + "-"
            + signal["direction"]
            + "-"
            + str(int(time.time()))
        )

        signal["signal_id"] = signal_id
        signal["timestamp"] = now_text()

        save_signal(
            signal,
            symbol,
        )

        message = (
            "🚨 ZETA MOMENTUM 10 SIGNAL\n\n"
            f"📊 Asset: {symbol}\n"
            f"🏦 Book: {VENUE}\n"
            f"🎯 Direction: {signal['direction']}\n"
            f"💰 Price: {signal['price']}\n"
            f"📈 Momentum: {signal['momentum']:.4f}\n"
            f"📉 Previous: {signal['previous']:.4f}\n"
            f"⚠️ Extreme: {signal['extreme']:.4f}\n\n"
            f"🆔 Signal ID: {signal_id}\n"
            f"⏱️ {signal['timestamp']}\n\n"
            "🤖 Automatic trading: OFF\n\n"
            f"Reason: {signal['reason']}"
        )

        print(
            "\n" + message,
            flush=True,
        )

        telegram(message)

    print(
        "\n✅ RUN COMPLETE",
        flush=True,
    )

    print(
        "No live stream opened.",
        flush=True,
    )

    print(
        "No automatic trade placed.",
        flush=True,
    )


if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:
        print(
            "Bot stopped.",
            flush=True,
        )

    except Exception as exc:
        print(
            f"❌ FATAL ERROR: {exc}",
            flush=True,
        )
        raise
