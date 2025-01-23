import asyncio
import json
import websockets
import logging
import signal
import sys
import sqlite3
from datetime import datetime
import dotenv
import os
import pytz
import aiohttp  # Add aiohttp for HTTP requests

dotenv.load_dotenv()

# Constants
SOLANA_MAINNET_WS = os.getenv("SOLANA_MAINNET_WS", "wss://api.mainnet-beta.solana.com")
SOLANA_MAINNET_HTTP = os.getenv(
    "SOLANA_MAINNET_HTTP", "https://api.mainnet-beta.solana.com"
)
DB_FILE = "token_watch.db"
TOKEN_PROGRAM_ID = os.getenv(
    "TOKEN_PROGRAM_ID", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
)
MIN_SUPPLY = float(os.getenv("MIN_SUPPLY", "10000"))  # Default to 10,000 if not set
INITIALIZE_MINT_IX = (
    "Program log: Instruction: InitializeMint"  # Log signature for mint initialization
)


# Set up logging
def setup_logger():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S UTC",
    )
    # Set timezone to UTC for logging
    logging.Formatter.converter = lambda *args: datetime.now(pytz.UTC).timetuple()
    return logging.getLogger(__name__)


logger = setup_logger()
logger.info("Starting Solana Token Program monitor...")
logger.info(f"Minimum supply threshold: {MIN_SUPPLY:,}")
logger.info(f"Using WebSocket endpoint: {SOLANA_MAINNET_WS}")
logger.info(f"Using HTTP endpoint: {SOLANA_MAINNET_HTTP}")
logger.info("-" * 50)


async def unsubscribe_all(websocket):
    unsubscribe_message = {"jsonrpc": "2.0", "id": 0, "method": "unsubscribeAll"}

    await websocket.send(json.dumps(unsubscribe_message))
    unsubscribe_response = await websocket.recv()
    logger.info(f"Unsubscribe response: {unsubscribe_response}")


def init_db():
    """Initialize the database"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # Create tokens table
    c.execute(
        """CREATE TABLE IF NOT EXISTS tokens
                 (mint_address TEXT PRIMARY KEY,
                  owner TEXT,
                  raw_supply INTEGER,
                  actual_supply REAL,
                  decimals INTEGER,
                  has_mint_authority BOOLEAN,
                  has_freeze_authority BOOLEAN,
                  is_pump_token BOOLEAN,
                  first_seen_slot INTEGER,
                  last_updated_slot INTEGER,
                  last_updated_time TIMESTAMP,
                  total_holders INTEGER DEFAULT 0,
                  top_holder_percentage REAL DEFAULT 0.0,
                  top5_holders_percentage REAL DEFAULT 0.0,
                  lp_holders_count INTEGER DEFAULT 0,
                  circulating_supply REAL DEFAULT 0.0,
                  holder_ratio REAL DEFAULT 0.0,
                  last_holder_check TIMESTAMP)"""
    )

    conn.commit()
    conn.close()


def get_latest_slot_from_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT MAX(last_updated_slot) FROM tokens")
        result = c.fetchone()
        conn.close()
        return result[0] if result and result[0] is not None else 0
    except Exception as e:
        logger.error(f"Error reading slot from DB: {e}")
        return 0


def save_token_to_db(token_data):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()

        current_time = datetime.now(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S.%f")

        c.execute(
            """INSERT OR REPLACE INTO tokens 
                    (mint_address, owner, raw_supply, actual_supply, decimals,
                     has_mint_authority, has_freeze_authority, is_pump_token,
                     first_seen_slot, last_updated_slot, last_updated_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                token_data["mint_address"],
                token_data["owner"],
                token_data["raw_supply"],
                token_data["actual_supply"],
                token_data["decimals"],
                token_data["has_mint_authority"],
                token_data["has_freeze_authority"],
                token_data["is_pump_token"],
                token_data["first_seen_slot"],
                token_data["last_updated_slot"],
                current_time,
            ),
        )

        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error saving token to DB: {e}")


async def get_transaction(signature: str) -> dict:
    """Get transaction details using HTTP endpoint"""
    async with aiohttp.ClientSession() as session:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
            ],
        }

        async with session.post(SOLANA_MAINNET_HTTP, json=payload) as response:
            return await response.json()


