def
import json
import time
import statistics
import requests
import websocket

API_BASE = "https://api.derivws.com"

APP_ID = os.getenv("DERIV_APP_ID")
PAT = os.getenv("DERIV_PAT")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ============================================================
# ZETA MOMENTUM 10 — DERIV DEMO
# ============================================================

SYMBOL = "frxEURUSD"
DISPLAY_SYMBOL = "EUR/USD"

TIMEFRAME_SECONDS = 60
CANDLE_COUNT = 200

MOMENTUM_PERIOD = 10
MOMENTUM_LOOKBACK = 50
EXTREME_PERCENTILE = 0.10
MIN_TURN_DISTANCE = 0.20

STAKE = 1.0
EXPIRY_MINUTES = 10

POLL_SECONDS = 60

# Safety: this bot only uses the Options DEMO account.
DEMO_ONLY = True


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
        print("Telegram error:", e)


# ============================================================
# FAILURE
# ============================================================

def fail(message):
    print(message)
    telegram("🔴 ZETA DERIV ERROR\n\n" + message)
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
# OPTIONS ACCOUNT
# ============================================================

def get_options_accounts():
    response = requests.get(
        f"{API_BASE}/trading/v1/options/accounts",
        headers=headers(),
        timeout=20,
    )

    print("Accounts HTTP:", response.status_code)

    if not response.ok:
        print(response.text)
        fail(
            f"Deriv account request failed.\n"
            f"HTTP {response.status_code}"
        )

    return response.json()


def find_demo_account(payload):
    data = payload.get("data", [])

    if isinstance(data, dict):
        data = [data]

    for account in data:
        if account.get("account_type") == "demo":
            return account

    return None


def get_otp(account_id):
    response = requests.post(
        f"{API_BASE}/trading/v1/options/accounts/"
        f"{account_id}/otp",
        headers=headers(),
        timeout=20,
    )

    print("OTP HTTP:", response.status_code)

    if not response.ok:
        print(response.text)
        fail(
            f"Deriv OTP request failed.\n"
            f"HTTP {response.status_code}"
        )

    payload = response.json()
    return payload.get("data", {}).get("url")


# ============================================================
# WEBSOCKET REQUEST
# ============================================================

def send_request(ws, payload):
    ws.send(json.dumps(payload))

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

        if (
            "req_id" not in data
            or data.get("req_id") == payload.get("req_id")
        ):
            return data


# ============================================================
# GET 1-MINUTE EUR/USD CANDLES
# ============================================================

def gedefandles(ws):
    request = {
    "ticks_history": SYMBOL,
    "end": "latest",
    "count": CANDLE_COUNT,
    "style": "candles",
    "granularity": TIMEFRAME_SECONDS,
    "req_id": 100,
    }
     ws.send(json.dumps(request))

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

        if data.get("msg_type") == "candles":
            return data.get("candles", [])


# ============================================================
# MOMENTUM 10
# ============================================================

def calculate_momentum(candles):
    closes = [
        float(c["close"])
        for c in candles
    ]

    if len(closes) < MOMENTUM_PERIOD + MOMENTUM_LOOKBACK + 5:
        return None

    momentum = []

    for i in range(
        MOMENTUM_PERIOD,
        len(closes)
    ):
        momentum.append(
            closes[i] -
            closes[i - MOMENTUM_PERIOD]
        )

    if len(momentum) < MOMENTUM_LOOKBACK:
        return None

    recent = momentum[-MOMENTUM_LOOKBACK:]

    current = recent[-1]

    ordered = sorted(recent)

    low_index = int(
        len(ordered) *
        EXTREME_PERCENTILE
    )

    high_index = int(
        len(ordered) *
        (1 - EXTREME_PERCENTILE)
    )

    low_index = max(
        0,
        min(low_index, len(ordered) - 1)
    )

    high_index = max(
        0,
        min(high_index, len(ordered) - 1)
    )

    lower_extreme = ordered[low_index]
    upper_extreme = ordered[high_index]

    return {
        "current": current,
        "lower_extreme": lower_extreme,
        "upper_extreme": upper_extreme,
        "recent": recent,
    }


# ============================================================
# SIGNAL
# ============================================================

