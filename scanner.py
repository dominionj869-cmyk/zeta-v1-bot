import os
import csv
import json
import time
import threading
from datetime import datetime, timezone

import requests


# ============================================================
# MOMENTUM 10 EXTREME-REVERSAL
# OTCHARTS -> QUOTEX MARKET DATA
# TELEGRAM SIGNAL SCANNER
#
# IMPORTANT:
# - No Quotex email/password
# - No Quotex session/SSID
# - No automatic Quotex trading
# - Market data comes from OTCharts
# ============================================================


# ============================================================
# STRATEGY SETTINGS
# ============================================================

MOMENTUM_PERIOD = 10
MOMENTUM_LOOKBACK = 50
EXTREME_PERCENTILE = 0.10

REQUIRE_TURN = True
MIN_TURN_DISTANCE = 0.20

CANDLE_SECONDS = 60
EXPIRY_MINUTES = 1

# DATA SCANNER ONLY
AUTO_TRADE = False

STAKE = 1.0
TARGET_TRADES = 50

# How often the local scanner checks whether a completed
# candle is ready for analysis.
SCAN_INTERVAL = 2

HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 15

# ============================================================
# QUOTEX SYMBOL
#
# OTCharts uses EURUSD_otc for Quotex.
# ============================================================

TARGET_SYMBOL = "EURUSD_otc"
VENUE = "quotex"

LOG_FILE = "momentum_signal_log.csv"

HTTP_TIMEOUT = 20
STREAM_TIMEOUT = 60

# Number of completed candles retained locally.
MAX_LOCAL_CANDLES = 300


# ============================================================
# SECRETS
# ============================================================

OTCHARTS_API_KEY = os.getenv(
    "OTCHARTS_API_KEY",
    "",
)

TELEGRAM_TOKEN = os.getenv(
    "TELEGRAM_TOKEN",
    "",
)

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    "",
)


# ============================================================
# GLOBAL STATE
# ============================================================

session = requests.Session()

session.headers.update({
    "Authorization": (
        "Bearer "
        + OTCHARTS_API_KEY
    ),
    "User-Agent": (
        "Momentum10-OTCharts-Scanner/1.0"
    ),
})

trading_assets = []

candles_by_asset = {}

current_candles = {}

last_signal_candle = {}

extreme_state = {}

trade_count = 0
wins = 0
losses = 0
pending_results = 0

last_stream_tick = 0
last_heartbeat = 0
last_signal_time = 0

stop_event = threading.Event()

stream_thread = None


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if (
        not TELEGRAM_TOKEN
        or not TELEGRAM_CHAT_ID
    ):
        print(
            "Telegram credentials not configured.",
            flush=True,
        )
        return

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )

    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }

    try:

        response = requests.post(
            url,
            data=data,
            timeout=15,
        )

        if not response.ok:

            print(
                "Telegram HTTP error: "
                + str(response.status_code),
                flush=True,
            )

    except Exception as exc:

        print(
            "Telegram error: "
            + str(exc),
            flush=True,
        )


# ============================================================
# CSV LOG
# ============================================================

def init_log():

    if os.path.exists(LOG_FILE):
        return

    try:

        with open(
            LOG_FILE,
            "w",
            newline="",
            encoding="utf-8",
        ) as file:

            writer = csv.writer(file)

            writer.writerow([
                "time",
                "asset",
                "direction",
                "signal_id",
                "entry_price",
                "result",
                "profit",
            ])

    except Exception as exc:

        print(
            "Log init error: "
            + str(exc),
            flush=True,
        )


def log_trade(
    asset,
    direction,
    signal_id,
    entry_price,
    result,
    profit,
):

    try:

        with open(
            LOG_FILE,
            "a",
            newline="",
            encoding="utf-8",
        ) as file:

            writer = csv.writer(file)

            writer.writerow([
                datetime.now(
                    timezone.utc
                ).isoformat(),
                asset,
                direction,
                signal_id,
                entry_price,
                result,
                profit,
            ])

    except Exception as exc:

        print(
            "Log error: "
            + str(exc),
            flush=True,
        )


# ============================================================
# OTCHARTS API
# ============================================================

