import os
import json
import time
import requests
import websocket


# ============================================================
# ZETA MOMENTUM 10 — DERIV DEMO TRADER
# ============================================================
#
# MARKET:
#   EUR/USD ONLY
#
# STRATEGY:
#   1-minute candles
#   Momentum 10 percentage calculation
#   50-value momentum lookback
#   10% lower extreme
#   90% upper extreme
#   3-point momentum reversal
#   Minimum turn distance = 0.02
#   CLOSED CANDLES ONLY
#
# EXECUTION:
#   Deriv Options API
#   DEMO ONLY
#   $1 stake
#   1-minute expiry
#
# ============================================================


# =========================
# CONFIGURATION
# =========================

DERIV_APP_ID = os.getenv("DERIV_APP_ID", "").strip()
DERIV_PAT = os.getenv("DERIV_PAT", "").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


# =========================
# MARKET
# =========================

SYMBOL = "frxEURUSD"
DISPLAY_SYMBOL = "EUR/USD"

TIMEFRAME_SECONDS = 60
CANDLE_COUNT = 200


# =========================
# MOMENTUM 10
# =========================

MOMENTUM_PERIOD = 10
MOMENTUM_LOOKBACK = 50

EXTREME_PERCENTILE = 0.10

REQUIRE_TURN = True

# CALIBRATION CHANGE:
# Original = 0.03
# Current = 0.02
MIN_TURN_DISTANCE = 0.02


# =========================
# TRADING
# =========================

STAKE = 1.0

EXPIRY_MINUTES = 1

# HARD SAFETY LOCK
DEMO_ONLY = True


# =========================
# TIMING
# =========================

POLL_SECONDS = 5

HEARTBEAT_SECONDS = 300

RECONNECT_DELAY = 10

MAX_RECONNECT_DELAY = 60


# =========================
# DERIV API
# =========================

REST_BASE = "https://api.derivws.com"


# =========================
# STATE
# =========================

last_signal_candle = None
last_extreme_state = None

total_signals = 0
total_trades = 0

wins = 0
losses = 0
draws = 0

session_profit = 0.0

last_heartbeat = 0
last_status_message = 0


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(message)
        return

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=15
        )

        if response.status_code != 200:

            print(
                "Telegram error:",
                response.status_code,
                response.text
            )

    except Exception as exc:

        print(
            "Telegram exception:",
            repr(exc)
        )


# ============================================================
# DERIV REST — GET OPTIONS ACCOUNT
# ============================================================

def get_options_account():

    if not DERIV_APP_ID:
        raise RuntimeError(
            "DERIV_APP_ID is missing."
        )

    if not DERIV_PAT:
        raise RuntimeError(
            "DERIV_PAT is missing."
        )

    url = (
        f"{REST_BASE}"
        f"/trading/v1/options/accounts"
    )

    headers = {
        "Authorization": f"Bearer {DERIV_PAT}",
        "Deriv-App-ID": DERIV_APP_ID,
    }

    response = requests.get(
        url,
        headers=headers,
        timeout=20
    )

    if response.status_code != 200:

        raise RuntimeError(
            "Options account request failed: "
            f"{response.status_code} "
            f"{response.text}"
        )

    data = response.json()

    accounts_data = data.get("data")

    if not accounts_data:

        raise RuntimeError(
            "Deriv returned no Options accounts."
        )

    if isinstance(accounts_data, dict):

        accounts = [accounts_data]

    elif isinstance(accounts_data, list):

        accounts = accounts_data

    else:

        raise RuntimeError(
            "Unexpected Deriv account response."
        )

    # --------------------------------------------------------
    # Prefer active DEMO account
    # --------------------------------------------------------

    for account in accounts:

        if not isinstance(account, dict):
            continue

        account_id = str(
            account.get(
                "account_id",
                ""
            )
        )

        account_type = str(
            account.get(
                "account_type",
                ""
            )
        ).lower()

        status = str(
            account.get(
                "status",
                ""
            )
        ).lower()

        if not account_id:
            continue

        if (
            account_type == "demo"
            and status in {"", "active"}
        ):

            return account

    # --------------------------------------------------------
    # Fallback: DOT account
    # --------------------------------------------------------

    for account in accounts:

        if not isinstance(account, dict):
            continue

        account_id = str(
            account.get(
                "account_id",
                ""
            )
        )

        if account_id.startswith("DOT"):

            return account

    raise RuntimeError(
        "No suitable DEMO Options account was found."
    )


