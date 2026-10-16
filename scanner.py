import os
import csv
import json
import time
import threading
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import iqoptionapi.constants as OP_code
from iqoptionapi.stable_api import IQ_Option

# MOMENTUM 10 CONTROLLED PRACTICE SCANNER
# Momentum 10 decides the entry. Momentum 20 confirms the same turn direction.
# Risk limits apply to each manual run: +$15 profit or -$5 loss, then stop.

WATCHLIST = [
    "EURUSD", "GBPUSD", "AUDUSD", "USDJPY",
    "EURUSD-OTC", "GBPUSD-OTC", "AUDUSD-OTC", "USDJPY-OTC",
]

MOMENTUM_PERIOD = 10
MOMENTUM_CONFIRM_PERIOD = 20
MOMENTUM_LOOKBACK = 50
EXTREME_PERCENTILE = 0.10
MIN_TURN_DISTANCE = 0.03

CANDLE_SECONDS = 60
EXPIRY_MINUTES = 1
AUTO_TRADE = True
BALANCE_MODE = "PRACTICE"
STAKE = 1.0

SESSION_PROFIT_TARGET = 15.0
SESSION_LOSS_LIMIT = 5.0
MAX_RESOLVED_TRADES = 50

SCAN_INTERVAL = 5
ASSET_REFRESH_SECONDS = 900
HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 30
CANDLE_REQUEST_TIMEOUT = 10
RESULT_RETRY_SECONDS = 15

LOG_FILE = "momentum_signal_log.csv"
STATE_FILE = "momentum_state.json"
LAGOS = ZoneInfo("Africa/Lagos")
SESSION_ID = (
    str(os.getenv("GITHUB_RUN_ID")) + "-" + str(os.getenv("GITHUB_RUN_ATTEMPT", "1"))
    if os.getenv("GITHUB_RUN_ID")
    else str(int(time.time()))
)

IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

api = None
asset_ids = {}
last_asset_refresh = 0.0
last_heartbeat = 0.0
candle_worker = None

stop_event = threading.Event()
state_lock = threading.RLock()
trade_lock = threading.Lock()
connection_lock = threading.Lock()

state = {
    "date": "",
    "session_id": "",
    "net_profit": 0.0,
    "wins": 0,
    "losses": 0,
    "draws": 0,
    "unknown": 0,
    "resolved": 0,
    "last_signal_candle": {},
    "active_order": None,
    "stop_reason": "",
}


def today_string():
    return datetime.now(LAGOS).strftime("%Y-%m-%d")


def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    try:
        response = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
        if response.status_code != 200:
            print("Telegram error:", response.status_code, flush=True)
    except Exception as exc:
        print("Telegram error:", exc, flush=True)


def persist_state_to_git():
    # For recovery after a cancelled GitHub runner, workflow needs contents: write.
    if os.getenv("GITHUB_ACTIONS", "").lower() != "true":
        return
    try:
        added = subprocess.run(
            ["git", "add", "-f", STATE_FILE],
            capture_output=True, text=True, timeout=10,
        )
        if added.returncode != 0:
            print("State git-add warning:", added.stderr.strip(), flush=True)
            return

        changed = subprocess.run(
            ["git", "diff", "--cached", "--quiet", "--", STATE_FILE],
            capture_output=True, text=True, timeout=10,
        )
        if changed.returncode == 0:
            return
        if changed.returncode != 1:
            print("State git-diff warning:", changed.stderr.strip(), flush=True)
            return

        subprocess.run(
            ["git", "config", "user.name", "github-actions[bot]"],
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "config", "user.email",
             "41898282+github-actions[bot]@users.noreply.github.com"],
            capture_output=True, timeout=10,
        )
        committed = subprocess.run(
            ["git", "commit", "-m", "Save Momentum scanner state"],
            capture_output=True, text=True, timeout=15,
        )
        if committed.returncode != 0:
            print("State commit warning:", committed.stderr.strip(), flush=True)
            return

        branch = os.getenv("GITHUB_REF_NAME", "main")
        pushed = subprocess.run(
            ["git", "push", "origin", "HEAD:" + branch],
            capture_output=True, text=True, timeout=25,
        )
        if pushed.returncode != 0:
            print("STATE PUSH FAILED:", pushed.stderr.strip(), flush=True)
            print("Enable workflow permission: contents: write.", flush=True)
    except Exception as exc:
        print("State persistence warning:", exc, flush=True)


