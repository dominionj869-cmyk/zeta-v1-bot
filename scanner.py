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

# ============================================================
# MOMENTUM 10 + MOMENTUM 20 + ENGULFING
# FOREX ONLY (NORMAL + OTC) - PRACTICE ACCOUNT ONLY
# ============================================================

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
MAX_LOSING_TRADES = 5
MAX_RESOLVED_TRADES = 20

SCAN_INTERVAL = 5
ASSET_REFRESH_SECONDS = 900
HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 30
CANDLE_REQUEST_TIMEOUT = 10
ORDER_RESULT_RETRY_SECONDS = 5
CANDLE_REQUEST_COUNT = MOMENTUM_LOOKBACK + MOMENTUM_CONFIRM_PERIOD + 12
LOG_FILE = "momentum_signal_log.csv"
STATE_FILE = "momentum_state.json"
LAGOS = ZoneInfo("Africa/Lagos")

# Currency codes used by mainstream FX pairs. Crypto symbols such as BTC/ETH
# are intentionally excluded because they are not in this list.
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
    """Disable the library's unused digital-market polling thread.

    This bot trades binary Forex options and does not need digital-underlying
    lists. The installed iqoptionapi worker assumes every response contains an
    ``underlying`` key and can crash when the broker returns None.
    """
    logging.info("Digital-market background watcher disabled; Forex scanner does not use it")


def _safe_get_candles(self, active, interval, count, endtime):
    """Bound candle requests so a temporary API outage cannot hang the scan forever."""
    try:
        active_code = OP_code.ACTIVES.get(active)
        if active_code is None:
            logging.warning("No IQ Option active ID found for %s", active)
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
                    logging.warning("Connection lost while waiting for candles for %s", active)
                    return None
            except Exception:
                return None
            time.sleep(0.1)
        logging.warning("Candle request timed out for %s after %s seconds", active, CANDLE_REQUEST_TIMEOUT)
        return None
    except Exception as exc:
        logging.warning("Candle request failed for %s: %s", active, exc)
        return None


# Patch the library BEFORE creating an IQ_Option instance. This matters because
# some iqoptionapi versions start the digital watcher during initialisation or
# connection setup, before a per-instance patch would be applied.
try:
    if hasattr(IQ_Option, "_IQ_Option__get_digital_open"):
        IQ_Option._IQ_Option__get_digital_open = _disable_digital_open_worker
        logging.info("Patched unused IQ Option digital-market watcher")
    else:
        logging.warning("Digital watcher method not found in this iqoptionapi version")
    IQ_Option.get_candles = _safe_get_candles
except Exception as patch_error:
    logging.warning("Could not apply IQ Option safety patches: %s", patch_error)

IQ = None
ASSETS = []
ASSET_REFRESH_AT = 0
STOP_EVENT = threading.Event()
TRADE_LOCK = threading.Lock()


def now_lagos():
    return datetime.now(LAGOS)


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.info("Telegram not configured: %s", message.replace("\n", " | "))
        return
    url = "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_TOKEN)
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, data=data, timeout=10).raise_for_status()
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
        "last_signals": {}
    }


def load_state():
    state = fresh_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        if isinstance(saved, dict):
            state.update(saved)
    except (OSError, ValueError, TypeError):
        pass

    state.setdefault("last_signals", {})
    today = now_lagos().date().isoformat()
    if state.get("date") != today:
        pending = state.get("pending_order_id")
        state = fresh_state()
        # Keep unresolved order details across midnight until it is checked.
        if pending:
            state["pending_order_id"] = pending
            state["pending_asset"] = saved.get("pending_asset") if "saved" in locals() else None
            state["pending_direction"] = saved.get("pending_direction") if "saved" in locals() else None
            state["pending_started"] = saved.get("pending_started") if "saved" in locals() else None
    return state


STATE = load_state()


