
import json
import os
import runpy
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from iqoptionapi.stable_api import IQ_Option


DAILY_PROFIT_TARGET = 10.0
DAILY_LOSS_LIMIT = -5.0
MAX_STAKE = 1.0
MAX_TRADES = 50
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


def save_state():
    temp_file = STATE_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(DAILY, file, indent=2)
    os.replace(temp_file, STATE_FILE)


def load_state():
    global DAILY

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        if data.get("date") == today():
            DAILY = data
            DAILY.setdefault("submitted", 0)
            DAILY.setdefault("net", 0.0)
            DAILY.setdefault("wins", 0)
            DAILY.setdefault("losses", 0)
            DAILY.setdefault("breakeven", 0)
            DAILY.setdefault("resolved", 0)
            DAILY.setdefault("blocked", 0)
            save_state()
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
        "submitted": 0,
        "blocked": 0,
    }
    save_state()


def stopped():
    global STOP_REASON

    if DAILY["net"] >= DAILY_PROFIT_TARGET:
        STOP_REASON = "Daily profit target reached"
        return True

    if DAILY["net"] <= DAILY_LOSS_LIMIT:
        STOP_REASON = "Daily loss limit reached"
        return True

    if DAILY["submitted"] >= MAX_TRADES:
        STOP_REASON = "50 trade submissions reached"
        return True

    return False


def guarded_buy(self, amount, asset, action, expiry, *args, **kwargs):
    with LOCK:
        if stopped():
            DAILY["blocked"] += 1
            save_state()
            print("RISK STOP:", STOP_REASON)
            return False, None

        try:
            stake = float(amount)
        except (TypeError, ValueError):
            print("RISK STOP: Invalid stake")
            return False, None

        if stake <= 0 or stake > MAX_STAKE:
            DAILY["blocked"] += 1
            save_state()
            print("RISK STOP: Stake outside permitted limit")
            return False, None

        result = ORIGINAL_BUY(
            self, amount, asset, action, expiry, *args, **kwargs
        )

        if isinstance(result, (tuple, list)) and len(result) >= 2:
            success, order_id = result[0], result[1]

            if success and order_id is not None:
                PENDING.add(str(order_id))
                DAILY["submitted"] += 1
                save_state()
                print(
                    "RISK TRACKING: Order",
                    order_id,
                    "Submitted:",
                    DAILY["submitted"],
                )

        return result


def guarded_check(self, order_id, *args, **kwargs):
    result = ORIGINAL_CHECK(self, order_id, *args, **kwargs)
    key = str(order_id)

    if isinstance(result, (int, float)) and not isinstance(result, bool):
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
    print("Maximum trade submissions:", MAX_TRADES)
    print("Strategy file: scanner.py (unchanged)")

    try:
        runpy.run_path("scanner.py", run_name="__main__")
    finally:
        IQ_Option.buy = ORIGINAL_BUY
        IQ_Option.check_win_v4 = ORIGINAL_CHECK


if __name__ == "__main__":
    main()
