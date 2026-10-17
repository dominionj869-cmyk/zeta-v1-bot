import os
import re
import csv
import json
import time
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import iqoptionapi.constants as OP_code
from iqoptionapi.stable_api import IQ_Option

# MOMENTUM 10 ONLY - PRACTICE MODE
# Reconstructed from the earliest settings available in project history.
# Signal rules use M10 extremes and a turn only: no M20 or engulfing filter.

IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BALANCE_MODE = "PRACTICE"
AUTO_TRADE = True
STAKE = 1.0
EXPIRY_MINUTES = 1
CANDLE_SECONDS = 60

MOMENTUM_PERIOD = 10
MOMENTUM_LOOKBACK = 50
EXTREME_PERCENTILE = 0.19
REQUIRE_TURN = True
MIN_TURN_DISTANCE = 0.19

SESSION_PROFIT_TARGET = 15.0
SESSION_LOSS_LIMIT = 5.0
MAX_RESOLVED_TRADES = 50

SCAN_INTERVAL = 5
ASSET_REFRESH_SECONDS = 900
HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 30
CANDLE_REQUEST_TIMEOUT = 10
ORDER_RESULT_RETRY_SECONDS = 5
CANDLE_REQUEST_COUNT = MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 12

LOG_FILE = "momentum_signal_log.csv"
STATE_FILE = "momentum_state.json"
LAGOS = ZoneInfo("Africa/Lagos")

CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNY",
    "HKD", "SGD", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "TRY",
    "ZAR", "MXN", "BRL", "INR", "THB", "AED", "SAR", "ILS", "KRW",
    "TWD", "RUB", "RON", "BGN", "ISK", "PHP", "IDR", "MYR"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)


def _disable_digital_open_worker(self):
    logging.info("Disabled unused IQ Option digital-market watcher")


def _safe_get_candles(self, active, interval, count, endtime):
    try:
        active_code = OP_code.ACTIVES.get(active)
        if active_code is None:
            logging.warning("No active ID found for %s", active)
            return None
        self.api.candles.candles_data = None
        self.api.getcandles(active_code, interval, count, endtime)
        deadline = time.time() + CANDLE_REQUEST_TIMEOUT
        while time.time() < deadline:
            candles = getattr(self.api.candles, "candles_data", None)
            if candles is not None:
                return candles
            try:
                if not self.check_connect():
                    return None
            except Exception:
                return None
            time.sleep(0.1)
        logging.warning("Candle request timed out for %s", active)
        return None
    except Exception as exc:
        logging.warning("Candle request failed for %s: %s", active, exc)
        return None


try:
    if hasattr(IQ_Option, "_IQ_Option__get_digital_open"):
        IQ_Option._IQ_Option__get_digital_open = _disable_digital_open_worker
        logging.info("Patched unused IQ Option digital-market watcher")
    IQ_Option.get_candles = _safe_get_candles
except Exception as exc:
    logging.warning("IQ Option safety patch failed: %s", exc)

IQ = None
ASSETS = []
ASSET_REFRESH_AT = 0.0
STOP_EVENT = threading.Event()
TRADE_LOCK = threading.Lock()

if os.getenv("GITHUB_RUN_ID"):
    SESSION_ID = str(os.getenv("GITHUB_RUN_ID")) + "-" + str(os.getenv("GITHUB_RUN_ATTEMPT", "1"))
else:
    SESSION_ID = str(int(time.time()))


def now_lagos():
    return datetime.now(LAGOS)


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_TOKEN)
    try:
        response = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=10
        )
        response.raise_for_status()
    except Exception as exc:
        logging.warning("Telegram send failed: %s", exc)


def fresh_state():
    return {
        "date": now_lagos().date().isoformat(),
        "session_id": SESSION_ID,
        "profit": 0.0,
        "wins": 0,
        "losses": 0,
        "resolved": 0,
        "pending_order_id": None,
        "pending_asset": None,
        "pending_direction": None,
        "pending_started": None,
        "last_signals": {}
    }


