
import os
import re
import csv
import json
import time
import math
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import iqoptionapi.constants as OP_code
from iqoptionapi.stable_api import IQ_Option

# MOMENTUM 10 + MOMENTUM 20 + ENGULFING
# PRACTICE MODE ONLY

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
MOMENTUM_CONFIRM_PERIOD = 20
MOMENTUM_LOOKBACK = 50
EXTREME_PERCENTILE = 0.08
MIN_TURN_DISTANCE = 0.06

SESSION_PROFIT_TARGET = 5.0
SESSION_LOSS_LIMIT = 3.0

SCAN_INTERVAL = 5
ASSET_REFRESH_SECONDS = 900
HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 30
CANDLE_REQUEST_TIMEOUT = 10
ORDER_RESULT_RETRY_SECONDS = 5
REJECTED_ASSET_COOLDOWN_SECONDS = 600

CANDLE_REQUEST_COUNT = (
    MOMENTUM_LOOKBACK + MOMENTUM_CONFIRM_PERIOD + 15
)

LOG_FILE = "momentum_signal_log.csv"
STATE_FILE = "momentum_state.json"
LAGOS = ZoneInfo("Africa/Lagos")

CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
    "CNY", "HKD", "SGD", "SEK", "NOK", "DKK", "PLN", "CZK",
    "HUF", "TRY", "ZAR", "MXN", "BRL", "INR", "THB", "AED",
    "SAR", "ILS", "KRW", "TWD", "RUB", "RON", "BGN", "ISK",
    "PHP", "IDR", "MYR"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

SESSION_ID = now_session = (
    datetime.now(LAGOS).strftime("%Y-%m-%d")
)

IQ = None
ASSETS = []
ASSET_REFRESH_AT = 0.0
REJECTED_ASSETS = {}
STOP_EVENT = threading.Event()
TRADE_LOCK = threading.Lock()
STATE_LOCK = threading.RLock()


def now_lagos():
    return datetime.now(LAGOS)


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = "https://api.telegram.org/bot{}/sendMessage".format(
        TELEGRAM_TOKEN
    )

    try:
        response = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        response.raise_for_status()
    except Exception as exc:
        logging.warning("Telegram send failed: %s", exc)


def fresh_state():
    return {
        "date": now_lagos().date().isoformat(),
        "profit": 0.0,
        "wins": 0,
        "losses": 0,
        "resolved": 0,
        "pending_order_id": None,
        "pending_asset": None,
        "pending_direction": None,
        "pending_started": None,
        "last_signals": {},
        "stop_reason": "",
    }


def load_state():
    current = fresh_state()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
    except (OSError, ValueError, TypeError):
        saved = {}

    if not isinstance(saved, dict):
        saved = {}

    if saved.get("date") == current["date"]:
        current.update(saved)

    # Preserve an unresolved order even if the date changes.
    else:
        for key in (
            "pending_order_id",
            "pending_asset",
            "pending_direction",
            "pending_started",
        ):
            current[key] = saved.get(key)

    if not isinstance(current.get("last_signals"), dict):
        current["last_signals"] = {}

    return current


STATE = load_state()


def save_state():
    try:
        with STATE_LOCK:
            with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as handle:
                json.dump(STATE, handle, indent=2)
            os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except OSError as exc:
        logging.warning("Could not save state: %s", exc)


def format_stats():
    return (
        "Session P/L: ${:+.2f}\n"
        "Wins: {}\nLosses: {}\nResolved trades: {}"
    ).format(
        float(STATE.get("profit", 0.0)),
        int(STATE.get("wins", 0)),
        int(STATE.get("losses", 0)),
        int(STATE.get("resolved", 0)),
    )