def save_state():
    try:
        with state_lock:
            with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as file:
                json.dump(state, file, indent=2)
            os.replace(STATE_FILE + ".tmp", STATE_FILE)
        persist_state_to_git()
    except Exception as exc:
        print("State save error:", exc, flush=True)


def reset_session_counters():
    state["date"] = today_string()
    state["session_id"] = SESSION_ID
    state["net_profit"] = 0.0
    state["wins"] = 0
    state["losses"] = 0
    state["draws"] = 0
    state["unknown"] = 0
    state["resolved"] = 0
    state["stop_reason"] = ""


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as file:
                saved = json.load(file)
            if isinstance(saved, dict):
                for key in state:
                    if key in saved:
                        state[key] = saved[key]
        except Exception as exc:
            raise RuntimeError(
                "Cannot safely read momentum_state.json. Check the file before running."
            ) from exc

    if not isinstance(state.get("last_signal_candle"), dict):
        state["last_signal_candle"] = {}
    if not isinstance(state.get("active_order"), (dict, type(None))):
        state["active_order"] = None

    # Each new manual workflow run gets fresh $15 / -$5 limits.
    # A saved unresolved order is retained and checked before another trade.
    if state.get("session_id") != SESSION_ID or state.get("date") != today_string():
        reset_session_counters()

    save_state()


def log_trade(values):
    fields = [
        "time", "date", "session_id", "asset", "direction", "momentum",
        "momentum20", "candle_time", "order_id", "result", "profit",
        "session_net",
    ]
    try:
        exists = os.path.exists(LOG_FILE)
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fields)
            if not exists or os.path.getsize(LOG_FILE) == 0:
                writer.writeheader()
            writer.writerow(values)
    except Exception as exc:
        print("CSV error:", exc, flush=True)


def normalize_name(value):
    return "".join(char for char in str(value).upper() if char.isalnum())