def load_state():
    state = fresh_state()
    saved = {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        if isinstance(saved, dict):
            state.update(saved)
    except (OSError, ValueError, TypeError):
        saved = {}

    # Each GitHub workflow run has its own +$15/-$5 session limits.
    # Keep an unresolved order and de-duplication history across a rerun.
    if saved.get("session_id") != SESSION_ID:
        pending = state.get("pending_order_id")
        asset = state.get("pending_asset")
        direction = state.get("pending_direction")
        started = state.get("pending_started")
        signals = state.get("last_signals", {})
        state = fresh_state()
        state["pending_order_id"] = pending
        state["pending_asset"] = asset
        state["pending_direction"] = direction
        state["pending_started"] = started
        state["last_signals"] = signals if isinstance(signals, dict) else {}

    if state.get("date") != now_lagos().date().isoformat():
        pending = state.get("pending_order_id")
        asset = state.get("pending_asset")
        direction = state.get("pending_direction")
        started = state.get("pending_started")
        state = fresh_state()
        state["pending_order_id"] = pending
        state["pending_asset"] = asset
        state["pending_direction"] = direction
        state["pending_started"] = started
    state.setdefault("last_signals", {})
    return state


STATE = load_state()


def save_state():
    try:
        with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as handle:
            json.dump(STATE, handle, indent=2)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except OSError as exc:
        logging.warning("Could not save state: %s", exc)


def log_trade(asset, signal, m10, status, profit=""):
    exists = os.path.exists(LOG_FILE)
    try:
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if not exists or os.path.getsize(LOG_FILE) == 0:
                writer.writerow([
                    "timestamp_lagos", "session_id", "asset", "signal",
                    "momentum10", "status", "profit"
                ])
            writer.writerow([
                now_lagos().isoformat(timespec="seconds"), SESSION_ID, asset,
                signal, "" if m10 is None else round(m10, 5), status, profit
            ])
    except OSError as exc:
        logging.warning("Could not write CSV log: %s", exc)


def normalise_forex_name(value):
    if not isinstance(value, str):
        return None, False
    raw = value.strip().upper()
    if not raw:
        return None, False
    raw = raw.split(".")[-1]
    is_otc = "OTC" in raw
    letters = re.sub(r"[^A-Z]", "", raw)
    for suffix in ("OTC", "BINARY", "TURBO", "DIGITAL"):
        if letters.endswith(suffix):
            letters = letters[:-len(suffix)]
    if len(letters) >= 6:
        pair = letters[-6:]
        if pair[:3] in CURRENCIES and pair[3:] in CURRENCIES:
            return pair, is_otc
    return None, False


def add_asset(result, name, active_id=None):
    pair, is_otc = normalise_forex_name(name)
    if not pair:
        return
    clean_name = pair + ("-OTC" if is_otc else "")
    resolved_id = None
    for candidate in (active_id, result.get(clean_name, {}).get("id")):
        try:
            if candidate is not None:
                resolved_id = int(candidate)
                break
        except (TypeError, ValueError):
            pass
    if resolved_id is None:
        try:
            candidate = OP_code.ACTIVES.get(clean_name)
            if candidate is not None:
                resolved_id = int(candidate)
        except (TypeError, ValueError, AttributeError):
            pass
    result[clean_name] = {
        "name": clean_name,
        "pair": pair,
        "otc": is_otc,
        "id": resolved_id
    }
    if resolved_id is not None:
        try:
            OP_code.ACTIVES[clean_name] = resolved_id
        except Exception:
            pass


def walk_asset_tree(node, result, parent_key=None):
    if isinstance(node, dict):
        name = node.get("name") or node.get("active_name") or node.get("symbol")
        active_id = node.get("id") or node.get("active_id") or node.get("activeId")
        if name:
            add_asset(result, str(name), active_id if active_id is not None else parent_key)
        elif isinstance(parent_key, str):
            add_asset(result, parent_key, active_id)
        for key, value in node.items():
            walk_asset_tree(value, result, key)
    elif isinstance(node, list):
        for item in node:
            walk_asset_tree(item, result, parent_key)


def refresh_assets(force=False):
    global ASSETS, ASSET_REFRESH_AT
    if not force and ASSETS and time.time() - ASSET_REFRESH_AT < ASSET_REFRESH_SECONDS:
        return ASSETS

    discovered = {}
    try:
        opcode_map = IQ.get_all_ACTIVES_OPCODE()
        if isinstance(opcode_map, dict):
            for name, active_id in opcode_map.items():
                add_asset(discovered, str(name), active_id)
    except Exception as exc:
        logging.warning("Active-code refresh failed: %s", exc)

    for method_name in ("get_all_init_v2", "get_all_init"):
        try:
            method = getattr(IQ, method_name, None)
            if method:
                walk_asset_tree(method(), discovered)
        except Exception as exc:
            logging.warning("%s failed: %s", method_name, exc)

    assets = list(discovered.values())
    assets.sort(key=lambda item: (item["otc"], item["name"]))
    if assets:
        ASSETS = assets
        ASSET_REFRESH_AT = time.time()
        normal_count = sum(not item["otc"] for item in assets)
        otc_count = sum(item["otc"] for item in assets)
        logging.info("Forex assets found: %s total (%s normal, %s OTC)", len(assets), normal_count, otc_count)
        send_telegram(
            "<b>Forex assets refreshed</b>\nTotal: {}\nNormal: {}\nOTC: {}".format(
                len(assets), normal_count, otc_count)
        )
    else:
        logging.warning("No Forex pairs identified from IQ Option active data")
    return ASSETS


def connect_iq():
    global IQ
    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError("Missing IQ_EMAIL or IQ_PASSWORD GitHub secret")
    api = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    last_error = "unknown reason"
    for attempt in range(1, 6):
        try:
            result = api.connect()
            ok = result[0] if isinstance(result, tuple) else bool(result)
            if not ok:
                last_error = result[1] if isinstance(result, tuple) and len(result) > 1 else "connection rejected"
                raise ConnectionError(str(last_error))
            api.change_balance(BALANCE_MODE)
            time.sleep(1)
            if not api.check_connect():
                raise ConnectionError("connection dropped after login")
            IQ = api
            logging.info("Connected to IQ Option in PRACTICE mode")
            send_telegram(
                "<b>Momentum 10-only scanner connected</b>\n"
                "Mode: PRACTICE\nStake: ${:.2f}\nExpiry: {} minute\n"
                "Session target: +${:.2f}\nSession stop: -${:.2f}\n"
                "Signal: M10 extreme + turn only".format(
                    STAKE, EXPIRY_MINUTES, SESSION_PROFIT_TARGET, SESSION_LOSS_LIMIT)
            )
            return
        except Exception as exc:
            last_error = str(exc)
            logging.warning("Connection attempt %s/5 failed: %s", attempt, last_error)
            if attempt < 5:
                time.sleep(RECONNECT_SECONDS)
    raise RuntimeError("Could not connect after 5 attempts: " + last_error)


def momentum_series(closes, period):
    values = []
    for index in range(period, len(closes)):
        old = closes[index - period]
        current = closes[index]
        if old <= 0:
            values.append(0.0)
        else:
            values.append(((current / old) - 1.0) * 100.0)
    return values


def percentile_value(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = int((len(ordered) - 1) * fraction)
    index = max(0, min(len(ordered) - 1, index))
    return ordered[index]


def get_closed_candles(asset):
    candles = IQ.get_candles(asset, CANDLE_SECONDS, CANDLE_REQUEST_COUNT, int(time.time()))
    if not isinstance(candles, list):
        return []
    candles = sorted(candles, key=lambda row: int(row.get("from", 0)))
    current_time = int(time.time())
    closed = []
    for candle in candles:
        try:
            start = int(candle.get("from", 0))
            if start + CANDLE_SECONDS <= current_time:
                closed.append(candle)
        except (TypeError, ValueError):
            continue
    return closed


def evaluate_signal(candles):
    """M10-only: percentile extreme, minimum turn and optional turn direction."""
    minimum = MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 2
    if len(candles) < minimum:
        return None
    try:
        closes = [float(row["close"]) for row in candles]
        candle_time = int(candles[-1].get("from", 0))
    except (KeyError, TypeError, ValueError):
        return None

    values = momentum_series(closes, MOMENTUM_PERIOD)
    if len(values) < MOMENTUM_LOOKBACK + 1:
        return None

    current = values[-1]
    previous = values[-2]
    history = values[-(MOMENTUM_LOOKBACK + 1):-1]
    low_extreme = percentile_value(history, EXTREME_PERCENTILE)
    high_extreme = percentile_value(history, 1.0 - EXTREME_PERCENTILE)

    turn_distance = abs(current - previous)
    if turn_distance < MIN_TURN_DISTANCE:
        return None

    turning_up = current > previous
    turning_down = current < previous

    if current <= low_extreme and (turning_up or not REQUIRE_TURN):
        return {
            "direction": "call",
            "momentum": round(current, 5),
            "previous": round(previous, 5),
            "candle_time": candle_time,
            "extreme": round(low_extreme, 5)
        }
    if current >= high_extreme and (turning_down or not REQUIRE_TURN):
        return {
            "direction": "put",
            "momentum": round(current, 5),
            "previous": round(previous, 5),
            "candle_time": candle_time,
            "extreme": round(high_extreme, 5)
        }
    return None


def format_stats():
    return "Profit: {:+.2f}\nWins: {}\nLosses: {}\nResolved: {}/{}".format(
        float(STATE.get("profit", 0.0)),
        int(STATE.get("wins", 0)),
        int(STATE.get("losses", 0)),
        int(STATE.get("resolved", 0)), MAX_RESOLVED_TRADES
    )


def risk_stop_reason():
    profit = float(STATE.get("profit", 0.0))
    if profit >= SESSION_PROFIT_TARGET:
        return "Session profit target reached"
    if profit <= -SESSION_LOSS_LIMIT:
        return "Session loss limit reached"
    if int(STATE.get("resolved", 0)) >= MAX_RESOLVED_TRADES:
        return "Maximum resolved-trade limit reached"
    return None


def extract_numeric_profit(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number == number and abs(number) != float("inf"):
            return number
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {
            "false", "none", "null", "pending", "open", "win", "won",
            "loss", "lost", "loose", "draw", "unknown"
        }:
            return None
        try:
            number = float(text)
            if number == number and abs(number) != float("inf"):
                return number
        except (TypeError, ValueError, OverflowError):
            return None
        return None
    if isinstance(value, dict):
        for key in (
            "profit", "profit_amount", "profitAmount", "close_profit",
            "closeProfit", "net_profit", "netProfit", "win_amount", "winAmount"
        ):
            if key in value:
                result = extract_numeric_profit(value.get(key))
                if result is not None:
                    return result
        for key in ("result", "data", "betinfo", "bet_info", "option"):
            nested = value.get(key)
            if isinstance(nested, (dict, list, tuple)):
                result = extract_numeric_profit(nested)
                if result is not None:
                    return result
        if isinstance(value.get("result"), (int, float, str)):
            result = extract_numeric_profit(value.get("result"))
            if result is not None:
                return result
    if isinstance(value, (list, tuple)):
        for item in value:
            result = extract_numeric_profit(item)
            if result is not None:
                return result
    return None


def query_order_profit(order_id):
    for name in ("check_win_v4", "check_win", "get_betinfo", "get_betinfo_v2"):
        method = getattr(IQ, name, None)
        if not callable(method):
            continue
        try:
            raw = method(order_id)
        except Exception as exc:
            logging.debug("%s failed for order %s: %s", name, order_id, exc)
            continue
        value = extract_numeric_profit(raw)
        if value is not None:
            logging.info("Order %s resolved through %s: %+.2f", order_id, name, value)
            return value
    return None


def settle_result(asset, direction, broker_profit, m10=None):
    if broker_profit is None:
        raise ValueError("No confirmed numeric broker result")

    # As requested in the earlier version, a zero result counts as a $1 loss
    # for both statistics and the session risk limits.
    if broker_profit > 0:
        net_change = float(broker_profit)
        label = "WIN"
    else:
        net_change = -STAKE
        label = "LOSS (zero counted as loss)" if broker_profit == 0 else "LOSS"

    STATE["profit"] = round(float(STATE.get("profit", 0.0)) + net_change, 2)
    STATE["resolved"] = int(STATE.get("resolved", 0)) + 1
    if label == "WIN":
        STATE["wins"] = int(STATE.get("wins", 0)) + 1
    else:
        STATE["losses"] = int(STATE.get("losses", 0)) + 1

    STATE["pending_order_id"] = None
    STATE["pending_asset"] = None
    STATE["pending_direction"] = None
    STATE["pending_started"] = None
    save_state()
    log_trade(asset, direction, m10, label, round(net_change, 2))

    message = "<b>Trade result: {}</b>\nAsset: {}\nDirection: {}\nNet result: ${:+.2f}\n{}".format(
        label, asset, direction.upper(), net_change, format_stats()
    )
    send_telegram(message)
    logging.info("%s | %s %s | %+0.2f | %s", label, asset, direction, net_change, format_stats())

    reason = risk_stop_reason()
    if reason:
        STOP_EVENT.set()
        send_telegram("<b>Scanner stopped</b>\n{}\n{}".format(reason, format_stats()))


def wait_for_order_result(order_id, asset, direction, m10):
    last_notice = 0.0
    while not STOP_EVENT.is_set():
        value = query_order_profit(order_id)
        if value is not None:
            settle_result(asset, direction, value, m10)
            return
        now = time.time()
        if now - last_notice >= 60:
            logging.warning("Order %s result is not readable. Keeping it pending; no new order will be placed.", order_id)
            last_notice = now
        time.sleep(ORDER_RESULT_RETRY_SECONDS)


def recover_pending_order():
    order_id = STATE.get("pending_order_id")
    if not order_id:
        return
    asset = STATE.get("pending_asset") or "UNKNOWN"
    direction = STATE.get("pending_direction") or "unknown"
    logging.warning("Recovering unresolved order %s (%s %s)", order_id, asset, direction)
    send_telegram("<b>Recovering unresolved order</b>\n{}\n{}\nNo new trade until it resolves.".format(order_id, asset))
    wait_for_order_result(order_id, asset, direction, None)


def place_trade(asset_info, signal):
    asset = asset_info["name"]
    direction = signal["direction"]
    with TRADE_LOCK:
        if STATE.get("pending_order_id"):
            return
        if risk_stop_reason():
            STOP_EVENT.set()
            return

        active_id = asset_info.get("id")
        if active_id is not None:
            try:
                OP_code.ACTIVES[asset] = int(active_id)
            except Exception:
                pass
        try:
            result = IQ.buy(STAKE, asset, direction, EXPIRY_MINUTES)
        except Exception as exc:
            log_trade(asset, direction, signal.get("momentum"), "ORDER_ERROR", "")
            logging.warning("Order failed for %s: %s", asset, exc)
            return

        if isinstance(result, tuple):
            success = bool(result[0])
            order_id = result[1] if len(result) > 1 else None
        else:
            success = bool(result)
            order_id = None
        if not success or order_id is None:
            log_trade(asset, direction, signal.get("momentum"), "ORDER_REJECTED", "")
            logging.warning("Broker rejected order on %s: %r", asset, result)
            return

        STATE["pending_order_id"] = order_id
        STATE["pending_asset"] = asset
        STATE["pending_direction"] = direction
        STATE["pending_started"] = now_lagos().isoformat(timespec="seconds")
        save_state()
        log_trade(asset, direction, signal.get("momentum"), "OPENED", "")
        send_telegram(
            "<b>Momentum 10 trade opened</b>\nAsset: {}\nDirection: {}\n"
            "Stake: ${:.2f}\nExpiry: {} minute\nM10: {:.5f}\nMode: PRACTICE\n{}".format(
                asset, direction.upper(), STAKE, EXPIRY_MINUTES,
                signal["momentum"], format_stats())
        )
        logging.info("Order opened: %s %s id=%s", asset, direction.upper(), order_id)

    wait_for_order_result(order_id, asset, direction, signal.get("momentum"))


def heartbeat(last_heartbeat):
    if time.time() - last_heartbeat < HEARTBEAT_SECONDS:
        return last_heartbeat
    message = "<b>Momentum 10 heartbeat</b>\n{}\nAssets: {}\n{}".format(
        now_lagos().strftime("%Y-%m-%d %H:%M:%S WAT"), len(ASSETS), format_stats())
    send_telegram(message)
    logging.info(message.replace("<b>", "").replace("</b>", "").replace("\n", " | "))
    return time.time()


def main():
    global ASSET_REFRESH_AT
    connect_iq()
    refresh_assets(force=True)
    if not ASSETS:
        raise RuntimeError("No Forex assets found in IQ Option active data")

    if STATE.get("pending_order_id"):
        recover_pending_order()
    if STOP_EVENT.is_set():
        logging.info("Stopped while recovering pending order")
        return

    send_telegram(
        "<b>Momentum 10-only scanner started</b>\n"
        "Signal: M10 percentile extreme + turning direction\n"
        "Lookback: {}\nExtreme percentile: {}\nMinimum turn: {}\n"
        "Target: +${:.2f} | Stop: -${:.2f}\n{}".format(
            MOMENTUM_LOOKBACK, EXTREME_PERCENTILE, MIN_TURN_DISTANCE,
            SESSION_PROFIT_TARGET, SESSION_LOSS_LIMIT, format_stats())
    )
    last_heartbeat = time.time()
    last_reconnect_notice = 0.0

    while not STOP_EVENT.is_set():
        reason = risk_stop_reason()
        if reason:
            STOP_EVENT.set()
            send_telegram("<b>Scanner stopped</b>\n{}\n{}".format(reason, format_stats()))
            break

        try:
            if not IQ.check_connect():
                raise ConnectionError("IQ Option connection lost")
        except Exception:
            if time.time() - last_reconnect_notice >= RECONNECT_SECONDS:
                logging.warning("Reconnecting to IQ Option")
                last_reconnect_notice = time.time()
            try:
                result = IQ.connect()
                ok = result[0] if isinstance(result, tuple) else bool(result)
                if not ok:
                    raise ConnectionError("reconnect rejected")
                IQ.change_balance(BALANCE_MODE)
                time.sleep(2)
                if not IQ.check_connect():
                    raise ConnectionError("reconnect check failed")
                refresh_assets(force=True)
            except Exception as exc:
                logging.warning("Reconnect failed: %s", exc)
                time.sleep(RECONNECT_SECONDS)
                continue

        if time.time() - ASSET_REFRESH_AT >= ASSET_REFRESH_SECONDS:
            refresh_assets(force=True)
        last_heartbeat = heartbeat(last_heartbeat)

        for item in list(ASSETS):
            if STOP_EVENT.is_set() or risk_stop_reason():
                if risk_stop_reason():
                    STOP_EVENT.set()
                break
            try:
                signal = evaluate_signal(get_closed_candles(item["name"]))
                if not signal:
                    continue
                candle_key = str(signal.get("candle_time", ""))
                last_signals = STATE.setdefault("last_signals", {})
                if last_signals.get(item["name"]) == candle_key:
                    continue
                last_signals[item["name"]] = candle_key
                save_state()
                log_trade(item["name"], signal["direction"], signal["momentum"], "SIGNAL", "")
                logging.info(
                    "SIGNAL %s %s | M10=%.5f previous=%.5f extreme=%.5f",
                    item["name"], signal["direction"].upper(), signal["momentum"],
                    signal["previous"], signal["extreme"])
                send_telegram(
                    "<b>Momentum 10 setup</b>\nAsset: {}\nDirection: {}\n"
                    "M10: {:.5f}\nPrevious M10: {:.5f}\nNo M20/engulfing filter".format(
                        item["name"], signal["direction"].upper(),
                        signal["momentum"], signal["previous"])
                )
                if AUTO_TRADE:
                    place_trade(item, signal)
            except Exception as exc:
                logging.warning("Scan error for %s: %s", item.get("name", "UNKNOWN"), exc)
            time.sleep(0.15)

        time.sleep(SCAN_INTERVAL)

    logging.info("Scanner finished. %s", format_stats())
    send_telegram("<b>Momentum 10 scanner finished</b>\n{}".format(format_stats()))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        STOP_EVENT.set()
        save_state()
        logging.info("Stopped by keyboard interrupt")
    except Exception as exc:
        logging.exception("Fatal scanner error")
        send_telegram("<b>Scanner error</b>\n{}".format(str(exc)))
        save_state()
        raise