def log_trade(asset, direction, momentum, status, profit="", order_id=""):
    fields = [
        "timestamp_lagos", "session_id", "asset", "direction",
        "momentum10", "status", "profit", "order_id", "session_profit"
    ]

    try:
        exists = os.path.exists(LOG_FILE)

        with open(LOG_FILE, "a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)

            if not exists or os.path.getsize(LOG_FILE) == 0:
                writer.writeheader()

            writer.writerow({
                "timestamp_lagos": now_lagos().isoformat(timespec="seconds"),
                "session_id": SESSION_ID,
                "asset": asset,
                "direction": direction,
                "momentum10": (
                    "" if momentum is None else round(float(momentum), 5)
                ),
                "status": status,
                "profit": profit,
                "order_id": order_id,
                "session_profit": round(float(STATE["profit"]), 2),
            })
    except Exception as exc:
        logging.warning("CSV logging error: %s", exc)


def risk_stop_reason():
    profit = round(float(STATE.get("profit", 0.0)), 2)

    if profit >= SESSION_PROFIT_TARGET:
        return "Profit target reached"

    if profit <= -SESSION_LOSS_LIMIT:
        return "Loss limit reached"

    return None


def stop_if_limit_reached():
    reason = risk_stop_reason()

    if reason and not STOP_EVENT.is_set():
        STOP_EVENT.set()
        STATE["stop_reason"] = reason
        save_state()

        message = (
            "<b>SCANNER STOPPED</b>\n{}\n{}"
        ).format(reason, format_stats())

        logging.warning(message.replace("<b>", "").replace("</b>", ""))
        send_telegram(message)

    return bool(reason)


def normalize_forex_name(value):
    if not isinstance(value, str):
        return None, False

    raw = value.strip().upper().split(".")[-1]
    if not raw:
        return None, False

    is_otc = "OTC" in raw
    letters = re.sub(r"[^A-Z]", "", raw)

    for suffix in ("OTC", "BINARY", "TURBO", "DIGITAL"):
        if letters.endswith(suffix):
            letters = letters[:-len(suffix)]

    if len(letters) < 6:
        return None, False

    pair = letters[-6:]

    if pair[:3] not in CURRENCIES:
        return None, False

    if pair[3:] not in CURRENCIES:
        return None, False

    return pair, is_otc


def add_asset(found, name, active_id=None):
    pair, is_otc = normalize_forex_name(name)

    if not pair:
        return

    clean_name = pair + ("-OTC" if is_otc else "")
    resolved_id = None

    candidates = [
        active_id,
        getattr(OP_code, "ACTIVES", {}).get(clean_name),
        getattr(OP_code, "ACTIVES", {}).get(pair),
    ]

    for candidate in candidates:
        try:
            if candidate is not None:
                resolved_id = int(candidate)
                break
        except (TypeError, ValueError, OverflowError):
            continue

    if resolved_id is None:
        return

    found[clean_name] = {
        "name": clean_name,
        "pair": pair,
        "otc": is_otc,
        "id": resolved_id,
    }

    try:
        OP_code.ACTIVES[clean_name] = resolved_id
    except Exception:
        pass


def walk_asset_tree(node, found, parent_key=None):
    if isinstance(node, dict):
        name = (
            node.get("name")
            or node.get("active_name")
            or node.get("symbol")
            or node.get("underlying")
        )

        active_id = (
            node.get("active_id")
            or node.get("activeId")
            or node.get("id")
        )

        if active_id is None and parent_key is not None:
            active_id = parent_key

        if isinstance(name, str):
            add_asset(found, name, active_id)

        # Some API responses wrap data inside "result".
        for key, value in node.items():
            walk_asset_tree(value, found, key)

    elif isinstance(node, list):
        for item in node:
            walk_asset_tree(item, found, parent_key)


def refresh_assets(force=False):
    global ASSETS, ASSET_REFRESH_AT

    if (
        ASSETS
        and not force
        and time.time() - ASSET_REFRESH_AT < ASSET_REFRESH_SECONDS
    ):
        return ASSETS

    found = {}

    try:
        active_map = IQ.get_all_ACTIVES_OPCODE()

        if isinstance(active_map, dict):
            for name, active_id in active_map.items():
                add_asset(found, str(name), active_id)

    except Exception as exc:
        logging.warning("Active-code discovery failed: %s", exc)

    for method_name in ("get_all_init_v2", "get_all_init"):
        method = getattr(IQ, method_name, None)

        if not callable(method):
            continue

        try:
            data = method()

            if data is None:
                logging.warning("%s returned no data", method_name)
                continue

            logging.info(
                "%s response type: %s",
                method_name,
                type(data).__name__,
            )

            if isinstance(data, dict):
                logging.info(
                    "%s top-level keys: %s",
                    method_name,
                    list(data.keys())[:30],
                )

            walk_asset_tree(data, found)

        except Exception as exc:
            logging.warning(
                "%s asset discovery failed: %s",
                method_name,
                exc,
            )

    assets = list(found.values())
    assets.sort(key=lambda item: (item["otc"], item["name"]))

    if assets:
        ASSETS = assets
        ASSET_REFRESH_AT = time.time()

        normal_count = sum(1 for item in assets if not item["otc"])
        otc_count = sum(1 for item in assets if item["otc"])

        logging.info(
            "Forex assets: %s total; %s normal; %s OTC",
            len(assets),
            normal_count,
            otc_count,
        )

        send_telegram(
            "<b>Forex assets refreshed</b>\n"
            "Total: {}\nNormal: {}\nOTC: {}".format(
                len(assets), normal_count, otc_count
            )
        )
    else:
        logging.warning(
            "No eligible forex assets found. "
            "Check the discovery logs above."
        )

    return ASSETS


def disable_digital_open_worker(self):
    logging.info("Disabled digital-market watcher")


def safe_get_candles(self, active, interval, count, endtime):
    try:
        active_code = getattr(OP_code, "ACTIVES", {}).get(active)

        if active_code is None:
            logging.debug("Missing active code for %s", active)
            return None

        self.api.candles.candles_data = None

        self.api.getcandles(
            active_code,
            interval,
            count,
            endtime,
        )

        deadline = time.time() + CANDLE_REQUEST_TIMEOUT

        while time.time() < deadline:
            data = getattr(self.api.candles, "candles_data", None)

            if data is not None:
                return data

            try:
                if not self.check_connect():
                    return None
            except Exception:
                return None

            time.sleep(0.1)

        logging.warning("Candle request timed out: %s", active)

    except Exception as exc:
        logging.warning("Candle request failed for %s: %s", active, exc)

    return None


if hasattr(IQ_Option, "_IQ_Option__get_digital_open"):
    IQ_Option._IQ_Option__get_digital_open = disable_digital_open_worker

IQ_Option.get_candles = safe_get_candles


def connect_iq():
    global IQ

    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError("Missing IQ_EMAIL or IQ_PASSWORD secrets")

    last_error = "unknown error"

    for attempt in range(1, 6):
        try:
            client = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
            result = client.connect()

            connected = (
                result[0] if isinstance(result, tuple) else bool(result)
            )

            if not connected:
                raise ConnectionError(str(result))

            client.change_balance(BALANCE_MODE)
            time.sleep(2)

            if not client.check_connect():
                raise ConnectionError("Connection dropped after login")

            IQ = client

            logging.info("Connected to IQ Option PRACTICE mode")

            send_telegram(
                "<b>Scanner connected</b>\n"
                "Mode: PRACTICE\nStake: $1\n"
                "Expiry: 1 minute\n"
                "Strategy: M10 + M20 + engulfing"
            )

            return

        except Exception as exc:
            last_error = str(exc)
            logging.warning(
                "Connection attempt %s/5 failed: %s",
                attempt,
                last_error,
            )

            if attempt < 5:
                time.sleep(RECONNECT_SECONDS)

    raise RuntimeError("Could not connect: " + last_error)


def momentum_series(closes, period):
    values = []

    for index in range(period, len(closes)):
        old = closes[index]
        current = closes[index]

        # Use the close from 'period' candles earlier.
        old = closes[index - period]

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
    try:
        raw = IQ.get_candles(
            asset,
            CANDLE_SECONDS,
            CANDLE_REQUEST_COUNT,
            int(time.time()),
        )
    except Exception as exc:
        logging.debug("Candle fetch failed for %s: %s", asset, exc)
        return []

    if not isinstance(raw, list):
        return []

    try:
        raw.sort(key=lambda row: int(row.get("from", 0)))
    except Exception:
        return []

    now = int(time.time())
    closed = []

    for candle in raw:
        try:
            start = int(candle.get("from", 0))

            if start > 0 and start + CANDLE_SECONDS <= now:
                closed.append(candle)

        except (TypeError, ValueError):
            continue

    return closed


def bullish_engulfing(previous, current):
    try:
        po = float(previous["open"])
        pc = float(previous["close"])
        co = float(current["open"])
        cc = float(current["close"])
    except (KeyError, TypeError, ValueError):
        return False

    return pc < po and cc > co and co <= pc and cc >= po


def bearish_engulfing(previous, current):
    try:
        po = float(previous["open"])
        pc = float(previous["close"])
        co = float(current["open"])
        cc = float(current["close"])
    except (KeyError, TypeError, ValueError):
        return False

    return pc > po and cc < co and co >= pc and cc <= po


def evaluate_signal(candles):
    minimum = MOMENTUM_LOOKBACK + MOMENTUM_CONFIRM_PERIOD + 2

    if len(candles) < minimum:
        return None

    try:
        closes = [float(row["close"]) for row in candles]
        candle_time = int(candles[-1].get("from", 0))
    except (KeyError, TypeError, ValueError):
        return None

    values10 = momentum_series(closes, MOMENTUM_PERIOD)
    values20 = momentum_series(closes, MOMENTUM_CONFIRM_PERIOD)

    if len(values10) < MOMENTUM_LOOKBACK + 3:
        return None

    if len(values20) < 3:
        return None

    current10 = values10[-1]
    previous10 = values10[-2]
    before_previous10 = values10[-3]

    history10 = values10[-(MOMENTUM_LOOKBACK + 1):-1]

    low_extreme = percentile_value(history10, EXTREME_PERCENTILE)
    high_extreme = percentile_value(
        history10,
        1.0 - EXTREME_PERCENTILE,
    )

    if abs(current10 - previous10) < MIN_TURN_DISTANCE:
        return None

    current20 = values20[-1]
    previous20 = values20[-2]

    m20_rising = current20 > previous20
    m20_falling = current20 < previous20

    previous_candle = candles[-2]
    current_candle = candles[-1]

    turning_up = (
        current10 > previous10
        and previous10 <= before_previous10
    )

    turning_down = (
        current10 < previous10
        and previous10 >= before_previous10
    )

    if (
        current10 <= low_extreme
        and turning_up
        and m20_rising
        and bullish_engulfing(previous_candle, current_candle)
    ):
        return {
            "direction": "call",
            "momentum": round(current10, 5),
            "momentum20": round(current20, 5),
            "candle_time": candle_time,
            "confirmation": "M20 rising + bullish engulfing",
        }

    if (
        current10 >= high_extreme
        and turning_down
        and m20_falling
        and bearish_engulfing(previous_candle, current_candle)
    ):
        return {
            "direction": "put",
            "momentum": round(current10, 5),
            "momentum20": round(current20, 5),
            "candle_time": candle_time,
            "confirmation": "M20 falling + bearish engulfing",
        }

    return None


def extract_numeric_profit(value):
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None

    if isinstance(value, str):
        text_value = value.strip().lower()

        if text_value in (
            "", "false", "none", "null", "pending",
            "open", "win", "won", "loss", "lost",
            "draw", "unknown",
        ):
            return None

        try:
            number = float(text_value)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError, OverflowError):
            return None

    if isinstance(value, dict):
        keys = (
            "profit", "profit_amount", "profitAmount",
            "close_profit", "closeProfit", "net_profit",
            "netProfit", "win_amount", "winAmount",
        )

        for key in keys:
            if key in value:
                result = extract_numeric_profit(value[key])
                if result is not None:
                    return result

        for key in ("result", "data", "betinfo", "bet_info", "option"):
            if key in value:
                result = extract_numeric_profit(value[key])
                if result is not None:
                    return result

    if isinstance(value, (list, tuple)):
        for item in value:
            result = extract_numeric_profit(item)
            if result is not None:
                return result

    return None


