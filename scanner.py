import os
import csv
import time
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import iqoptionapi.constants as OP_code
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# MOMENTUM 10 EXTREME-REVERSAL
# PRACTICE MODE WITH DAILY PROFIT / LOSS STOPS
# ============================================================

MOMENTUM_PERIOD = 10
MOMENTUM_LOOKBACK = 50
EXTREME_PERCENTILE = 0.10
REQUIRE_TURN = True
MIN_TURN_DISTANCE = 0.03

CANDLE_SECONDS = 60
EXPIRY_MINUTES = 1

AUTO_TRADE = True
BALANCE_MODE = "PRACTICE"
STAKE = 1.0
TARGET_TRADES = 50

DAILY_PROFIT_TARGET = 20.0
DAILY_LOSS_LIMIT = -5.0
DAILY_TIMEZONE = "Africa/Lagos"

SCAN_INTERVAL = 2
ASSET_REFRESH_SECONDS = 900
HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 15
MAX_ASSETS = 70

LOG_FILE = "momentum_signal_log.csv"
CANDLE_REQUEST_TIMEOUT = 8


# ============================================================
# SECRETS
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ============================================================
# GLOBAL STATE
# ============================================================

api = None
otc_assets = []
otc_active_ids = {}

trade_count = 0
wins = 0
losses = 0
pending_results = 0

daily_net_profit = 0.0
daily_limit_stopped = False
daily_date = None
daily_lock = threading.RLock()
trade_lock = threading.Lock()
active_order_ids = set()

last_signal_candle = {}
extreme_state = {}
stop_event = threading.Event()


# ============================================================
# DAILY ACCOUNTING
# ============================================================

def get_daily_date():
    return datetime.now(
        ZoneInfo(DAILY_TIMEZONE)
    ).date().isoformat()


def read_today_profit_from_log():
    total = 0.0
    today = get_daily_date()

    if not os.path.exists(LOG_FILE):
        return total

    try:
        with open(
            LOG_FILE,
            "r",
            newline="",
            encoding="utf-8",
        ) as file:
            reader = csv.DictReader(file)

            for row in reader:
                stamp = row.get("time", "")
                profit_text = row.get("profit", "")

                try:
                    parsed = datetime.fromisoformat(
                        stamp.replace("Z", "+00:00")
                    )
                    local_day = parsed.astimezone(
                        ZoneInfo(DAILY_TIMEZONE)
                    ).date().isoformat()
                    profit = float(profit_text)
                except Exception:
                    continue

                if local_day == today:
                    total += profit

    except Exception as exc:
        print(
            "Daily log recovery error: " + str(exc),
            flush=True,
        )

    return round(total, 2)


def check_daily_limits():
    global daily_date
    global daily_net_profit
    global daily_limit_stopped

    with daily_lock:
        today = get_daily_date()

        if daily_date != today:
            daily_date = today
            daily_net_profit = read_today_profit_from_log()
            daily_limit_stopped = False

            print(
                "Daily P/L restored: $"
                + str(round(daily_net_profit, 2)),
                flush=True,
            )

        if daily_net_profit >= DAILY_PROFIT_TARGET:
            daily_limit_stopped = True

        if daily_net_profit <= DAILY_LOSS_LIMIT:
            daily_limit_stopped = True

        return daily_limit_stopped


def update_daily_profit(profit):
    global daily_net_profit
    global daily_limit_stopped

    with daily_lock:
        if daily_date != get_daily_date():
            print(
                "Trade result crossed a calendar-day boundary. "
                "Daily total will be recalculated.",
                flush=True,
            )
            # check_daily_limits performs date reset and log recovery.
            pass

        daily_net_profit = round(
            read_today_profit_from_log(),
            2,
        )

        if daily_net_profit >= DAILY_PROFIT_TARGET:
            daily_limit_stopped = True
        elif daily_net_profit <= DAILY_LOSS_LIMIT:
            daily_limit_stopped = True

        print(
            "DAILY NET P/L: $"
            + str(daily_net_profit),
            flush=True,
        )


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }

    try:
        response = requests.post(
            url,
            data=data,
            timeout=15,
        )
        if not response.ok:
            print(
                "Telegram HTTP error: "
                + str(response.status_code),
                flush=True,
            )
    except Exception as exc:
        print("Telegram error:", exc, flush=True)