BASE_URL = "https://otcharts.com"


def api_get(
    path,
    params=None,
):

    if not OTCHARTS_API_KEY:

        raise RuntimeError(
            "OTCHARTS_API_KEY is missing."
        )

    url = (
        BASE_URL
        + path
    )

    response = session.get(
        url,
        params=params,
        timeout=HTTP_TIMEOUT,
    )

    if not response.ok:

        try:
            error_data = response.json()
        except Exception:
            error_data = {}

        error_message = error_data.get(
            "error",
            response.text[:300],
        )

        raise RuntimeError(
            "OTCharts HTTP "
            + str(response.status_code)
            + ": "
            + str(error_message)
        )

    return response.json()


# ============================================================
# CHECK API ACCESS
# ============================================================

def check_otcharts():

    print(
        "Checking OTCharts API...",
        flush=True,
    )

    try:

        data = api_get(
            "/v1/venues"
        )

        venues = data.get(
            "venues",
            [],
        )

        quotex_open = False

        for venue in venues:

            if (
                venue.get("id")
                == VENUE
            ):

                quotex_open = bool(
                    venue.get("open")
                )

                print(
                    "Quotex book open: "
                    + str(
                        quotex_open
                    ),
                    flush=True,
                )

                if not quotex_open:

                    print(
                        "❌ Your OTCharts key "
                        "does not currently open "
                        "the Quotex book.",
                        flush=True,
                    )

                    return False

                break

        if not quotex_open:

            print(
                "❌ Quotex venue unavailable "
                "for this API key.",
                flush=True,
            )

            return False

        return True

    except Exception as exc:

        print(
            "OTCharts API check failed:",
            str(exc),
            flush=True,
        )

        return False


# ============================================================
# USAGE
# ============================================================

def get_usage():

    try:

        data = api_get(
            "/v1/usage"
        )

        requests_data = data.get(
            "requests",
            {},
        )

        remaining = requests_data.get(
            "remaining"
        )

        quota = requests_data.get(
            "quota"
        )

        used = requests_data.get(
            "used"
        )

        print(
            "OTCharts usage: "
            + str(used)
            + "/"
            + str(quota)
            + " | remaining="
            + str(remaining),
            flush=True,
        )

        return data

    except Exception as exc:

        print(
            "Usage check warning:",
            str(exc),
            flush=True,
        )

        return None


# ============================================================
# SYMBOL DISCOVERY
# ============================================================

def get_symbols():

    global trading_assets

    print(
        "Discovering Quotex symbols...",
        flush=True,
    )

    try:

        data = api_get(
            "/v1/symbols",
            params={
                "venue": VENUE,
            },
        )

        symbols = data.get(
            "symbols",
            [],
        )

        found = []

        for item in symbols:

            if not isinstance(
                item,
                dict,
            ):
                continue

            symbol = item.get(
                "symbol"
            )

            if not symbol:
                continue

            if symbol == TARGET_SYMBOL:

                found.append(
                    TARGET_SYMBOL
                )

                print(
                    "✅ Found "
                    + TARGET_SYMBOL
                    + " | "
                    + str(
                        item.get(
                            "name",
                            "",
                        )
                    ),
                    flush=True,
                )

                if (
                    "payout"
                    in item
                ):

                    print(
                        "Payout: "
                        + str(
                            item.get(
                                "payout"
                            )
                        ),
                        flush=True,
                    )

                break

        if not found:

            print(
                "❌ "
                + TARGET_SYMBOL
                + " is not currently "
                "in the Quotex catalogue.",
                flush=True,
            )

            trading_assets = []

            return False

        trading_assets = found

        return True

    except Exception as exc:

        print(
            "Symbol discovery error:",
            str(exc),
            flush=True,
        )

        return False


# ============================================================
# INITIAL HISTORY
#
# This is called ONCE at startup.
#
# OTCharts explicitly recommends not polling candles
# repeatedly on a timer.
# ============================================================