# ============================================================
# DERIV REST — REQUEST OTP / WEBSOCKET URL
# ============================================================

def request_demo_ws_url(account_id):

    if not account_id:

        raise RuntimeError(
            "Missing Deriv Options account_id."
        )

    url = (
        f"{REST_BASE}"
        f"/trading/v1/options/accounts/"
        f"{account_id}/otp"
    )

    headers = {
        "Authorization": f"Bearer {DERIV_PAT}",
        "Deriv-App-ID": DERIV_APP_ID,
        "Content-Type": "application/json",
    }

    response = requests.post(
        url,
        headers=headers,
        json={},
        timeout=20
    )

    if response.status_code != 200:

        raise RuntimeError(
            "OTP request failed: "
            f"{response.status_code} "
            f"{response.text}"
        )

    data = response.json()

    result = data.get("data")

    if not isinstance(result, dict):

        raise RuntimeError(
            "Unexpected OTP response."
        )

    ws_url = result.get("url")

    if not ws_url:

        raise RuntimeError(
            "Deriv OTP response did not "
            "contain a WebSocket URL."
        )

    return ws_url


# ============================================================
# WEBSOCKET RECEIVE
# ============================================================

def ws_receive(ws, timeout=15):

    ws.settimeout(timeout)

    raw = ws.recv()

    if raw is None:

        raise ConnectionError(
            "WebSocket returned no data."
        )

    return json.loads(raw)


# ============================================================
# WEBSOCKET SEND
# ============================================================

def ws_send(ws, payload):

    ws.send(
        json.dumps(payload)
    )


# ============================================================
# WEBSOCKET REQUEST
# ============================================================

def ws_request(
    ws,
    payload,
    expected_type=None,
    timeout=20
):

    ws_send(
        ws,
        payload
    )

    deadline = (
        time.time()
        + timeout
    )

    while time.time() < deadline:

        remaining = max(
            1,
            int(
                deadline
                - time.time()
            )
        )

        message = ws_receive(
            ws,
            timeout=remaining
        )

        if "error" in message:

            error = message["error"]

            raise RuntimeError(
                "Deriv API error: "
                f"{error.get('code')} - "
                f"{error.get('message')}"
            )

        if expected_type is None:

            return message

        if (
            message.get("msg_type")
            == expected_type
        ):

            return message

    raise TimeoutError(
        f"Timed out waiting for {expected_type}"
    )


# ============================================================
# CANDLES
# ============================================================

def get_candles(ws):

    request = {

        "ticks_history": SYMBOL,

        "style": "candles",

        "granularity":
            TIMEFRAME_SECONDS,

        "count":
            CANDLE_COUNT,

        "end":
            "latest",

        "req_id":
            100,
    }

    response = ws_request(
        ws,
        request,
        expected_type="candles",
        timeout=20
    )

    candles = response.get(
        "candles",
        []
    )

    if not candles:

        raise RuntimeError(
            "Deriv returned no EUR/USD candles."
        )

    return candles


# ============================================================
# MOMENTUM CALCULATION
# ============================================================

