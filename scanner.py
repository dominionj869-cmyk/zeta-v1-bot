import os
import json
import time
import requests
import websocket

API_BASE = "https://api.derivws.com"

APP_ID = os.getenv("DERIV_APP_ID")
PAT = os.getenv("DERIV_PAT")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ============================================================
# ZETA MOMENTUM 10 — DERIV DEMO TRADER
# ============================================================

SYMBOL = "frxEURUSD"
DISPLAY_SYMBOL = "EUR/USD"

# Market analysis
TIMEFRAME_SECONDS = 60
CANDLE_COUNT = 200

# Momentum 10
MOMENTUM_PERIOD = 10
MOMENTUM_LOOKBACK = 50
EXTREME_PERCENTILE = 0.10

# Trading
STAKE = 1.0
EXPIRY_MINUTES = 1

# Scanner
POLL_SECONDS = 60

# Safety
DEMO_ONLY = True

# Reconnection
RECONNECT_DELAY = 10
MAX_RECONNECT_DELAY = 60


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            timeout=15,
        )
    except Exception as e:
        print("Telegram error:", repr(e))


# ============================================================
# FAILURE
# ============================================================

def fail(message):
    print(message)
    telegram(
        "🔴 ZETA DERIV ERROR\n\n"
        + message
    )
    raise SystemExit(1)


# ============================================================
# AUTH HEADERS
# ============================================================

def headers():
    return {
        "Authorization": f"Bearer {PAT}",
        "Deriv-App-ID": APP_ID,
        "Content-Type": "application/json",
    }


# ============================================================
# GET OPTIONS ACCOUNTS
# ============================================================

def get_options_accounts():

    response = requests.get(
        f"{API_BASE}/trading/v1/options/accounts",
        headers=headers(),
        timeout=20,
    )

    print(
        "Accounts HTTP:",
        response.status_code
    )

    if not response.ok:
        print(response.text)

        fail(
            "Deriv account request failed.\n"
            f"HTTP {response.status_code}"
        )

    return response.json()


# ============================================================
# FIND DEMO ACCOUNT
# ============================================================

def find_demo_account(payload):

    data = payload.get(
        "data",
        []
    )

    if isinstance(data, dict):
        data = [data]

    for account in data:

        if account.get(
            "account_type"
        ) == "demo":

            return account

    return None


# ============================================================
# GET FRESH OTP / WEBSOCKET URL
# ============================================================

def get_otp(account_id):

    response = requests.post(
        f"{API_BASE}/trading/v1/options/accounts/"
        f"{account_id}/otp",
        headers=headers(),
        timeout=20,
    )

    print(
        "OTP HTTP:",
        response.status_code
    )

    if not response.ok:

        print(response.text)

        fail(
            "Deriv OTP request failed.\n"
            f"HTTP {response.status_code}"
        )

    payload = response.json()

    return payload.get(
        "data",
        {}
    ).get(
        "url"
    )


# ============================================================
# CONNECT TO DEMO WEBSOCKET
# ============================================================

def connect_websocket(account_id):

    print(
        "Requesting fresh Deriv OTP..."
    )

    ws_url = get_otp(
        account_id
    )

    if not ws_url:

        raise RuntimeError(
            "Deriv returned no WebSocket URL."
        )

    print(
        "Connecting to Deriv demo WebSocket..."
    )

    ws = websocket.create_connection(
        ws_url,
        timeout=90,
        enable_multithread=True,
    )

    print(
        "🟢 Deriv WebSocket connected."
    )

    return ws


# ============================================================
# GET EUR/USD CANDLES
# ============================================================

def get_candles(ws):

    request = {
        "ticks_history": SYMBOL,
        "end": "latest",
        "count": CANDLE_COUNT,
        "style": "candles",
        "granularity": TIMEFRAME_SECONDS,
        "req_id": 100,
    }

    ws.send(
        json.dumps(request)
    )

    while True:

        raw = ws.recv()

        if not raw:
            continue

        data = json.loads(raw)

        if data.get("error"):

            raise RuntimeError(
                data["error"].get(
                    "message",
                    str(data["error"])
                )
            )

        if data.get(
            "msg_type"
        ) == "candles":

            return data.get(
                "candles",
                []
            )


# ============================================================
# MOMENTUM 10
# ============================================================

