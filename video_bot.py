import csv
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

from iqoptionapi.stable_api import IQ_Option


# ============================================================
# VIDEO BOT V1
# ============================================================
# This bot reproduces the behavior described in the video.
#
# STRATEGY:
#   One completed 1-minute candle
#
#   CLOSE > OPEN  -> CALL
#   CLOSE < OPEN  -> PUT
#   CLOSE == OPEN -> NO TRADE
#
# NO:
#   EMA
#   RSI
#   MACD
#   ADX
#   ATR
#   Support/resistance
#   Multi-timeframe analysis
#   Candlestick patterns
#   Martingale
#   Recovery
#   AI prediction
#
# DEMO/PRACTICE ONLY
# ============================================================


# -----------------------------
# Configuration
# -----------------------------

BALANCE_MODE = "PRACTICE"

ASSET = "EURGBP-OTC"

STAKE = 10.0

EXPIRY_MINUTES = 1

TIMEFRAME = 60

# Number of completed trades to collect.
TRADE_TARGET = 50

# Seconds after the beginning of a new minute before
# requesting the candle data.
CANDLE_READ_DELAY = 5

# How often to print status while waiting for a result.
RESULT_POLL_SECONDS = 2

# Maximum time allowed to wait for a result.
RESULT_TIMEOUT_SECONDS = 180

# Files used to preserve the experiment.
CSV_FILE = "video_bot_trade_history.csv"
JSON_FILE = "video_bot_trade_history.json"


# -----------------------------
# Global API object
# -----------------------------

api = None


# -----------------------------
# Statistics
# -----------------------------

total_trades = 0
completed_trades = 0
wins = 0
losses = 0
draws = 0
net_profit_loss = 0.0

trade_history = []


# ============================================================
# Utility
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def timestamp_string():
    return now_utc().strftime("%Y-%m-%d %H:%M:%S UTC")


def safe_float(value, default=None):
    try:
        return float(value)
    except Exception:
        return default


# ============================================================
# File handling
# ============================================================

def save_history():
    try:
        with open(JSON_FILE, "w", encoding="utf-8") as f:
            json.dump(
                trade_history,
                f,
                indent=2,
                ensure_ascii=False
            )
    except Exception as exc:
        print(
            f"JSON save warning: {exc}",
            flush=True
        )

    try:
        fieldnames = [
            "trade_number",
            "timestamp",
            "asset",
            "direction",
            "amount",
            "expiry_minutes",
            "candle_time",
            "candle_open",
            "candle_close",
            "change_percent",
            "order_id",
            "outcome",
            "profit_loss",
            "balance_after",
        ]

        with open(
            CSV_FILE,
            "w",
            newline="",
            encoding="utf-8"
        ) as f:

            writer = csv.DictWriter(
                f,
                fieldnames=fieldnames
            )

            writer.writeheader()

            for row in trade_history:
                writer.writerow(row)

    except Exception as exc:
        print(
            f"CSV save warning: {exc}",
            flush=True
        )


# ============================================================
# Statistics
# ============================================================

def print_stats():
    if completed_trades > 0:
        win_rate = (
            wins / completed_trades
        ) * 100.0
    else:
        win_rate = 0.0

    print(
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "VIDEO BOT SESSION STATS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Target:       {TRADE_TARGET}\n"
        f"Opened:       {total_trades}\n"
        f"Completed:    {completed_trades}\n"
        f"Wins:         {wins}\n"
        f"Losses:       {losses}\n"
        f"Draws:        {draws}\n"
        f"Win Rate:     {win_rate:.2f}%\n"
        f"Net Demo P/L: ${net_profit_loss:+.2f}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n",
        flush=True
    )


# ============================================================
# Connection
# ============================================================

