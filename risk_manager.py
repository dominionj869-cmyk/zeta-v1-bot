
import json
import os
import runpy
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from iqoptionapi.stable_api import IQ_Option


# RISK MANAGEMENT ONLY
# scanner.py remains unchanged.

DAILY_PROFIT_TARGET = 20
DAILY_LOSS_LIMIT = -10.5
MAX_STAKE = 1.0
STATE_FILE = "risk_state.json"
TIMEZONE = ZoneInfo("Africa/Lagos")

LOCK = threading.RLock()
PENDING = set()
DAILY = {}
STOP_REASON = ""
ORIGINAL_BUY = IQ_Option.buy
ORIGINAL_CHECK = IQ_Option.check_win_v4


def today():
    return datetime.now(TIMEZONE).date().isoformat()


def load_state():
    global DAILY
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        if data.get("date") == today():
            DAILY = data
            return
    except (OSError, ValueError, TypeError):
        pass

    DAILY = {
        "date": today(),
        "net": 0.0,
        "wins": 0,
        "losses": 0,
        "breakeven": 0,
        "resolved": 0,
        "blocked": 0,
    }
    save_state()


def save_state():
    temp_file = STATE_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(DAILY, file, indent=2)
    os.replace(temp_file, STATE_FILE)


def stopped():
    global STOP_REASON

    if DAILY["net"] >= DAILY_PROFIT_TARGET:
        STOP_REASON = "Daily profit target reached"
        return True

    if DAILY["net"] <= DAILY_LOSS_LIMIT:
        STOP_REASON = "Daily loss limit reached"
        return True

    if DAILY["resolved"] >= 50:
        STOP_REASON = "50 resolved trades reached"
        return True

    return False


def guarded_buy(self, amount, asset, action, expiry, *args, **kwargs):
    with LOCK:
        if stopped():
            DAILY["blocked"] += 1
            save_state()
            print("RISK STOP:", STOP_REASON)
            return False, None

        if float(amount) > MAX_STAKE:
            DAILY["blocked"] += 1
            save_state()
            print("RISK STOP: Stake exceeds limit")
            return False, None

        if PENDING:
            DAILY["blocked"] += 1
            save_state()
            print("RISK HOLD: Previous trade unresolved")
            return False, None

        result = ORIGINAL_BUY(
            self, amount, asset, action, expiry, *args, **kwargs
        )

        if isinstance(result, (tuple, list)) and len(result) >= 2:
            success, order_id = result[0], result[1]
            if success and order_id is not None:
                PENDING.add(str(order_id))
                print("RISK TRACKING: Order", order_id)

        return result


def guarded_check(self, order_id, *args, **kwargs):
    result = ORIGINAL_CHECK(self, order_id, *args, **kwargs)

    if isinstance(result, (int, float)) and not isinstance(result, bool):
        key = str(order_id)

        with LOCK:
            if key in PENDING:
                PENDING.remove(key)
                profit = float(result)

                DAILY["net"] = round(DAILY["net"] + profit, 2)
                DAILY["resolved"] += 1

                if profit > 0:
                    DAILY["wins"] += 1
                elif profit < 0:
                    DAILY["losses"] += 1
                else:
                    DAILY["breakeven"] += 1

                save_state()

                print(
                    "RISK UPDATE:",
                    "Net =", DAILY["net"],
                    "Resolved =", DAILY["resolved"],
                    "Wins =", DAILY["wins"],
                    "Losses =", DAILY["losses"],
                )

                if stopped():
                    print("RISK STOP:", STOP_REASON)

    return result


def main():
    load_state()

    IQ_Option.buy = guarded_buy
    IQ_Option.check_win_v4 = guarded_check

    print("Risk manager enabled")
    print("Daily profit target: +$", DAILY_PROFIT_TARGET)
    print("Daily loss limit: -$", abs(DAILY_LOSS_LIMIT))
    print("Maximum stake: $", MAX_STAKE)
    print("Maximum resolved trades: 50")
    print("Strategy file: scanner.py (unchanged)")

    try:
        runpy.run_path("scanner.py", run_name="__main__")
    finally:
        IQ_Option.buy = ORIGINAL_BUY
        IQ_Option.check_win_v4 = ORIGINAL_CHECK


if __name__ == "__main__":
    main()
