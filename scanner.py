import os
import json
import time
import math
import signal
import requests
import websocket
from datetime import datetime, timezone


# ============================================================
# ZETA MOMENTUM 10
# DERIV DEMO — 50 TRADE TEST
# ============================================================

APP_ID = os.getenv("DERIV_APP_ID", "").strip()
PAT = os.getenv("DERIV_PAT", "").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

API_BASE = "https://api.derivws.com"

SYMBOL = "frxEURUSD"
DISPLAY_SYMBOL = "EUR/USD"

# ------------------------------------------------------------
# TEST CONFIGURATION
# ------------------------------------------------------------

TIMEFRAME_SECONDS = 60

MOMENTUM_PERIOD = 10
MOMENTUM_LOOKBACK = 50
EXTREME_PERCENTILE = 0.10

REQUIRE_TURN = True
MIN_TURN_DISTANCE = 0.02

EXPIRY_MINUTES = 15
STAKE = 1.0

TARGET_TRADES = 50

DEMO_ONLY = True
AUTO_TRADE = True

CANDLE_COUNT = 180

HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 10

REQUEST_TIMEOUT = 20
WS_TIMEOUT = 40

STATE_FILE = "trade_state.json"

RUNNING = True


# ============================================================
# GLOBAL STATE
# ============================================================

ws = None
account_id = None

last_signal_candle = None
last_extreme_state = None

completed_trades = 0
wins = 0
losses = 0
total_profit = 0.0

active_contract = None
last_heartbeat = 0

request_counter = 100


# ============================================================
# LOGGING
# ============================================================

def log(message):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] {message}", flush=True)


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }

    try:
        requests.post(
            url,
            json=payload,
            timeout=15,
        )
    except Exception as exc:
        log(f"Telegram error: {exc}")


# ============================================================
# STATE
# ============================================================

def load_state():
    global completed_trades
    global wins
    global losses
    global total_profit

    if not os.path.exists(STATE_FILE):
        return

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        completed_trades = int(data.get("completed_trades", 0))
        wins = int(data.get("wins", 0))
        losses = int(data.get("losses", 0))
        total_profit = float(data.get("total_profit", 0.0))

        log(
            f"State restored: "
            f"{completed_trades}/{TARGET_TRADES} trades, "
            f"{wins}W/{losses}L, "
            f"P/L ${total_profit:.2f}"
        )

    except Exception as exc:
        log(f"Could not load state: {exc}")


def save_state():
    data = {
        "completed_trades": completed_trades,
        "wins": wins,
        "losses": losses,
        "total_profit": round(total_profit, 2),
        "target_trades": TARGET_TRADES,
        "symbol": SYMBOL,
        "momentum_period": MOMENTUM_PERIOD,
        "expiry_minutes": EXPIRY_MINUTES,
    }

    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log(f"Could not save state: {exc}")


# ============================================================
# REQUEST IDS
# ============================================================

def next_req_id():
    global request_counter
    request_counter += 1
    return request_counter


# ============================================================
# DERIV REST
# ============================================================

def rest_headers():
    return {
        "Authorization": f"Bearer {PAT}",
        "Deriv-App-ID": APP_ID,
        "Content-Type": "application/json",
    }


def validate_environment():
    if not APP_ID:
        raise RuntimeError("DERIV_APP_ID secret is missing.")

    if not PAT:
        raise RuntimeError("DERIV_PAT secret is missing.")

    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN secret is missing.")

    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID secret is missing.")

    if not DEMO_ONLY:
        raise RuntimeError("DEMO_ONLY must remain True.")

    if not AUTO_TRADE:
        raise RuntimeError("AUTO_TRADE is disabled.")

    if STAKE <= 0:
        raise RuntimeError("Invalid stake.")

    if EXPIRY_MINUTES != 15:
        raise RuntimeError("This test must use 15-minute expiry.")

    if MOMENTUM_PERIOD != 10:
        raise RuntimeError("This first test must use Momentum 10.")


def get_demo_account():
    url = f"{API_BASE}/trading/v1/options/accounts"

    response = requests.get(
        url,
        headers=rest_headers(),
        timeout=REQUEST_TIMEOUT,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Account request failed: HTTP {response.status_code} "
            f"{response.text[:500]}"
        )

    data = response.json().get("data")

    accounts = []

    if isinstance(data, list):
        accounts = data
    elif isinstance(data, dict):
        accounts = [data]

    for account in accounts:
        aid = account.get("account_id")
        account_type = str(
            account.get("account_type", "")
        ).lower()

        status = str(
            account.get("status", "")
        ).lower()

        if (
            aid
            and account_type == "demo"
            and status in ("active", "")
        ):
            return account

    raise RuntimeError(
        "No active Deriv demo Options account was found."
    )


