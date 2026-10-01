import os
import csv
import json
import time
from datetime import datetime, timezone

from iqoptionapi.stable_api import IQ_Option


# ============================================================
# VIDEO BOT V1
# ONE-CANDLE OPEN/CLOSE STRATEGY
# PRACTICE / DEMO ONLY
# ============================================================

BALANCE_MODE = "PRACTICE"

ASSET = "EURGBP-OTC"

STAKE = 10.0

EXPIRY_MINUTES = 1
TIMEFRAME = 60

TRADE_TARGET = 50

CANDLE_READ_DELAY = 5

RESULT_POLL_SECONDS = 2
RESULT_TIMEOUT_SECONDS = 180

CSV_FILE = "video_bot_trade_history.csv"
JSON_FILE = "video_bot_trade_history.json"


# ============================================================
# GLOBAL STATS
# ============================================================

stats = {
    "trades": 0,
    "wins": 0,
    "losses": 0,
    "draws": 0,
    "net_pnl": 0.0,
}


# ============================================================
# TIME
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def timestamp():
    return utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")


# ============================================================
# HISTORY
# ============================================================

def load_history():
    if not os.path.exists(JSON_FILE):
        return []

    try:
        with open(JSON_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            return data

    except Exception as e:
        print(f"Could not load history: {e}")

    return []


def save_history(history):
    try:
        with open(JSON_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)

    except Exception as e:
        print(f"JSON save error: {e}")


def append_csv(record):
    file_exists = os.path.exists(CSV_FILE)

    try:
        with open(
            CSV_FILE,
            "a",
            newline="",
            encoding="utf-8"
        ) as f:

            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "trade_number",
                    "timestamp",
                    "asset",
                    "direction",
                    "stake",
                    "expiry_minutes",
                    "candle_open",
                    "candle_close",
                    "candle_time",
                    "order_id",
                    "result",
                    "pnl",
                ],
            )

            if not file_exists:
                writer.writeheader()

            writer.writerow(record)

    except Exception as e:
        print(f"CSV save error: {e}")


# ============================================================
# STATS
# ============================================================

def print_stats():
    completed = stats["trades"]

    if completed > 0:
        win_rate = (stats["wins"] / completed) * 100
    else:
        win_rate = 0.0

    print()
    print("=" * 50)
    print("VIDEO BOT STATUS")
    print("=" * 50)
    print(f"Completed trades: {completed}/{TRADE_TARGET}")
    print(f"Wins:             {stats['wins']}")
    print(f"Losses:           {stats['losses']}")
    print(f"Draws:            {stats['draws']}")
    print(f"Win rate:         {win_rate:.2f}%")
    print(f"Net demo P/L:     ${stats['net_pnl']:+.2f}")
    print("=" * 50)
    print()


# ============================================================
# IQ OPTION CONNECTION
# ============================================================

def connect_iq_option():

    email = os.getenv("IQ_EMAIL")
    password = os.getenv("IQ_PASSWORD")

    if not email or not password:
        print("ERROR: IQ_EMAIL or IQ_PASSWORD is missing.")
        return None

    print("Connecting to IQ Option...")

    api = IQ_Option(email, password)

    try:

        # IMPORTANT:
        # Do NOT call set_max_reconnect().
        # The installed iqoptionapi version does not provide it.

        connected, reason = api.connect()

        if not connected:
            print(f"Connection failed: {reason}")
            return None

        print("IQ Option connection: OK")

        # Use PRACTICE/DEMO account.
        api.change_balance(BALANCE_MODE)

        time.sleep(2)

        print(f"Account mode: {BALANCE_MODE}")

        # Refresh active/instrument information.
        try:
            api.update_ACTIVES_OPCODE()
            print("Active instruments refreshed.")
        except Exception as e:
            print(f"Active refresh warning: {e}")

        return api

    except Exception as e:

        print(f"Connection error: {e}")

        return None


# ============================================================
# COMPLETED CANDLE
# ============================================================