# ============================================================
# CSV LOG
# ============================================================

def init_log():
    if os.path.exists(LOG_FILE):
        return

    try:
        with open(
            LOG_FILE,
            "w",
            newline="",
            encoding="utf-8",
        ) as file:
            writer = csv.writer(file)
            writer.writerow([
                "time",
                "asset",
                "direction",
                "signal_id",
                "entry_price",
                "result",
                "profit",
            ])
    except Exception as exc:
        print("Log init error:", exc, flush=True)


def log_trade(
    asset,
    direction,
    signal_id,
    entry_price,
    result,
    profit,
):
    try:
        with open(
            LOG_FILE,
            "a",
            newline="",
            encoding="utf-8",
        ) as file:
            writer = csv.writer(file)
            writer.writerow([
                datetime.now(ZoneInfo(DAILY_TIMEZONE)).isoformat(),
                asset,
                direction,
                signal_id,
                entry_price,
                result,
                profit,
            ])
    except Exception as exc:
        print("Log error:", exc, flush=True)


# ============================================================
# IQ OPTION CONNECTION
# ============================================================

def connect_iq():
    global api

    if not IQ_EMAIL or not IQ_PASSWORD:
        print(
            "Missing IQ_EMAIL or IQ_PASSWORD secret.",
            flush=True,
        )
        return False

    print("Connecting to IQ Option...", flush=True)

    try:
        candidate = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
        connected, reason = candidate.connect()

        if not connected:
            print(
                "IQ OPTION CONNECTION FAILED: "
                + str(reason),
                flush=True,
            )
            return False

        candidate.change_balance(BALANCE_MODE)

        try:
            balance_name = str(candidate.get_balance_mode()).upper()
        except Exception:
            balance_name = ""

        if balance_name and "PRACTICE" not in balance_name:
            print(
                "SAFETY STOP: account mode is not PRACTICE: "
                + balance_name,
                flush=True,
            )
            try:
                candidate.api.close()
            except Exception:
                pass
            return False

        api = candidate
        print("IQ OPTION CONNECTED", flush=True)
        print(
            "Balance mode verified: "
            + (balance_name or "change_balance accepted; mode API unavailable"),
            flush=True,
        )
        return True

    except Exception as exc:
        print("Connection error:", exc, flush=True)
        api = None
        return False


# ============================================================
# OTC ASSET DISCOVERY (ORIGINAL LOGIC)
# ============================================================