def calculate_momentum(candles):

    closes = [
        float(c["close"])
        for c in candles
    ]

    minimum = (
        MOMENTUM_PERIOD
        + MOMENTUM_LOOKBACK
        + 5
    )

    if len(closes) < minimum:
        return None

    momentum = []

    for i in range(
        MOMENTUM_PERIOD,
        len(closes)
    ):

        momentum.append(
            closes[i]
            - closes[
                i - MOMENTUM_PERIOD
            ]
        )

    recent = momentum[
        -MOMENTUM_LOOKBACK:
    ]

    if len(recent) < MOMENTUM_LOOKBACK:
        return None

    ordered = sorted(
        recent
    )

    low_index = int(
        len(ordered)
        * EXTREME_PERCENTILE
    )

    high_index = int(
        len(ordered)
        * (1 - EXTREME_PERCENTILE)
    )

    low_index = max(
        0,
        min(
            low_index,
            len(ordered) - 1
        )
    )

    high_index = max(
        0,
        min(
            high_index,
            len(ordered) - 1
        )
    )

    return {
        "current": recent[-1],
        "previous": recent[-2],
        "lower_extreme": ordered[
            low_index
        ],
        "upper_extreme": ordered[
            high_index
        ],
    }


# ============================================================
# MOMENTUM 10 REVERSAL SIGNAL
# ============================================================

def generate_signal(candles):

    if len(candles) < 60:

        return (
            None,
            "Not enough candles."
        )

    momentum = calculate_momentum(
        candles
    )

    if momentum is None:

        return (
            None,
            "Not enough Momentum 10 history."
        )

    # Use closed candles.
    previous = candles[-3]
    latest = candles[-2]

    prev_open = float(
        previous["open"]
    )

    prev_close = float(
        previous["close"]
    )

    latest_open = float(
        latest["open"]
    )

    latest_close = float(
        latest["close"]
    )

    latest_body = (
        latest_close
        - latest_open
    )

    current_momentum = momentum[
        "current"
    ]

    previous_momentum = momentum[
        "previous"
    ]

    lower_extreme = momentum[
        "lower_extreme"
    ]

    upper_extreme = momentum[
        "upper_extreme"
    ]

    # ========================================================
    # BULLISH REVERSAL
    # ========================================================

    bullish_extreme = (
        previous_momentum
        <= lower_extreme
    )

    bullish_momentum_turn = (
        current_momentum
        > previous_momentum
    )

    bullish_candle = (
        latest_body > 0
        and latest_close > prev_close
        and latest_close > latest_open
    )

    if (
        bullish_extreme
        and bullish_momentum_turn
        and bullish_candle
    ):

        return (
            "CALL",
            "Bullish Momentum 10 reversal | "
            f"Previous M="
            f"{previous_momentum:.6f} | "
            f"Current M="
            f"{current_momentum:.6f} | "
            f"LowExtreme="
            f"{lower_extreme:.6f}"
        )

    # ========================================================
    # BEARISH REVERSAL
    # ========================================================

    bearish_extreme = (
        previous_momentum
        >= upper_extreme
    )

    bearish_momentum_turn = (
        current_momentum
        < previous_momentum
    )

    bearish_candle = (
        latest_body < 0
        and latest_close < prev_close
        and latest_close < latest_open
    )

    if (
        bearish_extreme
        and bearish_momentum_turn
        and bearish_candle
    ):

        return (
            "PUT",
            "Bearish Momentum 10 reversal | "
            f"Previous M="
            f"{previous_momentum:.6f} | "
            f"Current M="
            f"{current_momentum:.6f} | "
            f"HighExtreme="
            f"{upper_extreme:.6f}"
        )

    return (
        None,
        "No Momentum 10 reversal | "
        f"M={current_momentum:.6f} | "
        f"Low={lower_extreme:.6f} | "
        f"High={upper_extreme:.6f}"
    )


# ============================================================
# GET PROPOSAL
# ============================================================

def get_proposal(
    ws,
    direction
):

    request = {
        "proposal": 1,
        "amount": STAKE,
        "basis": "stake",
        "contract_type": direction,
        "currency": "USD",
        "duration": EXPIRY_MINUTES,
        "duration_unit": "m",
        "underlying_symbol": SYMBOL,

        # IMPORTANT:
        # subscribe must be 1 if supplied.
        "subscribe": 1,

        "req_id": 200,
    }

    print(
        f"Requesting {direction} proposal "
        f"for {DISPLAY_SYMBOL}..."
    )

    ws.send(
        json.dumps(request)
    )

    while True:

        raw = ws.recv()

        if not raw:
            continue

        data = json.loads(raw)

        if data.get("error"):

            raise RuntimeError(
                "Proposal error: "
                + data["error"].get(
                    "message",
                    str(data["error"])
                )
            )

        if data.get(
            "msg_type"
        ) == "proposal":

            return data.get(
                "proposal",
                {}
            )