def get_latest_completed_candle(api):

    now = int(time.time())

    try:

        candles = api.get_candles(
            ASSET,
            TIMEFRAME,
            5,
            now
        )

    except Exception as e:

        print(f"Candle request error: {e}")
        return None

    if not candles:
        print("No candle data received.")
        return None

    completed = []

    for candle in candles:

        try:

            candle_from = int(candle["from"])

            candle_close_time = candle_from + TIMEFRAME

            # Only accept fully closed candles.
            if candle_close_time <= now:
                completed.append(candle)

        except Exception:
            continue

    if not completed:
        print("No completed candle available.")
        return None

    completed.sort(
        key=lambda x: int(x["from"])
    )

    return completed[-1]


# ============================================================
# SIGNAL
# ============================================================

def get_signal(candle):

    candle_open = float(candle["open"])
    candle_close = float(candle["close"])

    if candle_close > candle_open:
        return "call"

    if candle_close < candle_open:
        return "put"

    return None


# ============================================================
# WAIT FOR NEXT MINUTE
# ============================================================

def wait_for_new_minute():

    while True:

        now = time.time()

        seconds_into_minute = int(now) % 60

        remaining = 60 - seconds_into_minute

        # We want the previous candle to be completely closed.
        if remaining <= CANDLE_READ_DELAY:
            time.sleep(remaining + CANDLE_READ_DELAY)
            return

        time.sleep(1)


# ============================================================
# PLACE TRADE
# ============================================================

def execute_trade(
    api,
    direction,
    candle,
    trade_number,
):

    print()
    print("=" * 50)
    print(f"TRADE #{trade_number}")
    print("=" * 50)

    candle_open = float(candle["open"])
    candle_close = float(candle["close"])

    if direction == "call":
        display_direction = "CALL / HIGHER"
    else:
        display_direction = "PUT / LOWER"

    print(f"Asset:          {ASSET}")
    print(f"Direction:      {display_direction}")
    print(f"Stake:          ${STAKE:.2f}")
    print(f"Expiry:         {EXPIRY_MINUTES} minute")
    print(f"Candle open:    {candle_open}")
    print(f"Candle close:   {candle_close}")
    print(f"Candle time:    {candle['from']}")
    print("Sending order...")

    try:

        success, order_id = api.buy(
            STAKE,
            ASSET,
            direction,
            EXPIRY_MINUTES
        )

    except Exception as e:

        print(f"Order error: {e}")
        return None

    if not success:

        print("Order was rejected.")
        print(f"Order ID: {order_id}")
        return None

    if not order_id:

        print("Order accepted but no order ID was returned.")
        return None

    print("ORDER OPENED")
    print(f"Order ID: {order_id}")

    return order_id


# ============================================================
# CHECK RESULT
# ============================================================

def wait_for_trade_result(api, order_id):

    print("Waiting for actual trade result...")

    start_time = time.time()

    while True:

        elapsed = time.time() - start_time

        if elapsed >= RESULT_TIMEOUT_SECONDS:

            print("Result timeout reached.")

            return None

        try:

            pnl = api.check_win_v4(order_id)

            # Some API versions return None while trade is still open.
            if pnl is None:
                time.sleep(RESULT_POLL_SECONDS)
                continue

            pnl = float(pnl)

            if pnl > 0:
                result = "WIN"

            elif pnl < 0:
                result = "LOSS"

            else:
                result = "DRAW"

            return {
                "result": result,
                "pnl": pnl,
            }

        except Exception as e:

            print(f"Result check warning: {e}")
            time.sleep(RESULT_POLL_SECONDS)


# ============================================================
# RECORD RESULT
# ============================================================

