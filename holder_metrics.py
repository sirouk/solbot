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
SOLANA_MAINNET_HTTP = os.getenv("SOLANA_MAINNET_HTTP", "https://mainnet.helius-rpc.com/?api-key=YOUR_API_KEY")
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
UPDATE_INTERVAL = 0.001  # 5 minutes
DEV_WALLET_THRESHOLD = 0.8  # If a wallet holds more than 80% of supply, consider it a dev wallet

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S UTC'
)
logger = logging.getLogger(__name__)

def init_db():
    """Initialize database with new columns"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # First create the table if it doesn't exist
    c.execute('''CREATE TABLE IF NOT EXISTS tokens
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
                  last_updated_time TIMESTAMP,
                  total_holders INTEGER DEFAULT 0,
                  holder_ratio REAL DEFAULT 0.0,
                  top_holder_percentage REAL DEFAULT 0.0,
                  top5_holders_percentage REAL DEFAULT 0.0,
                  lp_holders_count INTEGER DEFAULT 0,
                  circulating_supply REAL DEFAULT 0.0,
                  last_holder_check TIMESTAMP)''')
    
    # Add new columns if they don't exist (in case we're upgrading an old table)
    columns_to_add = [
        ('total_holders', 'INTEGER DEFAULT 0'),
        ('holder_ratio', 'REAL DEFAULT 0.0'),
        ('top_holder_percentage', 'REAL DEFAULT 0.0'),
        ('top5_holders_percentage', 'REAL DEFAULT 0.0'),
        ('lp_holders_count', 'INTEGER DEFAULT 0'),
        ('circulating_supply', 'REAL DEFAULT 0.0'),
        ('last_holder_check', 'TIMESTAMP')
    ]
    
    for column_name, column_type in columns_to_add:
        try:
            c.execute(f'ALTER TABLE tokens ADD COLUMN {column_name} {column_type}')
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
                "dataSlice": {
                    "offset": 64,
                    "length": 8
                },
                "filters": [
                    {
                        "dataSize": 165  # Size of token account data
                    },
                    {
                        "memcmp": {
                            "offset": 0,
                            "bytes": mint_address
                        }
                    }
                ]
            }
        ]
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
                    balance = int.from_bytes(balance_bytes, 'little')
                    
                    if balance > 0:  # Only include non-zero balances
                        accounts.append({
                            "address": account["pubkey"],
                            "amount": balance,
                            "owner": account.get("account", {}).get("owner", "unknown")
                        })
                        logger.debug(f"Found holder {account['pubkey']} with balance {balance}")
                
                logger.info(f"Found {len(accounts)} holders with non-zero balances")
                return accounts
            else:
                logger.error(f"Error fetching accounts for {mint_address}: {data.get('error')}")
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
    total_supply = sum(float(h.get('amount', 0)) for h in holders)
    if total_supply == 0:
        return len(holders), 0.0, 0.0, 0.0, 0, 0.0

    # Sort holders by amount
    sorted_holders = sorted(holders, key=lambda x: float(x.get('amount', 0)), reverse=True)

    # Calculate top holder percentage
    top_holder_pct = float(sorted_holders[0].get('amount', 0)) / total_supply if sorted_holders else 0.0

    # Calculate top 5 holders percentage (or all holders if less than 5)
    top5_total = sum(float(h.get('amount', 0)) for h in sorted_holders[:min(5, len(sorted_holders))])
    top5_pct = top5_total / total_supply if total_supply > 0 else 0.0

    # Count LP holders (addresses containing 'lp' or 'pool')
    lp_count = sum(1 for h in holders if any(x in h.get('address', '').lower() for x in ['lp', 'pool']))

    # Calculate non-dev ratio (holders with less than threshold %)
    dev_holdings = sum(float(h.get('amount', 0)) for h in holders 
                      if float(h.get('amount', 0)) / total_supply > DEV_WALLET_THRESHOLD)
    non_dev_holdings = total_supply - dev_holdings
    holder_ratio = non_dev_holdings / total_supply if total_supply > 0 else 0.0

    return (
        len(holders),          # total_holders
        holder_ratio,          # holder_ratio (non-dev)
        top_holder_pct,        # top_holder_percentage
        top5_pct,             # top5_holders_percentage
        lp_count,             # lp_holders_count
        total_supply          # circulating_supply
    )

async def get_token_accounts_batch(session, mint_addresses, batch_size=25):
    """Fetch token accounts in batches"""
    all_accounts = {}
    
    for i in range(0, len(mint_addresses), batch_size):
        batch = mint_addresses[i:i + batch_size]
        
        for mint_address in batch:
            # Use getProgramAccounts to get all token accounts for this mint
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
                            {
                                "dataSize": 165  # Size of token account data
                            },
                            {
                                "memcmp": {
                                    "offset": 0,
                                    "bytes": mint_address
                                }
                            }
                        ]
                    }
                ]
            }

            try:
                async with session.post(SOLANA_MAINNET_HTTP, json=request) as response:
                    data = await response.json()
                    
                    if "result" in data:
                        accounts = []
                        for account in data["result"]:
                            try:
                                # Decode the base64 account data
                                account_data = base64.b64decode(account["account"]["data"][0])
                                
                                # Extract owner's address (32 bytes starting at offset 32)
                                owner = account_data[32:64]
                                # Convert bytes to base58 string
                                owner_str = base58.b58encode(owner).decode('ascii')
                                
                                # Extract amount (8 bytes starting at offset 64)
                                amount = int.from_bytes(account_data[64:72], 'little')
                                
                                if amount > 0:  # Only include non-zero balances
                                    holder_info = {
                                        "address": account["pubkey"],  # The token account address
                                        "amount": amount,
                                        "owner": owner_str  # The wallet address that owns this token account
                                    }
                                    accounts.append(holder_info)
                                    logger.debug(f"Found holder for {mint_address}:")
                                    logger.debug(json.dumps(holder_info, indent=2))
                            except Exception as e:
                                logger.error(f"Error parsing account for {mint_address}: {e}")
                                continue
                        
                        all_accounts[mint_address] = accounts
                        logger.info(f"Found {len(accounts)} holders for {mint_address}")
                    
            except Exception as e:
                logger.error(f"Exception fetching {mint_address}: {e}")
                continue
            
            # Brief pause between requests
            await asyncio.sleep(0.1)
    
    return all_accounts

async def update_token_metrics():
    """Update holder metrics for all tokens in the database"""
    # Initialize database with new columns
    init_db()
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Get tokens that haven't been checked recently
    cutoff_time = datetime.now(pytz.UTC) - timedelta(minutes=5)
    cutoff_time_str = cutoff_time.strftime("%Y-%m-%d %H:%M:%S.%f")

    cursor.execute("""
        SELECT mint_address 
        FROM tokens 
        WHERE last_holder_check IS NULL 
        OR last_holder_check < ?
        ORDER BY last_updated_time DESC
        LIMIT 100
    """, (cutoff_time_str,))

    tokens_to_check = [row[0] for row in cursor.fetchall()]
    
    if not tokens_to_check:
        logger.info("No tokens need updating")
        conn.close()
        return

    try:
        async with aiohttp.ClientSession() as session:
            # Get all accounts in batches
            all_accounts = await get_token_accounts_batch(session, tokens_to_check)
            
            # Process results for each token
            for mint_address, holders in all_accounts.items():
                if holders is not None:
                    metrics = calculate_holder_metrics(holders)
                    current_time = datetime.now(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S.%f")

                    cursor.execute("""
                        UPDATE tokens 
                        SET total_holders = ?,
                            holder_ratio = ?,
                            top_holder_percentage = ?,
                            top5_holders_percentage = ?,
                            lp_holders_count = ?,
                            circulating_supply = ?,
                            last_holder_check = ?
                        WHERE mint_address = ?
                    """, (*metrics, current_time, mint_address))

                    logger.info(f"Updated metrics for {mint_address}:")
                    logger.info(f"  - Total holders: {metrics[0]}")
                    logger.info(f"  - Non-dev ratio: {metrics[1]:.2%}")
                    logger.info(f"  - Top holder: {metrics[2]:.2%}")
                    logger.info(f"  - Top 5 holders: {metrics[3]:.2%}")
                    logger.info(f"  - LP holders: {metrics[4]}")
                    logger.info(f"  - Circulating supply: {metrics[5]:,.2f}")

    except Exception as e:
        logger.error(f"HTTP session error: {e}")

    conn.commit()
    conn.close()

async def main():
    logger.info("Starting holder metrics updater...")
    
    while True:
        try:
            await update_token_metrics()
        except Exception as e:
            logger.error(f"Error in main loop: {e}")
        
        await asyncio.sleep(UPDATE_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main()) 