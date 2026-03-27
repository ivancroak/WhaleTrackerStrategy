"""Generate CLOB API credentials from your private key.

Usage:
    python3 scripts/get_clob_creds.py

Prerequisites (do these FIRST):
    1. Your wallet needs USDC.e on Polygon network
    2. Go to polymarket.com, connect this wallet, and complete initial setup
    3. Approve tokens (USDC.e → CTF, CTF → Exchange, CTF → Neg Risk Exchange)

Reads PRIVATE_KEY from .env file, prints CLOB_API_KEY, SECRET, PASSPHRASE.
"""

import os

from dotenv import load_dotenv
from py_clob_client.client import ClobClient

load_dotenv()

private_key = os.environ.get("PRIVATE_KEY")
if not private_key:
    print("ERROR: Set PRIVATE_KEY in your .env file first")
    exit(1)

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=private_key,
)

# create_or_derive_api_creds is the recommended method:
# - derives existing creds if they exist
# - creates new ones if not
# - safe to call repeatedly
creds = client.create_or_derive_api_creds()

print()
print("Add these to your .env file:")
print(f"CLOB_API_KEY={creds.api_key}")
print(f"CLOB_API_SECRET={creds.api_secret}")
print(f"CLOB_API_PASSPHRASE={creds.api_passphrase}")
