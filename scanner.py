import os
import json
import time
import sys
import requests
import websocket


# ============================================================
# ZETA MOMENTUM 10 — DERIV DEMO EXECUTION TEST
# ============================================================
#
# TEMPORARY TEST MODE
#
# This version performs ONE real DEMO trade only.
#
# Purpose:
#   Prove that the complete execution path works:
#
#   DERIV CONNECTION
#        ↓
#   PROPOSAL
#        ↓
#   BUY
#        ↓
#   CONTRACT ID
#        ↓
#   1-MINUTE RESULT
#
# IMPORTANT:
#   This test DOES NOT use the Momentum strategy to trigger
#   the trade.
#
#   It deliberately places ONE fixed DEMO trade so we can
#   determine whether Deriv actually accepts the order.
#
# After the trade result is received, the program STOPS.
#
# ============================================================


# ============================================================
# CONFIGURATION
# ============================================================

DERIV_APP_ID = os.getenv("DERIV_APP_ID", "").strip()
DERIV_PAT = os.getenv("DERIV_PAT", "").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


# ============================================================
# MARKET
# ============================================================

SYMBOL = "frxEURUSD"
DISPLAY_SYMBOL = "EUR/USD"


# ============================================================
# TEST TRADE
# ============================================================

# HARD SAFETY LOCK
DEMO_ONLY = True

# IMPORTANT:
# This MUST remain True for this temporary test.
EXECUTION_TEST = True

# Exactly ONE trade.
TEST_DIRECTION = "CALL"

TEST_STAKE = 1.0

TEST_EXPIRY_MINUTES = 1


# ============================================================
# DERIV API
# ============================================================

REST_BASE = "https://api.derivws.com"


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
# GET DERIV OPTIONS ACCOUNT
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
# REQUEST DEMO WEBSOCKET URL
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
# PROPOSAL
# ============================================================

def get_proposal(
    ws,
    direction
):

    print(
        "\nRequesting DEMO proposal..."
    )

    request = {

        "proposal":
            1,

        "amount":
            TEST_STAKE,

        "basis":
            "stake",

        "contract_type":
            direction,

        "currency":
            "USD",

        "duration":
            TEST_EXPIRY_MINUTES,

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
            "Deriv returned no proposal."
        )

    proposal_id = proposal.get(
        "id"
    )

    if not proposal_id:

        raise RuntimeError(
            "Proposal has no ID."
        )

    ask_price = proposal.get(
        "ask_price",
        TEST_STAKE
    )

    payout = proposal.get(
        "payout"
    )

    print(
        "\n🟢 PROPOSAL RECEIVED"
    )

    print(
        f"Proposal ID: {proposal_id}"
    )

    print(
        f"Ask Price: ${float(ask_price):.2f}"
    )

    if payout is not None:

        print(
            f"Payout: ${float(payout):.2f}"
        )

    send_telegram(

        f"🟢 ZETA EXECUTION TEST\n\n"

        f"Proposal received successfully.\n\n"

        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Direction: {direction}\n"
        f"Stake: ${TEST_STAKE:.2f}\n"
        f"Expiry: {TEST_EXPIRY_MINUTES} minute\n"
        f"Proposal ID: {proposal_id}\n\n"

        f"Next step: BUY"
    )

    return proposal


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

    if not proposal_id:

        raise RuntimeError(
            "Missing proposal ID."
        )

    print(
        "\nAttempting DEMO BUY..."
    )

    request = {

        "buy":
            proposal_id,

        "price":
            TEST_STAKE,

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

    buy_price = buy_data.get(
        "buy_price",
        TEST_STAKE
    )

    payout = buy_data.get(
        "payout"
    )

    print(
        "\n🚀 BUY ACCEPTED"
    )

    print(
        f"Contract ID: {contract_id}"
    )

    print(
        f"Buy Price: ${float(buy_price):.2f}"
    )

    if payout is not None:

        print(
            f"Payout: ${float(payout):.2f}"
        )

    send_telegram(

        f"🚀 ZETA DEMO TRADE PLACED\n\n"

        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Direction: {TEST_DIRECTION}\n"
        f"Stake: ${TEST_STAKE:.2f}\n"
        f"Expiry: {TEST_EXPIRY_MINUTES} minute\n\n"

        f"Contract ID: {contract_id}\n"
        f"Buy Price: ${float(buy_price):.2f}\n\n"

        f"✅ Deriv ACCEPTED the trade.\n"
        f"Monitoring result..."
    )

    return buy_data


# ============================================================
# MONITOR CONTRACT
# ============================================================

def monitor_contract(
    ws,
    contract_id
):

    print(
        "\nMonitoring contract..."
    )

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

        status = str(
            contract.get(
                "status",
                ""
            )
        ).lower()

        is_sold = contract.get(
            "is_sold"
        )

        print(
            f"Contract status: {status}"
        )

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
# RESULT
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
# EXECUTION TEST RESULT
# ============================================================

def send_test_result(
    direction,
    contract_id,
    contract
):

    result, profit = determine_result(
        contract
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

        f"{emoji} ZETA EXECUTION TEST RESULT\n\n"

        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Direction: {direction}\n"
        f"Stake: ${TEST_STAKE:.2f}\n"
        f"Expiry: {TEST_EXPIRY_MINUTES} minute\n\n"

        f"Contract ID: {contract_id}\n"
        f"Result: {result}\n"
        f"Profit: ${profit:.2f}\n\n"

        f"✅ The DEMO execution path was tested.\n\n"

        f"Test complete.\n"
        f"The bot will STOP now.\n\n"

        f"Strategy was NOT used for this test."
    )

    send_telegram(message)

    print("\n" + message)


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
            "SAFETY STOP: "
            "Selected account is not DEMO."
        )

    print(
        f"DEMO account confirmed: "
        f"{account_id}"
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

    print(
        f"🟢 Connected to DEMO "
        f"account {account_id}"
    )

    send_telegram(

        f"🟢 ZETA DEMO CONNECTION ACTIVE\n\n"

        f"Account: {account_id}\n"
        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Mode: DEMO\n\n"

        f"🧪 EXECUTION TEST MODE\n"
        f"One ${TEST_STAKE:.2f} "
        f"{TEST_DIRECTION} trade only.\n\n"

        f"No strategy signal is being used."
    )

    return ws, account_id


# ============================================================
# ONE-TRADE EXECUTION TEST
# ============================================================

def run_execution_test(ws):

    if DEMO_ONLY is not True:

        raise RuntimeError(
            "Safety stop: DEMO_ONLY is not True."
        )

    if EXECUTION_TEST is not True:

        raise RuntimeError(
            "Execution test is disabled."
        )

    if TEST_STAKE != 1.0:

        raise RuntimeError(
            "Safety stop: test stake must be $1.00."
        )

    if TEST_DIRECTION not in {
        "CALL",
        "PUT",
    }:

        raise RuntimeError(
            "Safety stop: invalid test direction."
        )

    print(
        "\n"
        "================================================"
    )

    print(
        "🧪 ZETA EXECUTION TEST"
    )

    print(
        "================================================"
    )

    print(
        f"Asset: {DISPLAY_SYMBOL}"
    )

    print(
        f"Direction: {TEST_DIRECTION}"
    )

    print(
        f"Stake: ${TEST_STAKE:.2f}"
    )

    print(
        f"Expiry: {TEST_EXPIRY_MINUTES} minute"
    )

    print(
        "Mode: DEMO ONLY"
    )

    print(
        "Trades allowed: 1"
    )

    print(
        "================================================"
    )

    # --------------------------------------------------------
    # STEP 1 — PROPOSAL
    # --------------------------------------------------------

    proposal = get_proposal(
        ws,
        TEST_DIRECTION
    )

    # --------------------------------------------------------
    # STEP 2 — BUY
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
            "Trade was reported without "
            "a contract ID."
        )

    # --------------------------------------------------------
    # STEP 3 — MONITOR
    # --------------------------------------------------------

    contract = monitor_contract(
        ws,
        contract_id
    )

    # --------------------------------------------------------
    # STEP 4 — RESULT
    # --------------------------------------------------------

    send_test_result(
        TEST_DIRECTION,
        contract_id,
        contract
    )

    return True


