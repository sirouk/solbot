import asyncio
import aiohttp
import json
import sqlite3
import logging
import os
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv
import base64
import sys
import base58  # Add this at the top with other imports

# Load environment variables
load_dotenv()

# Constants
DB_FILE = "token_watch.db"
SOLANA_MAINNET_HTTP = os.getenv(
    "SOLANA_MAINNET_HTTP", "https://api.mainnet-beta.solana.com"
)
TOKEN_PROGRAM_ID = os.getenv(
    "TOKEN_PROGRAM_ID", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
)
UPDATE_INTERVAL = 1  # 1 second
DEV_WALLET_THRESHOLD = (
    0.8  # If a wallet holds more than 80% of supply, consider it a dev wallet
)
MIN_SUPPLY = float(os.getenv("MIN_SUPPLY", "10000"))  # Default to 10,000 if not set
TOKEN_SELECTION_MODE = os.getenv(
    "TOKEN_SELECTION_MODE", "pump"
).lower()  # Options: pump, non-pump, all

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S UTC",
)
logger = logging.getLogger(__name__)


def init_db():
    """Initialize database with new columns"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # First create the table if it doesn't exist
    c.execute(
        """CREATE TABLE IF NOT EXISTS tokens
                 (mint_address TEXT PRIMARY KEY,
                  raw_supply INTEGER,
                  actual_supply REAL,
                  decimals INTEGER,
                  has_mint_authority BOOLEAN,
                  has_freeze_authority BOOLEAN,
                  is_pump_token BOOLEAN,
                  owner TEXT,
                  first_seen_slot INTEGER,
                  last_updated_slot INTEGER,
                  first_seen_time TIMESTAMP,
                  last_updated_time TIMESTAMP,
                  total_holders INTEGER DEFAULT 0,
                  holder_ratio REAL DEFAULT 0.0,
                  top_holder_percentage REAL DEFAULT 0.0,
                  top5_holders_percentage REAL DEFAULT 0.0,
                  lp_holders_count INTEGER DEFAULT 0,
                  circulating_supply REAL DEFAULT 0.0,
                  last_holder_check TIMESTAMP)"""
    )

    # Add new columns if they don't exist (in case we're upgrading an old table)
    columns_to_add = [
        ("total_holders", "INTEGER DEFAULT 0"),
        ("holder_ratio", "REAL DEFAULT 0.0"),
        ("top_holder_percentage", "REAL DEFAULT 0.0"),
        ("top5_holders_percentage", "REAL DEFAULT 0.0"),
        ("lp_holders_count", "INTEGER DEFAULT 0"),
        ("circulating_supply", "REAL DEFAULT 0.0"),
        ("last_holder_check", "TIMESTAMP"),
        ("first_seen_time", "TIMESTAMP"),
    ]

    for column_name, column_type in columns_to_add:
        try:
            c.execute(f"ALTER TABLE tokens ADD COLUMN {column_name} {column_type}")
            logger.info(f"Added column {column_name} to tokens table")
        except sqlite3.OperationalError as e:
            # Column already exists
            logger.debug(f"Column {column_name} already exists: {e}")
            continue

    conn.commit()
    conn.close()


async def get_token_accounts(session, mint_address):
    """Fetch token accounts using getProgramAccounts with data slicing"""
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getProgramAccounts",
        "params": [
            TOKEN_PROGRAM_ID,
            {
                "commitment": "finalized",
                "encoding": "base64",
                "dataSlice": {"offset": 64, "length": 8},
                "filters": [
                    {"dataSize": 165},  # Size of token account data
                    {"memcmp": {"offset": 0, "bytes": mint_address}},
                ],
            },
        ],
    }

    try:
        async with session.post(SOLANA_MAINNET_HTTP, json=request) as response:
            data = await response.json()
            logger.info(f"Raw response for {mint_address}:")
            logger.info(json.dumps(data, indent=2))

            if "result" in data:
                accounts = []
                for account in data["result"]:
                    # Convert base64 data to bytes and read as little-endian uint64
                    balance_bytes = base64.b64decode(account["account"]["data"][0])
                    balance = int.from_bytes(balance_bytes, "little")

                    if balance > 0:  # Only include non-zero balances
                        accounts.append(
                            {
                                "address": account["pubkey"],
                                "amount": balance,
                                "owner": account.get("account", {}).get(
                                    "owner", "unknown"
                                ),
                            }
                        )
                        logger.debug(
                            f"Found holder {account['pubkey']} with balance {balance}"
                        )

                logger.info(f"Found {len(accounts)} holders with non-zero balances")
                return accounts
            else:
                logger.error(
                    f"Error fetching accounts for {mint_address}: {data.get('error')}"
                )
                return None
    except Exception as e:
        logger.error(f"Exception fetching accounts for {mint_address}: {e}")
        return None


def calculate_holder_metrics(holders):
    """Calculate detailed holder metrics"""
    # Return zeros if no holders
    if not holders:
        return 0, 0.0, 0.0, 0.0, 0, 0.0

    # Calculate total supply and holder count
    total_supply = sum(float(h.get("amount", 0)) for h in holders)
    if total_supply == 0:
        return len(holders), 0.0, 0.0, 0.0, 0, 0.0

    # Sort holders by amount
    sorted_holders = sorted(
        holders, key=lambda x: float(x.get("amount", 0)), reverse=True
    )

    # Calculate top holder percentage
    top_holder_pct = (
        float(sorted_holders[0].get("amount", 0)) / total_supply
        if sorted_holders
        else 0.0
    )

    # Calculate top 5 holders percentage (or all holders if less than 5)
    top5_total = sum(
        float(h.get("amount", 0)) for h in sorted_holders[: min(5, len(sorted_holders))]
    )
    top5_pct = top5_total / total_supply if total_supply > 0 else 0.0

    # Count LP holders (addresses containing 'lp' or 'pool')
    lp_count = sum(
        1
        for h in holders
        if any(x in h.get("address", "").lower() for x in ["lp", "pool"])
    )

    # Calculate non-dev ratio (holders with less than threshold %)
    dev_holdings = sum(
        float(h.get("amount", 0))
        for h in holders
        if float(h.get("amount", 0)) / total_supply > DEV_WALLET_THRESHOLD
    )
    non_dev_holdings = total_supply - dev_holdings
    holder_ratio = non_dev_holdings / total_supply if total_supply > 0 else 0.0

    return (
        len(holders),  # total_holders
        holder_ratio,  # holder_ratio (non-dev)
        top_holder_pct,  # top_holder_percentage
        top5_pct,  # top5_holders_percentage
        lp_count,  # lp_holders_count
        total_supply,  # circulating_supply
    )


async def fetch_single_token(session, mint_address):
    """Fetch token accounts for a single mint address"""
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getProgramAccounts",
        "params": [
            TOKEN_PROGRAM_ID,
            {
                "commitment": "finalized",
                "encoding": "base64",
                "filters": [
                    {"dataSize": 165},
                    {"memcmp": {"offset": 0, "bytes": mint_address}},
                ],
            },
        ],
    }

    try:
        async with session.post(SOLANA_MAINNET_HTTP, json=request) as response:
            if response.status == 429:
                logger.warning(
                    f"Rate limit hit for {mint_address}, waiting before retry..."
                )
                await asyncio.sleep(2)  # Wait before retrying
                return mint_address, None  # Signal retry needed

            if response.status != 200:
                logger.error(
                    f"HTTP {response.status} error for {mint_address}: {await response.text()}"
                )
                return mint_address, None  # Signal retry needed

            data = await response.json()

            if "error" in data:
                logger.error(f"RPC error for {mint_address}: {data['error']}")
                return mint_address, None  # Signal retry needed

            if "result" in data:
                accounts = []
                for account in data["result"]:
                    try:
                        account_data = base64.b64decode(account["account"]["data"][0])
                        owner = account_data[32:64]
                        owner_str = base58.b58encode(owner).decode("ascii")
                        amount = int.from_bytes(account_data[64:72], "little")

                        if amount > 0:
                            holder_info = {
                                "address": account["pubkey"],
                                "amount": amount,
                                "owner": owner_str,
                            }
                            accounts.append(holder_info)
                    except Exception as e:
                        logger.error(f"Error parsing account for {mint_address}: {e}")
                        continue

                return mint_address, accounts

    except aiohttp.ClientError as e:
        logger.error(f"Network error fetching {mint_address}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error fetching {mint_address}: {e}")
    return mint_address, None  # Signal retry needed


async def get_token_accounts_batch(
    session, mint_addresses, batch_size=25, max_retries=3
):
    """Fetch token accounts in batches with retries"""
    all_accounts = {}
    retry_tokens = set()

    # Process tokens in batches
    for i in range(0, len(mint_addresses), batch_size):
        batch = mint_addresses[i : i + batch_size]
        retry_count = 0

        while retry_count < max_retries:
            if retry_count > 0:
                logger.info(f"Retry attempt {retry_count} for failed tokens...")
                await asyncio.sleep(2 * retry_count)  # Exponential backoff

            # Create tasks for current batch
            batch_tasks = [
                fetch_single_token(session, mint)
                for mint in batch
                if mint not in all_accounts
            ]
            if not batch_tasks:
                break

            results = await asyncio.gather(*batch_tasks)

            # Process results and track which need retry
            retry_tokens.clear()
            for mint_address, accounts in results:
                if accounts is None:
                    retry_tokens.add(mint_address)
                else:
                    all_accounts[mint_address] = accounts
                    logger.info(f"Found {len(accounts)} holders for {mint_address}")

            # Update batch to only retry failed tokens
            batch = list(retry_tokens)
            if not batch:  # All succeeded
                break

            retry_count += 1

        if retry_tokens:
            logger.warning(
                f"Failed to fetch data for {len(retry_tokens)} tokens after {max_retries} retries"
            )

    return all_accounts


async def update_token_metrics():
    """Update holder metrics for all tokens in the database"""
    init_db()

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    token_age_minutes = float(os.getenv("TOKEN_AGE_MINUTES", "1"))
    current_time = datetime.now(pytz.UTC)
    current_time_str = current_time.strftime("%Y-%m-%d %H:%M:%S.%f")
    cutoff_time = current_time - timedelta(minutes=token_age_minutes)
    cutoff_time_str = cutoff_time.strftime("%Y-%m-%d %H:%M:%S.%f")

    # Build WHERE clause to check for recently updated tokens
    where_clause = """
        WHERE first_seen_time <= ?  -- Current time
        AND first_seen_time > ?     -- Cutoff time
        AND actual_supply >= ?
        AND has_mint_authority = 0
        AND has_freeze_authority = 0
        AND (last_holder_check IS NULL OR last_holder_check < last_updated_time)
    """

    params = [current_time_str, cutoff_time_str, MIN_SUPPLY]

    if TOKEN_SELECTION_MODE == "pump":
        where_clause += " AND is_pump_token = 1"
    elif TOKEN_SELECTION_MODE == "non-pump":
        where_clause += " AND is_pump_token = 0"

    query = f"""
        SELECT mint_address 
        FROM tokens 
        {where_clause}
        ORDER BY first_seen_time DESC
        LIMIT 100
    """

    cursor.execute(query, params)
    tokens_to_check = [row[0] for row in cursor.fetchall()]

    if not tokens_to_check:
        logger.info(
            f"No recently updated {TOKEN_SELECTION_MODE} tokens found in the last {token_age_minutes:.1f} minutes (min supply: {MIN_SUPPLY:,})"
        )
        conn.close()
        return

    logger.info(
        f"Found {len(tokens_to_check)} recently updated {TOKEN_SELECTION_MODE} tokens in the last {token_age_minutes:.1f} minutes..."
    )

    try:
        async with aiohttp.ClientSession() as session:
            # Get all accounts in parallel batches with retries
            all_accounts = await get_token_accounts_batch(session, tokens_to_check)

            # Process results for each token
            updates = []
            current_time = datetime.now(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S.%f")

            for mint_address, holders in all_accounts.items():
                if holders is not None:  # Only update if we got valid data
                    metrics = calculate_holder_metrics(holders)
                    updates.append((*metrics, current_time, mint_address))

                    logger.info(f"Updated metrics for {mint_address}:")
                    logger.info(f"  - Total holders: {metrics[0]}")
                    logger.info(f"  - Non-dev ratio: {metrics[1]:.2%}")
                    logger.info(f"  - Top holder: {metrics[2]:.2%}")
                    logger.info(f"  - Top 5 holders: {metrics[3]:.2%}")
                    logger.info(f"  - LP holders: {metrics[4]}")
                    logger.info(f"  - Circulating supply: {metrics[5]:,.2f}")

            # Batch update the database only for tokens with valid data
            if updates:
                cursor.executemany(
                    """
                    UPDATE tokens 
                    SET total_holders = ?,
                        holder_ratio = ?,
                        top_holder_percentage = ?,
                        top5_holders_percentage = ?,
                        lp_holders_count = ?,
                        circulating_supply = ?,
                        last_holder_check = ?
                    WHERE mint_address = ?
                """,
                    updates,
                )
                logger.info(f"Successfully updated {len(updates)} tokens in database")

    except Exception as e:
        logger.error(f"HTTP session error: {e}")

    conn.commit()
    conn.close()


async def main():
    logger.info(f"Starting holder metrics updater...")
    logger.info(f"Token selection mode: {TOKEN_SELECTION_MODE}")
    logger.info(f"Minimum supply threshold: {MIN_SUPPLY:,}")
    logger.info(f"Update interval: {UPDATE_INTERVAL} seconds")
    logger.info(f"Using HTTP endpoint: {SOLANA_MAINNET_HTTP}")
    logger.info("-" * 50)

    while True:
        try:
            await update_token_metrics()
        except Exception as e:
            logger.error(f"Error in main loop: {e}")

        await asyncio.sleep(UPDATE_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