def save_state():
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(STATE, handle, indent=2)
        os.replace(tmp, STATE_FILE)
    except OSError as exc:
        logging.warning("Could not save state: %s", exc)


def log_trade(asset, signal, m10, m20, status, profit=""):
    exists = os.path.exists(LOG_FILE)
    try:
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if not exists:
                writer.writerow([
                    "timestamp_lagos", "asset", "signal", "momentum10",
                    "momentum20", "status", "profit"
                ])
            writer.writerow([
                now_lagos().isoformat(timespec="seconds"), asset, signal,
                "" if m10 is None else round(m10, 5),
                "" if m20 is None else round(m20, 5), status, profit
            ])
    except OSError as exc:
        logging.warning("Could not write CSV log: %s", exc)


def normalise_forex_name(value):
    """Return (six-letter currency pair, is_otc), or (None, False)."""
    if not isinstance(value, str):
        return None, False
    raw = value.strip().upper()
    if not raw:
        return None, False
    # Most broker data labels the asset after a prefix and before any metadata.
    raw = raw.split(".")[-1]
    is_otc = "OTC" in raw
    letters = re.sub(r"[^A-Z]", "", raw)
    for suffix in ("OTC", "BINARY", "TURBO", "DIGITAL"):
        if letters.endswith(suffix):
            letters = letters[:-len(suffix)]
    # Require the pair to be at the end. This rejects symbols like EURUSDT.
    for start in range(max(0, len(letters) - 12), len(letters) - 5):
        pair = letters[start:start + 6]
        if len(pair) == 6 and pair[:3] in CURRENCIES and pair[3:] in CURRENCIES:
            if letters.endswith(pair):
                return pair, is_otc
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
    existing = result.get(clean_name, {})
    resolved_id = None
    for candidate in (active_id, existing.get("id")):
        try:
            if candidate is not None:
                resolved_id = int(candidate)
                break
        except (TypeError, ValueError):
            continue
    if resolved_id is None:
        try:
            candidate = OP_code.ACTIVES.get(clean_name)
            if candidate is not None:
                resolved_id = int(candidate)
        except (TypeError, ValueError, AttributeError):
            resolved_id = None
    result[clean_name] = {
        "name": clean_name,
        "pair": pair,
        "otc": is_otc,
        "id": resolved_id
    }
    try:
        if resolved_id is not None:
            OP_code.ACTIVES[clean_name] = resolved_id
    except (TypeError, ValueError, AttributeError):
        pass


def walk_asset_tree(node, result, parent_key=None):
    if isinstance(node, dict):
        name = node.get("name") or node.get("active_name") or node.get("symbol")
        active_id = node.get("id") or node.get("active_id") or node.get("activeId")
        if name:
            add_asset(result, str(name), active_id if active_id is not None else parent_key)
        elif isinstance(parent_key, str):
            # Some active listings use the asset name as the dictionary key.
            add_asset(result, parent_key, active_id)
        for key, value in node.items():
            walk_asset_tree(value, result, key)
    elif isinstance(node, list):
        for item in node:
            walk_asset_tree(item, result, parent_key)


def refresh_assets(force=False):
    global ASSETS, ASSET_REFRESH_AT
    if not force and time.time() - ASSET_REFRESH_AT < ASSET_REFRESH_SECONDS and ASSETS:
        return ASSETS

    discovered = {}
    # The broker's opcode map usually contains the name-to-id lookup.
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
                data = method()
                walk_asset_tree(data, discovered)
        except Exception as exc:
            logging.warning("%s failed: %s", method_name, exc)

    # Retain only known Forex pairs. Sort live FX first, then OTC FX.
    assets = list(discovered.values())
    assets.sort(key=lambda item: (item["otc"], item["name"]))
    if assets:
        ASSETS = assets
        ASSET_REFRESH_AT = time.time()
        logging.info("Forex assets found: %s total (%s normal, %s OTC)",
                     len(assets), sum(not item["otc"] for item in assets),
                     sum(item["otc"] for item in assets))
        send_telegram(
            "<b>Forex scanner asset refresh</b>\n"
            "Forex pairs: {} total\nNormal: {}\nOTC: {}\nCrypto/commodities/indices: excluded".format(
                len(assets), sum(not item["otc"] for item in assets),
                sum(item["otc"] for item in assets))
        )
    else:
        logging.warning("No Forex pairs identified from IQ Option asset data.")
    return ASSETS