def get_ws_url(account):
    aid = account.get("account_id")

    if not aid:
        raise RuntimeError("Demo account has no account_id.")

    url = (
        f"{API_BASE}/trading/v1/options/accounts/"
        f"{aid}/otp"
    )

    response = requests.post(
        url,
        headers=rest_headers(),
        timeout=REQUEST_TIMEOUT,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"OTP request failed: HTTP {response.status_code} "
            f"{response.text[:500]}"
        )

    data = response.json().get("data", {})
    ws_url = data.get("url")

    if not ws_url:
        raise RuntimeError("Deriv OTP response did not contain a WebSocket URL.")

    # HARD DEMO SAFETY CHECK
    if "/options/ws/demo" not in ws_url:
        raise RuntimeError(
            "SAFETY STOP: Deriv did not return the demo WebSocket endpoint."
        )

    return ws_url


# ============================================================
# WEBSOCKET
# ============================================================

def connect():
    global ws
    global account_id

    account = get_demo_account()
    account_id = account["account_id"]

    balance = account.get("balance")
    currency = account.get("currency", "USD")

    log(
        f"Demo account: {account_id} | "
        f"Balance: {balance} {currency}"
    )

    ws_url = get_ws_url(account)

    log("Requesting authenticated DEMO WebSocket...")

    ws = websocket.create_connection(
        ws_url,
        timeout=WS_TIMEOUT,
        enable_multithread=True,
    )

    ws.settimeout(WS_TIMEOUT)

    log("🟢 DEMO WebSocket connected.")

    return ws


def close_ws():
    global ws

    try:
        if ws:
            ws.close()
    except Exception:
        pass

    ws = None


def ws_send(payload):
    if ws is None:
        raise RuntimeError("WebSocket is not connected.")

    ws.send(json.dumps(payload))


def ws_request(payload, expected_types=None, timeout=20):
    if expected_types is None:
        expected_types = set()

    req_id = next_req_id()

    payload = dict(payload)
    payload["req_id"] = req_id

    ws_send(payload)

    deadline = time.time() + timeout

    while time.time() < deadline:
        remaining = max(1, deadline - time.time())

        ws.settimeout(remaining)

        raw = ws.recv()

        if not raw:
            continue

        data = json.loads(raw)

        if data.get("error"):
            error = data["error"]

            raise RuntimeError(
                f"{error.get('code', 'APIError')} - "
                f"{error.get('message', 'Unknown error')}"
            )

        if data.get("req_id") == req_id:
            return data

        if data.get("msg_type") in expected_types:
            return data

    raise TimeoutError(
        f"Timed out waiting for request {req_id}"
    )


# ============================================================
# MARKET DATA
# ============================================================

def get_candles():
    response = ws_request(
        {
            "ticks_history": SYMBOL,
            "end": "latest",
            "count": CANDLE_COUNT,
            "style": "candles",
            "granularity": TIMEFRAME_SECONDS,
            "subscribe": 0,
        },
        expected_types={"candles"},
        timeout=20,
    )

    candles = response.get("candles", [])

    if not candles:
        raise RuntimeError("Deriv returned no candles.")

    clean = []

    for candle in candles:
        try:
            clean.append(
                {
                    "epoch": int(candle["epoch"]),
                    "open": float(candle["open"]),
                    "high": float(candle["high"]),
                    "low": float(candle["low"]),
                    "close": float(candle["close"]),
                }
            )
        except Exception:
            continue

    clean.sort(key=lambda x: x["epoch"])

    if len(clean) < MOMENTUM_PERIOD + 5:
        raise RuntimeError(
            f"Not enough candles: {len(clean)}"
        )

    return clean


# ============================================================
# MOMENTUM ENGINE
# ============================================================

def calculate_momentum(candles):
    closes = []

    for candle in candles:
        try:
            close = float(candle["close"])

            if close > 0:
                closes.append(close)

        except Exception:
            continue

    if len(closes) < MOMENTUM_PERIOD + 3:
        return []

    momentum = []

    for i in range(MOMENTUM_PERIOD, len(closes)):
        old_price = closes[i - MOMENTUM_PERIOD]

        if old_price == 0:
            continue

        value = (
            (closes[i] - old_price)
            / old_price
        ) * 100.0

        momentum.append(value)

    return momentum


