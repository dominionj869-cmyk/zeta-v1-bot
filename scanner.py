
import os
import csv
import time
import threading
from datetime import datetime, timezone

import requests
import iqoptionapi.constants as OP_code
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# MOMENTUM 10 - PRACTICE MODE WITH STRICT RISK CONTROLS
# ============================================================

MOMENTUM_PERIOD = 10
MOMENTUM_LOOKBACK = 50
EXTREME_PERCENTILE = 0.10
MIN_TURN_DISTANCE = 0.03

CANDLE_SECONDS = 60
EXPIRY_MINUTES = 1

AUTO_TRADE = True
BALANCE_MODE = "PRACTICE"
STAKE = 1.0

TARGET_TRADES = 50
DAILY_PROFIT_TARGET = 10.0
DAILY_LOSS_LIMIT = 5.0

SCAN_INTERVAL = 2
ASSET_REFRESH_SECONDS = 900
HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 15
MAX_ASSETS = 70
CANDLE_REQUEST_TIMEOUT = 8

LOG_FILE = "momentum_signal_log.csv"
STATE_FILE = "momentum_daily_state.csv"

IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

api = None
otc_assets = []
otc_active_ids = {}

trade_count = 0
wins = 0
losses = 0
pending_results = 0

daily_net = 0.0
daily_date = datetime.now().strftime("%Y-%m-%d")

# A result we cannot verify must never be treated as resolved.
safety_halt = False
limit_notice_sent = False

daily_net_lock = threading.RLock()
trade_lock = threading.Lock()
stop_event = threading.Event()

last_signal_candle = {}
extreme_state = {}


# ============================================================
# PERSISTENT STATE
# ============================================================

def save_daily_state():
    with daily_net_lock:
        temp_file = STATE_FILE + ".tmp"

        try:
            with open(temp_file, "w", encoding="utf-8") as file:
                file.write(daily_date + "\n")
                file.write(str(round(daily_net, 2)) + "\n")
                file.write(str(trade_count) + "\n")
                file.write(str(wins) + "\n")
                file.write(str(losses) + "\n")
                file.write("1\n" if safety_halt else "0\n")

            os.replace(temp_file, STATE_FILE)

        except Exception as exc:
            print("STATE SAVE FAILED:", exc, flush=True)
            raise RuntimeError("Cannot safely save daily state.") from exc


def load_daily_state():
    global daily_net, daily_date
    global trade_count, wins, losses, safety_halt

    today = datetime.now().strftime("%Y-%m-%d")

    if not os.path.exists(STATE_FILE):
        daily_date = today
        daily_net = 0.0
        trade_count = 0
        wins = 0
        losses = 0
        safety_halt = False
        save_daily_state()
        return

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            values = file.read().splitlines()

        if len(values) < 5:
            raise ValueError("State file is incomplete.")

        saved_date = values[0]
        saved_net = float(values[1])
        saved_trades = int(values[2])
        saved_wins = int(values[3])
        saved_losses = int(values[4])
        saved_halt = len(values) >= 6 and values[5] == "1"

        if saved_trades < 0 or saved_wins < 0 or saved_losses < 0:
            raise ValueError("Invalid counters in state file.")

        if saved_date != today:
            # An unresolved-result halt must survive a date change.
            if saved_halt:
                daily_date = saved_date
                daily_net = saved_net
                trade_count = saved_trades
                wins = saved_wins
                losses = saved_losses
                safety_halt = True
                print(
                    "SAFETY HALT: previous trade needs investigation.",
                    flush=True,
                )
                return

            daily_date = today
            daily_net = 0.0
            trade_count = 0
            wins = 0
            losses = 0
            safety_halt = False
            save_daily_state()
            return

        daily_date = saved_date
        daily_net = saved_net
        trade_count = saved_trades
        wins = saved_wins
        losses = saved_losses
        safety_halt = saved_halt

    except Exception as exc:
        raise RuntimeError(
            "Cannot safely load state. Trading is blocked: " + str(exc)
        ) from exc


def check_new_day():
    global daily_date, daily_net
    global trade_count, wins, losses, limit_notice_sent

    today = datetime.now().strftime("%Y-%m-%d")

    with daily_net_lock:
        if today == daily_date:
            return

        if pending_results > 0 or safety_halt:
            return

        daily_date = today
        daily_net = 0.0
        trade_count = 0
        wins = 0
        losses = 0
        limit_notice_sent = False
        save_daily_state()

    print("New trading day. Counters reset.", flush=True)