def load_initial_candles(
    asset,
):

    print(
        "Loading initial candles for "
        + asset
        + "...",
        flush=True,
    )

    try:

        data = api_get(
            "/v1/candles",
            params={
                "venue": VENUE,
                "symbol": asset,
                "tf": CANDLE_SECONDS,
                "limit": MAX_LOCAL_CANDLES,
            },
        )

        raw_candles = data.get(
            "candles",
            [],
        )

        gaps = data.get(
            "gaps",
            {},
        )

        if (
            gaps.get("runs", 0)
            > 0
        ):

            print(
                "⚠️ Candle gaps detected: "
                + str(
                    gaps
                ),
                flush=True,
            )

        cleaned = []

        for candle in raw_candles:

            if not isinstance(
                candle,
                dict,
            ):
                continue

            try:

                cleaned.append({
                    "from": float(
                        candle[
                            "time"
                        ]
                    ),
                    "open": float(
                        candle[
                            "open"
                        ]
                    ),
                    "high": float(
                        candle[
                            "high"
                        ]
                    ),
                    "low": float(
                        candle[
                            "low"
                        ]
                    ),
                    "close": float(
                        candle[
                            "close"
                        ]
                    ),
                    "volume": float(
                        candle.get(
                            "volume",
                            0,
                        )
                    ),
                })

            except Exception:
                continue

        cleaned.sort(
            key=lambda x: x["from"]
        )

        if len(cleaned) < 60:

            print(
                "❌ Not enough historical candles: "
                + str(
                    len(cleaned)
                ),
                flush=True,
            )

            return False

        candles_by_asset[
            asset
        ] = cleaned[
            -MAX_LOCAL_CANDLES:
        ]

        print(
            "🟢 Loaded "
            + str(
                len(
                    candles_by_asset[
                        asset
                    ]
                )
            )
            + " candles.",
            flush=True,
        )

        return True

    except Exception as exc:

        print(
            "Initial candle error:",
            str(exc),
            flush=True,
        )

        return False


# ============================================================
# CANDLE BUILDER
#
# Converts OTCharts live ticks into local 1-minute candles.
# ============================================================

def process_tick(
    symbol,
    price,
    tick_time,
):

    global last_stream_tick

    try:

        price = float(price)
        tick_time = float(
            tick_time
        )

    except Exception:

        return

    last_stream_tick = time.time()

    candle_start = (
        int(tick_time)
        // CANDLE_SECONDS
    ) * CANDLE_SECONDS

    current = current_candles.get(
        symbol
    )

    # --------------------------------------------------------
    # FIRST TICK
    # --------------------------------------------------------

    if current is None:

        current_candles[
            symbol
        ] = {
            "from": candle_start,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 0.0,
        }

        return

    # --------------------------------------------------------
    # SAME CANDLE
    # --------------------------------------------------------

    if (
        current["from"]
        == candle_start
    ):

        if price > current["high"]:
            current["high"] = price

        if price < current["low"]:
            current["low"] = price

        current["close"] = price

        return

    # --------------------------------------------------------
    # NEW CANDLE
    # --------------------------------------------------------

    if candle_start > current["from"]:

        completed = dict(
            current
        )

        candles = candles_by_asset.setdefault(
            symbol,
            [],
        )

        # Avoid duplicate candle insertion.
        if not candles or (
            candles[-1]["from"]
            < completed["from"]
        ):

            candles.append(
                completed
            )

            if len(candles) > (
                MAX_LOCAL_CANDLES
            ):

                del candles[
                    :-MAX_LOCAL_CANDLES
                ]

            print(
                "🕯️ CLOSED 1M CANDLE "
                + symbol
                + " "
                + str(
                    completed["close"]
                ),
                flush=True,
            )

            process_completed_candle(
                symbol
            )

        # Start the new candle.
        current_candles[
            symbol
        ] = {
            "from": candle_start,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 0.0,
        }


# ============================================================
# STREAM PROCESSOR
# ============================================================