# ============================================================
# BUY CONTRACT
# ============================================================

def buy_contract(
    ws,
    proposal
):

    proposal_id = proposal.get(
        "id"
    )

    ask_price = proposal.get(
        "ask_price"
    )

    if not proposal_id:

        raise RuntimeError(
            "Deriv returned no proposal ID."
        )

    if ask_price is None:

        raise RuntimeError(
            "Deriv returned no ask price."
        )

    request = {
        "buy": proposal_id,
        "price": float(
            ask_price
        ),
        "req_id": 300,
    }

    print(
        f"Buying proposal "
        f"{proposal_id} for "
        f"${float(ask_price):.2f}"
    )

    ws.send(
        json.dumps(request)
    )

    while True:

        raw = ws.recv()

        if not raw:
            continue

        data = json.loads(raw)

        if data.get("error"):

            raise RuntimeError(
                "BUY error: "
                + data["error"].get(
                    "message",
                    str(data["error"])
                )
            )

        if data.get(
            "msg_type"
        ) == "buy":

            return data.get(
                "buy",
                {}
            )


# ============================================================
# MONITOR CONTRACT
# ============================================================

def monitor_contract(
    ws,
    contract_id
):

    request = {
        "proposal_open_contract": 1,
        "contract_id": int(
            contract_id
        ),
        "subscribe": 1,
        "req_id": 400,
    }

    ws.send(
        json.dumps(request)
    )

    while True:

        raw = ws.recv()

        if not raw:
            continue

        data = json.loads(raw)

        if data.get("error"):

            raise RuntimeError(
                "Contract monitoring error: "
                + data["error"].get(
                    "message",
                    str(data["error"])
                )
            )

        if data.get(
            "msg_type"
        ) != "proposal_open_contract":

            continue

        contract = data.get(
            "proposal_open_contract",
            {}
        )

        status = contract.get(
            "status"
        )

        is_sold = contract.get(
            "is_sold"
        )

        print(
            "Contract:",
            contract_id,
            "status:",
            status,
            "is_sold:",
            is_sold
        )

        if (
            is_sold == 1
            or status in (
                "won",
                "lost",
                "sold",
                "expired",
            )
        ):

            profit = float(
                contract.get(
                    "profit",
                    0
                ) or 0
            )

            if profit > 0:

                result = "WIN 🟢"

            elif profit < 0:

                result = "LOSS 🔴"

            else:

                result = "BREAKEVEN ⚪"

            return {
                "result": result,
                "profit": profit,
                "status": status,
            }


# ============================================================
# ONE TRADING CYCLE
# ============================================================

def trading_cycle(ws):

    candles = get_candles(
        ws
    )

    print(
        f"Received "
        f"{len(candles)} EUR/USD candles."
    )

    direction, reason = generate_signal(
        candles
    )

    print(
        "Signal:",
        direction
    )

    print(
        "Reason:",
        reason
    )

    # ========================================================
    # NO TRADE
    # ========================================================

    if not direction:

        telegram(
            "🟡 ZETA MOMENTUM 10\n\n"
            "Asset: EUR/USD\n"
            "Signal: NO TRADE\n\n"
            f"Reason: {reason}\n\n"
            "Automatic trading: ON\n"
            "No trade placed."
        )

        return

    # ========================================================
    # SIGNAL
    # ========================================================

    telegram(
        "🟢 ZETA MOMENTUM 10 SIGNAL\n\n"
        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Direction: {direction}\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Expiry: {EXPIRY_MINUTES} minute\n\n"
        f"Reason:\n{reason}\n\n"
        "Requesting Deriv proposal..."
    )

    # ========================================================
    # PROPOSAL
    # ========================================================

    proposal = get_proposal(
        ws,
        direction
    )

    ask_price = proposal.get(
        "ask_price"
    )

    if ask_price is None:

        raise RuntimeError(
            "Proposal returned without ask price."
        )

    # ========================================================
    # BUY
    # ========================================================

    bought = buy_contract(
        ws,
        proposal
    )

    contract_id = bought.get(
        "contract_id"
    )

    if not contract_id:

        raise RuntimeError(
            "BUY returned no contract ID."
        )

    buy_price = bought.get(
        "buy_price",
        ask_price
    )

    telegram(
        "🔵 ZETA TRADE OPENED\n\n"
        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Direction: {direction}\n"
        f"Stake: ${float(buy_price):.2f}\n"
        f"Expiry: {EXPIRY_MINUTES} minute\n"
        f"Contract ID: {contract_id}\n\n"
        "Monitoring result..."
    )

    # ========================================================
    # RESULT
    # ========================================================

    result = monitor_contract(
        ws,
        contract_id
    )

    telegram(
        f"{result['result']} "
        f"ZETA TRADE RESULT\n\n"
        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Direction: {direction}\n"
        f"Contract ID: {contract_id}\n"
        f"Profit/Loss: "
        f"${result['profit']:.2f}\n"
        f"Status: {result['status']}\n\n"
        "Automatic trading: ON"
    )


