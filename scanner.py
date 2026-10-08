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
# PURPOSE:
#   Prove that the actual Deriv execution path works.
#
# FLOW:
#
#   DEMO ACCOUNT
#        ↓
#   WEBSOCKET
#        ↓
#   TEST AVAILABLE DURATIONS
#        ↓
#   ACCEPTED PROPOSAL
#        ↓
#   ONE $1 DEMO BUY
#        ↓
#   CONTRACT ID
#        ↓
#   RESULT
#        ↓
#   STOP
#
# IMPORTANT:
#   This is NOT the Momentum strategy.
#
#   It deliberately performs ONE DEMO trade so we can
#   verify that Deriv will actually accept a trade.
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
# HARD SAFETY SETTINGS
# ============================================================

DEMO_ONLY = True

EXECUTION_TEST = True

TEST_DIRECTION = "CALL"

TEST_STAKE = 1.0


# ============================================================
# DURATION DISCOVERY
# ============================================================
#
# These are proposal checks only.
# They DO NOT place trades.
#
# The first duration accepted by Deriv will be used for
# the ONE actual demo trade.
#
# ============================================================

TEST_DURATIONS_MINUTES = [
    1,
    2,
    3,
    5,
    10,
    15,
]


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
            timeout=15,
        )

        if response.status_code != 200:

            print(
                "Telegram error:",
                response.status_code,
                response.text,
            )

    except Exception as exc:

        print(
            "Telegram exception:",
            repr(exc),
        )


# ============================================================
# GET OPTIONS ACCOUNT
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
        "Authorization":
            f"Bearer {DERIV_PAT}",

        "Deriv-App-ID":
            DERIV_APP_ID,
    }

    response = requests.get(
        url,
        headers=headers,
        timeout=20,
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

    if isinstance(
        accounts_data,
        dict,
    ):

        accounts = [
            accounts_data
        ]

    elif isinstance(
        accounts_data,
        list,
    ):

        accounts = accounts_data

    else:

        raise RuntimeError(
            "Unexpected Deriv account response."
        )

    # --------------------------------------------------------
    # Prefer active DEMO account
    # --------------------------------------------------------

    for account in accounts:

        if not isinstance(
            account,
            dict,
        ):

            continue

        account_id = str(
            account.get(
                "account_id",
                "",
            )
        )

        account_type = str(
            account.get(
                "account_type",
                "",
            )
        ).lower()

        status = str(
            account.get(
                "status",
                "",
            )
        ).lower()

        if not account_id:

            continue

        if (
            account_type == "demo"
            and
            status in {
                "",
                "active",
            }
        ):

            return account

    # --------------------------------------------------------
    # Fallback DOT account
    # --------------------------------------------------------

    for account in accounts:

        if not isinstance(
            account,
            dict,
        ):

            continue

        account_id = str(
            account.get(
                "account_id",
                "",
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
        "Authorization":
            f"Bearer {DERIV_PAT}",

        "Deriv-App-ID":
            DERIV_APP_ID,

        "Content-Type":
            "application/json",
    }

    response = requests.post(
        url,
        headers=headers,
        json={},
        timeout=20,
    )

    if response.status_code != 200:

        raise RuntimeError(
            "OTP request failed: "
            f"{response.status_code} "
            f"{response.text}"
        )

    data = response.json()

    result = data.get("data")

    if not isinstance(
        result,
        dict,
    ):

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

def ws_receive(
    ws,
    timeout=15,
):

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

def ws_send(
    ws,
    payload,
):

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
    timeout=20,
):

    ws_send(
        ws,
        payload,
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
            ),
        )

        message = ws_receive(
            ws,
            timeout=remaining,
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
        f"Timed out waiting for "
        f"{expected_type}"
    )


# ============================================================
# REQUEST ONE PROPOSAL
# ============================================================