def calculate_momentum(candles):

    closes = []

    for candle in candles:

        try:

            close = float(
                candle["close"]
            )

            closes.append(close)

        except Exception:

            continue

    if len(closes) < (
        MOMENTUM_PERIOD + 3
    ):

        return []

    momentum = []

    start = MOMENTUM_PERIOD

    for i in range(
        start,
        len(closes)
    ):

        old_price = closes[
            i - MOMENTUM_PERIOD
        ]

        if old_price == 0:

            continue

        value = (
            (
                (
                    closes[i]
                    - old_price
                )
                / old_price
            )
            * 100.0
        )

        momentum.append(value)

    return momentum


# ============================================================
# PERCENTILE
# ============================================================

def percentile(values, percent):

    if not values:

        return None

    ordered = sorted(values)

    if len(ordered) == 1:

        return ordered[0]

    position = (
        (len(ordered) - 1)
        * percent
    )

    lower = int(position)

    upper = lower + 1

    if upper >= len(ordered):

        return ordered[lower]

    weight = (
        position
        - lower
    )

    return (
        ordered[lower]
        +
        (
            ordered[upper]
            - ordered[lower]
        )
        * weight
    )


# ============================================================
# MOMENTUM 10 ANALYSIS
# ============================================================

def analyze_momentum(candles):

    momentum = calculate_momentum(
        candles
    )

    if len(momentum) < 4:

        return {
            "action": None,
            "reason":
                "Not enough momentum data."
        }

    lookback = momentum[
        -MOMENTUM_LOOKBACK:
    ]

    if len(lookback) < 4:

        return {
            "action": None,
            "reason":
                "Not enough lookback data."
        }

    current = lookback[-1]

    previous = lookback[-2]

    previous_two = lookback[-3]

    low_level = percentile(
        lookback,
        EXTREME_PERCENTILE
    )

    high_level = percentile(
        lookback,
        1.0 - EXTREME_PERCENTILE
    )

    if (
        low_level is None
        or high_level is None
    ):

        return {
            "action": None,
            "reason":
                "Unable to calculate extremes."
        }

    extreme = "NONE"

    action = None

    turn_distance = 0.0

    # --------------------------------------------------------
    # LOW EXTREME -> CALL
    # --------------------------------------------------------

    if current <= low_level:

        extreme = "LOW"

        turned_up = (
            previous < previous_two
            and
            current > previous
        )

        if turned_up:

            turn_distance = abs(
                current
                - previous
            )

            # CALIBRATED THRESHOLD:
            # 0.02 instead of original 0.03
            if (
                turn_distance
                >= MIN_TURN_DISTANCE
            ):

                action = "CALL"

    # --------------------------------------------------------
    # HIGH EXTREME -> PUT
    # --------------------------------------------------------

    elif current >= high_level:

        extreme = "HIGH"

        turned_down = (
            previous > previous_two
            and
            current < previous
        )

        if turned_down:

            turn_distance = abs(
                current
                - previous
            )

            # CALIBRATED THRESHOLD:
            # 0.02 instead of original 0.03
            if (
                turn_distance
                >= MIN_TURN_DISTANCE
            ):

                action = "PUT"

    # --------------------------------------------------------
    # REVERSAL STRENGTH
    # --------------------------------------------------------

    reversal_strength = 0.0

    if previous != 0:

        reversal_strength = (
            abs(
                current
                - previous
            )
            /
            max(
                abs(previous),
                0.000001
            )
        ) * 100.0

    return {

        "action":
            action,

        "current":
            current,

        "previous":
            previous,

        "previous_two":
            previous_two,

        "low_level":
            low_level,

        "high_level":
            high_level,

        "extreme":
            extreme,

        "turn_distance":
            turn_distance,

        "reversal_strength":
            reversal_strength,
    }


# ============================================================
# SIGNAL GENERATOR
# ============================================================