def stream_symbol(
    symbol,
):

    global last_stream_tick

    url = (
        BASE_URL
        + "/v1/stream"
    )

    params = {
        "venue": VENUE,
        "symbol": symbol,
    }

    print(
        "Opening OTCharts live stream for "
        + symbol
        + "...",
        flush=True,
    )

    try:

        with session.get(
            url,
            params=params,
            stream=True,
            timeout=(
                20,
                STREAM_TIMEOUT,
            ),
        ) as response:

            if not response.ok:

                try:
                    error_data = (
                        response.json()
                    )
                except Exception:
                    error_data = {}

                print(
                    "❌ Stream HTTP "
                    + str(
                        response.status_code
                    )
                    + ": "
                    + str(
                        error_data.get(
                            "error",
                            response.text[
                                :300
                            ],
                        )
                    ),
                    flush=True,
                )

                return False

            print(
                "🟢 OTCharts stream connected.",
                flush=True,
            )

            event_type = None

            for raw_line in response.iter_lines(
                decode_unicode=True
            ):

                if stop_event.is_set():
                    return True

                if raw_line is None:
                    continue

                line = raw_line.strip()

                if not line:
                    continue

                # SSE event name.
                if line.startswith(
                    "event:"
                ):

                    event_type = (
                        line[
                            6:
                        ].strip()
                    )

                    continue

                if not line.startswith(
                    "data:"
                ):

                    continue

                raw_data = (
                    line[
                        5:
                    ].strip()
                )

                try:

                    payload = json.loads(
                        raw_data
                    )

                except Exception:

                    continue

                # ------------------------------------------------
                # CONNECTED
                # ------------------------------------------------

                if event_type == "connected":

                    print(
                        "Stream subscription: "
                        + str(
                            payload
                        ),
                        flush=True,
                    )

                # ------------------------------------------------
                # DROPPED
                # ------------------------------------------------

                elif event_type == "dropped":

                    print(
                        "⚠️ Stream dropped: "
                        + str(
                            payload
                        ),
                        flush=True,
                    )

                # ------------------------------------------------
                # TICK
                # ------------------------------------------------

                elif event_type == "tick":

                    tick_symbol = payload.get(
                        "symbol"
                    )

                    price = payload.get(
                        "price"
                    )

                    tick_time = payload.get(
                        "time"
                    )

                    if (
                        tick_symbol
                        and price is not None
                        and tick_time is not None
                    ):

                        process_tick(
                            tick_symbol,
                            price,
                            tick_time,
                        )

                event_type = None

        return False

    except requests.exceptions.ReadTimeout:

        print(
            "Stream read timeout. Reconnecting...",
            flush=True,
        )

        return False

    except requests.exceptions.ConnectionError as exc:

        print(
            "Stream connection error: "
            + str(exc),
            flush=True,
        )

        return False

    except Exception as exc:

        print(
            "Stream error: "
            + str(exc),
            flush=True,
        )

        return False


# ============================================================
# STREAM LOOP
# ============================================================

def stream_loop():

    global stream_thread

    while not stop_event.is_set():

        if not trading_assets:

            time.sleep(
                RECONNECT_SECONDS
            )

            continue

        symbol = trading_assets[0]

        connected = stream_symbol(
            symbol
        )

        if stop_event.is_set():
            break

        if not connected:

            print(
                "🔄 Reconnecting OTCharts "
                "stream in "
                + str(
                    RECONNECT_SECONDS
                )
                + " seconds...",
                flush=True,
            )

            time.sleep(
                RECONNECT_SECONDS
            )


# ============================================================
# MOMENTUM 10
# ============================================================

def calculate_momentum(
    candles,
):

    values = []

    if len(candles) <= MOMENTUM_PERIOD:
        return values

    for index in range(
        MOMENTUM_PERIOD,
        len(candles),
    ):

        current = candles[
            index
        ]["close"]

        previous = candles[
            index - MOMENTUM_PERIOD
        ]["close"]

        if previous == 0:
            continue

        momentum = (
            current
            / previous
        ) * 100.0

        values.append(
            momentum
        )

    return values


