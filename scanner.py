import os
import json
import requests
import websocket

API_BASE = "https://api.derivws.com"

APP_ID = os.getenv("DERIV_APP_ID")
PAT = os.getenv("DERIV_PAT")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


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
    except Exception:
        pass


def fail(message):
    print(message)
    telegram("🔴 ZETA DERIV TEST\n\n" + message)
    raise SystemExit(1)


def headers():
    return {
        "Authorization": f"Bearer {PAT}",
        "Deriv-App-ID": APP_ID,
        "Content-Type": "application/json",
    }


def get_options_accounts():
    url = f"{API_BASE}/trading/v1/options/accounts"

    response = requests.get(
        url,
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
    url = (
        f"{API_BASE}/trading/v1/options/accounts/"
        f"{account_id}/otp"
    )

    response = requests.post(
        url,
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


def test_websocket(ws_url, account):
    print("Connecting to Deriv demo WebSocket...")

    ws = websocket.create_connection(
        ws_url,
        timeout=20,
    )

    print("WebSocket connected.")

    # Official balance request.
    ws.send(json.dumps({
        "balance": 1,
        "subscribe": 1,
        "req_id": 1,
    }))

    while True:
        raw = ws.recv()

        if not raw:
            continue

        message = json.loads(raw)

        print("Deriv response:")
        print(json.dumps(message, indent=2))

        if message.get("msg_type") == "balance":
            balance = message.get("balance", {})

            amount = balance.get("balance")
            currency = balance.get("currency")

            result = (
                "🟢 ZETA DERIV CONNECTION TEST\n\n"
                "Authentication: SUCCESS\n"
                "Options account: FOUND\n"
                "WebSocket: CONNECTED\n"
                f"Account ID: {account.get('account_id')}\n"
                f"Balance: {amount} {currency}\n\n"
                "Automatic trading: OFF\n"
                "No trade was placed."
            )

            print(result)
            telegram(result)

            ws.close()
            return


def main():
    print("====================================")
    print(" ZETA DERIV API CONNECTION TEST")
    print("====================================")

    if not APP_ID:
        fail("DERIV_APP_ID secret is missing.")

    if not PAT:
        fail("DERIV_PAT secret is missing.")

    print("App ID: loaded")
    print("PAT: loaded")
    print("PAT value is not displayed.")

    # 1. Get Options accounts
    payload = get_options_accounts()

    print(
        "Accounts response received."
    )

    # 2. Find demo account
    account = find_demo_account(payload)

    if not account:
        fail(
            "No Deriv Options demo account was found.\n"
            "Open Deriv Options and make sure the demo account exists."
        )

    account_id = account.get("account_id")

    print(f"Demo account found: {account_id}")
    print(f"Demo balance: {account.get('balance')} USD")

    # 3. Request short-lived OTP
    ws_url = get_otp(account_id)

    if not ws_url:
        fail("Deriv returned no WebSocket URL.")

    print("OTP/WebSocket URL received.")

    # 4. Connect immediately because OTP expires quickly
    test_websocket(ws_url, account)


if __name__ == "__main__":
    main()