def generate_signal(candles):

    global last_signal_candle
    global last_extreme_state

    if len(candles) < 20:

        return (
            None,
            "Not enough candles."
        )

    # Closed candles only.
    closed_candles = candles[:-1]

    if len(closed_candles) < (
        MOMENTUM_PERIOD + 5
    ):

        return (
            None,
            "Not enough closed candles."
        )

    analysis = analyze_momentum(
        closed_candles
    )

    if not analysis:

        return (
            None,
            "No analysis."
        )

    action = analysis.get(
        "action"
    )

    extreme = analysis.get(
        "extreme"
    )

    signal_candle = (
        closed_candles[-1]
    )

    candle_time = (
        signal_candle.get("epoch")
        or
        signal_candle.get("from")
        or
        signal_candle.get("to")
        or
        0
    )

    # Same candle protection.
    if (
        action
        and
        last_signal_candle
        == candle_time
    ):

        return (
            None,
            "Signal already processed "
            "for this candle."
        )

    # Same extreme protection.
    if (
        action
        and
        extreme
        and
        last_extreme_state
        == extreme
    ):

        return (
            None,
            f"Already signaled in "
            f"{extreme} extreme state."
        )

    # No trade.
    if action is None:

        reason = (
            "No Momentum 10 reversal | "
            f"M={analysis.get('current', 0):.6f} | "
            f"Prev={analysis.get('previous', 0):.6f} | "
            f"Prev2={analysis.get('previous_two', 0):.6f} | "
            f"Low={analysis.get('low_level', 0):.6f} | "
            f"High={analysis.get('high_level', 0):.6f} | "
            f"Extreme={extreme}"
        )

        return (
            None,
            reason
        )

    # Valid signal.
    last_signal_candle = candle_time

    last_extreme_state = extreme

    signal = {

        "action":
            action,

        "candle_time":
            candle_time,

        "current":
            analysis["current"],

        "previous":
            analysis["previous"],

        "previous_two":
            analysis["previous_two"],

        "low_level":
            analysis["low_level"],

        "high_level":
            analysis["high_level"],

        "extreme":
            analysis["extreme"],

        "turn_distance":
            analysis["turn_distance"],

        "reversal_strength":
            analysis[
                "reversal_strength"
            ],
    }

    return (
        signal,
        None
    )


# ============================================================
# SIGNAL ID
# ============================================================

def create_signal_id(signal):

    timestamp = int(
        time.time()
    )

    return (
        f"EURUSD-"
        f"{signal['action']}-"
        f"{timestamp}"
    )


# ============================================================
# PROPOSAL
# ============================================================

def get_proposal(
    ws,
    direction
):

    request = {

        "proposal":
            1,

        "amount":
            STAKE,

        "basis":
            "stake",

        "contract_type":
            direction,

        "currency":
            "USD",

        "duration":
            EXPIRY_MINUTES,

        "duration_unit":
            "m",

        "underlying_symbol":
            SYMBOL,

        "subscribe":
            1,

        "req_id":
            200,
    }

    response = ws_request(
        ws,
        request,
        expected_type="proposal",
        timeout=20
    )

    proposal = response.get(
        "proposal"
    )

    if not proposal:

        raise RuntimeError(
            "No proposal returned."
        )

    proposal_id = proposal.get(
        "id"
    )

    if not proposal_id:

        raise RuntimeError(
            "Proposal has no ID."
        )

    return proposal


# ============================================================
# BUY
# ============================================================

def buy_contract(
    ws,
    proposal
):

    proposal_id = proposal.get(
        "id"
    )

    if not proposal_id:

        raise RuntimeError(
            "Missing proposal ID."
        )

    request = {

        "buy":
            proposal_id,

        "price":
            STAKE,

        "req_id":
            300,
    }

    response = ws_request(
        ws,
        request,
        expected_type="buy",
        timeout=20
    )

    buy_data = response.get(
        "buy"
    )

    if not buy_data:

        raise RuntimeError(
            "Deriv returned no buy data."
        )

    contract_id = buy_data.get(
        "contract_id"
    )

    if not contract_id:

        raise RuntimeError(
            "Buy response has no contract ID."
        )

    return buy_data


# ============================================================
# MONITOR CONTRACT
# ============================================================