def request_proposal(
    ws,
    direction,
    duration_minutes,
    req_id,
):

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
            duration_minutes,

        "duration_unit":
            "m",

        "underlying_symbol":
            SYMBOL,

        "subscribe":
            1,

        "req_id":
            req_id,
    }

    response = ws_request(
        ws,
        request,
        expected_type="proposal",
        timeout=15,
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
# FIND AVAILABLE DURATION
# ============================================================

def find_available_duration(ws):

    print(
        "\n"
        "================================================"
    )

    print(
        "🔎 CHECKING AVAILABLE DURATIONS"
    )

    print(
        "================================================"
    )

    send_telegram(

        f"🔎 ZETA DURATION CHECK\n\n"

        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Direction: {TEST_DIRECTION}\n"
        f"Stake: ${TEST_STAKE:.2f}\n\n"

        f"Checking which contract duration "
        f"Deriv currently accepts..."
    )

    request_id = 500

    rejected = []

    for duration in TEST_DURATIONS_MINUTES:

        print(
            f"\nChecking {duration} minute..."
        )

        try:

            proposal = request_proposal(
                ws,
                TEST_DIRECTION,
                duration,
                request_id,
            )

            proposal_id = proposal.get(
                "id"
            )

            ask_price = proposal.get(
                "ask_price",
                TEST_STAKE,
            )

            print(
                f"🟢 {duration} minute "
                f"ACCEPTED"
            )

            print(
                f"Proposal ID: {proposal_id}"
            )

            print(
                f"Ask Price: "
                f"${float(ask_price):.2f}"
            )

            send_telegram(

                f"🟢 DURATION ACCEPTED\n\n"

                f"Asset: {DISPLAY_SYMBOL}\n"
                f"Direction: {TEST_DIRECTION}\n"
                f"Duration: {duration} minute\n"
                f"Stake: ${TEST_STAKE:.2f}\n\n"

                f"Proposal ID: {proposal_id}\n\n"

                f"Next step:\n"
                f"ONE DEMO BUY"
            )

            return (
                duration,
                proposal,
            )

        except Exception as exc:

            error_text = str(exc)

            rejected.append(
                (
                    duration,
                    error_text,
                )
            )

            print(
                f"🔴 {duration} minute "
                f"rejected:"
            )

            print(
                error_text
            )

        request_id += 1

    print(
        "\nNo tested duration was accepted."
    )

    details = "\n".join(
        [
            f"{duration}m: {error}"
            for duration, error
            in rejected
        ]
    )

    raise RuntimeError(
        "No available duration found.\n"
        + details
    )


# ============================================================
# BUY CONTRACT
# ============================================================

def buy_contract(
    ws,
    proposal,
):

    proposal_id = proposal.get(
        "id"
    )

    if not proposal_id:

        raise RuntimeError(
            "Missing proposal ID."
        )

    ask_price = float(
        proposal.get(
            "ask_price",
            TEST_STAKE,
        )
        or
        TEST_STAKE
    )

    print(
        "\n"
        "================================================"
    )

    print(
        "🚀 ATTEMPTING ONE DEMO BUY"
    )

    print(
        "================================================"
    )

    print(
        f"Proposal ID: {proposal_id}"
    )

    print(
        f"Price: ${ask_price:.2f}"
    )

    request = {

        "buy":
            proposal_id,

        "price":
            ask_price,

        "req_id":
            900,
    }

    response = ws_request(
        ws,
        request,
        expected_type="buy",
        timeout=20,
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
        ask_price,
    )

    payout = buy_data.get(
        "payout"
    )

    print(
        "\n"
        "================================================"
    )

    print(
        "✅ BUY ACCEPTED BY DERIV"
    )

    print(
        "================================================"
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
        f"Duration: "
        f"CHECKED DURATION\n"
        f"Stake: ${TEST_STAKE:.2f}\n\n"

        f"Contract ID: {contract_id}\n"
        f"Buy Price: "
        f"${float(buy_price):.2f}\n\n"

        f"✅ DERIV ACCEPTED THE BUY.\n"
        f"Monitoring contract..."
    )

    return buy_data


# ============================================================
# MONITOR CONTRACT
# ============================================================

def monitor_contract(
    ws,
    contract_id,
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
            1000,
    }

    ws_send(
        ws,
        request,
    )

    deadline = (
        time.time()
        + 300
    )

    while time.time() < deadline:

        message = ws_receive(
            ws,
            timeout=30,
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
                "",
            )
        ).lower()

        is_sold = contract.get(
            "is_sold"
        )

        profit = contract.get(
            "profit",
            0,
        )

        print(
            f"Contract status: {status} | "
            f"Profit: {profit}"
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
        "Timed out waiting for "
        "contract result."
    )