def analyze_momentum(
    candles,
):

    if len(candles) < (
        MOMENTUM_LOOKBACK + 3
    ):
        return None

    momentum_values = (
        calculate_momentum(
            candles
        )
    )

    if len(momentum_values) < (
        MOMENTUM_LOOKBACK + 3
    ):
        return None

    recent = momentum_values[
        -MOMENTUM_LOOKBACK:
    ]

    current = momentum_values[
        -1
    ]

    previous = momentum_values[
        -2
    ]

    previous_previous = (
        momentum_values[-3]
    )

    sorted_values = sorted(
        recent
    )

    low_index = int(
        len(sorted_values)
        * EXTREME_PERCENTILE
    )

    high_index = int(
        len(sorted_values)
        * (
            1.0
            - EXTREME_PERCENTILE
        )
    )

    if low_index >= len(
        sorted_values
    ):

        low_index = (
            len(sorted_values)
            - 1
        )

    if high_index >= len(
        sorted_values
    ):

        high_index = (
            len(sorted_values)
            - 1
        )

    low_threshold = (
        sorted_values[
            low_index
        ]
    )

    high_threshold = (
        sorted_values[
            high_index
        ]
    )

    recent_low = min(
        recent
    )

    recent_high = max(
        recent
    )

    signal = None
    extreme = None
    reversal_strength = 0.0

    # --------------------------------------------------------
    # LOW EXTREME -> CALL
    # --------------------------------------------------------

    if current <= low_threshold:

        extreme = "LOW"

        if (
            current > previous
            and previous
            < previous_previous
        ):

            turn_distance = (
                current
                - previous
            )

            if (
                turn_distance
                >= MIN_TURN_DISTANCE
            ):

                signal = "CALL"

                if recent_low != 0:

                    reversal_strength = (
                        turn_distance
                        / abs(
                            recent_low
                        )
                    ) * 100.0

    # --------------------------------------------------------
    # HIGH EXTREME -> PUT
    # --------------------------------------------------------

    elif current >= high_threshold:

        extreme = "HIGH"

        if (
            current < previous
            and previous
            > previous_previous
        ):

            turn_distance = (
                previous
                - current
            )

            if (
                turn_distance
                >= MIN_TURN_DISTANCE
            ):

                signal = "PUT"

                if recent_high != 0:

                    reversal_strength = (
                        turn_distance
                        / abs(
                            recent_high
                        )
                    ) * 100.0

    return {
        "signal": signal,
        "extreme": extreme,
        "momentum": current,
        "previous": previous,
        "previous_previous":
            previous_previous,
        "recent_low": recent_low,
        "recent_high": recent_high,
        "low_threshold":
            low_threshold,
        "high_threshold":
            high_threshold,
        "reversal_strength":
            reversal_strength,
    }


# ============================================================
# PRICE ACTION REVERSAL CONFIRMATION
# ============================================================

