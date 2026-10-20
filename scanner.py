
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


# ============================================================
# FOREX OTC BREAKOUT + RETEST WITH IPDA DETECTION
# PRACTICE ACCOUNT ONLY
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

# Original breakout + retest rules.
BREAKOUT_LOOKBACK = 10
RETEST_TOLERANCE = 0.20
MIN_BREAKOUT_BODY_RATIO = 0.50
MAX_RETEST_CANDLES = 6

# IPDA-inspired additional analysis.
REQUIRE_LIQUIDITY_SWEEP = False
REQUIRE_FVG_CONFLUENCE = False
LIQUIDITY_LOOKBACK = 5

# Risk management.
SESSION_PROFIT_TARGET = 5.0
SESSION_LOSS_LIMIT = 3.0
MAX_CONSECUTIVE_LOSSES = 3
MAX_RESOLVED_TRADES = 20

# Runtime settings.
SCAN_INTERVAL = 5
ASSET_REFRESH_SECONDS = 900
HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 30
CANDLE_REQUEST_TIMEOUT = 10
ORDER_RESULT_RETRY_SECONDS = 5
REJECTED_ASSET_COOLDOWN_SECONDS = 600

CANDLE_REQUEST_COUNT = max(
    BREAKOUT_LOOKBACK + MAX_RETEST_CANDLES + 10,
    LIQUIDITY_LOOKBACK + 10,
)

LOG_FILE = "breakout_retest_log.csv"
STATE_FILE = "breakout_state.json"
LAGOS = ZoneInfo("Africa/Lagos")

CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
    "CNY", "HKD", "SGD", "SEK", "NOK", "DKK", "PLN", "CZK",
    "HUF", "TRY", "ZAR", "MXN", "BRL", "INR", "THB", "AED",
    "SAR", "ILS", "KRW", "TWD", "RUB", "RON", "BGN", "ISK",
    "PHP", "IDR", "MYR",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

IQ = None
ASSETS = []
ASSET_REFRESH_AT = 0.0
REJECTED_ASSETS = {}
STOP_EVENT = threading.Event()
TRADE_LOCK = threading.Lock()
STATE_LOCK = threading.RLock()
PENDING_BREAKOUTS = {}


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
        logging.warning("Telegram error: %s", exc)