def get_otc_assets():
    global otc_assets
    global otc_active_ids

    print("Preparing OTC assets...", flush=True)

    try:
        init_data = None

        try:
            init_data = api.get_all_init_v2()
        except Exception as exc:
            print(
                "get_all_init_v2 warning: " + str(exc),
                flush=True,
            )

        new_assets = []
        new_active_ids = {}

        if isinstance(init_data, dict):
            sections = [
                init_data.get("turbo", {}),
                init_data.get("binary", {}),
            ]

            for section in sections:
                if not isinstance(section, dict):
                    continue

                actives = section.get("actives", {})
                if not isinstance(actives, dict):
                    continue

                for key, info in actives.items():
                    if not isinstance(info, dict):
                        continue

                    active_id = info.get("active_id")
                    if active_id is None:
                        active_id = key

                    try:
                        active_id = int(active_id)
                    except Exception:
                        continue

                    name = info.get("name", "")
                    if not name:
                        continue

                    name = str(name)
                    if "." in name:
                        name = name.split(".", 1)[1]

                    if "-OTC" not in name:
                        continue

                    if info.get("enabled") is False:
                        continue

                    if info.get("is_suspended") is True:
                        continue

                    if name in new_active_ids:
                        continue

                    new_assets.append(name)
                    new_active_ids[name] = active_id

                    if len(new_assets) >= MAX_ASSETS:
                        break

                if len(new_assets) >= MAX_ASSETS:
                    break

        if not new_assets:
            print(
                "Primary OTC discovery returned no assets.",
                flush=True,
            )

            try:
                legacy_data = api.get_all_init()
            except Exception as exc:
                print(
                    "get_all_init warning: " + str(exc),
                    flush=True,
                )
                legacy_data = None

            if isinstance(legacy_data, dict):
                result = legacy_data.get("result", {})

                if isinstance(result, dict):
                    sections = [
                        result.get("turbo", {}),
                        result.get("binary", {}),
                    ]

                    for section in sections:
                        if not isinstance(section, dict):
                            continue

                        actives = section.get("actives", {})
                        if not isinstance(actives, dict):
                            continue

                        for key, info in actives.items():
                            if not isinstance(info, dict):
                                continue

                            try:
                                active_id = int(key)
                            except Exception:
                                continue

                            name = info.get("name", "")
                            if not name:
                                continue

                            name = str(name)
                            if "." in name:
                                name = name.split(".", 1)[1]

                            if "-OTC" not in name:
                                continue

                            if info.get("enabled") is False:
                                continue

                            if info.get("is_suspended") is True:
                                continue

                            if name in new_active_ids:
                                continue

                            new_assets.append(name)
                            new_active_ids[name] = active_id

                            if len(new_assets) >= MAX_ASSETS:
                                break

                        if len(new_assets) >= MAX_ASSETS:
                            break

        if not new_assets:
            print("No OPEN OTC assets found.", flush=True)
            return False

        otc_assets = new_assets
        otc_active_ids = new_active_ids

        for asset_name, active_id in otc_active_ids.items():
            OP_code.ACTIVES[asset_name] = active_id

        print("OTC ASSETS READY", flush=True)
        print(
            "Found "
            + str(len(otc_assets))
            + " OPEN OTC assets.",
            flush=True,
        )
        print(
            "IQ active-code mappings loaded: "
            + str(len(otc_active_ids)),
            flush=True,
        )
        return True

    except Exception as exc:
        print("OTC discovery error:", exc, flush=True)
        return False


# ============================================================
# CANDLE DATA (ORIGINAL LOGIC)
# ============================================================

def get_candles(asset, count):
    active_id = otc_active_ids.get(asset)

    if active_id is None:
        print("No active ID for " + asset, flush=True)
        return []

    try:
        api.api.candles.candles_data = []

        server_time = time.time()
        try:
            server_time = api.api.timesync.server_timestamp
        except Exception:
            pass

        api.api.getcandles(
            active_id,
            CANDLE_SECONDS,
            count,
            server_time,
        )

        started = time.time()

        while time.time() - started < CANDLE_REQUEST_TIMEOUT:
            candles = api.api.candles.candles_data
            if candles:
                break
            time.sleep(0.1)

        candles = api.api.candles.candles_data
        if not candles:
            return []

        result = []

        for candle in candles:
            if not isinstance(candle, dict):
                continue

            try:
                result.append({
                    "from": float(candle.get("from")),
                    "open": float(candle.get("open")),
                    "close": float(candle.get("close")),
                    "high": float(candle.get("max")),
                    "low": float(candle.get("min")),
                })
            except Exception:
                continue

        return result

    except Exception as exc:
        print(
            "Candle error " + asset + ": " + str(exc),
            flush=True,
        )
        return []


def remove_open_candle(candles):
    if len(candles) < 2:
        return candles

    now = time.time()
    last = candles[-1]
    candle_from = last.get("from", 0)

    if now - candle_from < CANDLE_SECONDS:
        return candles[:-1]

    return candles


# ============================================================
# MOMENTUM 10 (SIGNAL RULES UNCHANGED)
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

        momentum = (current / previous) * 100.0
        values.append(momentum)

    return values


def analyze_momentum(candles):
    if len(candles) < MOMENTUM_LOOKBACK + 3:
        return None

    momentum_values = calculate_momentum(candles)

    if len(momentum_values) < MOMENTUM_LOOKBACK + 3:
        return None

    recent = momentum_values[-MOMENTUM_LOOKBACK:]
    current = momentum_values[-1]
    previous = momentum_values[-2]
    previous_previous = momentum_values[-3]
    sorted_values = sorted(recent)

    low_index = int(len(sorted_values) * EXTREME_PERCENTILE)
    high_index = int(len(sorted_values) * (1.0 - EXTREME_PERCENTILE))

    if low_index >= len(sorted_values):
        low_index = len(sorted_values) - 1

    if high_index >= len(sorted_values):
        high_index = len(sorted_values) - 1

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
            turn_distance = current - previous

            if turn_distance >= MIN_TURN_DISTANCE:
                signal = "CALL"

                if recent_low != 0:
                    reversal_strength = (
                        turn_distance / abs(recent_low)
                    ) * 100.0

    elif current >= high_threshold:
        extreme = "HIGH"

        if current < previous and previous > previous_previous:
            turn_distance = previous - current

            if turn_distance >= MIN_TURN_DISTANCE:
                signal = "PUT"

                if recent_high != 0:
                    reversal_strength = (
                        turn_distance / abs(recent_high)
                    ) * 100.0

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
# SIGNAL MESSAGE (ORIGINAL CONTENT)
# ============================================================