def connect_iq_option():

    global api

    email = os.getenv("IQ_EMAIL")
    password = os.getenv("IQ_PASSWORD")

    if not email or not password:
        print(
            "ERROR: IQ_EMAIL and IQ_PASSWORD "
            "environment variables are required.",
            flush=True
        )
        return False

    print(
        "Connecting to IQ Option...",
        flush=True
    )

    try:

        api = IQ_Option(
            email,
            password
        )

        api.set_max_reconnect(5)

        connected, reason = api.connect()

        if not connected:

            print(
                f"IQ Option connection failed: {reason}",
                flush=True
            )

            return False

        print(
            "IQ Option connection: OK",
            flush=True
        )

        try:
            api.update_ACTIVES_OPCODE()
        except Exception as exc:
            print(
                f"Active-ID refresh warning: {exc}",
                flush=True
            )

        try:
            api.change_balance(
                BALANCE_MODE
            )
        except Exception as exc:
            print(
                f"Balance mode warning: {exc}",
                flush=True
            )

        time.sleep(2)

        balance = api.get_balance()

        print(
            f"Account: {BALANCE_MODE}",
            flush=True
        )

        print(
            f"Balance: ${float(balance):.2f}",
            flush=True
        )

        return True

    except Exception as exc:

        print(
            f"Connection error: {exc}",
            flush=True
        )

        traceback.print_exc()

        return False


# ============================================================
# Connection check
# ============================================================

def ensure_connection():

    global api

    try:

        if api is None:
            return connect_iq_option()

        if api.check_connect():
            return True

        print(
            "IQ Option connection lost. Reconnecting...",
            flush=True
        )

        connected, reason = api.connect()

        if connected:
            try:
                api.change_balance(
                    BALANCE_MODE
                )
            except Exception:
                pass

            print(
                "Reconnected successfully.",
                flush=True
            )

            return True

        print(
            f"Reconnect failed: {reason}",
            flush=True
        )

        return False

    except Exception as exc:

        print(
            f"Connection check error: {exc}",
            flush=True
        )

        return False


# ============================================================
# Wait for beginning of a new minute
# ============================================================