def record_result(
    history,
    trade_number,
    candle,
    order_id,
    direction,
    result_data,
):

    result = result_data["result"]
    pnl = result_data["pnl"]

    candle_open = float(candle["open"])
    candle_close = float(candle["close"])

    record = {
        "trade_number": trade_number,
        "timestamp": timestamp(),
        "asset": ASSET,
        "direction": direction.upper(),
        "stake": STAKE,
        "expiry_minutes": EXPIRY_MINUTES,
        "candle_open": candle_open,
        "candle_close": candle_close,
        "candle_time": candle["from"],
        "order_id": order_id,
        "result": result,
        "pnl": pnl,
    }

    history.append(record)

    save_history(history)
    append_csv(record)

    stats["trades"] += 1

    if result == "WIN":
        stats["wins"] += 1

    elif result == "LOSS":
        stats["losses"] += 1

    else:
        stats["draws"] += 1

    stats["net_pnl"] += pnl

    print()
    print("=" * 50)
    print(f"TRADE #{trade_number} RESULT")
    print("=" * 50)
    print(f"Result:       {result}")
    print(f"P/L:          ${pnl:+.2f}")
    print("=" * 50)

    print_stats()


# ============================================================
# MAIN LOOP
# ============================================================

def run():

    print()
    print("=" * 50)
    print("VIDEO BOT V1 STARTING")
    print("=" * 50)
    print(f"Account:      {BALANCE_MODE}")
    print(f"Asset:        {ASSET}")
    print(f"Stake:        ${STAKE:.2f}")
    print(f"Expiry:       {EXPIRY_MINUTES} minute")
    print(f"Timeframe:    {TIMEFRAME // 60} minute")
    print("Strategy:     ONE CANDLE OPEN/CLOSE")
    print(f"Trade target: {TRADE_TARGET}")
    print("=" * 50)

    history = load_history()

    api = connect_iq_option()

    if api is None:
        print("Unable to connect. Exiting.")
        return

    print()
    print("VIDEO BOT ONLINE")
    print("Scanning for completed 1-minute candles.")
    print()

    try:

        while stats["trades"] < TRADE_TARGET:

            print(
                f"Waiting for next completed "
                f"{TIMEFRAME // 60}-minute candle..."
            )

            wait_for_new_minute()

            candle = get_latest_completed_candle(api)

            if candle is None:
                print("No valid completed candle. Retrying.")
                time.sleep(5)
                continue

            candle_open = float(candle["open"])
            candle_close = float(candle["close"])

            print()
            print("-" * 50)
            print("NEW COMPLETED CANDLE")
            print("-" * 50)
            print(f"Open:   {candle_open}")
            print(f"Close:  {candle_close}")

            direction = get_signal(candle)

            # ==================================================
            # DOJI / INDECISION
            # ==================================================

            if direction is None:

                print("Signal: INDECISION")
                print("Open equals close.")
                print("NO TRADE.")
                print("-" * 50)

                continue

            # ==================================================
            # SIGNAL
            # ==================================================

            if direction == "call":
                print("Signal: CALL / HIGHER")
            else:
                print("Signal: PUT / LOWER")

            order_id = execute_trade(
                api,
                direction,
                candle,
                stats["trades"] + 1,
            )

            if order_id is None:

                print("Trade was not counted.")
                print("Retrying on the next completed candle.")
                continue

            result_data = wait_for_trade_result(
                api,
                order_id
            )

            if result_data is None:

                print(
                    "Trade result could not be confirmed."
                )

                print(
                    "Trade will NOT be counted "
                    "as a completed test trade."
                )

                continue

            record_result(
                history,
                stats["trades"] + 1,
                candle,
                order_id,
                direction,
                result_data,
            )

            if stats["trades"] >= TRADE_TARGET:
                break

    except KeyboardInterrupt:

        print()
        print("Bot stopped manually.")

    except Exception as e:

        print()
        print("=" * 50)
        print("BOT ERROR")
        print("=" * 50)
        print(e)
        print("=" * 50)

    finally:

        print()
        print("=" * 50)
        print("VIDEO BOT FINAL REPORT")
        print("=" * 50)

        print_stats()

        try:
            api.close_connect()
        except Exception:
            pass

        print("IQ Option connection closed.")
        print("Video Bot stopped.")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    run()