# ============================================================
# RISK CONTROLS
# ============================================================

def daily_limit_reached():
    with daily_net_lock:
        if safety_halt:
            return True, "SAFETY HALT: trade result requires investigation."

        if daily_net >= DAILY_PROFIT_TARGET:
            return True, "Daily net profit target reached."

        if daily_net <= -DAILY_LOSS_LIMIT:
            return True, "Daily net loss limit reached."

        if trade_count >= TARGET_TRADES:
            return True, "Daily trade limit reached."

    return False, ""


def announce_limit(reason):
    global limit_notice_sent

    with daily_net_lock:
        if limit_notice_sent:
            return

        limit_notice_sent = True

        message = (
            "MOMENTUM 10 TRADING PAUSED\n"
            + reason
            + "\nDaily net: $"
            + str(round(daily_net, 2))
            + "\nTrades opened: "
            + str(trade_count)
            + "\nWins: "
            + str(wins)
            + "\nLosses: "
            + str(losses)
            + "\nUnresolved trades: "
            + str(pending_results)
        )

    print(message, flush=True)
    send_telegram(message)


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"

    try:
        response = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=15,
        )

        if response.status_code != 200:
            print("Telegram error:", response.status_code, flush=True)

    except Exception as exc:
        print("Telegram error:", exc, flush=True)


# ============================================================
# CSV LOGGING
# ============================================================

def init_log():
    if os.path.exists(LOG_FILE):
        return

    with open(LOG_FILE, "w", newline="", encoding="utf-8") as file:
        csv.writer(file).writerow([
            "time", "asset", "direction", "signal_id",
            "entry_price", "result", "profit", "daily_net"
        ])


def log_trade(asset, direction, signal_id, entry_price,
              result, profit, current_net):
    try:
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as file:
            csv.writer(file).writerow([
                datetime.now(timezone.utc).isoformat(),
                asset, direction, signal_id, entry_price,
                result, profit, current_net,
            ])
    except Exception as exc:
        print("LOG ERROR:", exc, flush=True)


# ============================================================
# IQ OPTION CONNECTION
# ============================================================

def connect_iq():
    global api

    if not IQ_EMAIL or not IQ_PASSWORD:
        print("Missing IQ_EMAIL or IQ_PASSWORD secrets.", flush=True)
        return False

    print("Connecting to IQ Option...", flush=True)

    try:
        new_api = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
        connected, reason = new_api.connect()

        if not connected:
            print("CONNECTION FAILED:", reason, flush=True)
            return False

        new_api.change_balance(BALANCE_MODE)

        if not new_api.check_connect():
            print("Connection verification failed.", flush=True)
            return False

        api = new_api

        print("IQ OPTION CONNECTED", flush=True)
        print("Requested balance mode:", BALANCE_MODE, flush=True)

        send_telegram(
            "MOMENTUM 10 CONNECTED\n"
            "Requested mode: PRACTICE\n"
            "Stake: $1\n"
            "Daily net profit target: +$10\n"
            "Daily net loss limit: -$5\n"
            "Maximum unresolved trades: 1"
        )

        return True

    except Exception as exc:
        print("CONNECTION ERROR:", exc, flush=True)
        api = None
        return False


# ============================================================
# OTC ASSET DISCOVERY
# ============================================================