async def get_token_metadata_from_tx(tx_data: dict) -> dict:
    """Extract token metadata from transaction data"""
    if not tx_data.get("result"):
        return None

    tx = tx_data["result"]
    token_data = {}

    # Get mint address and decimals from initializeMint2 instruction
    for inner in tx["meta"]["innerInstructions"]:
        for ix in inner["instructions"]:
            if (
                ix.get("program") == "spl-token"
                and ix.get("parsed", {}).get("type") == "initializeMint2"
            ):
                info = ix["parsed"]["info"]
                token_data["mint_address"] = info["mint"]
                token_data["decimals"] = int(info["decimals"])
                token_data["mint_authority"] = info["mintAuthority"]
                break

    # Get initial supply from mintTo instruction
    for inner in tx["meta"]["innerInstructions"]:
        for ix in inner["instructions"]:
            if (
                ix.get("program") == "spl-token"
                and ix.get("parsed", {}).get("type") == "mintTo"
            ):
                info = ix["parsed"]["info"]
                token_data["raw_supply"] = int(info["amount"])
                break

    # Check if mint authority is revoked (SetAuthority instruction)
    token_data["has_mint_authority"] = True  # Default to True
    token_data["has_freeze_authority"] = False  # Default to False
    for inner in tx["meta"]["innerInstructions"]:
        for ix in inner["instructions"]:
            if (
                ix.get("program") == "spl-token"
                and ix.get("parsed", {}).get("type") == "setAuthority"
            ):
                info = ix["parsed"]["info"]
                if (
                    info["authorityType"] == "mintTokens"
                    and info["newAuthority"] is None
                ):
                    token_data["has_mint_authority"] = False

    # Calculate actual supply
    if "raw_supply" in token_data and "decimals" in token_data:
        token_data["actual_supply"] = token_data["raw_supply"] / (
            10 ** token_data["decimals"]
        )

    # Get owner (first signer in accountKeys)
    for acc in tx["transaction"]["message"]["accountKeys"]:
        if acc["signer"] and acc["writable"]:
            token_data["owner"] = acc["pubkey"]
            break

    # Get slot
    token_data["first_seen_slot"] = tx["slot"]
    token_data["last_updated_slot"] = tx["slot"]  # Initially same as first_seen

    # Check if token name contains 'pump' (case insensitive)
    token_data["is_pump_token"] = False
    # Check program data logs for 'pump'
    for log in tx["meta"]["logMessages"]:
        if "Program data:" in log:
            try:
                if "pump" in log.lower():
                    token_data["is_pump_token"] = True
                    break
            except:
                continue

    # Also check mint address for 'pump'
    if "mint_address" in token_data and "pump" in token_data["mint_address"].lower():
        token_data["is_pump_token"] = True

    return token_data


async def subscribe_to_program():
    # Initialize database
    init_db()

    async with websockets.connect(SOLANA_MAINNET_WS) as websocket:
        await unsubscribe_all(websocket)

        # Subscribe to program logs for token initialization
        subscribe_message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [TOKEN_PROGRAM_ID]},  # Filter for Token Program logs
                {"commitment": "finalized", "encoding": "jsonParsed"},
            ],
        }

        await websocket.send(json.dumps(subscribe_message))
        subscription_response = await websocket.recv()
        subscription_data = json.loads(subscription_response)

        subscription_id = subscription_data.get("result")
        logger.info(f"Subscribed to Token Program logs with id: {subscription_id}")

        try:
            while True:
                try:
                    message = await websocket.recv()
                    data = json.loads(message)

                    if "params" in data and "result" in data["params"]:
                        result = data["params"]["result"]
                        logs = result.get("value", {}).get("logs", [])

                        # Look for mint initialization in logs
                        is_mint_operation = False
                        for log in logs:
                            if "Program log: Instruction: InitializeMint" in log:
                                is_mint_operation = True
                                break

                        if is_mint_operation:
                            signature = result["value"]["signature"]
                            logger.info(
                                f"Found mint initialization! Signature: {signature}"
                            )

                            # Get transaction details via HTTP
                            tx_response = await get_transaction(signature)
                            token_data = await get_token_metadata_from_tx(tx_response)

                            if token_data:
                                actual_supply = token_data["actual_supply"]
                                if actual_supply >= MIN_SUPPLY:
                                    logger.info(f"New token mint detected!")
                                    logger.info(
                                        f"Mint address: {token_data['mint_address']}"
                                    )
                                    logger.info(f"Owner: {token_data['owner']}")
                                    logger.info(f"Supply: {actual_supply:,.2f}")
                                    logger.info(f"Decimals: {token_data['decimals']}")
                                    logger.info(f"⚠️  RISK FACTORS:")
                                    logger.info(
                                        f"  - Can mint more: {'🚨 YES' if token_data['has_mint_authority'] else '✅ NO'}"
                                    )
                                    logger.info(
                                        f"  - Can freeze: {'🚨 YES' if token_data['has_freeze_authority'] else '✅ NO'}"
                                    )

                                    # Display safety information
                                    if (
                                        not token_data["has_mint_authority"]
                                        and not token_data["has_freeze_authority"]
                                    ):
                                        logger.info(f"🎯 FOUND SAFE TOKEN!")
                                        logger.info(f"✅ SAFETY CHECKS:")
                                        logger.info(f"  - No mint authority: ✅")
                                        logger.info(f"  - No freeze authority: ✅")
                                        logger.info(f"  - Supply >= {MIN_SUPPLY:,}: ✅")
                                        if token_data["is_pump_token"]:
                                            logger.info(f"  - Contains 'pump': ✅")

                                        save_token_to_db(token_data)
                                    else:
                                        logger.info(f"⚠️ Token has risks:")
                                        if token_data["has_mint_authority"]:
                                            logger.info("❌ Has mint authority")
                                        if token_data["has_freeze_authority"]:
                                            logger.info("❌ Has freeze authority")
                                        if not token_data["is_pump_token"]:
                                            logger.info("❌ Missing 'pump' in name")
                                    logger.info("-" * 50)
                                else:
                                    logger.info(
                                        f"Skipping token with insufficient supply: {actual_supply:,.2f}"
                                    )

                except json.JSONDecodeError:
                    logger.error("Failed to parse message")
                except Exception as e:
                    logger.error(f"Error processing message: {str(e)}")

        except websockets.exceptions.ConnectionClosed:
            logger.error("WebSocket connection closed")


async def cleanup(websocket):
    if websocket and not websocket.closed:
        try:
            await unsubscribe_all(websocket)
            await websocket.close()
        except:
            pass


def signal_handler(sig, frame):
    logger.info("Shutting down gracefully...")
    sys.exit(0)


async def main():
    while True:
        try:
            await subscribe_to_program()
        except Exception as e:
            logger.error(f"Connection error: {str(e)}")
            logger.info("Reconnecting in 5 seconds...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    asyncio.run(main())