def price_action_confirmation(
    candles,
    direction,
):

    if len(candles) < 2:

        return (
            False,
            "NOT_ENOUGH_CANDLES",
        )

    current = candles[-1]
    previous = candles[-2]

    current_open = current[
        "open"
    ]

    current_close = current[
        "close"
    ]

    current_high = current[
        "high"
    ]

    current_low = current[
        "low"
    ]

    previous_open = previous[
        "open"
    ]

    previous_close = previous[
        "close"
    ]

    body = abs(
        current_close
        - current_open
    )

    upper_wick = (
        current_high
        - max(
            current_open,
            current_close,
        )
    )

    lower_wick = (
        min(
            current_open,
            current_close,
        )
        - current_low
    )

    candle_range = (
        current_high
        - current_low
    )

    if candle_range <= 0:

        return (
            False,
            "ZERO_RANGE",
        )

    # --------------------------------------------------------
    # CALL
    # --------------------------------------------------------

    if direction == "CALL":

        bullish_candle = (
            current_close
            > current_open
        )

        bullish_rejection = (
            bullish_candle
            and lower_wick >= body
            and lower_wick
            >= candle_range * 0.25
        )

        bullish_engulfing = (
            bullish_candle
            and previous_close
            < previous_open
            and current_open
            <= previous_close
            and current_close
            >= previous_open
        )

        if bullish_rejection:

            return (
                True,
                "BULLISH_REJECTION",
            )

        if bullish_engulfing:

            return (
                True,
                "BULLISH_ENGULFING",
            )

        return (
            False,
            "NO_BULLISH_CONFIRMATION",
        )

    # --------------------------------------------------------
    # PUT
    # --------------------------------------------------------

    if direction == "PUT":

        bearish_candle = (
            current_close
            < current_open
        )

        bearish_rejection = (
            bearish_candle
            and upper_wick >= body
            and upper_wick
            >= candle_range * 0.25
        )

        bearish_engulfing = (
            bearish_candle
            and previous_close
            > previous_open
            and current_open
            >= previous_close
            and current_close
            <= previous_open
        )

        if bearish_rejection:

            return (
                True,
                "BEARISH_REJECTION",
            )

        if bearish_engulfing:

            return (
                True,
                "BEARISH_ENGULFING",
            )

        return (
            False,
            "NO_BEARISH_CONFIRMATION",
        )

    return (
        False,
        "INVALID_DIRECTION",
    )


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def build_signal_message(
    asset,
    direction,
    analysis,
    price,
    signal_id,
    confirmation,
):

    return (
        "🔔 <b>MOMENTUM 10 SIGNAL</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Source: OTCharts\n"
        "Venue: Quotex\n"
        "Asset: "
        + asset
        + "\n"
        "Direction: "
        + direction
        + "\n"
        "Timeframe: 1M\n"
        "Expiry Reference: 1 minute\n"
        "Strategy: Momentum 10 Extreme-Reversal\n"
        "Extreme: "
        + str(
            analysis["extreme"]
        )
        + "\n"
        "Price Confirmation: "
        + confirmation
        + "\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Momentum: "
        + str(
            round(
                analysis[
                    "momentum"
                ],
                5,
            )
        )
        + "\n"
        "Previous: "
        + str(
            round(
                analysis[
                    "previous"
                ],
                5,
            )
        )
        + "\n"
        "Previous 2: "
        + str(
            round(
                analysis[
                    "previous_previous"
                ],
                5,
            )
        )
        + "\n"
        "Recent Low: "
        + str(
            round(
                analysis[
                    "recent_low"
                ],
                5,
            )
        )
        + "\n"
        "Recent High: "
        + str(
            round(
                analysis[
                    "recent_high"
                ],
                5,
            )
        )
        + "\n"
        "Low Threshold: "
        + str(
            round(
                analysis[
                    "low_threshold"
                ],
                5,
            )
        )
        + "\n"
        "High Threshold: "
        + str(
            round(
                analysis[
                    "high_threshold"
                ],
                5,
            )
        )
        + "\n"
        "Reversal Strength: "
        + str(
            round(
                analysis[
                    "reversal_strength"
                ],
                1,
            )
        )
        + "%\n"
        "Price: "
        + str(price)
        + "\n"
        "Signal ID: "
        + signal_id
    )


# ============================================================
# PROCESS COMPLETED CANDLE
# ============================================================