def build_signal_message(
    asset,
    direction,
    analysis,
    price,
    signal_id,
):
    return (
        "🔔 <b>MOMENTUM 10 SIGNAL</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Asset: " + asset + "\n"
        "Direction: " + direction + "\n"
        "Timeframe: 1M\n"
        "Expiry: 1 minute\n"
        "Strategy: Momentum 10 Extreme-Reversal\n"
        "Extreme: " + str(analysis["extreme"]) + "\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Momentum: " + str(round(analysis["momentum"], 5)) + "\n"
        "Previous: " + str(round(analysis["previous"], 5)) + "\n"
        "Previous 2: " + str(round(analysis["previous_previous"], 5)) + "\n"
        "Recent Low: " + str(round(analysis["recent_low"], 5)) + "\n"
        "Recent High: " + str(round(analysis["recent_high"], 5)) + "\n"
        "Low Threshold: " + str(round(analysis["low_threshold"], 5)) + "\n"
        "High Threshold: " + str(round(analysis["high_threshold"], 5)) + "\n"
        "Reversal Strength: "
        + str(round(analysis["reversal_strength"], 1))
        + "%\n"
        "Price: " + str(price) + "\n"
        "Signal ID: " + signal_id
    )


# ============================================================
# TRADE RESULT MONITOR
# ============================================================

def monitor_trade(
    order_id,
    asset,
    direction,
    signal_id,
    entry_price,
):
    global pending_results
    global wins
    global losses

    try:
        time.sleep((EXPIRY_MINUTES * 60) + 5)
        result = None

        for _ in range(20):
            try:
                result = api.check_win_v4(order_id)
            except Exception:
                result = None

            if result is not None:
                break

            time.sleep(1)

        if result is None:
            print(
                "Could not get result for " + asset
                + ". Daily limit remains conservative only "
                "until result is recovered.",
                flush=True,
            )
            return

        try:
            profit = float(result)
        except Exception:
            print(
                "Unparseable trade result; not counted as zero.",
                flush=True,
            )
            return

        if profit > 0:
            wins += 1
            result_text = "WIN"
        elif profit < 0:
            losses += 1
            result_text = "LOSS"
        else:
            result_text = "DRAW"

        log_trade(
            asset,
            direction,
            signal_id,
            entry_price,
            result_text,
            profit,
        )
        update_daily_profit(profit)

        print(
            "RESULT " + asset + " " + result_text
            + " " + str(profit),
            flush=True,
        )

        send_telegram(
            "📊 <b>MOMENTUM 10 RESULT</b>\n"
            "Asset: " + asset + "\n"
            "Direction: " + direction + "\n"
            "Result: " + result_text + "\n"
            "Profit: " + str(profit) + "\n"
            "Daily net P/L: $" + str(round(daily_net_profit, 2)) + "\n"
            "Signal ID: " + signal_id
        )

        if check_daily_limits():
            send_telegram(
                "🛑 <b>DAILY LIMIT REACHED</b>\n"
                "Daily net P/L: $"
                + str(round(daily_net_profit, 2))
                + "\nNo new trades will be opened."
            )

    except Exception as exc:
        print("Result monitor error: " + str(exc), flush=True)

    finally:
        with trade_lock:
            pending_results -= 1
            active_order_ids.discard(order_id)


# ============================================================
# EXECUTE TRADE
# ============================================================