def fresh_state():
    return {
        "date": now_lagos().date().isoformat(),
        "profit": 0.0,
        "wins": 0,
        "losses": 0,
        "consecutive_losses": 0,
        "resolved": 0,
        "pending_order_id": None,
        "pending_asset": None,
        "pending_direction": None,
        "pending_started": None,
        "last_signals": {},
        "pending_breakouts": {},
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
    else:
        # Reset daily statistics but preserve an unresolved order.
        for key in (
            "pending_order_id",
            "pending_asset",
            "pending_direction",
            "pending_started",
        ):
            current[key] = saved.get(key)

    if not isinstance(current.get("last_signals"), dict):
        current["last_signals"] = {}

    if not isinstance(current.get("pending_breakouts"), dict):
        current["pending_breakouts"] = {}

    return current


STATE = load_state()
PENDING_BREAKOUTS = STATE.get("pending_breakouts", {})


def save_state():
    try:
        with STATE_LOCK:
            STATE["pending_breakouts"] = PENDING_BREAKOUTS

            with open(
                STATE_FILE + ".tmp",
                "w",
                encoding="utf-8",
            ) as handle:
                json.dump(STATE, handle, indent=2)

            os.replace(STATE_FILE + ".tmp", STATE_FILE)

    except OSError as exc:
        logging.warning("State save failed: %s", exc)


def format_stats():
    return (
        "Session P/L: ${:+.2f}\n"
        "Wins: {}\n"
        "Losses: {}\n"
        "Consecutive losses: {}\n"
        "Resolved trades: {}/{}"
    ).format(
        float(STATE.get("profit", 0.0)),
        int(STATE.get("wins", 0)),
        int(STATE.get("losses", 0)),
        int(STATE.get("consecutive_losses", 0)),
        int(STATE.get("resolved", 0)),
        MAX_RESOLVED_TRADES,
    )


def risk_stop_reason():
    profit = round(float(STATE.get("profit", 0.0)), 2)

    if profit >= SESSION_PROFIT_TARGET:
        return "Session profit target reached"

    if profit <= -SESSION_LOSS_LIMIT:
        return "Session loss limit reached"

    if int(STATE.get("consecutive_losses", 0)) >= MAX_CONSECUTIVE_LOSSES:
        return "Maximum consecutive losses reached"

    if int(STATE.get("resolved", 0)) >= MAX_RESOLVED_TRADES:
        return "Maximum resolved trades reached"

    return None


def stop_if_limit_reached():
    reason = risk_stop_reason()

    if reason and not STOP_EVENT.is_set():
        STOP_EVENT.set()
        STATE["stop_reason"] = reason
        save_state()

        logging.warning("%s", reason)
        send_telegram(
            "<b>SCANNER STOPPED</b>\n{}\n{}".format(
                reason,
                format_stats(),
            )
        )

    return bool(reason)


def log_trade(
    asset,
    direction,
    status,
    profit="",
    order_id="",
    level="",
    liquidity_sweep="",
    fvg="",
):
    fields = [
        "timestamp_lagos",
        "session_id",
        "asset",
        "direction",
        "status",
        "profit",
        "order_id",
        "breakout_level",
        "liquidity_sweep",
        "fvg_confluence",
        "session_profit",
    ]

    try:
        exists = os.path.exists(LOG_FILE)

        with open(
            LOG_FILE,
            "a",
            newline="",
            encoding="utf-8",
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)

            if not exists or os.path.getsize(LOG_FILE) == 0:
                writer.writeheader()

            writer.writerow({
                "timestamp_lagos": now_lagos().isoformat(
                    timespec="seconds"
                ),
                "session_id": now_lagos().date().isoformat(),
                "asset": asset,
                "direction": direction,
                "status": status,
                "profit": profit,
                "order_id": order_id,
                "breakout_level": level,
                "liquidity_sweep": liquidity_sweep,
                "fvg_confluence": fvg,
                "session_profit": round(
                    float(STATE.get("profit", 0.0)), 2
                ),
            })

    except Exception as exc:
        logging.warning("CSV logging error: %s", exc)


# ============================================================
# FOREX OTC ASSET DISCOVERY
# ============================================================

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

    # This strategy is restricted to Forex OTC.
    if not pair or not is_otc:
        return

    clean_name = pair + "-OTC"
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
        "id": resolved_id,
        "otc": True,
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
    assets.sort(key=lambda item: item["name"])

    if not assets:
        logging.warning(
            "No OTC forex assets found. Check the asset discovery logs."
        )
        return ASSETS

    ASSETS = assets
    ASSET_REFRESH_AT = time.time()

    logging.info(
        "OTC forex assets discovered: %s",
        len(ASSETS),
    )

    send_telegram(
        "<b>OTC forex assets refreshed</b>\nCount: {}".format(
            len(ASSETS)
        )
    )

    return ASSETS


# ============================================================
# IQ OPTION CONNECTION AND CANDLE FETCHING
# ============================================================

def disable_digital_open_worker(self):
    logging.info("Unused digital-market watcher disabled")


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
            data = getattr(
                self.api.candles,
                "candles_data",
                None,
            )

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
        logging.warning(
            "Candle request failed for %s: %s",
            active,
            exc,
        )

    return None


# Compatibility workaround for the unused digital-market worker.
# It is specific to this library's private implementation and is
# deliberately guarded in case a future version changes the API.
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
                result[0]
                if isinstance(result, tuple)
                else bool(result)
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
                "Strategy: Forex OTC Breakout + Retest\n"
                "Candles: 1 minute\nExpiry: 1 minute\n"
                "Stake: $1 fixed\nRecovery: disabled"
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

    raise RuntimeError("IQ Option connection failed: " + last_error)


def get_closed_candles(asset):
    try:
        raw = IQ.get_candles(
            asset,
            CANDLE_SECONDS,
            CANDLE_REQUEST_COUNT,
            int(time.time()),
        )
    except Exception as exc:
        logging.debug(
            "Candle fetch failed for %s: %s",
            asset,
            exc,
        )
        return []

    if not isinstance(raw, list):
        return []

    try:
        raw.sort(key=lambda row: int(row.get("from", 0)))
    except Exception:
        return []

    current_time = int(time.time())
    closed = []

    for candle in raw:
        try:
            start = int(candle.get("from", 0))

            if start > 0 and start + CANDLE_SECONDS <= current_time:
                closed.append(candle)

        except (TypeError, ValueError):
            continue

    return closed


# ============================================================
# IPDA-INSPIRED LIQUIDITY SWEEP AND FVG DETECTION
# ============================================================

def detect_liquidity_sweep(candles, direction):
    if len(candles) < LIQUIDITY_LOOKBACK + 1:
        return False

    current = candles[-1]
    previous = candles[-(LIQUIDITY_LOOKBACK + 1):-1]

    try:
        high_price = float(current["max"])
        low_price = float(current["min"])
        close_price = float(current["close"])

        recent_high = max(float(c["max"]) for c in previous)
        recent_low = min(float(c["min"]) for c in previous)

    except (KeyError, TypeError, ValueError):
        return False

    if direction == "call":
        # Sweep below recent lows, then close back above them.
        return (
            low_price < recent_low
            and close_price > recent_low
        )

    if direction == "put":
        # Sweep above recent highs, then close back below them.
        return (
            high_price > recent_high
            and close_price < recent_high
        )

    return False


def detect_fvg(candles, direction, level, tolerance):
    if len(candles) < 3:
        return False

    first = candles[-3]
    third = candles[-1]

    try:
        first_high = float(first["max"])
        first_low = float(first["min"])
        third_high = float(third["max"])
        third_low = float(third["min"])

    except (KeyError, TypeError, ValueError):
        return False

    if direction == "call":
        # Bullish FVG: third candle low is above first candle high.
        if third_low > first_high:
            gap_low = first_high
            gap_high = third_low

            return (
                gap_low - tolerance
                <= level
                <= gap_high + tolerance
            )

    if direction == "put":
        # Bearish FVG: third candle high is below first candle low.
        if third_high < first_low:
            gap_low = third_high
            gap_high = first_low

            return (
                gap_low - tolerance
                <= level
                <= gap_high + tolerance
            )

    return False


# ============================================================
# BREAKOUT + RETEST STRATEGY
# ============================================================

def evaluate_breakout_retest(candles, asset):
    """
    Core entry rules:
    1. Find the recent 10-candle resistance and support.
    2. Require a breakout candle with a body/range ratio of at
       least 0.50.
    3. Wait for a later candle to retest the broken level.
    4. Require the retest candle to close back on the breakout side.

    Liquidity sweep and FVG are detected as extra evidence.
    """

    minimum = BREAKOUT_LOOKBACK + 2

    if len(candles) < minimum:
        return None

    try:
        candles = sorted(
            candles,
            key=lambda candle: int(candle.get("from", 0)),
        )

        current = candles[-1]
        candle_time = int(current.get("from", 0))

        current_open = float(current["open"])
        current_close = float(current["close"])
        current_high = float(current["max"])
        current_low = float(current["min"])

    except (KeyError, TypeError, ValueError):
        return None

    if candle_time <= 0:
        return None

    pending = PENDING_BREAKOUTS.get(asset)

    # Check an existing breakout before searching for a new one.
    if pending:
        breakout_time = int(pending["time"])

        later_candles = [
            candle
            for candle in candles
            if int(candle.get("from", 0)) > breakout_time
        ]

        elapsed = len(later_candles)

        if elapsed > MAX_RETEST_CANDLES:
            logging.info(
                "Retest expired: %s %s",
                asset,
                pending["direction"],
            )
            PENDING_BREAKOUTS.pop(asset, None)
            save_state()
            pending = None

        elif elapsed > 0:
            if candle_time <= int(pending.get("last_checked", 0)):
                return None

            pending["last_checked"] = candle_time

            direction = pending["direction"]
            level = float(pending["level"])

            breakout_range = float(pending["breakout_range"])
            tolerance = max(
                breakout_range * RETEST_TOLERANCE,
                0.0,
            )

            if direction == "call":
                touched = (
                    current_low >= level - tolerance
                    and current_low <= level + tolerance
                )

                confirmed = (
                    touched
                    and current_close > level
                    and current_close > current_open
                )

            else:
                touched = (
                    current_high >= level - tolerance
                    and current_high <= level + tolerance
                )

                confirmed = (
                    touched
                    and current_close < level
                    and current_close < current_open
                )

            if not confirmed:
                save_state()
                return None

            sweep = detect_liquidity_sweep(candles, direction)
            fvg = detect_fvg(
                candles,
                direction,
                level,
                tolerance,
            )

            if REQUIRE_LIQUIDITY_SWEEP and not sweep:
                logging.info(
                    "Retest rejected: no liquidity sweep on %s",
                    asset,
                )
                PENDING_BREAKOUTS.pop(asset, None)
                save_state()
                return None

            if REQUIRE_FVG_CONFLUENCE and not fvg:
                logging.info(
                    "Retest rejected: no FVG confluence on %s",
                    asset,
                )
                PENDING_BREAKOUTS.pop(asset, None)
                save_state()
                return None

            PENDING_BREAKOUTS.pop(asset, None)
            save_state()

            confirmation = (
                "Breakout + retest; liquidity sweep={}; FVG={}"
            ).format(sweep, fvg)

            logging.info(
                "RETEST CONFIRMED %s %s level=%.6f sweep=%s fvg=%s",
                asset,
                direction.upper(),
                level,
                sweep,
                fvg,
            )

            return {
                "direction": direction,
                "level": round(level, 6),
                "candle_time": candle_time,
                "liquidity_sweep": sweep,
                "fvg_confluence": fvg,
                "confirmation": confirmation,
            }

    # Search for a fresh breakout.
    history = candles[-(BREAKOUT_LOOKBACK + 1):-1]

    if len(history) < BREAKOUT_LOOKBACK:
        return None

    try:
        resistance = max(float(c["max"]) for c in history)
        support = min(float(c["min"]) for c in history)

        breakout_range = current_high - current_low

        if breakout_range <= 0:
            return None

        body_ratio = (
            abs(current_close - current_open) / breakout_range
        )

    except (KeyError, TypeError, ValueError):
        return None

    if body_ratio < MIN_BREAKOUT_BODY_RATIO:
        return None

    direction = None
    level = None

    if current_close > resistance and current_close > current_open:
        direction = "call"
        level = resistance

    elif current_close < support and current_close < current_open:
        direction = "put"
        level = support

    if direction is None:
        return None

    PENDING_BREAKOUTS[asset] = {
        "direction": direction,
        "level": float(level),
        "time": candle_time,
        "last_checked": candle_time,
        "breakout_range": float(breakout_range),
    }

    save_state()

    logging.info(
        "BREAKOUT DETECTED %s %s level=%.6f; waiting for retest",
        asset,
        direction.upper(),
        level,
    )

    return None


# ============================================================
# ORDER RESULT HANDLING
# ============================================================

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
        for key in (
            "profit", "profit_amount", "profitAmount",
            "close_profit", "closeProfit", "net_profit",
            "netProfit", "win_amount", "winAmount",
        ):
            if key in value:
                result = extract_numeric_profit(value[key])

                if result is not None:
                    return result

        for key in (
            "result", "data", "betinfo", "bet_info", "option",
        ):
            nested = value.get(key)

            if isinstance(nested, (dict, list, tuple)):
                result = extract_numeric_profit(nested)

                if result is not None:
                    return result

    if isinstance(value, (list, tuple)):
        for item in value:
            result = extract_numeric_profit(item)

            if result is not None:
                return result

    return None


def query_order_profit(order_id):
    for method_name in (
        "check_win_v4",
        "check_win",
        "get_betinfo",
    ):
        method = getattr(IQ, method_name, None)

        if not callable(method):
            continue

        try:
            raw = method(order_id)
        except Exception as exc:
            logging.debug(
                "%s failed for order %s: %s",
                method_name,
                order_id,
                exc,
            )
            continue

        value = extract_numeric_profit(raw)

        if value is not None:
            logging.info(
                "Order %s result via %s: %+.2f",
                order_id,
                method_name,
                value,
            )
            return value

    return None


def settle_result(asset, direction, broker_profit, order_id):
    if broker_profit is None:
        raise ValueError("Cannot settle order without a numeric result")

    # Preserve the requested reporting rule:
    # a zero API result is counted as a $1 loss.
    net_change = -STAKE if broker_profit == 0 else float(broker_profit)
    is_win = net_change > 0

    with STATE_LOCK:
        STATE["profit"] = round(
            float(STATE.get("profit", 0.0)) + net_change,
            2,
        )
        STATE["resolved"] = int(STATE.get("resolved", 0)) + 1

        if is_win:
            STATE["wins"] = int(STATE.get("wins", 0)) + 1
            STATE["consecutive_losses"] = 0
        else:
            STATE["losses"] = int(STATE.get("losses", 0)) + 1
            STATE["consecutive_losses"] = (
                int(STATE.get("consecutive_losses", 0)) + 1
            )

        STATE["pending_order_id"] = None
        STATE["pending_asset"] = None
        STATE["pending_direction"] = None
        STATE["pending_started"] = None

    save_state()

    label = "WIN" if is_win else "LOSS"

    log_trade(
        asset,
        direction,
        label,
        net_change,
        order_id,
    )

    logging.info(
        "%s | %s %s | %+.2f | %s",
        label,
        asset,
        direction.upper(),
        net_change,
        format_stats(),
    )

    send_telegram(
        "<b>Trade result: {}</b>\n"
        "Asset: {}\nDirection: {}\n"
        "Broker result: ${:+.2f}\n"
        "Accounted result: ${:+.2f}\n{}".format(
            label,
            asset,
            direction.upper(),
            broker_profit,
            net_change,
            format_stats(),
        )
    )

    stop_if_limit_reached()


def wait_for_order_result(order_id, asset, direction):
    last_notice = 0.0

    while True:
        value = query_order_profit(order_id)

        if value is not None:
            settle_result(
                asset,
                direction,
                value,
                order_id,
            )
            return

        if time.time() - last_notice >= 60:
            logging.warning(
                "Order %s unresolved; no new order will be placed",
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

    logging.warning("Recovering unresolved order %s", order_id)

    send_telegram(
        "<b>Recovering unresolved order</b>\n"
        "Order: {}\nAsset: {}\n"
        "No new order until it is resolved.".format(
            order_id,
            asset,
        )
    )

    wait_for_order_result(
        order_id,
        asset,
        direction,
    )


def place_trade(asset_info, signal):
    asset = asset_info["name"]
    direction = signal["direction"]

    with TRADE_LOCK:
        if STOP_EVENT.is_set() or stop_if_limit_reached():
            return

        if STATE.get("pending_order_id"):
            logging.warning("Pending order exists; skipping new trade")
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
                asset,
                direction,
                "ORDER_ERROR",
                level=signal["level"],
            )
            logging.warning("Order placement error: %s", exc)
            return

        if isinstance(result, tuple):
            success = bool(result[0])
            order_id = result[1] if len(result) > 1 else None
        else:
            success = bool(result)
            order_id = None

        if not success or order_id is None:
            REJECTED_ASSETS[asset] = (
                time.time() + REJECTED_ASSET_COOLDOWN_SECONDS
            )

            log_trade(
                asset,
                direction,
                "ORDER_REJECTED",
                level=signal["level"],
            )

            logging.warning(
                "Order rejected on %s: %r",
                asset,
                result,
            )
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
            "OPENED",
            order_id=order_id,
            level=signal["level"],
            liquidity_sweep=signal["liquidity_sweep"],
            fvg=signal["fvg_confluence"],
        )

        logging.info(
            "Trade opened: %s %s order=%s",
            asset,
            direction.upper(),
            order_id,
        )

        send_telegram(
            "<b>Practice trade opened</b>\n"
            "Strategy: Breakout + Retest\n"
            "Asset: {}\nDirection: {}\n"
            "Stake: $1\nExpiry: 1 minute\n"
            "Breakout level: {}\n"
            "Liquidity sweep: {}\nFVG confluence: {}".format(
                asset,
                direction.upper(),
                signal["level"],
                signal["liquidity_sweep"],
                signal["fvg_confluence"],
            )
        )

    wait_for_order_result(
        order_id,
        asset,
        direction,
    )