def query_order_profit(order_id):
    for name in ("check_win_v4", "check_win", "get_betinfo"):
        method = getattr(IQ, name, None)

        if not callable(method):
            continue

        try:
            raw = method(order_id)
        except Exception as exc:
            logging.debug("%s failed: %s", name, exc)
            continue

        value = extract_numeric_profit(raw)

        if value is not None:
            return value

    return None


def settle_result(asset, direction, broker_profit, momentum, order_id):
    if broker_profit is None:
        raise ValueError("Missing numeric broker result")

    # A zero result is counted as a full $1 loss by your chosen rule.
    net_change = -STAKE if broker_profit == 0 else float(broker_profit)
    is_win = net_change > 0

    with STATE_LOCK:
        STATE["profit"] = round(
            float(STATE.get("profit", 0.0)) + net_change, 2
        )
        STATE["resolved"] = int(STATE.get("resolved", 0)) + 1

        if is_win:
            STATE["wins"] = int(STATE.get("wins", 0)) + 1
        else:
            STATE["losses"] = int(STATE.get("losses", 0)) + 1

        STATE["pending_order_id"] = None
        STATE["pending_asset"] = None
        STATE["pending_direction"] = None
        STATE["pending_started"] = None

    save_state()

    label = "WIN" if is_win else "LOSS"
    log_trade(asset, direction, momentum, label, net_change, order_id)

    logging.info(
        "%s | %s %s | %+.2f | %s",
        label, asset, direction, net_change, format_stats()
    )

    send_telegram(
        "<b>{}</b>\nAsset: {}\nDirection: {}\n"
        "Broker result: ${:+.2f}\nAccounted result: ${:+.2f}\n{}".format(
            label, asset, direction.upper(),
            broker_profit, net_change, format_stats()
        )
    )

    stop_if_limit_reached()