def percentile(values, percent):
    if not values:
        return None

    ordered = sorted(values)

    if len(ordered) == 1:
        return ordered[0]

    position = (len(ordered) - 1) * percent

    lower = int(position)
    upper = lower + 1

    if upper >= len(ordered):
        return ordered[lower]

    weight = position - lower

    return (
        ordered[lower]
        + (ordered[upper] - ordered[lower])
        * weight
    )


def analyze_momentum(candles):
    momentum = calculate_momentum(candles)

    if len(momentum) < 4:
        return None

    lookback = momentum[-MOMENTUM_LOOKBACK:]

    if len(lookback) < 4:
        return None

    current = lookback[-1]
    previous = lookback[-2]
    previous_two = lookback[-3]

    low_level = percentile(
        lookback,
        EXTREME_PERCENTILE,
    )

    high_level = percentile(
        lookback,
        1.0 - EXTREME_PERCENTILE,
    )

    if low_level is None or high_level is None:
        return None

    extreme = "NONE"
    action = None

    # --------------------------------------------------------
    # LOW EXTREME -> TURN UP -> CALL
    # --------------------------------------------------------

    if current <= low_level:
        extreme = "LOW"

        turned_up = (
            previous < previous_two
            and current > previous
        )

        if turned_up:
            turn_distance = abs(
                current - previous
            )

            if turn_distance >= MIN_TURN_DISTANCE:
                action = "CALL"

    # --------------------------------------------------------
    # HIGH EXTREME -> TURN DOWN -> PUT
    # --------------------------------------------------------

    elif current >= high_level:
        extreme = "HIGH"

        turned_down = (
            previous > previous_two
            and current < previous
        )

        if turned_down:
            turn_distance = abs(
                current - previous
            )

            if turn_distance >= MIN_TURN_DISTANCE:
                action = "PUT"

    result = {
        "action": action,
        "current": current,
        "previous": previous,
        "previous_two": previous_two,
        "low_level": low_level,
        "high_level": high_level,
        "extreme": extreme,
    }

    if action is not None:
        reversal_strength = 0.0

        if previous != 0:
            reversal_strength = (
                abs(current - previous)
                / max(abs(previous), 0.000001)
            ) * 100.0

        result["reversal_strength"] = reversal_strength

    return result


# ============================================================
# PROPOSAL
# ============================================================

def get_proposal(action):
    contract_type = action

    payload = {
        "proposal": 1,
        "amount": STAKE,
        "basis": "stake",
        "contract_type": contract_type,
        "currency": "USD",
        "duration": EXPIRY_MINUTES,
        "duration_unit": "m",
        "underlying_symbol": SYMBOL,
    }

    response = ws_request(
        payload,
        expected_types={"proposal"},
        timeout=20,
    )

    proposal = response.get("proposal")

    if not proposal:
        raise RuntimeError(
            "Deriv returned no proposal."
        )

    proposal_id = proposal.get("id")
    ask_price = proposal.get("ask_price")

    if not proposal_id:
        raise RuntimeError(
            "Proposal has no proposal ID."
        )

    if ask_price is None:
        raise RuntimeError(
            "Proposal has no ask price."
        )

    return {
        "id": str(proposal_id),
        "ask_price": float(ask_price),
        "proposal": proposal,
    }


# ============================================================
# BUY
# ============================================================

def buy_contract(proposal):
    if not DEMO_ONLY:
        raise RuntimeError(
            "SAFETY STOP: DEMO_ONLY is false."
        )

    payload = {
        "buy": proposal["id"],
        "price": proposal["ask_price"],
    }

    response = ws_request(
        payload,
        expected_types={"buy"},
        timeout=20,
    )

    buy = response.get("buy")

    if not buy:
        raise RuntimeError(
            "Deriv returned no buy result."
        )

    contract_id = buy.get("contract_id")

    if not contract_id:
        raise RuntimeError(
            "Buy response has no contract_id."
        )

    return buy


# ============================================================
# CONTRACT MONITOR
# ============================================================