def generate_signal(candles):
    if len(candles) < 20:
        return None, "Not enough candles."

    momentum = calculate_momentum(candles)

    if momentum is None:
        return None, "Not enough Momentum 10 history."

    previous = candles[-2]
    latest = candles[-1]

    prev_open = float(previous["open"])
    prev_close = float(previous["close"])

    latest_open = float(latest["open"])
    latest_close = float(latest["close"])

    prev_body = prev_close - prev_open
    latest_body = latest_close - latest_open

    current_momentum = momentum["current"]
    lower_extreme = momentum["lower_extreme"]
    upper_extreme = momentum["upper_extreme"]

    # --------------------------------------------------------
    # BULLISH REVERSAL
    # Momentum has reached a lower extreme and price turns up.
    # --------------------------------------------------------

    bullish_extreme = (
        current_momentum <= lower_extreme
    )

    bullish_reversal = (
        latest_body > 0
        and latest_close > prev_close
        and latest_close > latest_open
    )

    bullish_distance = (
        abs(current_momentum - lower_extreme)
    )

    if (
        bullish_extreme
        and bullish_reversal
        and bullish_distance >= MIN_TURN_DISTANCE
    ):
        return "CALL", (
            f"Bullish Momentum 10 reversal | "
            f"M={current_momentum:.6f} | "
            f"LowExtreme={lower_extreme:.6f}"
        )

    # --------------------------------------------------------
    # BEARISH REVERSAL
    # Momentum has reached an upper extreme and price turns down.
    # --------------------------------------------------------

    bearish_extreme = (
        current_momentum >= upper_extreme
    )

    bearish_reversal = (
        latest_body < 0
        and latest_close < prev_close
        and latest_close < latest_open
    )

    bearish_distance = (
        abs(current_momentum - upper_extreme)
    )

    if (
        bearish_extreme
        and bearish_reversal
        and bearish_distance >= MIN_TURN_DISTANCE
    ):
        return "PUT", (
            f"Bearish Momentum 10 reversal | "
            f"M={current_momentum:.6f} | "
            f"HighExtreme={upper_extreme:.6f}"
        )

    return None, (
        f"No Momentum 10 extreme-reversal setup | "
        f"M={current_momentum:.6f} | "
        f"Low={lower_extreme:.6f} | "
        f"High={upper_extreme:.6f}"
    )


# ============================================================
# GET PROPOSAL
# ============================================================

def get_proposal(ws, direction):
    contract_type = direction

    request = {
        "proposal": 1,
        "amount": STAKE,
        "basis": "stake",
        "contract_type": contract_type,
        "currency": "USD",
        "duration": EXPIRY_MINUTES,
        "duration_unit": "m",
        "underlying_symbol": SYMBOL,
        "subscribe": 0,
        "req_id": 200,
    }

    print(
        f"Requesting {direction} proposal "
        f"for {DISPLAY_SYMBOL}..."
    )

    ws.send(json.dumps(request))

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

        if data.get("msg_type") == "proposal":
            return data.get("proposal", {})


# ============================================================
# BUY
# ============================================================

def buy_contract(ws, proposal):
    proposal_id = proposal.get("id")
    ask_price = proposal.get("ask_price")

    if not proposal_id:
        raise RuntimeError(
            "Deriv returned no proposal ID."
        )

    if ask_price is None:
        raise RuntimeError(
            "Deriv returned no ask price."
        )

    print(
        f"Buying proposal {proposal_id} "
        f"for {ask_price} USD"
    )

    request = {
        "buy": proposal_id,
        "price": float(ask_price),
        "req_id": 300,
    }

    ws.send(json.dumps(request))

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

        if data.get("msg_type") == "buy":
            return data.get("buy", {})


# ============================================================
# MONITOR CONTRACT
# ============================================================

def monitor_contract(ws, contract_id):
    print(
        f"Monitoring contract {contract_id}..."
    )

    request = {
        "proposal_open_contract": 1,
        "contract_id": int(contract_id),
        "subscribe": 1,
        "req_id": 400,
    }

    ws.send(json.dumps(request))

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

        if data.get("msg_type") != "proposal_open_contract":
            continue

        contract = data.get(
            "proposal_open_contract",
            {}
        )

        status = contract.get("status")
        is_sold = contract.get("is_sold")

        print(
            "Contract status:",
            status,
            "is_sold:",
            is_sold
        )

        # Contract has finished.
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
                contract.get("profit", 0) or 0
            )

            payout = contract.get("payout")
            buy_price = contract.get(
                "buy_price",
                STAKE
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
                "payout": payout,
                "buy_price": buy_price,
                "status": status,
            }


# ============================================================
# ONE TRADING CYCLE
# ============================================================