def process_completed_candle(
    asset,
):

    global trade_count
    global last_signal_time

    try:

        candles = candles_by_asset.get(
            asset,
            [],
        )

        if len(candles) < (
            MOMENTUM_LOOKBACK + 3
        ):

            print(
                "Waiting for enough candles: "
                + str(
                    len(candles)
                )
                + "/"
                + str(
                    MOMENTUM_LOOKBACK
                    + 3
                ),
                flush=True,
            )

            return

        analysis = (
            analyze_momentum(
                candles
            )
        )

        if not analysis:
            return

        signal = analysis[
            "signal"
        ]

        # ----------------------------------------------------
        # NO TRADE
        # ----------------------------------------------------

        if signal is None:

            print(
                "NO TRADE | "
                + asset
                + " | Momentum extreme "
                "not confirmed.",
                flush=True,
            )

            return

        # ----------------------------------------------------
        # PRICE ACTION CONFIRMATION
        # ----------------------------------------------------

        confirmed, confirmation = (
            price_action_confirmation(
                candles,
                signal,
            )
        )

        if not confirmed:

            print(
                "NO TRADE | "
                + asset
                + " | "
                + confirmation,
                flush=True,
            )

            return

        # ----------------------------------------------------
        # DUPLICATE SIGNAL PROTECTION
        # ----------------------------------------------------

        current_candle = candles[-1]

        candle_id = current_candle[
            "from"
        ]

        if (
            last_signal_candle.get(
                asset
            )
            == candle_id
        ):

            return

        extreme = analysis[
            "extreme"
        ]

        if (
            extreme_state.get(
                asset
            )
            == extreme
        ):

            print(
                "NO TRADE | "
                + asset
                + " | Same extreme already "
                "triggered.",
                flush=True,
            )

            return

        last_signal_candle[
            asset
        ] = candle_id

        extreme_state[
            asset
        ] = extreme

        price = current_candle[
            "close"
        ]

        signal_id = (
            "M10-"
            + asset.replace(
                "_",
                "",
            )
            + "-"
            + signal
            + "-"
            + str(
                int(
                    time.time()
                )
            )
        )

        last_signal_time = (
            time.time()
        )

        message = (
            build_signal_message(
                asset,
                signal,
                analysis,
                price,
                signal_id,
                confirmation,
            )
        )

        print(
            "",
            flush=True,
        )

        print(
            "🔔 MOMENTUM 10 SIGNAL",
            flush=True,
        )

        print(
            "━━━━━━━━━━━━━━━━━━",
            flush=True,
        )

        print(
            "Asset: "
            + asset,
            flush=True,
        )

        print(
            "Direction: "
            + signal,
            flush=True,
        )

        print(
            "Timeframe: 1M",
            flush=True,
        )

        print(
            "Confirmation: "
            + confirmation,
            flush=True,
        )

        print(
            "Price: "
            + str(price),
            flush=True,
        )

        print(
            "Signal ID: "
            + signal_id,
            flush=True,
        )

        send_telegram(
            message
        )

        # ----------------------------------------------------
        # NO AUTOMATIC BROKER TRADE
        # ----------------------------------------------------

        print(
            "ℹ️ Signal sent to Telegram. "
            "Automatic Quotex trading is disabled.",
            flush=True,
        )

    except Exception as exc:

        print(
            "Process completed candle error: "
            + str(exc),
            flush=True,
        )


# ============================================================
# HEARTBEAT
# ============================================================