def monitor_contract(contract_id):
    log(
        f"Monitoring contract {contract_id} "
        f"for {EXPIRY_MINUTES} minutes..."
    )

    deadline = time.time() + (
        EXPIRY_MINUTES * 60
        + 180
    )

    last_status = None

    while time.time() < deadline:
        response = ws_request(
            {
                "proposal_open_contract": 1,
                "contract_id": int(contract_id),
                "subscribe": 0,
            },
            expected_types={"proposal_open_contract"},
            timeout=20,
        )

        contract = response.get(
            "proposal_open_contract",
            {},
        )

        status = str(
            contract.get("status", "")
        ).lower()

        is_sold = contract.get("is_sold")

        if status != last_status:
            log(
                f"Contract {contract_id} status: "
                f"{status or 'OPEN'}"
            )
            last_status = status

        if (
            is_sold in (1, True)
            or status in (
                "won",
                "lost",
                "sold",
                "expired",
            )
        ):
            profit = contract.get("profit")

            if profit is None:
                payout = contract.get("payout")
                buy_price = contract.get("buy_price")

                if payout is not None and buy_price is not None:
                    profit = (
                        float(payout)
                        - float(buy_price)
                    )

            if profit is None:
                profit = 0.0

            return {
                "status": status,
                "profit": float(profit),
                "contract": contract,
            }

        time.sleep(3)

    raise TimeoutError(
        f"Contract {contract_id} did not settle "
        f"within the monitoring window."
    )


# ============================================================
# TRADE ID
# ============================================================

def make_signal_id(action, candle_epoch):
    direction = "CALL" if action == "CALL" else "PUT"

    return (
        f"{DISPLAY_SYMBOL.replace('/', '')}-"
        f"{direction}-"
        f"{int(candle_epoch)}"
    )


# ============================================================
# TRADE RESULT
# ============================================================

def record_result(
    signal_id,
    action,
    result,
):
    global completed_trades
    global wins
    global losses
    global total_profit
    global active_contract

    profit = float(
        result.get("profit", 0.0)
    )

    status = str(
        result.get("status", "")
    ).upper()

    completed_trades += 1
    total_profit += profit

    if profit > 0:
        wins += 1
        outcome = "WIN"
        emoji = "🟢"
    elif profit < 0:
        losses += 1
        outcome = "LOSS"
        emoji = "🔴"
    else:
        outcome = "BREAKEVEN"
        emoji = "🟡"

    active_contract = None

    win_rate = (
        wins / completed_trades * 100
        if completed_trades
        else 0
    )

    save_state()

    message = (
        f"{emoji} ZETA TRADE RESULT\n\n"
        f"Signal: {signal_id}\n"
        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Direction: {action}\n"
        f"Outcome: {outcome}\n"
        f"Profit: ${profit:.2f}\n\n"
        f"Trade: {completed_trades}/{TARGET_TRADES}\n"
        f"Record: {wins}W / {losses}L\n"
        f"Win rate: {win_rate:.2f}%\n"
        f"Total P/L: ${total_profit:.2f}\n\n"
        f"Expiry: {EXPIRY_MINUTES} minutes"
    )

    telegram(message)

    log(
        f"{outcome}: {signal_id} | "
        f"P/L ${profit:.2f} | "
        f"{completed_trades}/{TARGET_TRADES} | "
        f"{wins}W/{losses}L | "
        f"${total_profit:.2f}"
    )


# ============================================================
# HEARTBEAT
# ============================================================

def send_heartbeat():
    global last_heartbeat

    now = time.time()

    if now - last_heartbeat < HEARTBEAT_SECONDS:
        return

    last_heartbeat = now

    win_rate = (
        wins / completed_trades * 100
        if completed_trades
        else 0
    )

    if completed_trades >= TARGET_TRADES:
        return

    message = (
        f"💓 ZETA MOMENTUM 10 HEARTBEAT\n\n"
        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Chart: 1-minute\n"
        f"Momentum: 10\n"
        f"Expiry: 15 minutes\n"
        f"Stake: ${STAKE:.2f}\n\n"
        f"Completed: {completed_trades}/{TARGET_TRADES}\n"
        f"Record: {wins}W / {losses}L\n"
        f"Win rate: {win_rate:.2f}%\n"
        f"P/L: ${total_profit:.2f}\n\n"
        f"Status: RUNNING\n"
        f"DEMO ONLY"
    )

    telegram(message)


# ============================================================
# FINISH TEST
# ============================================================