def wait_for_order_result(order_id, asset, direction, momentum):
    last_notice = 0.0

    while True:
        value = query_order_profit(order_id)

        if value is not None:
            settle_result(
                asset, direction, value, momentum, order_id
            )
            return

        if time.time() - last_notice >= 60:
            logging.warning(
                "Order %s is still unresolved. No new order will be placed.",
                order_id,
            )
            last_notice = time.time()

        time.sleep(ORDER_RESULT_RETRY_SECONDS)


def recover_pending_order():
    order_id = STATE.get("pending_order_id")

    if not order_id:
        return

    asset = STATE.get("pending_asset") or "UNKNOWN"
    direction = STATE.get("pending_direction") or "unknown"

    logging.warning("Recovering pending order %s", order_id)

    wait_for_order_result(
        order_id,
        asset,
        direction,
        None,
    )


def place_trade(asset_info, signal):
    asset = asset_info["name"]
    direction = signal["direction"]

    with TRADE_LOCK:
        if STOP_EVENT.is_set() or stop_if_limit_reached():
            return

        if STATE.get("pending_order_id"):
            logging.warning("Pending order exists; refusing another trade")
            return

        try:
            result = IQ.buy(
                STAKE,
                asset,
                direction,
                EXPIRY_MINUTES,
            )
        except Exception as exc:
            log_trade(
                asset, direction, signal["momentum"], "ORDER_ERROR"
            )
            logging.warning("Order placement error: %s", exc)
            return

        if isinstance(result, tuple):
            success = bool(result[0])
            order_id = result[1] if len(result) > 1 else None
        else:
            success = bool(result)
            order_id = result

        if not success or order_id is None or order_id is False:
            REJECTED_ASSETS[asset] = (
                time.time() + REJECTED_ASSET_COOLDOWN_SECONDS
            )
            log_trade(
                asset, direction, signal["momentum"], "ORDER_REJECTED"
            )
            logging.warning("Order rejected on %s: %r", asset, result)
            return

        STATE["pending_order_id"] = order_id
        STATE["pending_asset"] = asset
        STATE["pending_direction"] = direction
        STATE["pending_started"] = now_lagos().isoformat(
            timespec="seconds"
        )
        save_state()

        log_trade(
            asset,
            direction,
            signal["momentum"],
            "OPENED",
            "",
            order_id,
        )

        send_telegram(
            "<b>Practice trade opened</b>\n"
            "Asset: {}\nDirection: {}\nStake: $1\n"
            "Expiry: 1 minute\nM10: {:.5f}\nM20: {:.5f}\n{}".format(
                asset,
                direction.upper(),
                signal["momentum"],
                signal["momentum20"],
                signal["confirmation"],
            )
        )

    wait_for_order_result(
        order_id,
        asset,
        direction,
        signal["momentum"],
    )