def monitor_contract(
    ws,
    contract_id
):

    request = {

        "proposal_open_contract":
            1,

        "contract_id":
            int(contract_id),

        "subscribe":
            1,

        "req_id":
            400,
    }

    ws_send(
        ws,
        request
    )

    deadline = (
        time.time()
        + 180
    )

    while time.time() < deadline:

        message = ws_receive(
            ws,
            timeout=30
        )

        if "error" in message:

            error = message["error"]

            raise RuntimeError(
                "Contract monitor error: "
                f"{error.get('code')} - "
                f"{error.get('message')}"
            )

        if (
            message.get("msg_type")
            != "proposal_open_contract"
        ):

            continue

        contract = message.get(
            "proposal_open_contract"
        )

        if not contract:

            continue

        is_sold = contract.get(
            "is_sold"
        )

        status = str(
            contract.get(
                "status",
                ""
            )
        ).lower()

        if (
            is_sold
            or
            status in {
                "won",
                "lost",
                "sold",
                "expired",
                "cancelled",
            }
        ):

            return contract

    raise TimeoutError(
        "Timed out waiting for contract result."
    )


# ============================================================
# DETERMINE RESULT
# ============================================================

def determine_result(contract):

    profit = float(
        contract.get(
            "profit",
            0
        )
        or
        0
    )

    status = str(
        contract.get(
            "status",
            ""
        )
    ).lower()

    if (
        status == "won"
        or
        profit > 0
    ):

        return (
            "WIN",
            profit
        )

    if (
        status == "lost"
        or
        profit < 0
    ):

        return (
            "LOSS",
            profit
        )

    return (
        "DRAW",
        profit
    )


# ============================================================
# TRADE RESULT
# ============================================================

def handle_trade_result(
    signal_id,
    direction,
    contract
):

    global wins
    global losses
    global draws
    global session_profit

    result, profit = determine_result(
        contract
    )

    session_profit += profit

    if result == "WIN":

        wins += 1

    elif result == "LOSS":

        losses += 1

    else:

        draws += 1

    closed = (
        wins
        + losses
        + draws
    )

    win_rate = (
        (wins / closed) * 100.0
        if closed
        else
        0.0
    )

    emoji = (
        "🟢"
        if result == "WIN"
        else
        "🔴"
        if result == "LOSS"
        else
        "🟡"
    )

    message = (

        f"{emoji} "
        f"ZETA MOMENTUM 10 RESULT\n\n"

        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Direction: {direction}\n"
        f"Signal ID: {signal_id}\n"
        f"Result: {result}\n"
        f"Profit: ${profit:.2f}\n\n"

        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Draws: {draws}\n"
        f"Win Rate: {win_rate:.2f}%\n"
        f"Session P/L: "
        f"${session_profit:.2f}"
    )

    send_telegram(message)

    print(message)


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def send_signal_message(
    signal,
    signal_id
):

    direction = signal["action"]

    emoji = (
        "🟢"
        if direction == "CALL"
        else
        "🔴"
    )

    message = (

        f"{emoji} "
        f"ZETA MOMENTUM 10 SIGNAL\n\n"

        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Signal: {direction}\n"
        f"Expiry: "
        f"{EXPIRY_MINUTES} minute\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Mode: DEMO\n"
        f"Signal ID: {signal_id}\n\n"

        f"Momentum: "
        f"{signal['current']:.6f}\n"

        f"Previous: "
        f"{signal['previous']:.6f}\n"

        f"Previous-2: "
        f"{signal['previous_two']:.6f}\n\n"

        f"Extreme: "
        f"{signal['extreme']}\n"

        f"Low Level: "
        f"{signal['low_level']:.6f}\n"

        f"High Level: "
        f"{signal['high_level']:.6f}\n"

        f"Turn Distance: "
        f"{signal['turn_distance']:.6f}\n"

        f"Reversal Strength: "
        f"{signal['reversal_strength']:.2f}%\n\n"

        f"Automatic trading: ON"
    )

    send_telegram(message)

    print(message)