def wait_for_minute_start():

    current = time.time()

    next_minute = (
        int(current // 60) + 1
    ) * 60

    wait_seconds = (
        next_minute - current
    )

    print(
        f"Waiting {wait_seconds:.1f}s "
        "for next minute...",
        flush=True
    )

    time.sleep(
        max(0.0, wait_seconds)
    )

    time.sleep(
        CANDLE_READ_DELAY
    )


# ============================================================
# Get ONE completed candle
# ============================================================

def get_latest_completed_candle():

    if not ensure_connection():
        return None

    try:

        current_time = time.time()

        candles = api.get_candles(
            ASSET,
            TIMEFRAME,
            5,
            current_time
        )

        if not candles:
            print(
                "No candle data received.",
                flush=True
            )
            return None

        completed = []

        for candle in candles:

            candle_from = safe_float(
                candle.get("from")
            )

            if candle_from is None:
                continue

            candle_end = (
                candle_from + TIMEFRAME
            )

            # Only use a candle that has fully closed.
            if candle_end <= current_time:
                completed.append(candle)

        if not completed:

            print(
                "No completed candle available yet.",
                flush=True
            )

            return None

        completed.sort(
            key=lambda x: x.get("from", 0)
        )

        return completed[-1]

    except Exception as exc:

        print(
            f"Candle error: {exc}",
            flush=True
        )

        return None


# ============================================================
# Generate signal
# ============================================================

def get_signal(candle):

    candle_open = safe_float(
        candle.get("open")
    )

    candle_close = safe_float(
        candle.get("close")
    )

    if candle_open is None:
        return None, None

    if candle_close is None:
        return None, None

    if candle_open == 0:
        return None, None

    change_percent = (
        (candle_close - candle_open)
        / candle_open
    ) * 100.0

    print(
        "\n"
        "SIGNAL GENERATED\n"
        "----------------\n"
        f"Open:  {candle_open}\n"
        f"Close: {candle_close}\n"
        f"1 MIN Change: "
        f"{change_percent:+.4f}%\n",
        flush=True
    )

    if candle_close > candle_open:

        print(
            "Direction: CALL (Bullish)",
            flush=True
        )

        return "call", change_percent

    if candle_close < candle_open:

        print(
            "Direction: PUT (Bearish)",
            flush=True
        )

        return "put", change_percent

    print(
        "Direction: INDECISION",
        flush=True
    )

    return None, change_percent


# ============================================================
# Place binary trade
# ============================================================

def execute_trade(
    direction,
    candle,
    change_percent
):

    global total_trades

    if not ensure_connection():
        return None

    print(
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "PLACING TRADE\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Asset:     {ASSET}\n"
        f"Direction: {direction.upper()}\n"
        f"Amount:    ${STAKE:.2f}\n"
        f"Expiry:    {EXPIRY_MINUTES} minute\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        flush=True
    )

    try:

        success, order_id = api.buy(
            STAKE,
            ASSET,
            direction,
            EXPIRY_MINUTES
        )

        if not success or not order_id:

            print(
                "Trade was NOT opened.",
                flush=True
            )

            print(
                f"API response: "
                f"success={success}, "
                f"order_id={order_id}",
                flush=True
            )

            return None

        total_trades += 1

        candle_time = candle.get(
            "from"
        )

        if candle_time:

            try:
                candle_time_text = (
                    datetime.fromtimestamp(
                        float(candle_time),
                        tz=timezone.utc
                    ).strftime(
                        "%Y-%m-%d %H:%M:%S UTC"
                    )
                )
            except Exception:
                candle_time_text = str(
                    candle_time
                )

        else:
            candle_time_text = ""

        record = {
            "trade_number": total_trades,
            "timestamp": timestamp_string(),
            "asset": ASSET,
            "direction": direction.upper(),
            "amount": STAKE,
            "expiry_minutes": EXPIRY_MINUTES,
            "candle_time": candle_time_text,
            "candle_open": safe_float(
                candle.get("open"),
                0
            ),
            "candle_close": safe_float(
                candle.get("close"),
                0
            ),
            "change_percent": change_percent,
            "order_id": str(order_id),
            "outcome": "PENDING",
            "profit_loss": None,
            "balance_after": None,
        }

        trade_history.append(record)

        save_history()

        print(
            "\n"
            "🚀 TRADE PLACED\n"
            "----------------\n"
            f"Trade #: {total_trades}\n"
            f"Order ID: {order_id}\n"
            f"Asset: {ASSET}\n"
            f"Direction: {direction.upper()}\n"
            f"Amount: ${STAKE:.2f}\n"
            f"Expiry: {EXPIRY_MINUTES} minute\n"
            f"Time: {timestamp_string()}",
            flush=True
        )

        return record

    except Exception as exc:

        print(
            f"Trade execution error: {exc}",
            flush=True
        )

        traceback.print_exc()

        return None


# ============================================================
# Wait for trade result
# ============================================================

def wait_for_trade_result(record):

    global completed_trades
    global wins
    global losses
    global draws
    global net_profit_loss

    order_id = record["order_id"]

    print(
        "\nWaiting for trade result...",
        flush=True
    )

    started = time.time()

    while True:

        if (
            time.time() - started
            > RESULT_TIMEOUT_SECONDS
        ):

            print(
                "WARNING: Result timeout.",
                flush=True
            )

            print(
                "The trade will NOT be counted "
                "as WIN/LOSS/DRAW until an actual "
                "P/L is obtained.",
                flush=True
            )

            return False

        try:

            # The community API provides check_win_v4
            # for binary option results.
            pnl = api.check_win_v4(
                order_id
            )

            if pnl is None:
                time.sleep(
                    RESULT_POLL_SECONDS
                )
                continue

            pnl = safe_float(
                pnl
            )

            if pnl is None:
                time.sleep(
                    RESULT_POLL_SECONDS
                )
                continue

            completed_trades += 1

            record["profit_loss"] = pnl

            if pnl > 0:

                wins += 1

                record["outcome"] = "WIN"

            elif pnl < 0:

                losses += 1

                record["outcome"] = "LOSS"

            else:

                draws += 1

                record["outcome"] = "DRAW"

            net_profit_loss += pnl

            try:

                balance = api.get_balance()

                record["balance_after"] = safe_float(
                    balance
                )

            except Exception:

                record["balance_after"] = None

            save_history()

            print(
                "\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "TRADE RESULT\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Order ID: {order_id}\n"
                f"Asset: {ASSET}\n"
                f"Direction: {record['direction']}\n"
                f"Outcome: {record['outcome']}\n"
                f"P/L: ${pnl:+.2f}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                flush=True
            )

            print_stats()

            return True

        except Exception as exc:

            print(
                f"Result check warning: {exc}",
                flush=True
            )

            time.sleep(
                RESULT_POLL_SECONDS
            )


# ============================================================
# Final report
# ============================================================

def final_report():

    print(
        "\n"
        "==================================================\n"
        "VIDEO BOT TEST COMPLETE\n"
        "==================================================",
        flush=True
    )

    print_stats()

    print(
        f"CSV history:  {CSV_FILE}",
        flush=True
    )

    print(
        f"JSON history: {JSON_FILE}",
        flush=True
    )

    print(
        "==================================================",
        flush=True
    )


# ============================================================
# Main loop
# ============================================================

def run():

    global api

    print(
        "\n"
        "==================================================\n"
        "VIDEO BOT V1 STARTING\n"
        "==================================================\n"
        f"Account:      {BALANCE_MODE}\n"
        f"Asset:        {ASSET}\n"
        f"Stake:        ${STAKE:.2f}\n"
        f"Expiry:       {EXPIRY_MINUTES} minute\n"
        f"Timeframe:    1 minute\n"
        f"Strategy:     ONE CANDLE OPEN/CLOSE\n"
        f"Trade target: {TRADE_TARGET}\n"
        "==================================================",
        flush=True
    )

    if not connect_iq_option():

        print(
            "Unable to connect. Exiting.",
            flush=True
        )

        return

    print(
        "\n"
        "🟢 VIDEO BOT ONLINE\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Asset: {ASSET}\n"
        "Feed: IQ Option\n"
        "Signal: 1 completed candle\n"
        "CALL: Close > Open\n"
        "PUT: Close < Open\n"
        "No trade: Close == Open\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Expiry: {EXPIRY_MINUTES} minute\n"
        f"Target: {TRADE_TARGET} completed trades\n"
        "Mode: PRACTICE\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        flush=True
    )

    try:

        while completed_trades < TRADE_TARGET:

            if not ensure_connection():

                print(
                    "Connection unavailable. "
                    "Waiting 10 seconds...",
                    flush=True
                )

                time.sleep(10)
                continue

            wait_for_minute_start()

            candle = (
                get_latest_completed_candle()
            )

            if candle is None:

                print(
                    "No usable completed candle. "
                    "Skipping this minute.",
                    flush=True
                )

                continue

            direction, change_percent = (
                get_signal(candle)
            )

            if direction is None:

                print(
                    "NO TRADE — waiting for "
                    "next minute.",
                    flush=True
                )

                continue

            record = execute_trade(
                direction,
                candle,
                change_percent
            )

            if record is None:

                print(
                    "Trade was not opened. "
                    "Waiting for next minute.",
                    flush=True
                )

                continue

            wait_for_trade_result(
                record
            )

            if completed_trades >= TRADE_TARGET:

                break

            print(
                "\nNext trade cycle...",
                flush=True
            )

    except KeyboardInterrupt:

        print(
            "\nBot stopped manually.",
            flush=True
        )

    except Exception as exc:

        print(
            f"\nFatal runtime error: {exc}",
            flush=True
        )

        traceback.print_exc()

    finally:

        final_report()

        try:

            if api is not None:
                api.close()

        except Exception:
            pass

        print(
            "Disconnected.",
            flush=True
        )


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    run()