# ============================================================
# MAIN SCANNER LOOP
# ============================================================

def main():
    connect_iq()
    refresh_assets(force=True)

    if not ASSETS:
        raise RuntimeError(
            "No OTC forex assets discovered. Check discovery logs."
        )

    if stop_if_limit_reached():
        return

    if STATE.get("pending_order_id"):
        recover_pending_order()

    if STOP_EVENT.is_set() or stop_if_limit_reached():
        return

    send_telegram(
        "<b>Breakout + Retest scanner started</b>\n"
        "Mode: PRACTICE\n"
        "Market: Forex OTC only\n"
        "Candles: 1 minute\nExpiry: 1 minute\n"
        "Stake: fixed $1\n"
        "Profit target: +$5\nLoss limit: -$3\n"
        "Max consecutive losses: 3\n"
        "Max resolved trades: 20\n"
        "Recovery staking: disabled\n{}".format(format_stats())
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
                    result[0]
                    if isinstance(result, tuple)
                    else bool(result)
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
                "Heartbeat | OTC assets: %s | %s",
                len(ASSETS),
                format_stats(),
            )

            send_telegram(
                "<b>Scanner heartbeat</b>\n"
                "OTC assets: {}\n{}".format(
                    len(ASSETS),
                    format_stats(),
                )
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

                signal = evaluate_breakout_retest(
                    candles,
                    asset,
                )

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
                    "SIGNAL",
                    level=signal["level"],
                    liquidity_sweep=signal["liquidity_sweep"],
                    fvg=signal["fvg_confluence"],
                )

                logging.info(
                    "CONFIRMED %s %s level=%s sweep=%s fvg=%s",
                    asset,
                    signal["direction"].upper(),
                    signal["level"],
                    signal["liquidity_sweep"],
                    signal["fvg_confluence"],
                )

                send_telegram(
                    "<b>Breakout + Retest confirmed</b>\n"
                    "Asset: {}\nDirection: {}\n"
                    "Level: {}\nLiquidity sweep: {}\n"
                    "FVG confluence: {}".format(
                        asset,
                        signal["direction"].upper(),
                        signal["level"],
                        signal["liquidity_sweep"],
                        signal["fvg_confluence"],
                    )
                )

                if AUTO_TRADE:
                    place_trade(item, signal)

            except Exception as exc:
                logging.warning(
                    "Scan error for %s: %s",
                    asset,
                    exc,
                )

            time.sleep(0.15)

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        STOP_EVENT.set()
        save_state()
        logging.info("Scanner stopped manually")

    except Exception as exc:
        logging.exception("Fatal scanner error")
        send_telegram(
            "<b>Scanner error</b>\n{}".format(exc)
        )
        save_state()
        raise