# ============================================================
# NO TRADE
# ============================================================

def send_no_trade(reason):

    global last_status_message

    now = time.time()

    if (
        now
        - last_status_message
        < 55
    ):

        return

    last_status_message = now

    message = (

        f"🟡 ZETA MOMENTUM 10\n\n"

        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Signal: NO TRADE\n"

        f"Reason: {reason}\n\n"

        f"Connection: ACTIVE\n"
        f"Automatic trading: ON\n"
        f"No trade placed."
    )

    send_telegram(message)

    print(message)


# ============================================================
# HEARTBEAT
# ============================================================

def send_heartbeat():

    global last_heartbeat

    now = time.time()

    if (
        now
        - last_heartbeat
        < HEARTBEAT_SECONDS
    ):

        return

    last_heartbeat = now

    closed = (
        wins
        + losses
        + draws
    )

    win_rate = (
        (wins / closed) * 100.0
        if closed
        else
        0.0
    )

    message = (

        f"💓 ZETA MOMENTUM 10 HEARTBEAT\n\n"

        f"Status: RUNNING\n"
        f"Connection: ACTIVE\n"
        f"Asset: {DISPLAY_SYMBOL} ONLY\n"
        f"Strategy: Momentum 10 Reversal\n"
        f"Expiry: "
        f"{EXPIRY_MINUTES} minute\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Mode: DEMO\n\n"

        f"Signals: {total_signals}\n"
        f"Trades: {total_trades}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Draws: {draws}\n"
        f"Win Rate: {win_rate:.2f}%\n"
        f"Session P/L: "
        f"${session_profit:.2f}\n\n"

        f"Automatic trading: ON"
    )

    send_telegram(message)

    print(message)


# ============================================================
# STARTUP
# ============================================================

def send_startup():

    message = (

        f"🟢 ZETA MOMENTUM 10 STARTED\n\n"

        f"Asset: {DISPLAY_SYMBOL} ONLY\n"
        f"Chart: 1-minute\n"
        f"Momentum: 10\n"
        f"Lookback: 50\n"
        f"Extreme: 10% / 90%\n"
        f"Min Turn: "
        f"{MIN_TURN_DISTANCE}\n"
        f"Expiry: "
        f"{EXPIRY_MINUTES} minute\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Account: DEMO\n\n"

        f"Automatic trading: ON\n"
        f"Deriv Options API\n"
        f"Automatic reconnect: ON"
    )

    send_telegram(message)

    print(message)


# ============================================================
# TRADING CYCLE
# ============================================================

def trading_cycle(ws):

    global total_signals
    global total_trades

    candles = get_candles(ws)

    signal, reason = generate_signal(
        candles
    )

    if signal is None:

        send_no_trade(reason)

        send_heartbeat()

        return

    total_signals += 1

    signal_id = create_signal_id(
        signal
    )

    direction = signal["action"]

    send_signal_message(
        signal,
        signal_id
    )

    # --------------------------------------------------------
    # PROPOSAL
    # --------------------------------------------------------

    proposal = get_proposal(
        ws,
        direction
    )

    # --------------------------------------------------------
    # BUY
    # --------------------------------------------------------

    buy_data = buy_contract(
        ws,
        proposal
    )

    contract_id = buy_data.get(
        "contract_id"
    )

    if not contract_id:

        raise RuntimeError(
            "Trade has no contract ID."
        )

    total_trades += 1

    buy_price = buy_data.get(
        "buy_price",
        STAKE
    )

    message = (

        f"🚀 ZETA TRADE PLACED\n\n"

        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Direction: {direction}\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Expiry: "
        f"{EXPIRY_MINUTES} minute\n"

        f"Signal ID: {signal_id}\n"
        f"Contract ID: {contract_id}\n"
        f"Buy Price: "
        f"${float(buy_price):.2f}\n"

        f"Mode: DEMO\n\n"

        f"Monitoring result..."
    )

    send_telegram(message)

    print(message)

    # --------------------------------------------------------
    # MONITOR
    # --------------------------------------------------------

    contract = monitor_contract(
        ws,
        contract_id
    )

    # --------------------------------------------------------
    # RESULT
    # --------------------------------------------------------

    handle_trade_result(
        signal_id,
        direction,
        contract
    )


