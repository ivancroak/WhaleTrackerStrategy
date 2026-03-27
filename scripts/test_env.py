"""Test all .env credentials and API connectivity.

Usage:
    python3 scripts/test_env.py
"""

import os

from dotenv import load_dotenv

load_dotenv()


def check_env(key: str, required: bool = True) -> str | None:
    val = os.environ.get(key)
    if val:
        # Show first 8 chars only
        masked = val[:8] + "..." if len(val) > 8 else val
        print(f"  [OK] {key} = {masked}")
    elif required:
        print(f"  [MISSING] {key} — required!")
    else:
        print(f"  [SKIP] {key} — optional")
    return val


def test_polymarket_data_api() -> None:
    """Test Polymarket Data API (no auth needed)."""
    import httpx

    print("\n--- Polymarket Data API (no auth) ---")
    try:
        r = httpx.get(
            "https://data-api.polymarket.com/v1/leaderboard",
            params={"orderBy": "VOL", "timePeriod": "MONTH", "limit": 3},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        print(f"  [OK] Leaderboard returned {len(data)} traders")
        if data:
            top = data[0]
            print(f"       Top trader: volume=${top.get('vol', 'N/A')}")
    except Exception as e:
        print(f"  [FAIL] {e}")


def test_polygonscan_api(api_key: str) -> None:
    """Test Polygonscan/Etherscan V2 API."""
    import httpx

    print("\n--- Polygonscan (Etherscan V2) ---")

    # Etherscan V2 uses api.etherscan.io with chainid param
    url = "https://api.etherscan.io/v2/api"
    params = {
        "chainid": 137,
        "module": "stats",
        "action": "maticprice",
        "apikey": api_key,
    }

    try:
        r = httpx.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "1":
            price = data["result"].get("maticusd", "N/A")
            print(f"  [OK] POL price: ${price}")
        else:
            print(f"  [FAIL] API returned: {data.get('message', data)}")
    except Exception as e:
        print(f"  [FAIL] {e}")


def test_clob_api(api_key: str) -> None:
    """Test Polymarket CLOB API."""
    import httpx

    print("\n--- Polymarket CLOB API ---")
    try:
        r = httpx.get("https://clob.polymarket.com/time", timeout=10)
        r.raise_for_status()
        print(f"  [OK] CLOB server time: {r.text.strip()}")
    except Exception as e:
        print(f"  [FAIL] {e}")


def test_telegram(token: str, chat_id: str) -> None:
    """Test Telegram bot by sending a test message."""
    import httpx

    print("\n--- Telegram Bot ---")
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "WhaleTracker bot test - connection OK!"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("ok"):
            print("  [OK] Test message sent to Telegram!")
        else:
            print(f"  [FAIL] {data.get('description', data)}")
    except Exception as e:
        print(f"  [FAIL] {e}")


def main() -> None:
    print("=== WhaleTracker .env Credential Check ===\n")
    print("--- Environment Variables ---")

    pk = check_env("PRIVATE_KEY")
    clob_key = check_env("CLOB_API_KEY")
    check_env("CLOB_API_SECRET")
    check_env("CLOB_API_PASSPHRASE")
    tg_token = check_env("TELEGRAM_BOT_TOKEN")
    tg_chat = check_env("TELEGRAM_CHAT_ID")
    poly_key = check_env("POLYGONSCAN_API_KEY")

    # Test APIs
    test_polymarket_data_api()

    if clob_key:
        test_clob_api(clob_key)

    if poly_key:
        test_polygonscan_api(poly_key)

    if tg_token and tg_chat:
        test_telegram(tg_token, tg_chat)

    print("\n=== Done ===")


if __name__ == "__main__":
    main()