def as_active_id(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def find_asset_id(data, target):
    wanted = normalize_name(target)
    id_keys = (
        "active_id", "activeid", "id", "instrument_id",
        "instrumentid", "actives_id",
    )
    name_keys = ("name", "active", "symbol", "ticker", "instrument")

    if isinstance(data, list):
        for item in data:
            found = find_asset_id(item, target)
            if found is not None:
                return found
        return None
    if not isinstance(data, dict):
        return None

    names = []
    for key in name_keys:
        value = data.get(key)
        if isinstance(value, str):
            names.append(value)
    if any(normalize_name(name) == wanted for name in names):
        for key in id_keys:
            found = as_active_id(data.get(key))
            if found is not None:
                return found

    for key, value in data.items():
        if normalize_name(key) == "ACTIVES" and isinstance(value, dict):
            for active_key, info in value.items():
                if not isinstance(info, dict):
                    continue
                names = []
                for name_key in name_keys:
                    name_value = info.get(name_key)
                    if isinstance(name_value, str):
                        names.append(name_value)
                if any(normalize_name(name) == wanted for name in names):
                    for id_key in id_keys:
                        found = as_active_id(info.get(id_key))
                        if found is not None:
                            return found
                    found = as_active_id(active_key)
                    if found is not None:
                        return found
        if normalize_name(key) == wanted and isinstance(value, dict):
            for id_key in id_keys:
                found = as_active_id(value.get(id_key))
                if found is not None:
                    return found

    for value in data.values():
        found = find_asset_id(value, target)
        if found is not None:
            return found
    return None


def refresh_assets(reset=False):
    global asset_ids, last_asset_refresh
    if api is None:
        return False
    sources = []
    try:
        sources.append(api.get_all_init_v2())
    except Exception as exc:
        print("Init V2 refresh warning:", exc, flush=True)
    try:
        sources.append(api.get_all_init())
    except Exception as exc:
        print("Legacy init refresh warning:", exc, flush=True)

    discovered = {}
    for asset in WATCHLIST:
        active_id = None
        for source in sources:
            active_id = find_asset_id(source, asset)
            if active_id is not None:
                break
        if active_id is None:
            try:
                active_id = as_active_id(OP_code.ACTIVES.get(asset))
            except Exception:
                active_id = None
        if active_id is not None:
            discovered[asset] = active_id
            try:
                OP_code.ACTIVES[asset] = active_id
            except Exception:
                pass

    if discovered:
        asset_ids = discovered
    elif reset:
        asset_ids = {}
    last_asset_refresh = time.time()
    print("Asset refresh:", len(asset_ids), "of", len(WATCHLIST), "pairs found.", flush=True)
    for asset in WATCHLIST:
        if asset not in asset_ids:
            print("Pair not found in broker data:", asset, flush=True)
    return bool(asset_ids)


def connect_iq(notify=True):
    global api
    with connection_lock:
        if api is not None:
            try:
                if api.check_connect() and (asset_ids or refresh_assets()):
                    return True
            except Exception:
                pass
        if not IQ_EMAIL or not IQ_PASSWORD:
            print("Missing IQ_EMAIL or IQ_PASSWORD GitHub secrets.", flush=True)
            api = None
            return False
        try:
            print("Connecting to IQ Option...", flush=True)
            new_api = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
            connected, reason = new_api.connect()
            if not connected:
                print("IQ Option connection failed:", reason, flush=True)
                api = None
                return False
            new_api.change_balance(BALANCE_MODE)
            if not new_api.check_connect():
                print("Connection check failed.", flush=True)
                api = None
                return False
            api = new_api
            if not refresh_assets(True):
                print("Connected, but no watchlist IDs found. Will retry.", flush=True)
                api = None
                return False
            pairs = ", ".join(pair for pair in WATCHLIST if pair in asset_ids)
            print("Connected in", BALANCE_MODE, "mode. Pairs:", pairs, flush=True)
            if notify:
                telegram(
                    "Momentum scanner connected in PRACTICE mode\n"
                    "Pairs: " + pairs + "\nStake: $1 | Expiry: 1 minute\n"
                    "Run target: +$15 | Run loss stop: -$5\nMax resolved trades: 50"
                )
            return True
        except Exception as exc:
            print("IQ Option connection error:", exc, flush=True)
            api = None
            return False


def candle_request_worker(api_instance, active_id, count, result):
    try:
        candle_api = api_instance.api.candles
        candle_api.candles_data = []
        try:
            server_time = api_instance.get_server_timestamp()
        except Exception:
            server_time = time.time()
        api_instance.api.getcandles(active_id, CANDLE_SECONDS, count, server_time)
        started = time.time()
        while time.time() - started < CANDLE_REQUEST_TIMEOUT:
            candles = candle_api.candles_data
            if candles and len(candles) >= count:
                result["candles"] = list(candles)
                return
            time.sleep(0.2)
        result["candles"] = list(candle_api.candles_data or [])
    except Exception as exc:
        result["error"] = str(exc)


def get_candles(asset, count):
    global candle_worker
    if api is None:
        return []
    active_id = asset_ids.get(asset)
    if active_id is None:
        return []
    if candle_worker is not None and candle_worker.is_alive():
        return []
    result = {"candles": [], "error": ""}
    worker = threading.Thread(
        target=candle_request_worker,
        args=(api, active_id, count, result),
        daemon=True,
    )
    candle_worker = worker
    worker.start()
    worker.join(CANDLE_REQUEST_TIMEOUT + 2)
    if worker.is_alive():
        print("Candle request timed out for", asset, flush=True)
        return []
    if result["error"]:
        print("Candle error for", asset, ":", result["error"], flush=True)
        return []
    try:
        candles = sorted(result["candles"], key=lambda item: float(item.get("from", 0)))
        now = time.time()
        return [
            candle for candle in candles
            if float(candle.get("from", 0)) + CANDLE_SECONDS <= now - 2
        ]
    except Exception as exc:
        print("Candle validation error:", exc, flush=True)
        return []


def calculate_signal(candles):
    required = MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 2
    if len(candles) < required:
        return None
    try:
        closes = [float(item["close"]) for item in candles]
        opens = [float(item["open"]) for item in candles]
        candle_times = [int(item["from"]) for item in candles]
    except Exception:
        print("Invalid candle data.", flush=True)
        return None

    momentums = []
    for index in range(MOMENTUM_PERIOD, len(closes)):
        old_price = closes[index - MOMENTUM_PERIOD]
        if old_price <= 0:
            continue
        momentums.append((closes[index] - old_price) / old_price * 100.0)
    if len(momentums) < MOMENTUM_LOOKBACK + 1:
        return None

    recent = momentums[-(MOMENTUM_LOOKBACK + 1):-1]
    current = momentums[-1]
    previous = momentums[-2]
    ordered = sorted(recent)
    low_index = int((len(ordered) - 1) * EXTREME_PERCENTILE)
    high_index = int((len(ordered) - 1) * (1.0 - EXTREME_PERCENTILE))
    low_limit = ordered[low_index]
    high_limit = ordered[high_index]
    if abs(current - previous) < MIN_TURN_DISTANCE:
        return None

    # Momentum 20 is confirmation only. It cannot create an entry by itself.
    old20_now = closes[-1 - MOMENTUM_CONFIRM_PERIOD]
    old20_previous = closes[-2 - MOMENTUM_CONFIRM_PERIOD]
    if old20_now <= 0 or old20_previous <= 0:
        return None
    momentum20_current = (closes[-1] - old20_now) / old20_now * 100.0
    momentum20_previous = (closes[-2] - old20_previous) / old20_previous * 100.0
    momentum20_rising = momentum20_current > momentum20_previous
    momentum20_falling = momentum20_current < momentum20_previous

    last_open = opens[-1]
    last_close = closes[-1]
    direction = None
    # Momentum 10 extreme and turn selects the entry; Momentum 20 confirms the turn.
    if (
        current <= low_limit
        and current > previous
        and momentum20_rising
        and last_close > last_open
    ):
        direction = "call"
    elif (
        current >= high_limit
        and current < previous
        and momentum20_falling
        and last_close < last_open
    ):
        direction = "put"
    if direction is None:
        return None
    return {
        "direction": direction,
        "momentum": round(current, 5),
        "previous": round(previous, 5),
        "momentum20": round(momentum20_current, 5),
        "momentum20_previous": round(momentum20_previous, 5),
        "candle_time": candle_times[-1],
    }


def risk_status():
    with state_lock:
        net = float(state.get("net_profit", 0.0))
        resolved = int(state.get("resolved", 0))
    if net >= SESSION_PROFIT_TARGET:
        return False, "Run profit target reached (+$15)."
    if net <= -SESSION_LOSS_LIMIT:
        return False, "Run loss limit reached (-$5)."
    if resolved >= MAX_RESOLVED_TRADES:
        return False, "Maximum 50 resolved trades reached."
    return True, ""


def stop_for_risk(reason):
    # Stop the scan immediately, before any slow state-save or Git push.
    stop_event.set()
    with state_lock:
        state["stop_reason"] = reason
    save_state()
    print("RISK STOP:", reason, flush=True)
    telegram("RISK STOP\n" + reason + "\nNo further trades will be opened in this run.")


def monitor_trade(order):
    order_id = order["order_id"]
    asset = order.get("asset", "EURUSD-OTC")
    direction = order.get("direction", "call")
    opened_at = float(order.get("opened_at", time.time()))
    trade_session = str(order.get("session_id", ""))
    trade_date = order.get("trade_date", today_string())
    wait_until = opened_at + EXPIRY_MINUTES * 60 + 5

    while not stop_event.is_set():
        delay = wait_until - time.time()
        if delay > 0:
            stop_event.wait(min(delay, 5))
            continue
        try:
            if api is None or not api.check_connect():
                if not connect_iq(False):
                    stop_event.wait(RECONNECT_SECONDS)
                    continue
            result = api.check_win_v4(order_id)
            if result is None:
                raise RuntimeError("Trade result is not available yet.")
            broker_profit = float(result)

            # Any zero/negative result is booked as a full $1 stake loss, per user rule.
            if broker_profit > 0:
                outcome = "WIN"
                net_change = round(broker_profit, 2)
            else:
                outcome = "LOSS"
                net_change = -STAKE

            same_session = trade_session == SESSION_ID
            with state_lock:
                if same_session:
                    state["net_profit"] = round(float(state["net_profit"]) + net_change, 2)
                    if outcome == "WIN":
                        state["wins"] = int(state["wins"]) + 1
                    else:
                        state["losses"] = int(state["losses"]) + 1
                    state["resolved"] = int(state["resolved"]) + 1
                state["active_order"] = None
                daily_net = round(float(state.get("net_profit", 0.0)), 2)
                resolved = int(state.get("resolved", 0))
                wins_now = int(state.get("wins", 0))
                losses_now = int(state.get("losses", 0))
            save_state()

            log_trade({
                "time": datetime.now(LAGOS).isoformat(),
                "date": trade_date,
                "session_id": trade_session,
                "asset": asset,
                "direction": direction.upper(),
                "momentum": order.get("momentum", ""),
                "momentum20": order.get("momentum20", ""),
                "candle_time": order.get("candle_time", ""),
                "order_id": order_id,
                "result": outcome,
                "profit": net_change,
                "session_net": daily_net,
            })
            message = (
                "MOMENTUM SCANNER RESULT\n"
                "Asset: " + asset + "\n"
                "Direction: " + direction.upper() + "\n"
                "Result: " + outcome + "\n"
                "Net trade result: $" + str(round(net_change, 2)) + "\n"
                "Run net: $" + str(daily_net) + "\n"
                "Resolved: " + str(resolved) + "/" + str(MAX_RESOLVED_TRADES) + "\n"
                "Wins: " + str(wins_now) + " | Losses: " + str(losses_now)
            )
            if not same_session:
                message += "\nPrior-run order: excluded from this run's risk totals."
            print(message, flush=True)
            telegram(message)

            if same_session:
                allowed, reason = risk_status()
                if not allowed:
                    stop_for_risk(reason)
            return
        except Exception as exc:
            print("Trade result unresolved; retrying:", exc, flush=True)
            stop_event.wait(RESULT_RETRY_SECONDS)


def place_trade(asset, direction, signal):
    if not AUTO_TRADE:
        print("Auto trading disabled.", flush=True)
        return False
    allowed, reason = risk_status()
    if not allowed:
        stop_for_risk(reason)
        return False
    if not trade_lock.acquire(blocking=False):
        print("Trade placement already in progress.", flush=True)
        return False
    try:
        allowed, reason = risk_status()
        if not allowed:
            stop_for_risk(reason)
            return False
        with state_lock:
            if state.get("active_order") is not None:
                print("Trade blocked: an order is unresolved.", flush=True)
                return False
        if api is None or not api.check_connect():
            print("Disconnected; trade skipped.", flush=True)
            return False
        if asset not in asset_ids:
            print("No active ID for", asset, "- trade skipped.", flush=True)
            return False

        print("PRACTICE balance:", api.get_balance(), flush=True)
        success, order_id = api.buy(STAKE, asset, direction, EXPIRY_MINUTES)
        if not success:
            print("Order rejected for", asset, ":", order_id, flush=True)
            telegram("Order rejected for " + asset + ". No trade recorded.")
            return False

        order = {
            "order_id": order_id,
            "asset": asset,
            "direction": direction,
            "momentum": signal["momentum"],
            "momentum20": signal.get("momentum20", ""),
            "candle_time": signal["candle_time"],
            "opened_at": time.time(),
            "trade_date": today_string(),
            "session_id": SESSION_ID,
        }
        with state_lock:
            state["active_order"] = order
        save_state()

        print("PRACTICE order accepted:", asset, direction.upper(), order_id, flush=True)
        telegram(
            "PRACTICE TRADE OPENED\n"
            "Asset: " + asset + "\nDirection: " + direction.upper() + "\n"
            "Stake: $1 | Expiry: 1 minute\n"
            "Run target: +$15 | Run loss stop: -$5\nOrder ID: " + str(order_id)
        )
        thread = threading.Thread(target=monitor_trade, args=(order,), daemon=True)
        thread.start()
        return True
    except Exception as exc:
        print("Trade placement error:", exc, flush=True)
        telegram("Trade placement error: " + str(exc))
        return False
    finally:
        trade_lock.release()


def heartbeat():
    global last_heartbeat
    now = time.time()
    if now - last_heartbeat < HEARTBEAT_SECONDS:
        return
    last_heartbeat = now
    with state_lock:
        active = state.get("active_order")
        print(
            "HEARTBEAT | Run:", state.get("session_id"),
            "| Pairs:", len(asset_ids), "/", len(WATCHLIST),
            "| Resolved:", state.get("resolved"),
            "| Wins:", state.get("wins"),
            "| Losses:", state.get("losses"),
            "| Net: $", state.get("net_profit"),
            "| Active order:", active.get("asset") if isinstance(active, dict) else "None",
            flush=True,
        )


def run_scanner():
    global last_asset_refresh
    load_state()
    print("Momentum 10/20 scanner starting in PRACTICE mode.", flush=True)
    print("Run ID:", SESSION_ID, flush=True)
    print("Watchlist:", ", ".join(WATCHLIST), flush=True)
    print("Run profit target: +$15 | Run loss stop: -$5", flush=True)
    print("Maximum resolved trades:", MAX_RESOLVED_TRADES, flush=True)

    if not connect_iq(True):
        print("Initial connection not ready. Scanner will retry.", flush=True)

    with state_lock:
        saved_order = state.get("active_order")
    if isinstance(saved_order, dict):
        print("Saved unresolved order found; checking it before new entries.", flush=True)
        thread = threading.Thread(target=monitor_trade, args=(saved_order,), daemon=True)
        thread.start()

    while not stop_event.is_set():
        try:
            if api is None or not api.check_connect():
                if not connect_iq(False):
                    stop_event.wait(RECONNECT_SECONDS)
                    continue
        except Exception:
            if not connect_iq(False):
                stop_event.wait(RECONNECT_SECONDS)
                continue

        if time.time() - last_asset_refresh >= ASSET_REFRESH_SECONDS:
            refresh_assets()

        allowed, reason = risk_status()
        if not allowed:
            stop_for_risk(reason)
            break

        with state_lock:
            active = state.get("active_order")
        if active is not None:
            heartbeat()
            stop_event.wait(SCAN_INTERVAL)
            continue

        for asset in WATCHLIST:
            if stop_event.is_set():
                break
            with state_lock:
                if state.get("active_order") is not None:
                    break
            allowed, reason = risk_status()
            if not allowed:
                stop_for_risk(reason)
                break
            if asset not in asset_ids:
                continue

            candles = get_candles(asset, MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 5)
            if not candles:
                continue
            signal = calculate_signal(candles)
            if signal is None:
                continue

            signal_time = int(signal["candle_time"])
            with state_lock:
                previous_signal = state["last_signal_candle"].get(asset)
                duplicate = previous_signal == signal_time
                if not duplicate:
                    state["last_signal_candle"][asset] = signal_time
            if not duplicate:
                save_state()
            if duplicate:
                continue

            message = (
                "MOMENTUM 10 SIGNAL (MOMENTUM 20 CONFIRMED)\n"
                "Asset: " + asset + "\nDirection: " + signal["direction"].upper() + "\n"
                "Timeframe: 1 minute | Expiry: 1 minute\n"
                "Momentum 10: " + str(signal["momentum"]) + "\n"
                "Momentum 20: " + str(signal["momentum20"]) + "\nMode: PRACTICE"
            )
            print(message, flush=True)
            telegram(message)
            place_trade(asset, signal["direction"], signal)
            with state_lock:
                if state.get("active_order") is not None:
                    break
            if stop_event.is_set():
                break

        heartbeat()
        stop_event.wait(SCAN_INTERVAL)

    with state_lock:
        active = state.get("active_order")
        summary = (
            "MOMENTUM SCANNER STOPPED\n"
            "Run ID: " + str(state.get("session_id")) + "\n"
            "Resolved: " + str(state.get("resolved")) + "/" + str(MAX_RESOLVED_TRADES) + "\n"
            "Wins: " + str(state.get("wins")) + " | Losses: " + str(state.get("losses")) + "\n"
            "Run net: $" + str(state.get("net_profit")) + "\n"
            "Target: +$15 | Loss stop: -$5\n"
            "Stop reason: " + str(state.get("stop_reason") or "Manual/workflow stop") + "\n"
            "Unresolved order: " + str(active is not None)
        )
    save_state()
    print(summary, flush=True)
    telegram(summary)


if __name__ == "__main__":
    try:
        run_scanner()
    except KeyboardInterrupt:
        print("Scanner stopped by user.", flush=True)
        stop_event.set()
    except Exception as exc:
        print("Fatal scanner error:", exc, flush=True)
        telegram("Fatal scanner error: " + str(exc))
        raise