def get_otc_assets():
    global otc_assets, otc_active_ids

    print("Discovering OTC assets...", flush=True)

    try:
        try:
            init_data = api.get_all_init_v2()
        except Exception:
            init_data = None

        new_assets = []
        new_ids = {}

        def collect(sections):
            for section in sections:
                if not isinstance(section, dict):
                    continue

                actives = section.get("actives", {})
                if not isinstance(actives, dict):
                    continue

                for key, info in actives.items():
                    if not isinstance(info, dict):
                        continue

                    name = str(info.get("name", ""))
                    if "." in name:
                        name = name.split(".", 1)[1]

                    if "-OTC" not in name:
                        continue

                    if info.get("enabled") is False:
                        continue

                    if info.get("is_suspended") is True:
                        continue

                    try:
                        active_id = int(info.get("active_id", key))
                    except Exception:
                        continue

                    if name not in new_ids:
                        new_assets.append(name)
                        new_ids[name] = active_id

                    if len(new_assets) >= MAX_ASSETS:
                        return

        if isinstance(init_data, dict):
            collect([
                init_data.get("turbo", {}),
                init_data.get("binary", {}),
            ])

        if not new_assets:
            try:
                legacy = api.get_all_init()
            except Exception:
                legacy = None

            if isinstance(legacy, dict):
                result = legacy.get("result", {})
                if isinstance(result, dict):
                    collect([
                        result.get("turbo", {}),
                        result.get("binary", {}),
                    ])

        if not new_assets:
            print("No OPEN OTC assets found.", flush=True)
            return False

        otc_assets = new_assets
        otc_active_ids = new_ids

        for name, active_id in new_ids.items():
            OP_code.ACTIVES[name] = active_id

        print("OTC assets ready:", len(otc_assets), flush=True)
        return True

    except Exception as exc:
        print("OTC DISCOVERY ERROR:", exc, flush=True)
        return False


# ============================================================
# CANDLE DATA
# ============================================================

def get_candles(asset, count):
    if api is None:
        return []

    active_id = otc_active_ids.get(asset)
    if active_id is None:
        return []

    try:
        api.api.candles.candles_data = []

        server_time = time.time()
        try:
            server_time = api.api.timesync.server_timestamp
        except Exception:
            pass

        api.api.getcandles(
            active_id, CANDLE_SECONDS, count, server_time
        )

        started = time.time()

        while time.time() - started < CANDLE_REQUEST_TIMEOUT:
            candles = api.api.candles.candles_data
            if candles:
                break
            time.sleep(0.1)

        result = []

        for candle in api.api.candles.candles_data:
            try:
                result.append({
                    "from": float(candle["from"]),
                    "open": float(candle["open"]),
                    "close": float(candle["close"]),
                    "high": float(candle["max"]),
                    "low": float(candle["min"]),
                })
            except Exception:
                continue

        result.sort(key=lambda item: item["from"])
        return result

    except Exception as exc:
        print("CANDLE ERROR:", asset, exc, flush=True)
        return []


def remove_open_candle(candles):
    if len(candles) < 2:
        return candles

    if time.time() - candles[-1]["from"] < CANDLE_SECONDS:
        return candles[:-1]

    return candles


# ============================================================
# MOMENTUM 10 EXTREME-REVERSAL STRATEGY
# ============================================================

def calculate_momentum(candles):
    values = []

    if len(candles) <= MOMENTUM_PERIOD:
        return values

    for index in range(MOMENTUM_PERIOD, len(candles)):
        current = candles[index]["close"]
        previous = candles[index - MOMENTUM_PERIOD]["close"]

        if previous == 0:
            continue

        values.append((current / previous) * 100.0)

    return values