def main():
    connect_iq()
    refresh_assets(force=True)

    if not ASSETS:
        raise RuntimeError(
            "No forex assets discovered. Check asset discovery logs."
        )

    if stop_if_limit_reached():
        return

    if STATE.get("pending_order_id"):
        recover_pending_order()

    if STOP_EVENT.is_set() or stop_if_limit_reached():
        return

    send_telegram(
        "<b>Momentum scanner started</b>\n"
        "Mode: PRACTICE\n"
        "Entry: M10 extreme and turn\n"
        "Confirmations: M20 trend + engulfing\n"
        "Target: +$5 | Stop loss: -$3\n{}".format(format_stats())
    )

    last_heartbeat = time.time()

    while not STOP_EVENT.is_set():
        if stop_if_limit_reached():
            break

        try:
            if not IQ.check_connect():
                raise ConnectionError("Connection lost")
        except Exception:
            logging.warning("Connection lost; reconnecting")

            try:
                result = IQ.connect()
                connected = (
                    result[0] if isinstance(result, tuple) else bool(result)
                )

                if not connected:
                    raise ConnectionError("Reconnect rejected")

                IQ.change_balance(BALANCE_MODE)
                time.sleep(2)

                if not IQ.check_connect():
                    raise ConnectionError("Reconnect not ready")

                refresh_assets(force=True)

            except Exception as exc:
                logging.warning("Reconnect failed: %s", exc)
                time.sleep(RECONNECT_SECONDS)
                continue

        if time.time() - ASSET_REFRESH_AT >= ASSET_REFRESH_SECONDS:
            refresh_assets(force=True)

        if time.time() - last_heartbeat >= HEARTBEAT_SECONDS:
            logging.info(
                "Heartbeat | Assets: %s | %s",
                len(ASSETS), format_stats()
            )
            send_telegram(
                "<b>Scanner heartbeat</b>\n"
                "Assets: {}\n{}".format(len(ASSETS), format_stats())
            )
            last_heartbeat = time.time()

        for item in list(ASSETS):
            if STOP_EVENT.is_set() or stop_if_limit_reached():
                break

            asset = item["name"]
            cooldown = REJECTED_ASSETS.get(asset, 0.0)

            if time.time() < cooldown:
                continue

            if cooldown:
                REJECTED_ASSETS.pop(asset, None)

            try:
                candles = get_closed_candles(asset)
                signal = evaluate_signal(candles)

                if signal is None:
                    continue

                candle_key = str(signal["candle_time"])
                previous_key = STATE["last_signals"].get(asset)

                if previous_key == candle_key:
                    continue

                STATE["last_signals"][asset] = candle_key
                save_state()

                log_trade(
                    asset,
                    signal["direction"],
                    signal["momentum"],
                    "SIGNAL",
                )

                logging.info(
                    "CONFIRMED SIGNAL %s %s | M10 %.5f | M20 %.5f | %s",
                    asset,
                    signal["direction"].upper(),
                    signal["momentum"],
                    signal["momentum20"],
                    signal["confirmation"],
                )

                send_telegram(
                    "<b>Confirmed setup</b>\n"
                    "Asset: {}\nDirection: {}\n"
                    "M10: {:.5f}\nM20: {:.5f}\n{}".format(
                        asset,
                        signal["direction"].upper(),
                        signal["momentum"],
                        signal["momentum20"],
                        signal["confirmation"],
                    )
                )

                if AUTO_TRADE:
                    place_trade(item, signal)

            except Exception as exc:
                logging.warning("Scan error for %s: %s", asset, exc)

            time.sleep(0.15)

        time.sleep(SCAN_INTERVAL)

    logging.info("Scanner stopped | %s", format_stats())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        STOP_EVENT.set()
        save_state()
        logging.info("Scanner stopped by keyboard interrupt")
    except Exception as exc:
        logging.exception("Fatal scanner error")
        send_telegram("<b>Scanner error</b>\n{}".format(exc))
        save_state()
        raise