def heartbeat():

    uptime_stream = (
        "CONNECTED"
        if (
            last_stream_tick
            and (
                time.time()
                - last_stream_tick
            ) < 45
        )
        else "WAITING"
    )

    win_rate = 0.0

    if trade_count > 0:

        win_rate = (
            wins
            / trade_count
        ) * 100.0

    print(
        "",
        flush=True,
    )

    print(
        "💚 MOMENTUM 10 OTCHARTS BOT ALIVE",
        flush=True,
    )

    print(
        "━━━━━━━━━━━━━━━━━━",
        flush=True,
    )

    print(
        "OTCharts stream: "
        + uptime_stream,
        flush=True,
    )

    print(
        "Venue: Quotex",
        flush=True,
    )

    print(
        "Asset: "
        + TARGET_SYMBOL,
        flush=True,
    )

    print(
        "Strategy: Momentum 10",
        flush=True,
    )

    print(
        "Timeframe: 1M",
        flush=True,
    )

    print(
        "Expiry reference: 1 minute",
        flush=True,
    )

    print(
        "Automatic trading: OFF",
        flush=True,
    )

    print(
        "Signals observed: "
        + str(trade_count),
        flush=True,
    )

    print(
        "Wins: "
        + str(wins),
        flush=True,
    )

    print(
        "Losses: "
        + str(losses),
        flush=True,
    )

    print(
        "Win Rate: "
        + str(
            round(
                win_rate,
                1,
            )
        )
        + "%",
        flush=True,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    global last_heartbeat
    global stream_thread

    init_log()

    print(
        "🚀 MOMENTUM 10 OTCHARTS SCANNER STARTING",
        flush=True,
    )

    print(
        "━━━━━━━━━━━━━━━━━━",
        flush=True,
    )

    print(
        "Venue: Quotex",
        flush=True,
    )

    print(
        "Asset: "
        + TARGET_SYMBOL,
        flush=True,
    )

    print(
        "Timeframe: 1M",
        flush=True,
    )

    print(
        "Strategy: Momentum 10 Extreme-Reversal",
        flush=True,
    )

    print(
        "Automatic trading: OFF",
        flush=True,
    )

    print(
        "",
        flush=True,
    )

    # --------------------------------------------------------
    # API KEY CHECK
    # --------------------------------------------------------

    if not OTCHARTS_API_KEY:

        print(
            "❌ OTCHARTS_API_KEY is missing.",
            flush=True,
        )

        send_telegram(
            "❌ <b>Momentum 10 Bot</b>\n"
            "OTCHARTS_API_KEY is missing."
        )

        return

    # --------------------------------------------------------
    # TELEGRAM CHECK
    # --------------------------------------------------------

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:

        print(
            "⚠️ Telegram credentials missing. "
            "Scanner will still run.",
            flush=True,
        )

    # --------------------------------------------------------
    # API ACCESS
    # --------------------------------------------------------

    if not check_otcharts():

        print(
            "❌ OTCharts access check failed.",
            flush=True,
        )

        return

    # --------------------------------------------------------
    # USAGE
    # --------------------------------------------------------

    get_usage()

    # --------------------------------------------------------
    # SYMBOL
    # --------------------------------------------------------

    if not get_symbols():

        print(
            "❌ Target symbol unavailable.",
            flush=True,
        )

        return

    # --------------------------------------------------------
    # INITIAL HISTORY
    # --------------------------------------------------------

    if not load_initial_candles(
        TARGET_SYMBOL
    ):

        print(
            "❌ Initial candle load failed.",
            flush=True,
        )

        return

    # --------------------------------------------------------
    # TELEGRAM STARTUP
    # --------------------------------------------------------

    send_telegram(
        "🟢 <b>MOMENTUM 10 SCANNER ONLINE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Source: OTCharts\n"
        "Venue: Quotex\n"
        "Asset: "
        + TARGET_SYMBOL
        + "\n"
        "Timeframe: 1M\n"
        "Strategy: Momentum 10 Extreme-Reversal\n"
        "Auto Trading: OFF\n"
        "Waiting for completed candles."
    )

    # --------------------------------------------------------
    # HEARTBEAT CLOCK
    # --------------------------------------------------------

    last_heartbeat = time.time()

    # --------------------------------------------------------
    # START STREAM
    # --------------------------------------------------------

    stream_thread = threading.Thread(
        target=stream_loop,
        daemon=True,
    )

    stream_thread.start()

    print(
        "🟢 Live market-data stream started.",
        flush=True,
    )

    # --------------------------------------------------------
    # MAIN WATCHDOG
    # --------------------------------------------------------

    while not stop_event.is_set():

        now = time.time()

        # ----------------------------------------------------
        # HEARTBEAT
        # ----------------------------------------------------

        if (
            now
            - last_heartbeat
            >= HEARTBEAT_SECONDS
        ):

            heartbeat()

            last_heartbeat = now

        # ----------------------------------------------------
        # STREAM WATCHDOG
        # ----------------------------------------------------

        if (
            last_stream_tick
            and (
                now
                - last_stream_tick
            ) > 90
        ):

            print(
                "⚠️ No live tick received "
                "for more than 90 seconds.",
                flush=True,
            )

        # ----------------------------------------------------
        # TARGET
        # ----------------------------------------------------

        if (
            trade_count
            >= TARGET_TRADES
        ):

            print(
                "🎯 Signal target reached.",
                flush=True,
            )

            send_telegram(
                "🎯 <b>MOMENTUM 10 "
                "SIGNAL TEST COMPLETE</b>\n"
                "Asset: "
                + TARGET_SYMBOL
                + "\n"
                "Signals: "
                + str(
                    trade_count
                )
            )

            break

        time.sleep(
            SCAN_INTERVAL
        )

    stop_event.set()

    print(
        "Scanner stopped.",
        flush=True,
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        stop_event.set()

        print(
            "Bot stopped.",
            flush=True,
        )

    except Exception as exc:

        stop_event    except Exception as exc:

        stop_event.set()

        print(
            "FATAL ERROR: "
            + str(exc),
            flush=True,
        )