def connect_iq():
    global IQ
    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError("Missing IQ_EMAIL or IQ_PASSWORD GitHub secret")

    api = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    last_error = "unknown reason"
    for attempt in range(1, 6):
        try:
            connected = api.connect()
            ok = connected[0] if isinstance(connected, tuple) else bool(connected)
            if not ok:
                last_error = connected[1] if isinstance(connected, tuple) and len(connected) > 1 else "connection rejected"
                raise ConnectionError(str(last_error))
            api.change_balance(BALANCE_MODE)
            time.sleep(1)
            if not api.check_connect():
                raise ConnectionError("connection dropped after login")
            IQ = api
            logging.info("Connected to IQ Option in %s mode", BALANCE_MODE)
            send_telegram(
                "<b>Momentum Forex Scanner connected</b>\n"
                "Balance: PRACTICE\nStake: ${:.2f}\nTarget: +${:.2f}\nLoss limit: -${:.2f}\n"
                "Max losing trades: {}\nMax resolved trades: {}\nForex only: normal + OTC".format(
                    STAKE, SESSION_PROFIT_TARGET, SESSION_LOSS_LIMIT,
                    MAX_LOSING_TRADES, MAX_RESOLVED_TRADES)
            )
            return
        except Exception as exc:
            last_error = str(exc)
            logging.warning("IQ Option connection attempt %s/5 failed: %s", attempt, last_error)
            if attempt < 5:
                time.sleep(RECONNECT_SECONDS)
    raise RuntimeError("Could not connect to IQ Option after 5 attempts: {}".format(last_error))


def momentum_series(closes, period):
    values = []
    for index in range(period, len(closes)):
        old = closes[index - period]
        current = closes[index]
        if old == 0:
            values.append(0.0)
        else:
            values.append(((current / old) - 1.0) * 100.0)
    return values


def percentile_value(values, fraction):
    if not values:
        return 0.0
    ordered = sorted(values)
    position = int(round((len(ordered) - 1) * fraction))
    position = max(0, min(len(ordered) - 1, position))
    return ordered[position]


def candle_is_bullish_engulfing(previous, current):
    prev_open = float(previous["open"])
    prev_close = float(previous["close"])
    cur_open = float(current["open"])
    cur_close = float(current["close"])
    return (
        prev_close < prev_open and
        cur_close > cur_open and
        cur_open <= prev_close and
        cur_close >= prev_open
    )