# ============================================================
# DETERMINE RESULT
# ============================================================

def determine_result(
    contract,
):

    profit = float(
        contract.get(
            "profit",
            0,
        )
        or
        0
    )

    status = str(
        contract.get(
            "status",
            "",
        )
    ).lower()

    if (
        status == "won"
        or
        profit > 0
    ):

        return (
            "WIN",
            profit,
        )

    if (
        status == "lost"
        or
        profit < 0
    ):

        return (
            "LOSS",
            profit,
        )

    return (
        "DRAW",
        profit,
    )


# ============================================================
# RESULT MESSAGE
# ============================================================

def send_test_result(
    direction,
    duration,
    contract_id,
    contract,
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
        f"Duration: {duration} minute\n"
        f"Stake: ${TEST_STAKE:.2f}\n\n"

        f"Contract ID: {contract_id}\n"
        f"Result: {result}\n"
        f"Profit: ${profit:.2f}\n\n"

        f"✅ Actual Deriv DEMO execution "
        f"was confirmed.\n\n"

        f"Test complete.\n"
        f"The bot will STOP now.\n\n"

        f"Strategy was NOT used."
    )

    send_telegram(
        message
    )

    print(
        "\n"
        + message
    )


# ============================================================
# CONNECT TO DEMO
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
            "",
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
            "",
        )
    ).lower()

    if account_type != "demo":

        raise RuntimeError(
            "SAFETY STOP: selected account "
            "is not DEMO."
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
        timeout=30,
    )

    print(
        f"🟢 Connected to DEMO "
        f"account {account_id}"
    )

    send_telegram(

        f"🟢 ZETA DERIV CONNECTION ACTIVE\n\n"

        f"Account: {account_id}\n"
        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Mode: DEMO\n\n"

        f"🧪 EXECUTION TEST MODE\n"
        f"One ${TEST_STAKE:.2f} trade maximum.\n\n"

        f"First available duration will be used."
    )

    return ws


# ============================================================
# MAIN EXECUTION TEST
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

        if TEST_STAKE != 1.0:

            raise RuntimeError(
                "Safety stop: "
                "test stake must remain $1.00."
            )

        if TEST_DIRECTION not in {
            "CALL",
            "PUT",
        }:

            raise RuntimeError(
                "Safety stop: invalid direction."
            )

        # ----------------------------------------------------
        # CONNECT
        # ----------------------------------------------------

        ws = connect_demo()

        # ----------------------------------------------------
        # FIND AVAILABLE DURATION
        # ----------------------------------------------------

        duration, proposal = (
            find_available_duration(ws)
        )

        print(
            "\n"
            "================================================"
        )

        print(
            "🟢 VALID DURATION FOUND"
        )

        print(
            "================================================"
        )

        print(
            f"Duration: {duration} minute"
        )

        print(
            f"Direction: {TEST_DIRECTION}"
        )

        print(
            f"Stake: ${TEST_STAKE:.2f}"
        )

        print(
            "Now placing exactly ONE DEMO trade."
        )

        print(
            "================================================"
        )

        # ----------------------------------------------------
        # ONE BUY ONLY
        # ----------------------------------------------------

        buy_data = buy_contract(
            ws,
            proposal,
        )

        contract_id = buy_data.get(
            "contract_id"
        )

        if not contract_id:

            raise RuntimeError(
                "Trade was reported without "
                "a contract ID."
            )

        # ----------------------------------------------------
        # MONITOR
        # ----------------------------------------------------

        contract = monitor_contract(
            ws,
            contract_id,
        )

        # ----------------------------------------------------
        # RESULT
        # ----------------------------------------------------

        send_test_result(
            TEST_DIRECTION,
            duration,
            contract_id,
            contract,
        )

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
            "One DEMO trade completed."
        )

        print(
            "No second trade will be attempted."
        )

        print(
            "Bot stopping now."
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

        send_telegram(

            f"🔴 ZETA EXECUTION TEST FAILED\n\n"

            f"Asset: {DISPLAY_SYMBOL}\n"
            f"Mode: DEMO ONLY\n"
            f"Stake: ${TEST_STAKE:.2f}\n\n"

            f"Error:\n"
            f"{repr(exc)}\n\n"

            f"❌ No further trade will be attempted.\n"
            f"Test stopped."
        )

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