def analyze_momentum(candles):
    if len(candles) < MOMENTUM_LOOKBACK + 3:
        return None

    values = calculate_momentum(candles)

    if len(values) < MOMENTUM_LOOKBACK + 3:
        return None

    recent = values[-MOMENTUM_LOOKBACK:]
    current = values[-1]
    previous = values[-2]
    previous_previous = values[-3]

    sorted_values = sorted(recent)

    low_index = int(len(sorted_values) * EXTREME_PERCENTILE)
    high_index = int(len(sorted_values) * (1.0 - EXTREME_PERCENTILE))
    high_index = min(high_index, len(sorted_values) - 1)

    low_threshold = sorted_values[low_index]
    high_threshold = sorted_values[high_index]

    recent_low = min(recent)
    recent_high = max(recent)

    signal = None
    extreme = None
    reversal_strength = 0.0

    if current <= low_threshold:
        extreme = "LOW"

        if current > previous and previous < previous_previous:
            distance = current - previous

            if distance >= MIN_TURN_DISTANCE:
                signal = "CALL"
                reversal_strength = distance / abs(recent_low) * 100.0

    elif current >= high_threshold:
        extreme = "HIGH"

        if current < previous and previous > previous_previous:
            distance = previous - current

            if distance >= MIN_TURN_DISTANCE:
                signal = "PUT"
                reversal_strength = distance / abs(recent_high) * 100.0

    return {
        "signal": signal,
        "extreme": extreme,
        "momentum": current,
        "previous": previous,
        "previous_previous": previous_previous,
        "recent_low": recent_low,
        "recent_high": recent_high,
        "low_threshold": low_threshold,
        "high_threshold": high_threshold,
        "reversal_strength": reversal_strength,
    }


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def build_signal_message(asset, direction, analysis, price, signal_id):
    return (
        "MOMENTUM 10 SIGNAL\n"
        "Asset: " + asset + "\n"
        "Direction: " + direction + "\n"
        "Timeframe: 1M\n"
        "Expiry: 1 minute\n"
        "Strategy: Momentum 10 Extreme-Reversal\n"
        "Extreme: " + str(analysis["extreme"]) + "\n"
        "Momentum: " + str(round(analysis["momentum"], 5)) + "\n"
        "Previous: " + str(round(analysis["previous"], 5)) + "\n"
        "Previous 2: " + str(round(analysis["previous_previous"], 5)) + "\n"
        "Recent Low: " + str(round(analysis["recent_low"], 5)) + "\n"
        "Recent High: " + str(round(analysis["recent_high"], 5)) + "\n"
        "Low Threshold: " + str(round(analysis["low_threshold"], 5)) + "\n"
        "High Threshold: " + str(round(analysis["high_threshold"], 5)) + "\n"
        "Reversal Strength: "
        + str(round(analysis["reversal_strength"], 1)) + "%\n"
        "Price: " + str(price) + "\n"
        "Signal ID: " + signal_id
    )


# ============================================================
# TRADE RESULT MONITOR
# ============================================================

def monitor_trade(order_id, asset, direction, signal_id, entry_price):
    global pending_results, wins, losses, daily_net, safety_halt

    try:
        time.sleep(EXPIRY_MINUTES * 60 + 5)

        result = None

        for attempt in range(20):
            try:
                if api is not None and api.check_connect():
                    result = api.check_win_v4(order_id)
            except Exception as exc:
                print("RESULT CHECK WARNING:", exc, flush=True)

            if result is not None:
                break

            time.sleep(1)

        if result is None:
            with daily_net_lock:
                safety_halt = True
                save_daily_state()

            message = (
                "CRITICAL SAFETY HALT\n"
                "Trade result could not be verified.\n"
                "Asset: " + asset + "\n"
                "Order ID: " + str(order_id) + "\n"
                "No more trades are allowed.\n"
                "Check IQ Option trade history before resuming."
            )

            print(message, flush=True)
            send_telegram(message)
            return

        profit = float(result)

        # IQ Option's result is expected to be net profit/loss.
        # A zero result is treated conservatively as a $1 loss.
        if profit > 0:
            result_text = "WIN"
            net_change = profit
        else:
            result_text = "LOSS"
            net_change = -STAKE

        with daily_net_lock:
            if result_text == "WIN":
                wins += 1
            else:
                losses += 1

            daily_net = round(daily_net + net_change, 2)

            if daily_net <= -DAILY_LOSS_LIMIT:
                daily_net = min(daily_net, -DAILY_LOSS_LIMIT)

            current_net = daily_net
            save_daily_state()

        log_trade(
            asset, direction, signal_id, entry_price,
            result_text, net_change, current_net,
        )

        print(
            "RESULT:", asset, result_text,
            "Trade net:", net_change,
            "Daily net:", current_net,
            flush=True,
        )

        send_telegram(
            "MOMENTUM 10 RESULT\n"
            "Asset: " + asset + "\n"
            "Direction: " + direction + "\n"
            "Result: " + result_text + "\n"
            "Trade net: $" + str(round(net_change, 2)) + "\n"
            "Daily net: $" + str(current_net) + "\n"
            "Trades: " + str(trade_count) + "/" + str(TARGET_TRADES)
        )

        stopped, reason = daily_limit_reached()
        if stopped:
            announce_limit(reason)

    except Exception as exc:
        with daily_net_lock:
            safety_halt = True
            save_daily_state()

        print("RESULT MONITOR FAILED:", exc, flush=True)
        send_telegram(
            "CRITICAL SAFETY HALT: result monitor failed. "
            "Check broker history before restarting."
        )

    finally:
        with daily_net_lock:
            pending_results = max(0, pending_results - 1)