def candle_is_bearish_engulfing(previous, current):
    prev_open = float(previous["open"])
    prev_close = float(previous["close"])
    cur_open = float(current["open"])
    cur_close = float(current["close"])
    return (
        prev_close > prev_open and
        cur_close < cur_open and
        cur_open >= prev_close and
        cur_close <= prev_open
    )


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
    """Return signal details only when every confirmation is present."""
    minimum = MOMENTUM_LOOKBACK + MOMENTUM_CONFIRM_PERIOD + 2
    if len(candles) < minimum:
        return None

    opens = [float(row["open"]) for row in candles]
    closes = [float(row["close"]) for row in candles]
    m10 = momentum_series(closes, MOMENTUM_PERIOD)
    m20 = momentum_series(closes, MOMENTUM_CONFIRM_PERIOD)
    if len(m10) < MOMENTUM_LOOKBACK + 3 or len(m20) < 3:
        return None

    latest10 = m10[-1]
    previous10 = m10[-2]
    before_previous10 = m10[-3]
    latest20 = m20[-1]
    previous20 = m20[-2]

    # Exclude the signal candle's value when calculating recent extreme levels.
    history10 = m10[-(MOMENTUM_LOOKBACK + 1):-1]
    lower_extreme = percentile_value(history10, EXTREME_PERCENTILE)
    upper_extreme = percentile_value(history10, 1.0 - EXTREME_PERCENTILE)

    m10_turning_up = latest10 > previous10 and previous10 <= before_previous10
    m10_turning_down = latest10 < previous10 and previous10 >= before_previous10
    turn_up_size = latest10 - previous10
    turn_down_size = previous10 - latest10
    m20_rising = latest20 > previous20
    m20_falling = latest20 < previous20

    previous_candle = candles[-2]
    current_candle = candles[-1]
    bullish_engulfing = candle_is_bullish_engulfing(previous_candle, current_candle)
    bearish_engulfing = candle_is_bearish_engulfing(previous_candle, current_candle)

    call_ok = (
        latest10 <= lower_extreme and
        m10_turning_up and
        turn_up_size >= MIN_TURN_DISTANCE and
        m20_rising and
        bullish_engulfing
    )
    put_ok = (
        latest10 >= upper_extreme and
        m10_turning_down and
        turn_down_size >= MIN_TURN_DISTANCE and
        m20_falling and
        bearish_engulfing
    )

    if call_ok:
        return {"signal": "call", "m10": latest10, "m20": latest20,
                "extreme": lower_extreme, "candle": current_candle}
    if put_ok:
        return {"signal": "put", "m10": latest10, "m20": latest20,
                "extreme": upper_extreme, "candle": current_candle}
    return None


def risk_stop_reason():
    if float(STATE.get("profit", 0.0)) >= SESSION_PROFIT_TARGET:
        return "Session profit target reached"
    if float(STATE.get("profit", 0.0)) <= -SESSION_LOSS_LIMIT:
        return "Session loss limit reached"
    if int(STATE.get("losses", 0)) >= MAX_LOSING_TRADES:
        return "Maximum losing trades reached"
    if int(STATE.get("resolved", 0)) >= MAX_RESOLVED_TRADES:
        return "Maximum resolved trades reached"
    return None


def format_stats():
    return (
        "Profit: {:+.2f}\nWins: {}\nLosses: {}\nResolved: {}/{}".format(
            float(STATE.get("profit", 0.0)), int(STATE.get("wins", 0)),
            int(STATE.get("losses", 0)), int(STATE.get("resolved", 0)),
            MAX_RESOLVED_TRADES)
    )


def settle_result(asset, direction, profit, m10=None, m20=None):
    try:
        profit = float(profit)
    except (TypeError, ValueError):
        raise ValueError("Broker returned an unreadable trade result")

    # The user's risk rule treats $0 as a loss for win/loss statistics.
    is_win = profit > 0.0
    STATE["profit"] = round(float(STATE.get("profit", 0.0)) + profit, 2)
    STATE["resolved"] = int(STATE.get("resolved", 0)) + 1
    if is_win:
        STATE["wins"] = int(STATE.get("wins", 0)) + 1
        label = "WIN"
    else:
        STATE["losses"] = int(STATE.get("losses", 0)) + 1
        label = "LOSS (zero result counted as loss)" if profit == 0 else "LOSS"
    STATE["pending_order_id"] = None
    STATE["pending_asset"] = None
    STATE["pending_direction"] = None
    STATE["pending_started"] = None
    save_state()
    log_trade(asset, direction, m10, m20, label, round(profit, 2))

    message = (
        "<b>Trade result: {}</b>\nAsset: {}\nDirection: {}\nResult: ${:+.2f}\n{}".format(
            label, asset, direction.upper(), profit, format_stats())
    )
    send_telegram(message)
    logging.info("%s | %s %s | result=%+.2f | %s", label, asset, direction, profit, format_stats())

    reason = risk_stop_reason()
    if reason:
        STOP_EVENT.set()
        send_telegram("<b>Scanner stopped</b>\n{}\n{}".format(reason, format_stats()))