# ============================================================
# ERROR REPORT
# ============================================================

def send_test_error(exc):

    message = (

        f"🔴 ZETA EXECUTION TEST FAILED\n\n"

        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Mode: DEMO ONLY\n"
        f"Stake: ${TEST_STAKE:.2f}\n\n"

        f"Error:\n"
        f"{repr(exc)}\n\n"

        f"❌ No further trade will be attempted.\n"
        f"Test stopped."
    )

    send_telegram(message)

    print(message)


# ============================================================
# MAIN
# ============================================================

def main():

    ws = None

    try:

        # ----------------------------------------------------
        # HARD SAFETY CHECKS
        # ----------------------------------------------------

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

        if EXECUTION_TEST is not True:

            raise RuntimeError(
                "EXECUTION_TEST must be True."
            )

        # ----------------------------------------------------
        # CONNECT
        # ----------------------------------------------------

        ws, account_id = connect_demo()

        # ----------------------------------------------------
        # RUN EXACTLY ONE TRADE
        # ----------------------------------------------------

        run_execution_test(ws)

        print(
            "\n"
            "================================================"
        )

        print(
            "✅ EXECUTION TEST COMPLETE"
        )

        print(
            "================================================"
        )

        print(
            "One DEMO trade was completed."
        )

        print(
            "The bot will now STOP."
        )

        print(
            "No second trade will be attempted."
        )

        print(
            "================================================"
        )

    except KeyboardInterrupt:

        print(
            "\nTest stopped manually."
        )

    except Exception as exc:

        print(
            "\n"
            "================================================"
        )

        print(
            "🔴 EXECUTION TEST FAILED"
        )

        print(
            "================================================"
        )

        print(
            repr(exc)
        )

        print(
            "================================================"
        )

        send_test_error(exc)

        sys.exit(1)

    finally:

        if ws:

            try:

                ws.close()

            except Exception:

                pass


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    main()
