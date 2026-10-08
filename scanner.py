import os
import json
import time
import sys
import requests
import websocket


# ============================================================
# ZETA MOMENTUM 10 — DERIV DURATION DISCOVERY
# ============================================================
#
# PURPOSE:
#   Find the shortest EUR/USD contract duration that Deriv
#   currently accepts.
#
# IMPORTANT:
#   THIS VERSION PLACES ZERO TRADES.
#
# FLOW:
#
#   DEMO ACCOUNT
#        ↓
#   AUTHENTICATED WEBSOCKET
#        ↓
#   TEST 1m
#        ↓
#   TEST 2m
#        ↓
#   TEST 3m
#        ↓
#   TEST 5m
#        ↓
#   TEST 10m
#        ↓
#   TEST 15m
#        ↓
#   REPORT ALL RESULTS
#        ↓
#   STOP
#
# NO BUY REQUEST IS SENT.
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

DURATION_TEST_ONLY = True

STAKE_FOR_PROPOSAL = 1.0

TEST_DIRECTION = "CALL"


# ============================================================
# DURATIONS TO TEST
# ============================================================
#
# Tested from shortest to longest.
#
# IMPORTANT:
#   These are PROPOSAL CHECKS ONLY.
#   No trade is placed for any duration.
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
            STAKE_FOR_PROPOSAL,

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
# TEST ALL DURATIONS
# ============================================================

def test_all_durations(ws):

    print(
        "\n"
        "================================================"
    )

    print(
        "🔎 TESTING EUR/USD CONTRACT DURATIONS"
    )

    print(
        "================================================"
    )

    print(
        "No trades will be placed."
    )

    print(
        "Testing:"
    )

    print(
        "1m → 2m → 3m → 5m → 10m → 15m"
    )

    print(
        "================================================"
    )

    send_telegram(

        f"🔎 ZETA DURATION DISCOVERY\n\n"

        f"Asset: {DISPLAY_SYMBOL}\n"
        f"Direction: {TEST_DIRECTION}\n"
        f"Proposal stake: ${STAKE_FOR_PROPOSAL:.2f}\n\n"

        f"Testing:\n"
        f"1m → 2m → 3m → 5m → 10m → 15m\n\n"

        f"🚫 NO TRADE WILL BE PLACED.\n"
        f"Finding the shortest accepted expiry..."
    )

    results = []

    request_id = 500

    for duration in TEST_DURATIONS_MINUTES:

        print(
            "\n"
            f"Checking {duration} minute..."
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
                "ask_price"
            )

            if ask_price is not None:

                ask_price_text = (
                    f"${float(ask_price):.2f}"
                )

            else:

                ask_price_text = "N/A"

            result = {
                "duration": duration,
                "accepted": True,
                "proposal_id": proposal_id,
                "ask_price": ask_price,
                "error": None,
            }

            results.append(result)

            print(
                f"🟢 {duration}m ACCEPTED"
            )

            print(
                f"Proposal ID: {proposal_id}"
            )

            print(
                f"Ask Price: {ask_price_text}"
            )

        except Exception as exc:

            error_text = str(exc)

            result = {
                "duration": duration,
                "accepted": False,
                "proposal_id": None,
                "ask_price": None,
                "error": error_text,
            }

            results.append(result)

            print(
                f"🔴 {duration}m REJECTED"
            )

            print(
                error_text
            )

        request_id += 1

        # Small pause between requests so the
        # broker is not hit with rapid requests.
        time.sleep(1)

    return results


# ============================================================
# SEND FINAL DURATION REPORT
# ============================================================

def send_duration_report(results):

    accepted = [
        item
        for item in results
        if item["accepted"]
    ]

    rejected = [
        item
        for item in results
        if not item["accepted"]
    ]

    if accepted:

        shortest = min(
            item["duration"]
            for item in accepted
        )

    else:

        shortest = None

    # --------------------------------------------------------
    # Build Telegram report
    # --------------------------------------------------------

    lines = []

    lines.append(
        "📊 ZETA DURATION TEST RESULT"
    )

    lines.append("")
    lines.append(
        f"Asset: {DISPLAY_SYMBOL}"
    )
    lines.append(
        "Chart strategy target: 1-minute candles"
    )
    lines.append("")
    lines.append(
        "Contract duration results:"
    )

    for item in results:

        duration = item["duration"]

        if item["accepted"]:

            lines.append(
                f"🟢 {duration}m — ACCEPTED"
            )

        else:

            error = item["error"]

            # Keep Telegram readable.
            if len(error) > 180:

                error = (
                    error[:177]
                    + "..."
                )

            lines.append(
                f"🔴 {duration}m — REJECTED"
            )

            lines.append(
                f"   {error}"
            )

    lines.append("")

    if shortest is not None:

        lines.append(
            f"🏆 SHORTEST ACCEPTED: "
            f"{shortest} minute"
        )

        lines.append("")

        lines.append(
            "Recommended configuration:"
        )

        lines.append(
            "1-minute candles"
        )

        lines.append(
            f"{shortest}-minute contract expiry"
        )

        lines.append(
            "Momentum 10 strategy"
        )

    else:

        lines.append(
            "❌ NONE OF THE TESTED "
            "DURATIONS WERE ACCEPTED."
        )

    lines.append("")
    lines.append(
        "🚫 NO TRADE WAS PLACED."
    )

    lines.append(
        "Duration discovery complete."
    )

    message = "\n".join(lines)

    send_telegram(message)

    print(
        "\n"
        "================================================"
    )

    print(
        message
    )

    print(
        "================================================"
    )

    return shortest


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

        f"🔎 DURATION DISCOVERY MODE\n"
        f"Testing contract durations only.\n\n"

        f"🚫 NO TRADE WILL BE PLACED."
    )

    return ws


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

        if DURATION_TEST_ONLY is not True:

            raise RuntimeError(
                "DURATION_TEST_ONLY must be True."
            )

        if STAKE_FOR_PROPOSAL != 1.0:

            raise RuntimeError(
                "Safety stop: proposal stake "
                "must remain $1.00."
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
        # TEST ALL DURATIONS
        # ----------------------------------------------------

        results = test_all_durations(
            ws
        )

        # ----------------------------------------------------
        # FINAL REPORT
        # ----------------------------------------------------

        shortest = send_duration_report(
            results
        )

        # ----------------------------------------------------
        # STOP
        # ----------------------------------------------------

        print(
            "\n"
            "================================================"
        )

        print(
            "✅ DURATION DISCOVERY COMPLETE"
        )

        print(
            "================================================"
        )

        if shortest is not None:

            print(
                f"Shortest accepted duration: "
                f"{shortest} minute"
            )

        else:

            print(
                "No tested duration was accepted."
            )

        print(
            ""
        )

        print(
            "🚫 ZERO TRADES WERE PLACED."
        )

        print(
            "The bot is stopping now."
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
            "🔴 DURATION TEST FAILED"
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

            f"🔴 ZETA DURATION TEST FAILED\n\n"

            f"Asset: {DISPLAY_SYMBOL}\n"
            f"Mode: DEMO ONLY\n\n"

            f"Error:\n"
            f"{repr(exc)}\n\n"

            f"🚫 No trade was placed.\n"
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