# ============================================================
# CONNECT TO DERIV DEMO
# ============================================================

def connect_demo():

    if DEMO_ONLY is not True:

        raise RuntimeError(
            "Safety check failed: "
            "DEMO_ONLY must remain True."
        )

    account = get_options_account()

    account_id = str(
        account.get(
            "account_id",
            ""
        )
    )

    if not account_id:

        raise RuntimeError(
            "Deriv Options account "
            "has no account_id."
        )

    account_type = str(
        account.get(
            "account_type",
            ""
        )
    ).lower()

    if account_type != "demo":

        raise RuntimeError(
            "Safety check stopped the bot: "
            "selected account is not DEMO."
        )

    ws_url = request_demo_ws_url(
        account_id
    )

    print(
        "Connecting to Deriv DEMO..."
    )

    ws = websocket.create_connection(
        ws_url,
        timeout=30
    )

    send_telegram(

        f"🟢 ZETA DERIV CONNECTED\n\n"

        f"Account: {account_id}\n"
        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Mode: DEMO\n"
        f"Automatic trading: ON"
    )

    print(
        f"🟢 Connected to "
        f"DEMO account {account_id}"
    )

    return ws


# ============================================================
# MAIN
# ============================================================

def main():

    global last_signal_candle
    global last_extreme_state

    if not DERIV_APP_ID:

        raise RuntimeError(
            "DERIV_APP_ID GitHub secret "
            "is missing."
        )

    if not DERIV_PAT:

        raise RuntimeError(
            "DERIV_PAT GitHub secret "
            "is missing."
        )

    if DEMO_ONLY is not True:

        raise RuntimeError(
            "DEMO_ONLY must be True."
        )

    send_startup()

    reconnect_delay = RECONNECT_DELAY

    while True:

        ws = None

        try:

            ws = connect_demo()

            reconnect_delay = RECONNECT_DELAY

            last_signal_candle = None
            last_extreme_state = None

            while True:

                cycle_start = time.time()

                try:

                    trading_cycle(ws)

                except (
                    websocket.WebSocketConnectionClosedException,
                    websocket.WebSocketTimeoutException,
                    ConnectionError,
                    OSError,
                ) as exc:

                    raise ConnectionError(
                        "WebSocket connection lost: "
                        f"{repr(exc)}"
                    )

                elapsed = (
                    time.time()
                    - cycle_start
                )

                sleep_for = max(
                    1,
                    POLL_SECONDS
                    - elapsed
                )

                time.sleep(
                    sleep_for
                )

        except KeyboardInterrupt:

            print(
                "ZETA Momentum 10 stopped."
            )

            if ws:

                try:
                    ws.close()

                except Exception:
                    pass

            return

        except Exception as exc:

            error_text = repr(exc)

            print(
                "\n🔴 "
                "ZETA MOMENTUM 10 ERROR"
            )

            print(
                error_text
            )

            send_telegram(

                f"🟠 ZETA MOMENTUM 10 "
                f"DISCONNECTED\n\n"

                f"Reason:\n"
                f"{error_text}\n\n"

                f"Automatic reconnect: ON\n"
                f"Retrying in "
                f"{reconnect_delay} seconds..."
            )

            if ws:

                try:
                    ws.close()

                except Exception:
                    pass

            time.sleep(
                reconnect_delay
            )

            reconnect_delay = min(
                reconnect_delay * 2,
                MAX_RECONNECT_DELAY
            )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    main()