def execute_trade(
    asset,
    direction,
    signal_id,
    entry_price,
):
    global trade_count
    global pending_results

    with trade_lock:
        if check_daily_limits():
            print("Daily limit reached. Trade blocked.", flush=True)
            return False

        if trade_count >= TARGET_TRADES:
            print("Trade-count target reached. Trade blocked.", flush=True)
            return False

        if pending_results > 0:
            print(
                "Existing result pending; skipping new trade "
                "to keep daily limits controlled.",
                flush=True,
            )
            return False

        try:
            if not api.check_connect():
                print("Connection lost; trade blocked.", flush=True)
                return False
        except Exception:
            print("Connection check failed; trade blocked.", flush=True)
            return False

        action = direction.lower()

        print(
            "Attempting trade: " + asset + " " + action,
            flush=True,
        )

        try:
            order_result = api.buy(
                STAKE,
                asset,
                action,
                EXPIRY_MINUTES,
            )
        except Exception as exc:
            print("Trade request error: " + str(exc), flush=True)
            return False

        if isinstance(order_result, tuple):
            success = order_result[0]
            order_id = order_result[1]
        else:
            success = bool(order_result)
            order_id = order_result

        if not success or order_id is None:
            print(
                "TRADE FAILED " + asset + " " + direction,
                flush=True,
            )
            return False

        trade_count += 1
        pending_results += 1
        active_order_ids.add(order_id)

    print(
        "TRADE OPENED " + asset + " " + direction
        + " Order: " + str(order_id),
        flush=True,
    )

    send_telegram(
        "✅ <b>TRADE OPENED</b>\n"
        "Asset: " + asset + "\n"
        "Direction: " + direction + "\n"
        "Stake: $" + str(STAKE) + "\n"
        "Expiry: 1 minute\n"
        "Daily target: +$" + str(DAILY_PROFIT_TARGET) + "\n"
        "Daily loss limit: -$" + str(abs(DAILY_LOSS_LIMIT)) + "\n"
        "Signal ID: " + signal_id
    )

    thread = threading.Thread(
        target=monitor_trade,
        args=(
            order_id,
            asset,
            direction,
            signal_id,
            entry_price,
        ),
        daemon=True,
    )
    thread.start()
    return True


# ============================================================
# PROCESS ASSET
# ============================================================

def process_asset(asset):
    try:
        if check_daily_limits():
            return

        print("Scanning: " + asset, flush=True)

        candles = get_candles(
            asset,
            MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 10,
        )

        if not candles:
            return

        candles = remove_open_candle(candles)

        if len(candles) < MOMENTUM_LOOKBACK + 3:
            return

        analysis = analyze_momentum(candles)
        if not analysis:
            return

        signal = analysis["signal"]
        if signal is None:
            return

        current_candle = candles[-1]
        candle_id = current_candle["from"]

        if last_signal_candle.get(asset) == candle_id:
            return

        extreme = analysis["extreme"]
        if extreme_state.get(asset) == extreme:
            return

        last_signal_candle[asset] = candle_id
        extreme_state[asset] = extreme

        price = current_candle["close"]
        signal_id = (
            "M10-" + asset.replace("-", "") + "-"
            + signal + "-" + str(int(time.time()))
        )

        message = build_signal_message(
            asset,
            signal,
            analysis,
            price,
            signal_id,
        )

        print("", flush=True)
        print(message.replace("<b>", "").replace("</b>", ""), flush=True)
        send_telegram(message)

        if AUTO_TRADE:
            execute_trade(
                asset,
                signal,
                signal_id,
                price,
            )

    except Exception as exc:
        print(
            "Process asset error " + asset + ": " + str(exc),
            flush=True,
        )


# ============================================================
# HEARTBEAT
# ============================================================