# ============================================================
# EXECUTE TRADE - ONE UNRESOLVED TRADE MAXIMUM
# ============================================================

def execute_trade(asset, direction, signal_id, entry_price):
    global trade_count, pending_results

    if not AUTO_TRADE:
        return False

    if not trade_lock.acquire(blocking=False):
        return False

    try:
        stopped, reason = daily_limit_reached()

        if stopped:
            announce_limit(reason)
            return False

        # Critical: do not open another trade until the previous
        # trade has a verified result.
        with daily_net_lock:
            if pending_results > 0:
                return False

            # Do not risk crossing the daily loss boundary.
            if daily_net - STAKE < -DAILY_LOSS_LIMIT:
                announce_reason = "Next trade could exceed the loss limit."
            else:
                announce_reason = ""

        if announce_reason:
            announce_limit(announce_reason)
            return False

        if api is None or not api.check_connect():
            print("Disconnected; trade skipped.", flush=True)
            return False

        print("Attempting practice trade:", asset, direction, flush=True)

        order_result = api.buy(
            STAKE, asset, direction.lower(), EXPIRY_MINUTES
        )

        if not isinstance(order_result, tuple) or len(order_result) < 2:
            # An uncertain order response must not trigger another order.
            with daily_net_lock:
                safety_halt = True
                save_daily_state()

            send_telegram(
                "CRITICAL SAFETY HALT\n"
                "Order response is uncertain.\n"
                "Check broker history before restarting."
            )
            return False

        success, order_id = order_result

        if not success or not order_id:
            print("TRADE NOT CONFIRMED:", asset, direction, flush=True)
            return False

        with daily_net_lock:
            trade_count += 1
            pending_results += 1
            save_daily_state()

        print("TRADE OPENED:", asset, direction, order_id, flush=True)

        send_telegram(
            "TRADE OPENED - PRACTICE\n"
            "Asset: " + asset + "\n"
            "Direction: " + direction + "\n"
            "Stake: $1\n"
            "Expiry: 1 minute\n"
            "Daily net: $" + str(round(daily_net, 2)) + "\n"
            "Signal ID: " + signal_id
        )

        thread = threading.Thread(
            target=monitor_trade,
            args=(order_id, asset, direction, signal_id, entry_price),
            daemon=False,
        )
        thread.start()
        return True

    except Exception as exc:
        print("TRADE EXCEPTION:", asset, direction, exc, flush=True)

        # An exception after order submission may leave the order
        # status uncertain. Halt instead of blindly submitting again.
        with daily_net_lock:
            safety_halt = True
            save_daily_state()

        send_telegram(
            "CRITICAL SAFETY HALT\n"
            "Trade submission status may be uncertain.\n"
            "Check broker history before restarting."
        )
        return False

    finally:
        trade_lock.release()


# ============================================================
# PROCESS ASSET
# ============================================================

def process_asset(asset):
    try:
        stopped, reason = daily_limit_reached()
        if stopped:
            announce_limit(reason)
            return

        # Scan signals, but never submit another trade while one
        # is still awaiting a verified result.
        print("Scanning:", asset, flush=True)

        candles = get_candles(
            asset, MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 10
        )

        if not candles:
            return

        candles = remove_open_candle(candles)

        if len(candles) < MOMENTUM_LOOKBACK + 3:
            return

        analysis = analyze_momentum(candles)
        if not analysis or analysis["signal"] is None:
            return

        signal = analysis["signal"]
        candle_id = candles[-1]["from"]

        if last_signal_candle.get(asset) == candle_id:
            return

        if extreme_state.get(asset) == analysis["extreme"]:
            return

        last_signal_candle[asset] = candle_id
        extreme_state[asset] = analysis["extreme"]

        price = candles[-1]["close"]

        signal_id = (
            "M10-" + asset.replace("-", "") + "-"
            + signal + "-" + str(int(time.time()))
        )

        message = build_signal_message(
            asset, signal, analysis, price, signal_id
        )

        print(message, flush=True)
        send_telegram(message)

        if AUTO_TRADE:
            execute_trade(asset, signal, signal_id, price)

    except Exception as exc:
        print("PROCESS ASSET ERROR:", asset, exc, flush=True)