def trading_cycle(ws):
    candles = get_candles(ws)

    print(
        f"Received {len(candles)} EUR/USD candles."
    )

    direction, reason = generate_signal(candles)

    print("Signal:", direction)
    print("Reason:", reason)

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

    # --------------------------------------------------------
    # SIGNAL FOUND
    # --------------------------------------------------------

    telegram(
        "🟢 ZETA MOMENTUM 10 SIGNAL\n\n"
        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Direction: {direction}\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Expiry: {EXPIRY_MINUTES} minutes\n\n"
        f"Reason:\n{reason}\n\n"
        "Requesting Deriv proposal..."
    )

    # --------------------------------------------------------
    # PROPOSAL
    # --------------------------------------------------------

    proposal = get_proposal(
        ws,
        direction
    )

    ask_price = proposal.get(
        "ask_price"
    )

    telegram(
        "🟠 ZETA PROPOSAL\n\n"
        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Direction: {direction}\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Ask price: ${float(ask_price):.2f}\n\n"
        "Buying demo contract..."
    )

    # --------------------------------------------------------
    # BUY
    # --------------------------------------------------------

    bought = buy_contract(
        ws,
        proposal
    )

    contract_id = bought.get(
        "contract_id"
    )

    if not contract_id:
        raise RuntimeError(
            "Deriv BUY succeeded but "
            "no contract ID was returned."
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
        f"Expiry: {EXPIRY_MINUTES} minutes\n"
        f"Contract ID: {contract_id}\n\n"
        "Monitoring result..."
    )

    # --------------------------------------------------------
    # RESULT
    # --------------------------------------------------------

    result = monitor_contract(
        ws,
        contract_id
    )

    profit = result["profit"]

    telegram(
        f"{result['result']} ZETA TRADE RESULT\n\n"
        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Direction: {direction}\n"
        f"Contract ID: {contract_id}\n"
        f"Profit/Loss: ${profit:.2f}\n"
        f"Status: {result['status']}\n\n"
        "Automatic trading: ON"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    print("============================================")
    print(" ZETA MOMENTUM 10 — DERIV DEMO TRADER")
    print("============================================")

    if not APP_ID:
        fail("DERIV_APP_ID secret is missing.")

    if not PAT:
        fail("DERIV_PAT secret is missing.")

    if not DEMO_ONLY:
        fail("DEMO_ONLY safety switch is disabled.")

    # --------------------------------------------------------
    # Find demo Options account
    # --------------------------------------------------------

    payload = get_options_accounts()

    account = find_demo_account(payload)

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

    # --------------------------------------------------------
    # Get authenticated demo WebSocket
    # --------------------------------------------------------

    ws_url = get_otp(account_id)

    if not ws_url:
        fail(
            "Deriv returned no WebSocket URL."
        )

    print(
        "Connecting to Deriv demo WebSocket..."
    )

    ws = websocket.create_connection(
        ws_url,
        timeout=90,
    )

    print(
        "🟢 Deriv demo WebSocket connected."
    )

    telegram(
        "🟢 ZETA MOMENTUM 10 STARTED\n\n"
        f"Asset: {DISPLAY_SYMBOL} ONLY\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Expiry: {EXPIRY_MINUTES} minutes\n"
        "Account: DEMO\n\n"
        "Automatic trading: ON"
    )

    # Balance subscription.
    ws.send(json.dumps({
        "balance": 1,
        "subscribe": 1,
        "req_id": 1,
    }))

    # --------------------------------------------------------
    # Continuous EUR/USD scanning
    # --------------------------------------------------------

    last_candle_time = None

    while True:
        try:
            candles = get_candles(ws)

            if not candles:
                print(
                    "No candles received."
                )
                time.sleep(POLL_SECONDS)
                continue

            current_candle_time = candles[-1].get(
                "epoch"
            )

            # Only process a new candle.
            if (
                current_candle_time
                and current_candle_time == last_candle_time
            ):
                print(
                    "Same candle. Waiting..."
                )
                time.sleep(POLL_SECONDS)
                continue

            last_candle_time = current_candle_time

            print(
                "\n================================"
            )
            print(
                "NEW EUR/USD CANDLE"
            )
            print(
                "================================"
            )

            trading_cycle(ws)

            # Give Deriv a moment before next scan.
            time.sleep(POLL_SECONDS)

        except websocket.WebSocketTimeoutException:
            print(
                "WebSocket timeout. Reconnecting..."
            )
            break

        except websocket.WebSocketConnectionClosedException:
            print(
                "WebSocket closed. Reconnecting..."
            )
            break

        except Exception as e:
            print(
                "Trading cycle error:",
                repr(e)
            )

            telegram(
                "🔴 ZETA TRADING ERROR\n\n"
                f"{repr(e)}\n\n"
                "Bot will retry."
            )

            time.sleep(30)


if __name__ == "__main__":
    main()