def heartbeat():
    with daily_lock:
        current_daily_profit = daily_net_profit
        stopped = daily_limit_stopped

    win_rate = 0.0
    if trade_count > 0:
        win_rate = (wins / trade_count) * 100.0

    print("", flush=True)
    print("MOMENTUM 10 BOT ALIVE", flush=True)
    print("Connection: OK", flush=True)
    print("Balance: " + BALANCE_MODE, flush=True)
    print("Auto Trading: " + str(AUTO_TRADE), flush=True)
    print("Strategy: Momentum 10", flush=True)
    print("Timeframe: 1M", flush=True)
    print("Expiry: 1 minute", flush=True)
    print("OTC Assets: " + str(len(otc_assets)), flush=True)
    print(
        "Trades: " + str(trade_count) + "/" + str(TARGET_TRADES),
        flush=True,
    )
    print("Pending Results: " + str(pending_results), flush=True)
    print("Wins: " + str(wins), flush=True)
    print("Losses: " + str(losses), flush=True)
    print("Win Rate: " + str(round(win_rate, 1)) + "%", flush=True)
    print("Daily net P/L: $" + str(round(current_daily_profit, 2)), flush=True)
    print("Daily target: +$" + str(DAILY_PROFIT_TARGET), flush=True)
    print("Daily loss limit: -$" + str(abs(DAILY_LOSS_LIMIT)), flush=True)
    print("Daily stopped: " + str(stopped), flush=True)


# ============================================================
# MAIN
# ============================================================

def main():
    global api

    init_log()

    # Restore today's completed P/L before connecting/trading.
    check_daily_limits()

    print("MOMENTUM 10 BOT STARTING", flush=True)
    print("Mode: " + BALANCE_MODE, flush=True)
    print("Auto Trading: " + str(AUTO_TRADE), flush=True)
    print("Strategy: Momentum 10 Extreme-Reversal", flush=True)
    print("Timeframe: 1M", flush=True)
    print("Expiry: 1 minute", flush=True)
    print("Stake: $" + str(STAKE), flush=True)
    print("Trade target: " + str(TARGET_TRADES), flush=True)
    print("Daily net profit target: +$" + str(DAILY_PROFIT_TARGET), flush=True)
    print("Daily net loss limit: -$" + str(abs(DAILY_LOSS_LIMIT)), flush=True)
    print("Timezone: " + DAILY_TIMEZONE, flush=True)
    print("Continuous scanning: ON", flush=True)

    last_asset_refresh = 0
    last_heartbeat = 0

    while not stop_event.is_set():
        if check_daily_limits():
            print(
                "DAILY LIMIT REACHED. No new trades will be opened.",
                flush=True,
            )
            break

        if trade_count >= TARGET_TRADES:
            print("TRADE COUNT TARGET REACHED.", flush=True)
            send_telegram(
                "🎯 <b>TRADE COUNT TARGET REACHED</b>\n"
                "Trades: " + str(trade_count) + "\n"
                "Wins: " + str(wins) + "\n"
                "Losses: " + str(losses)
            )
            break

        if api is None:
            if not connect_iq():
                time.sleep(RECONNECT_SECONDS)
                continue

        try:
            if not api.check_connect():
                print("IQ Option disconnected.", flush=True)
                api = None
                otc_assets = []
                otc_active_ids = {}
                time.sleep(RECONNECT_SECONDS)
                continue
        except Exception:
            api = None
            otc_assets = []
            otc_active_ids = {}
            time.sleep(RECONNECT_SECONDS)
            continue

        if not otc_assets:
            if not get_otc_assets():
                print("OTC assets unavailable. Retrying...", flush=True)
                time.sleep(RECONNECT_SECONDS)
                continue

            last_asset_refresh = time.time()
            last_heartbeat = time.time()

        now = time.time()

        if now - last_asset_refresh >= ASSET_REFRESH_SECONDS:
            if get_otc_assets():
                last_asset_refresh = now

        if now - last_heartbeat >= HEARTBEAT_SECONDS:
            heartbeat()
            last_heartbeat = now

        for asset in list(otc_assets):
            if stop_event.is_set():
                break
            if check_daily_limits():
                break
            if trade_count >= TARGET_TRADES:
                break
            process_asset(asset)
            time.sleep(SCAN_INTERVAL)

        time.sleep(SCAN_INTERVAL)

    # Wait briefly for in-flight result monitors before process exit.
    wait_started = time.time()
    while pending_results > 0 and time.time() - wait_started < 180:
        print(
            "Waiting for pending results: "
            + str(pending_results),
            flush=True,
        )
        time.sleep(2)

    print(
        "Scanner stopped. Daily net P/L: $"
        + str(round(daily_net_profit, 2)),
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Bot stopped.", flush=True)
    except Exception as exc:
        print("FATAL ERROR: " + str(exc), flush=True)