def finish_test():
    win_rate = (
        wins / completed_trades * 100
        if completed_trades
        else 0
    )

    message = (
        f"🏁 ZETA 50-TRADE TEST COMPLETE\n\n"
        f"Strategy: Momentum 10\n"
        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Chart: 1-minute\n"
        f"Expiry: 15 minutes\n\n"
        f"Trades: {completed_trades}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Win rate: {win_rate:.2f}%\n"
        f"Total P/L: ${total_profit:.2f}\n\n"
        f"Next decision:\n"
        f"Evaluate Momentum 10 before changing to Momentum 20."
    )

    telegram(message)

    log("=" * 60)
    log("50-TRADE TEST COMPLETE")
    log(f"Trades: {completed_trades}")
    log(f"Wins: {wins}")
    log(f"Losses: {losses}")
    log(f"Win rate: {win_rate:.2f}%")
    log(f"Total P/L: ${total_profit:.2f}")
    log("=" * 60)


# ============================================================
# SIGNAL HANDLING
# ============================================================

def process_signal(candles, analysis):
    global last_signal_candle
    global last_extreme_state
    global active_contract

    action = analysis.get("action")

    if action not in ("CALL", "PUT"):
        return

    if active_contract is not None:
        log(
            "Signal detected but an existing contract "
            "is still active. Skipping."
        )
        return

    signal_candle = candles[-1]
    candle_epoch = signal_candle["epoch"]

    if last_signal_candle == candle_epoch:
        return

    extreme = analysis.get("extreme")

    if (
        last_extreme_state is not None
        and last_extreme_state == extreme
    ):
        last_signal_candle = candle_epoch
        return

    last_signal_candle = candle_epoch
    last_extreme_state = extreme

    signal_id = make_signal_id(
        action,
        candle_epoch,
    )

    current = analysis["current"]
    previous = analysis["previous"]
    low_level = analysis["low_level"]
    high_level = analysis["high_level"]

    reversal_strength = analysis.get(
        "reversal_strength",
        0.0,
    )

    log(
        f"🚨 SIGNAL {signal_id} | "
        f"{action} | "
        f"Momentum {current:.5f}%"
    )

    telegram(
        f"🚨 ZETA SIGNAL\n\n"
        f"ID: {signal_id}\n"
        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Direction: {action}\n\n"
        f"Momentum: {current:.5f}%\n"
        f"Previous: {previous:.5f}%\n"
        f"Low extreme: {low_level:.5f}%\n"
        f"High extreme: {high_level:.5f}%\n"
        f"Reversal: {reversal_strength:.3f}%\n\n"
        f"Expiry: {EXPIRY_MINUTES} minutes\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Trade #{completed_trades + 1}/{TARGET_TRADES}\n\n"
        f"DEMO ONLY"
    )

    try:
        proposal = get_proposal(action)

        log(
            f"Proposal accepted: "
            f"{proposal['id']} | "
            f"ask ${proposal['ask_price']:.2f}"
        )

        buy = buy_contract(proposal)

        contract_id = buy.get("contract_id")

        active_contract = {
            "contract_id": contract_id,
            "signal_id": signal_id,
            "action": action,
        }

        log(
            f"🟢 DEMO TRADE PLACED | "
            f"Contract {contract_id}"
        )

        telegram(
            f"🟢 ZETA TRADE OPENED\n\n"
            f"ID: {signal_id}\n"
            f"Contract: {contract_id}\n"
            f"Asset: {DISPLAY_SYMBOL}\n"
            f"Direction: {action}\n"
            f"Stake: ${STAKE:.2f}\n"
            f"Expiry: {EXPIRY_MINUTES} minutes\n\n"
            f"Monitoring result..."
        )

        result = monitor_contract(
            contract_id
        )

        record_result(
            signal_id,
            action,
            result,
        )

    except Exception as exc:
        active_contract = None

        log(
            f"Trade processing error for "
            f"{signal_id}: {exc}"
        )

        telegram(
            f"⚠️ ZETA TRADE ERROR\n\n"
            f"ID: {signal_id}\n"
            f"Direction: {action}\n"
            f"Error: {str(exc)[:500]}\n\n"
            f"No completed trade counted."
        )

        raise


# ============================================================
# MAIN SCAN
# ============================================================

def scan_once():
    candles = get_candles()

    # The final candle is the currently forming candle.
    # We only analyze CLOSED candles.
    if len(candles) < 5:
        return

    closed_candles = candles[:-1]

    analysis = analyze_momentum(
        closed_candles
    )

    if analysis is None:
        return

    action = analysis.get("action")

    if action is None:
        return

    process_signal(
        closed_candles,
        analysis,
    )