def wait_for_order_result(order_id, asset, direction, m10, m20):
    """Resolve the one open trade before allowing another entry."""
    last_notice = time.time()
    while not STOP_EVENT.is_set():
        try:
            # This method normally blocks until the binary option is settled.
            result = IQ.check_win_v4(order_id)
            if result is not None:
                settle_result(asset, direction, result, m10, m20)
                return
        except Exception as exc:
            logging.warning("Result check failed for order %s: %s", order_id, exc)

        # If an API version returns None early, keep monitoring this same order.
        if time.time() - last_notice >= 30:
            logging.info("Still checking open order %s; no new trade will be placed", order_id)
            last_notice = time.time()
        time.sleep(ORDER_RESULT_RETRY_SECONDS)


def recover_pending_order():
    order_id = STATE.get("pending_order_id")
    if not order_id:
        return
    asset = STATE.get("pending_asset") or "UNKNOWN"
    direction = STATE.get("pending_direction") or "unknown"
    started = STATE.get("pending_started")
    logging.warning("Recovering unresolved order %s (%s %s)", order_id, asset, direction)
    send_telegram("<b>Recovering unresolved trade</b>\nOrder: {}\nAsset: {}\nNo new trade until it is resolved.".format(order_id, asset))
    wait_for_order_result(order_id, asset, direction, None, None)


def place_trade(asset_info, signal):
    asset = asset_info["name"]
    direction = signal["signal"]
    with TRADE_LOCK:
        if STATE.get("pending_order_id"):
            logging.warning("Pending order exists; skipping new entry")
            return
        if risk_stop_reason():
            STOP_EVENT.set()
            return

        active_id = asset_info.get("id")
        # iqoptionapi uses OP_code.ACTIVES to translate symbol to broker ID.
        try:
            if active_id is not None:
                OP_code.ACTIVES[asset] = int(active_id)
        except Exception:
            pass
        try:
            result = IQ.buy(STAKE, asset, direction, EXPIRY_MINUTES)
        except Exception as exc:
            log_trade(asset, direction, signal.get("m10"), signal.get("m20"), "ORDER_ERROR", "")
            logging.warning("Order failed for %s: %s", asset, exc)
            return

        if isinstance(result, tuple):
            success = bool(result[0])
            order_id = result[1] if len(result) > 1 else None
        else:
            success = bool(result)
            order_id = None

        if not success or order_id is None:
            log_trade(asset, direction, signal.get("m10"), signal.get("m20"), "ORDER_REJECTED", "")
            logging.warning("Broker rejected order on %s: %r", asset, result)
            return

        STATE["pending_order_id"] = order_id
        STATE["pending_asset"] = asset
        STATE["pending_direction"] = direction
        STATE["pending_started"] = now_lagos().isoformat(timespec="seconds")
        save_state()
        log_trade(asset, direction, signal.get("m10"), signal.get("m20"), "OPENED", "")
        send_telegram(
            "<b>Forex signal traded</b>\nAsset: {}\nDirection: {}\nStake: ${:.2f}\nExpiry: {} minute\n"
            "M10: {:.4f}\nM20: {:.4f}\nMode: PRACTICE\n{}".format(
                asset, direction.upper(), STAKE, EXPIRY_MINUTES,
                signal["m10"], signal["m20"], format_stats())
        )
        logging.info("Order opened: %s %s id=%s", asset, direction, order_id)

    wait_for_order_result(order_id, asset, direction, signal.get("m10"), signal.get("m20"))


def heartbeat(last_heartbeat):
    if time.time() - last_heartbeat < HEARTBEAT_SECONDS:
        return last_heartbeat
    message = "<b>Forex scanner heartbeat</b>\n{}\nAssets: {}\n{}".format(
        now_lagos().strftime("%Y-%m-%d %H:%M:%S WAT"), len(ASSETS), format_stats())
    send_telegram(message)
    logging.info(message.replace("<b>", "").replace("</b>", "").replace("\n", " | "))
    return time.time()