# ============================================================
# HEARTBEAT
# ============================================================

def heartbeat():
    with daily_net_lock:
        current_net = daily_net
        current_trades = trade_count
        current_wins = wins
        current_losses = losses
        current_pending = pending_results
        current_halt = safety_halt

    resolved = current_wins + current_losses
    win_rate = current_wins / resolved * 100.0 if resolved else 0.0

    print("", flush=True)
    print("MOMENTUM 10 BOT ALIVE", flush=True)
    print("Connection:", "OK" if api and api.check_connect() else "CHECK",
          flush=True)
    print("Balance mode requested:", BALANCE_MODE, flush=True)
    print("Auto trading:", AUTO_TRADE, flush=True)
    print("OTC assets:", len(otc_assets), flush=True)
    print("Trades:", str(current_trades) + "/" + str(TARGET_TRADES), flush=True)
    print("Resolved:", resolved, flush=True)
    print("Pending:", current_pending, flush=True)
    print("Wins:", current_wins, flush=True)
    print("Losses:", current_losses, flush=True)
    print("Win rate:", str(round(win_rate, 1)) + "%", flush=True)
    print("Daily net: $" + str(round(current_net, 2)), flush=True)
    print("Safety halt:", current_halt, flush=True)


# ============================================================
# MAIN
# ============================================================

def main():
    global api

    init_log()
    load_daily_state()

    print("MOMENTUM 10 BOT STARTING", flush=True)
    print("Mode:", BALANCE_MODE, flush=True)
    print("Stake: $", STAKE, flush=True)
    print("Daily net profit target: +$10", flush=True)
    print("Daily net loss limit: -$5", flush=True)
    print("Maximum unresolved trades: 1", flush=True)
    print("Trade limit:", TARGET_TRADES, flush=True)

    if safety_halt:
        announce_limit("Saved safety halt requires manual investigation.")
        return

    last_asset_refresh = 0
    last_heartbeat = 0
    stopped_reason = ""

    while not stop_event.is_set():
        check_new_day()

        stopped, reason = daily_limit_reached()

        if stopped:
            stopped_reason = reason
            announce_limit(reason)

            if pending_results == 0:
                break

            time.sleep(1)
            continue

        if api is None:
            if not connect_iq():
                time.sleep(RECONNECT_SECONDS)
                continue

        try:
            if not api.check_connect():
                print("IQ Option disconnected.", flush=True)
                api = None
                time.sleep(RECONNECT_SECONDS)
                continue
        except Exception:
            api = None
            time.sleep(RECONNECT_SECONDS)
            continue

        now = time.time()

        if not otc_assets or now - last_asset_refresh >= ASSET_REFRESH_SECONDS:
            if get_otc_assets():
                last_asset_refresh = time.time()
            else:
                time.sleep(RECONNECT_SECONDS)
                continue

        now = time.time()

        if now - last_heartbeat >= HEARTBEAT_SECONDS:
            heartbeat()
            last_heartbeat = now

        for asset in list(otc_assets):
            if stop_event.is_set():
                break

            stopped, reason = daily_limit_reached()
            if stopped:
                stopped_reason = reason
                announce_limit(reason)
                break

            process_asset(asset)
            time.sleep(SCAN_INTERVAL)

        time.sleep(SCAN_INTERVAL)

    if not stopped_reason:
        stopped_reason = "Scanner stopped."

    with daily_net_lock:
        summary = (
            "MOMENTUM 10 TEST STOPPED\n"
            "Reason: " + stopped_reason + "\n"
            "Daily net: $" + str(round(daily_net, 2)) + "\n"
            "Trades opened: " + str(trade_count) + "/" + str(TARGET_TRADES) + "\n"
            "Wins: " + str(wins) + "\n"
            "Losses: " + str(losses) + "\n"
            "Pending results: " + str(pending_results) + "\n"
            "Safety halt: " + str(safety_halt)
        )

    print(summary, flush=True)
    send_telegram(summary)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        stop_event.set()
        print("Bot stopped by user.", flush=True)
    except Exception as exc:
        print("FATAL ERROR:", exc, flush=True)
        send_telegram("FATAL SCANNER ERROR: " + str(exc))
        raise