# ============================================================
# SIGNAL HANDLERS
# ============================================================

def shutdown_handler(signum, frame):
    global RUNNING

    RUNNING = False

    log("Shutdown requested.")

    close_ws()


signal.signal(
    signal.SIGTERM,
    shutdown_handler,
)

signal.signal(
    signal.SIGINT,
    shutdown_handler,
)


# ============================================================
# STARTUP
# ============================================================

def startup_message():
    telegram(
        f"🚀 ZETA MOMENTUM 10 STARTED\n\n"
        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Chart: 1-minute candles\n"
        f"Momentum period: {MOMENTUM_PERIOD}\n"
        f"Momentum lookback: {MOMENTUM_LOOKBACK}\n"
        f"Extreme zones: 10% / 90%\n"
        f"Turn threshold: {MIN_TURN_DISTANCE}\n"
        f"Expiry: {EXPIRY_MINUTES} minutes\n"
        f"Stake: ${STAKE:.2f}\n\n"
        f"Target: {TARGET_TRADES} completed trades\n"
        f"Mode: DEMO ONLY\n"
        f"Automatic trading: ON\n\n"
        f"Continuous reconnect: ON"
    )


# ============================================================
# CONTINUOUS RUNNER
# ============================================================

def run_forever():
    global ws

    last_candle_epoch = None

    while RUNNING:

        if completed_trades >= TARGET_TRADES:
            finish_test()
            return

        try:
            if ws is None:
                log("Connecting to Deriv demo...")
                connect()

                log(
                    f"Starting/resuming test: "
                    f"{completed_trades}/{TARGET_TRADES}"
                )

            send_heartbeat()

            candles = get_candles()

            if len(candles) < 2:
                time.sleep(5)
                continue

            # Last candle is forming.
            # Second-last candle is the newest CLOSED candle.
            closed_candle = candles[-2]
            candle_epoch = closed_candle["epoch"]

            # Analyze once per newly closed 1-minute candle.
            if (
                last_candle_epoch is not None
                and candle_epoch == last_candle_epoch
            ):
                time.sleep(5)
                continue

            last_candle_epoch = candle_epoch

            log(
                f"New closed candle: "
                f"{datetime.fromtimestamp("
                f"candle_epoch, timezone.utc"
                f").strftime('%H:%M:%S UTC')}"
            )

            analysis = analyze_momentum(
                candles[:-1]
            )

            if analysis is None:
                time.sleep(5)
                continue

            action = analysis.get("action")

            if action is None:
                log(
                    f"NO TRADE | "
                    f"Momentum {analysis['current']:.5f}% | "
                    f"Extreme {analysis['extreme']}"
                )
            else:
                process_signal(
                    candles[:-1],
                    analysis,
                )

            if completed_trades >= TARGET_TRADES:
                finish_test()
                return

            # Keep checking, but don't hammer the API.
            time.sleep(5)

        except websocket.WebSocketConnectionClosedException:
            log(
                "WebSocket disconnected. "
                "Reconnecting..."
            )

            close_ws()
            time.sleep(RECONNECT_SECONDS)

        except (
            websocket.WebSocketTimeoutException,
            TimeoutError,
        ) as exc:
            log(
                f"WebSocket/API timeout: {exc}. "
                f"Reconnecting..."
            )

            close_ws()
            time.sleep(RECONNECT_SECONDS)

        except requests.RequestException as exc:
            log(
                f"REST/network error: {exc}. "
                f"Retrying..."
            )

            close_ws()
            time.sleep(RECONNECT_SECONDS)

        except Exception as exc:
            log(
                f"Runtime error: {exc}"
            )

            close_ws()

            telegram(
                f"⚠️ ZETA RECOVERY\n\n"
                f"Temporary error occurred:\n"
                f"{str(exc)[:500]}\n\n"
                f"The bot will reconnect automatically.\n"
                f"Progress: "
                f"{completed_trades}/{TARGET_TRADES}"
            )

            time.sleep(RECONNECT_SECONDS)


# ============================================================
# MAIN
# ============================================================

def main():
    log("=" * 60)
    log("ZETA MOMENTUM 10 — 50 TRADE DEMO TEST")
    log("=" * 60)

    validate_environment()

    load_state()

    if completed_trades >= TARGET_TRADES:
        finish_test()
        return

    startup_message()

    run_forever()


if __name__ == "__main__":
    main()