# ============================================================
# RUN CONNECTED SESSION
# ============================================================

def run_session(
    account_id
):

    ws = None

    try:

        ws = connect_websocket(
            account_id
        )

        telegram(
            "🟢 ZETA MOMENTUM 10 CONNECTED\n\n"
            "Asset: EUR/USD ONLY\n"
            "Chart: 1-minute\n"
            "Stake: $1.00\n"
            "Expiry: 1 minute\n"
            "Account: DEMO\n\n"
            "Automatic trading: ON"
        )

        last_candle_time = None

        while True:

            candles = get_candles(
                ws
            )

            if not candles:

                print(
                    "No candles received."
                )

                time.sleep(
                    POLL_SECONDS
                )

                continue

            current_candle_time = (
                candles[-1].get(
                    "epoch"
                )
            )

            # Do not process same candle twice.
            if (
                current_candle_time
                and current_candle_time
                == last_candle_time
            ):

                print(
                    "Same candle. Waiting..."
                )

                time.sleep(
                    POLL_SECONDS
                )

                continue

            last_candle_time = (
                current_candle_time
            )

            print(
                "\n================================"
            )

            print(
                "NEW EUR/USD CANDLE"
            )

            print(
                "================================"
            )

            trading_cycle(
                ws
            )

            time.sleep(
                POLL_SECONDS
            )

    finally:

        if ws is not None:

            try:
                ws.close()

            except Exception:
                pass


# ============================================================
# MAIN WITH AUTOMATIC RECONNECTION
# ============================================================

def main():

    print(
        "============================================"
    )

    print(
        " ZETA MOMENTUM 10 — DERIV DEMO TRADER"
    )

    print(
        "============================================"
    )

    if not APP_ID:

        fail(
            "DERIV_APP_ID secret is missing."
        )

    if not PAT:

        fail(
            "DERIV_PAT secret is missing."
        )

    if not DEMO_ONLY:

        fail(
            "DEMO_ONLY safety switch is disabled."
        )

    # ========================================================
    # FIND DEMO ACCOUNT
    # ========================================================

    payload = get_options_accounts()

    account = find_demo_account(
        payload
    )

    if not account:

        fail(
            "No Deriv Options demo account found."
        )

    account_id = account.get(
        "account_id"
    )

    print(
        f"Demo account: {account_id}"
    )

    telegram(
        "🟢 ZETA MOMENTUM 10 STARTED\n\n"
        "Asset: EUR/USD ONLY\n"
        "Chart: 1-minute\n"
        "Stake: $1.00\n"
        "Expiry: 1 minute\n"
        "Account: DEMO\n\n"
        "Automatic trading: ON"
    )

    # ========================================================
    # PERMANENT RECONNECT LOOP
    # ========================================================

    reconnect_delay = RECONNECT_DELAY

    while True:

        try:

            print(
                "\nConnecting trading session..."
            )

            run_session(
                account_id
            )

            # If session exits normally,
            # reconnect anyway.

            print(
                "Trading session ended."
            )

        except (
            websocket.WebSocketConnectionClosedException,
            websocket.WebSocketTimeoutException,
            ConnectionError,
            OSError,
        ) as e:

            print(
                "WebSocket disconnected:",
                repr(e)
            )

            telegram(
                "🟠 ZETA WEBSOCKET DISCONNECTED\n\n"
                f"{repr(e)}\n\n"
                f"Reconnecting in "
                f"{reconnect_delay} seconds..."
            )

        except Exception as e:

            print(
                "Trading session error:",
                repr(e)
            )

            telegram(
                "🔴 ZETA SESSION ERROR\n\n"
                f"{repr(e)}\n\n"
                f"Reconnecting in "
                f"{reconnect_delay} seconds..."
            )

        print(
            f"Waiting {reconnect_delay} seconds "
            "before reconnect..."
        )

        time.sleep(
            reconnect_delay
        )

        # Gradually increase delay,
        # but never above MAX_RECONNECT_DELAY.
        reconnect_delay = min(
            reconnect_delay * 2,
            MAX_RECONNECT_DELAY
        )

        print(
            "Requesting fresh OTP and reconnecting..."
        )


# ============================================================
# START
# ============================================================
if __name__ == "__main__":

    main()