def main():
    global ASSET_REFRESH_AT
    connect_iq()
    refresh_assets(force=True)
    if not ASSETS:
        raise RuntimeError("No Forex assets were found. Check IQ Option active-list data.")

    if STATE.get("pending_order_id"):
        recover_pending_order()
    if STOP_EVENT.is_set():
        logging.info("Stopped while recovering an earlier order. %s", format_stats())
        return

    last_heartbeat = time.time()
    last_reconnect_notice = 0
    send_telegram(
        "<b>Scanner started</b>\nRules: extreme M10 + M10 turn + M20 direction + closed engulfing candle\n"
        "CALL: lower extreme, M10 up, M20 rising, bullish engulfing\n"
        "PUT: upper extreme, M10 down, M20 falling, bearish engulfing\n"
        "Anything missing = NO TRADE\n{}".format(format_stats())
    )

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
            if time.time() - last_reconnect_notice > RECONNECT_SECONDS:
                logging.warning("Reconnecting to IQ Option...")
                last_reconnect_notice = time.time()
            try:
                connected = IQ.connect()
                ok = connected[0] if isinstance(connected, tuple) else bool(connected)
                if not ok:
                    reason = connected[1] if isinstance(connected, tuple) and len(connected) > 1 else "connection rejected"
                    raise ConnectionError(str(reason))
                IQ.change_balance(BALANCE_MODE)
                time.sleep(2)
                if not IQ.check_connect():
                    raise ConnectionError("connection dropped after reconnect")
                refresh_assets(force=True)
            except Exception as exc:
                logging.warning("Reconnect failed: %s", exc)
                time.sleep(RECONNECT_SECONDS)
                continue

        if time.time() - ASSET_REFRESH_AT >= ASSET_REFRESH_SECONDS:
            refresh_assets(force=True)

        last_heartbeat = heartbeat(last_heartbeat)
        if not ASSETS:
            time.sleep(SCAN_INTERVAL)
            continue

        for item in list(ASSETS):
            if STOP_EVENT.is_set():
                break
            if risk_stop_reason():
                STOP_EVENT.set()
                break
            try:
                active_id = item.get("id")
                if active_id is not None:
                    try:
                        OP_code.ACTIVES[item["name"]] = int(active_id)
                    except (TypeError, ValueError):
                        pass
                candles = get_closed_candles(item["name"])
                signal = evaluate_signal(candles)
                if signal:
                    candle_key = str(signal["candle"].get("from", ""))
                    last_signals = STATE.setdefault("last_signals", {})
                    if last_signals.get(item["name"]) == candle_key:
                        continue
                    last_signals[item["name"]] = candle_key
                    save_state()
                    log_trade(item["name"], signal["signal"], signal["m10"], signal["m20"], "SIGNAL", "")
                    logging.info(
                        "SIGNAL %s %s | M10=%.4f M20=%.4f extreme=%.4f",
                        item["name"], signal["signal"], signal["m10"], signal["m20"], signal["extreme"])
                    send_telegram(
                        "<b>Confirmed Forex setup</b>\nAsset: {}\nSignal: {}\nM10: {:.4f}\nM20: {:.4f}\n"
                        "Engulfing candle: confirmed\nClosed candle: confirmed".format(
                            item["name"], signal["signal"].upper(), signal["m10"], signal["m20"])
                    )
                    if AUTO_TRADE:
                        place_trade(item, signal)
            except Exception as exc:
                logging.warning("Scan error for %s: %s", item["name"], exc)
            time.sleep(0.15)

        time.sleep(SCAN_INTERVAL)

    logging.info("Scanner finished. %s", format_stats())
    send_telegram("<b>Scanner finished</b>\n{}".format(format_stats()))


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
