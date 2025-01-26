import asyncio
import sqlite3
from datetime import datetime, timedelta
import requests
from telethon import TelegramClient, events
from dotenv import load_dotenv
import os
import re
import argparse
import pytz
import logging
import json
import time

# Constants for Trojan bot response patterns
TROJAN_HELP_PATTERN = "how do I use trojan?"
TROJAN_TRANSACTION_SENT_PATTERN = "transaction sent"

TROJAN_SUCCESS_PATTERNS = ["buy success"]

TROJAN_FAILURE_PATTERNS = [
    "insufficient balance",
    "error",
    "failed",
    "🔴",
    "token not found",
    "transaction failed",
]

TOKENS_DB = "tokens.db"
TOKEN_WATCH_DB = "token_watch.db"


def parse_args():
    parser = argparse.ArgumentParser(description="Solana Token Monitor")
    parser.add_argument(
        "--auto", action="store_true", help="Skip configuration prompts if .env exists"
    )
    return parser.parse_args()


def create_database():
    """Create the SQLite database and tables"""
    conn = sqlite3.connect(TOKENS_DB)
    cursor = conn.cursor()

    # Create tokens table
    cursor.execute(
        """
    CREATE TABLE IF NOT EXISTS verified_tokens (
        mint TEXT PRIMARY KEY,
        name TEXT,
        symbol TEXT,
        description TEXT,
        jup_verified BOOLEAN,
        date_added TIMESTAMP,
        date_bought TIMESTAMP NULL,
        is_bought BOOLEAN DEFAULT FALSE,
        retry_count INTEGER DEFAULT 0,
        last_attempt TIMESTAMP NULL
    )
    """
    )

    # Create communications table
    cursor.execute(
        """
    CREATE TABLE IF NOT EXISTS bot_communications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mint TEXT,
        timestamp TIMESTAMP,
        message_type TEXT,
        message_content TEXT,
        attempt_number INTEGER DEFAULT 1,
        was_successful BOOLEAN DEFAULT FALSE,
        FOREIGN KEY (mint) REFERENCES verified_tokens(mint)
    )
    """
    )

    conn.commit()
    conn.close()


def log_communication(mint, message_type, message_content):
    """Log communication with Trojan bot"""
    conn = sqlite3.connect(TOKENS_DB)
    cursor = conn.cursor()

    # Format timestamp with microseconds precision
    current_time = datetime.now(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S.%f")

    cursor.execute(
        """
    INSERT INTO bot_communications (mint, timestamp, message_type, message_content)
    VALUES (?, ?, ?, ?)
    """,
        (mint, current_time, message_type, message_content),
    )

    conn.commit()
    conn.close()


async def verify_telegram_connection(client):
    """Test Telegram connection by sending a help command to Trojan bot"""
    try:
        logger.info("Testing Telegram connection...")
        await client.send_message(os.getenv("BOT_TG_HANDLE"), "/help")
        log_communication(None, "sent", "/help")
        logger.info("Successfully connected to Telegram and Trojan bot!")
        return True
    except Exception as e:
        logger.error(f"Error connecting to Telegram: {e}")
        return False


async def send_personal_message(client, message):
    """Send a message to your saved messages chat"""
    try:
        # Clean up the channel ID and ensure it's properly formatted
        chat_id = os.getenv("RECIPIENT_IDS").strip()
        chat_id = re.sub(r".*#", "", chat_id)  # Remove any URL parts

        try:
            # Try as integer first
            await client.send_message(int(chat_id), message)
        except ValueError:
            # If not an integer, try as string
            await client.send_message(chat_id, message)

    except Exception as e:
        logger.error(f"Error sending message to saved chat: {e}")
        logger.error(f"Message that failed to send: {message}")


def fetch_verified_tokens():
    """Fetch tokens from the Jupiter API and filter by age"""
    try:
        print("Fetching verified tokens from Jupiter API...")
        api_url = "https://tokens.jup.ag/tokens?tags=verified,community,strict,lst,birdeye-trending,clone,pump"
        response = requests.get(api_url)
        response.raise_for_status()
        tokens = response.json()

        # Get configured token age and ensure cutoff is in UTC
        token_age_minutes = float(os.getenv("TOKEN_AGE_MINUTES", "1"))
        current_time = datetime.now(pytz.UTC)
        cutoff_time = current_time - timedelta(minutes=token_age_minutes)

        formatted_tokens = []
        for token in tokens:
            created_at = token.get("created_at")
            minted_at = token.get("minted_at")

            # Only process tokens that have both dates and meet our criteria
            if (
                created_at
                and minted_at
                and (
                    token.get("freeze_authority") is None
                    and token.get("mint_authority") is None
                    and token.get("permanent_delegate") is None
                )
            ):
                try:
                    created_time = datetime.fromisoformat(
                        created_at.replace("Z", "+00:00")
                    )
                    minted_time = datetime.fromisoformat(
                        minted_at.replace("Z", "+00:00")
                    )

                    # Use minted_time for age filtering instead of created_time
                    if minted_time > cutoff_time:
                        formatted_tokens.append(
                            {
                                "mint": token["address"],
                                "name": token.get("name", ""),
                                "symbol": token.get("symbol", ""),
                                "description": "",
                                "jup_verified": True,
                                "created_at": created_time,
                                "minted_at": minted_time,
                                "daily_volume": token.get("daily_volume", 0),
                                "time_diff_seconds": (
                                    created_time - minted_time
                                ).total_seconds(),
                            }
                        )
                except ValueError:
                    continue

        # Calculate and print time difference statistics
        if formatted_tokens:
            time_diffs = [t["time_diff_seconds"] for t in formatted_tokens]
            avg_diff = sum(time_diffs) / len(time_diffs)
            logger.info("\nAnalysis of minted vs created times:")
            logger.info(f"Number of tokens with both dates: {len(time_diffs)}")
            logger.info(
                f"Average time difference: {avg_diff:.2f} seconds ({avg_diff / 60:.2f} minutes)"
            )
            logger.info(f"Min difference: {min(time_diffs):.2f} seconds")
            logger.info(f"Max difference: {max(time_diffs):.2f} seconds")

        logger.info(
            f"Fetched {len(formatted_tokens)} recent tokens from Jupiter API (last {token_age_minutes} minutes)"
        )
        return formatted_tokens
    except Exception as e:
        logger.error(f"Error fetching tokens: {e}")
        return []


def fetch_from_bitquery():
    """Fetch recent pump tokens from Bitquery"""
    try:
        print("Fetching pump tokens from Bitquery...")
        api_token = os.getenv("BITQUERY_API_TOKEN")
        min_mcap = float(os.getenv("MIN_MCAP_THRESHOLD", "5000"))  # Default 5000
        min_mcap_decimal = (
            f"{(min_mcap / 10000000000):.10f}"  # Convert to decimal format for query
        )

        if not api_token:
            logger.error("BITQUERY_API_TOKEN not found in environment variables")
            return []

        # Build query with proper escaping for nested curly braces
        query = (
            """
        {
            Solana {
                DEXTrades(
                    limitBy: {count: 1, by: Trade_Buy_Currency_MintAddress}
                    limit: {count: 100}
                    orderBy: {descending: Block_Time}
                    where: {
                        Trade: {
                            Buy: {
                                Price: {gt: %s}
                                Currency: {MintAddress: {notIn: ["11111111111111111111111111111111"]}}
                            }
                            Dex: {ProtocolName: {is: "pump"}}
                        }
                        Transaction: {Result: {Success: true}}
                    }
                ) {
                    Block {
                        Time
                    }
                    Trade {
                        Buy {
                            Currency {
                                Name
                                Symbol
                                MintAddress
                                Decimals
                                Fungible
                                Uri
                            }
                        }
                        Sell {
                            Currency {
                                Name
                                Symbol
                                MintAddress
                                Decimals
                                Fungible
                                Uri
                            }
                        }
                    }
                }
            }
        }
        """
            % min_mcap_decimal
        )

        url = "https://streaming.bitquery.io/eap"
        payload = json.dumps({"query": query, "variables": "{}"})
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_token}",
        }

        response = requests.request(
            "POST", url, headers=headers, data=payload, timeout=10
        )
        response.raise_for_status()
        data = response.json()

        # Extract tokens from response
        tokens = []
        if (
            data
            and "data" in data
            and "Solana" in data["data"]
            and "DEXTrades" in data["data"]["Solana"]
        ):
            for trade in data["data"]["Solana"]["DEXTrades"]:
                token = trade["Trade"]["Buy"]["Currency"]
                block_time = datetime.fromisoformat(
                    trade["Block"]["Time"].replace("Z", "+00:00")
                )
                tokens.append(
                    {
                        "mint": token["MintAddress"],
                        "name": token.get("Name", ""),
                        "symbol": token.get("Symbol", ""),
                        "description": "",
                        "jup_verified": True,  # Mark all tokens as verified
                        "created_at": block_time,  # Use block time as created_at
                        "minted_at": block_time,  # Use block time as minted_at
                        "daily_volume": None,  # Bitquery tokens don't have volume info
                    }
                )

        logger.info(f"Fetched {len(tokens)} pump tokens from Bitquery")
        return tokens
    except Exception as e:
        logger.error(f"Error fetching pump tokens: {e}")
        return []


def fetch_solwatch_tokens():
    """Fetch qualifying tokens from SolWatch database"""
    try:
        print("Fetching tokens from SolWatch database...")
        token_age_minutes = float(os.getenv("TOKEN_AGE_MINUTES", "1"))
        selection_mode = os.getenv("TOKEN_SELECTION_MODE", "pump").lower()

        # Get current time in UTC
        current_time = datetime.now(pytz.UTC)
        current_time_str = current_time.strftime("%Y-%m-%d %H:%M:%S.%f")
        cutoff_time = current_time - timedelta(minutes=token_age_minutes)
        cutoff_time_str = cutoff_time.strftime("%Y-%m-%d %H:%M:%S.%f")

        # Connect to SolWatch database in read-only mode
        solwatch_db = sqlite3.connect(f"file:{TOKEN_WATCH_DB}?mode=ro", uri=True)
        cursor = solwatch_db.cursor()

        # Build WHERE clause based on selection mode
        pump_filter = ""
        if selection_mode == "pump":
            pump_filter = "AND is_pump_token = TRUE"
        elif selection_mode == "non-pump":
            pump_filter = "AND is_pump_token = FALSE"
        # For "all" mode, we don't add any pump filter

        query = f"""
            SELECT 
                mint_address,
                raw_supply,
                actual_supply,
                decimals,
                first_seen_slot,
                first_seen_time,
                is_pump_token
            FROM tokens 
            WHERE has_mint_authority = FALSE 
            AND has_freeze_authority = FALSE 
            AND first_seen_time <= ?  -- Current time
            AND first_seen_time > ?   -- Cutoff time
            {pump_filter}
            -- Holder metrics criteria
            AND total_holders >= 100                    -- At least 10 holders to show interest
            -- AND total_holders <= 20                   -- But not too many (still early)
            -- AND holder_ratio >= 0.02                    -- At least 30% held by non-dev wallets
            -- AND top_holder_percentage <= 0.95           -- Top holder owns max 50%
            -- AND top5_holders_percentage <= 0.98         -- Top 5 holders own max 80%
            -- AND lp_holders_count >= 1                  -- At least 1 LP holder
            -- AND last_holder_check IS NOT NULL         -- Ensure we have holder metrics
            ORDER BY first_seen_time DESC
        """

        print(f"\nExecuting query on {TOKEN_WATCH_DB}:")
        print(f"Using time window: {cutoff_time_str} to {current_time_str}")
        print(f"Selection mode: {selection_mode}")

        cursor.execute(query, (current_time_str, cutoff_time_str))

        tokens = []
        for row in cursor.fetchall():
            (
                mint_address,
                raw_supply,
                actual_supply,
                decimals,
                first_seen_slot,
                first_seen_time,
                is_pump_token,
            ) = row
            # Parse timestamp and make it timezone-aware
            created_time = datetime.strptime(
                first_seen_time, "%Y-%m-%d %H:%M:%S.%f"
            ).replace(tzinfo=pytz.UTC)

            tokens.append(
                {
                    "mint": mint_address,
                    "name": "",
                    "symbol": "",
                    "description": f"Supply: {actual_supply:,.2f}, Decimals: {decimals}",
                    "jup_verified": False,
                    "created_at": created_time,  # Now timezone-aware
                    "minted_at": created_time,  # Now timezone-aware
                    "daily_volume": None,
                    "has_mint_authority": False,  # Add these fields since we already filtered for them
                    "has_freeze_authority": False,  # in the SQL query
                    "notification_msg": (
                        f"🔍 SolWatch Token Found!\n"
                        f"Address: {mint_address}\n"
                        f"Supply: {actual_supply:,.2f}\n"
                        f"Decimals: {decimals}\n"
                        f"First seen slot: {first_seen_slot}\n"
                        f"First seen time: {first_seen_time}\n"
                        f"✅ SAFETY CHECKS:\n"
                        f"  - Contains 'pump': {'✅' if is_pump_token else '❌'}\n"
                        f"  - No mint authority: ✅\n"
                        f"  - No freeze authority: ✅"
                    ),
                }
            )

        solwatch_db.close()
        logger.info(f"Fetched {len(tokens)} qualifying tokens from SolWatch")
        return tokens

    except Exception as e:
        logger.error(f"Error fetching SolWatch tokens: {e}")
        return []


def process_tokens(tokens):
    """Process tokens and return new verified tokens and status changes"""
    conn = sqlite3.connect(TOKENS_DB)
    cursor = conn.cursor()

    # Add minted_at column if it doesn't exist
    try:
        cursor.execute(
            "ALTER TABLE verified_tokens ADD COLUMN minted_at TIMESTAMP NULL"
        )
        print("Added minted_at column to verified_tokens table")
    except sqlite3.OperationalError:
        # Column already exists
        pass

    new_verified_tokens = []
    status_changes = []

    # Get configured token age for cleanup
    token_age_minutes = float(os.getenv("TOKEN_AGE_MINUTES", "1"))
    max_retries = int(os.getenv("MAX_TOKEN_RETRIES", "3"))
    selection_mode = os.getenv("TOKEN_SELECTION_MODE", "pump").lower()
    cutoff_time = datetime.now(pytz.UTC) - timedelta(minutes=token_age_minutes)
    cutoff_time_str = cutoff_time.strftime("%Y-%m-%d %H:%M:%S.%f")

    # Clean up old tokens that were never bought
    # cursor.execute(
    #     """
    # DELETE FROM verified_tokens
    # WHERE is_bought = FALSE
    # AND date_added < ?
    # AND retry_count >= ?
    # """,
    #     (cutoff_time_str, max_retries),
    # )

    for token in tokens:
        mint = token["mint"]
        is_verified = token.get("jup_verified", False)
        created_at = token[
            "created_at"
        ]  # Should already be timezone-aware from fetch_solwatch_tokens

        # Check if token matches the selection mode
        has_pump = mint.endswith("pump")
        if selection_mode == "pump" and not has_pump:
            continue
        elif selection_mode == "non-pump" and has_pump:
            continue
        # If selection_mode is "all", process all tokens

        # Only process tokens within our monitoring window
        if created_at > cutoff_time:  # Now comparing timezone-aware datetimes
            # Check if token exists
            cursor.execute(
                "SELECT jup_verified, is_bought, retry_count FROM verified_tokens WHERE mint = ?",
                (mint,),
            )
            result = cursor.fetchone()

            if result is None:
                # New token
                if is_verified:
                    new_verified_tokens.append(token)
                    # Format message for new token notification
                    volume_str = (
                        f"${token['daily_volume']:,.2f}"
                        if token.get("daily_volume")
                        else "No volume data"
                    )
                    created_at_str = token["created_at"].strftime(
                        "%Y-%m-%d %H:%M:%S UTC"
                    )
                    minted_at_str = token["minted_at"].strftime("%Y-%m-%d %H:%M:%S UTC")
                    token["notification_msg"] = (
                        f"🆕 New Token Listed!\n"
                        f"Symbol: {token.get('symbol', 'N/A')}\n"
                        f"Name: {token.get('name', 'N/A')}\n"
                        f"Address: {mint}\n"
                        f"Listed: {created_at_str}\n"
                        f"Minted: {minted_at_str}\n"
                        f"Daily Volume: {volume_str}\n"
                        f"✅ SAFETY CHECKS:\n"
                        f"  - No mint authority: ✅\n"
                        f"  - No freeze authority: ✅\n"
                        f"  - Contains 'pump': {'✅' if has_pump else '❌'}"
                    )

                cursor.execute(
                    """
                INSERT INTO verified_tokens 
                (mint, name, symbol, description, jup_verified, date_added, retry_count, minted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        mint,
                        token.get("name", ""),
                        token.get("symbol", ""),
                        token.get("description", ""),
                        is_verified,
                        token["created_at"],
                        0,
                        token["minted_at"],  # New field
                    ),
                )
            else:
                old_status, is_bought, retry_count = result

                if old_status != is_verified:
                    status_changes.append(
                        {
                            "mint": mint,
                            "name": token.get("name", ""),
                            "old_status": old_status,
                            "new_status": is_verified,
                        }
                    )

                    cursor.execute(
                        """
                    UPDATE verified_tokens 
                    SET jup_verified = ?
                    WHERE mint = ?
                    """,
                        (is_verified, mint),
                    )

    # Print cleanup stats
    if cursor.rowcount > 0:
        print(f"\nCleaned up {cursor.rowcount} old unbought tokens from database")

    conn.commit()
    conn.close()
    return new_verified_tokens, status_changes


def setup_environment(auto=False):
    """Interactive setup for first-time users"""
    print("\n=== Welcome to the Solana Token Monitor Setup ===")
    print("\nLet's get you set up with everything you need!\n")

    # Load current values if they exist
    load_dotenv()
    current_api_id = os.getenv("TELEGRAM_API_ID", "")
    current_api_hash = os.getenv("TELEGRAM_API_HASH", "")
    current_phone = os.getenv("TELEGRAM_PHONE", "")
    current_channel_id = os.getenv("RECIPIENT_IDS", "")
    current_token_age = os.getenv("TOKEN_AGE_MINUTES", "1")
    current_bitquery_token = os.getenv("BITQUERY_API_TOKEN", "")
    current_min_mcap = os.getenv("MIN_MCAP_THRESHOLD", "5000")
    current_selection_mode = os.getenv("TOKEN_SELECTION_MODE", "pump")

    # Check if .env exists
    if os.path.exists(".env"):
        if auto:
            return True  # Skip reconfiguration prompt in auto mode
        print("Existing .env file found. Would you like to reconfigure? (y/n)")
        if input().lower() != "y":
            return (
                True  # Return True to indicate we should continue with existing config
            )

    print("\nStep 1: Telegram API Setup")
    print("First, you'll need to get your Telegram API credentials:")
    print("1. Go to https://my.telegram.org/apps")
    print("2. Log in with your phone number")
    print("3. Create a new application if you haven't already")
    print("\nOnce you have that ready, enter the following information:")
    print("(Press Enter to keep current value)")

    api_id_prompt = f" [current: {current_api_id}]: " if current_api_id else ": "
    api_id = input(f"Enter your API ID{api_id_prompt}").strip()
    if not api_id and current_api_id:
        api_id = current_api_id

    api_hash_prompt = f" [current: {current_api_hash}]: " if current_api_hash else ": "
    api_hash = input(f"Enter your API Hash{api_hash_prompt}").strip()
    if not api_hash and current_api_hash:
        api_hash = current_api_hash

    phone_prompt = (
        f" [current: {current_phone}]: "
        if current_phone
        else " (with country code, e.g., +1234567890): "
    )
    phone = input(f"Enter your phone number{phone_prompt}").strip()
    if not phone and current_phone:
        phone = current_phone

    print("\nStep 2: Telegram Channel Setup")
    print("Now, let's set up your notification channel:")
    print("1. Open Telegram")
    print("2. Go to your saved messages chat")
    print("3. Forward any message from there to @userinfobot")
    print("4. Copy the ID number it gives you")

    channel_id_prompt = (
        f" [current: {current_channel_id}]: " if current_channel_id else ": "
    )
    channel_id = input(f"\nEnter your Telegram chat ID{channel_id_prompt}").strip()
    if not channel_id and current_channel_id:
        channel_id = current_channel_id

    # Clean up the channel ID (remove any URL parts if they paste the full URL)
    channel_id = re.sub(r".*#", "", channel_id)

    print("\nStep 3: Token Age Configuration")
    while True:
        try:
            age_prompt = (
                f" [current: {current_token_age} minutes]: "
                if current_token_age
                else " (default 1): "
            )
            age_input = input(
                f"\nEnter the maximum age of tokens to monitor in minutes{age_prompt}"
            ).strip()
            if not age_input:
                token_age = current_token_age if current_token_age else "1"
                break
            token_age = float(age_input)
            if token_age <= 0:
                print("Please enter a positive number")
                continue
            token_age = str(token_age)
            break
        except ValueError:
            print("Please enter a valid number")

    print("\nStep 4: Bitquery API Setup")
    print("You'll need a Bitquery API token to monitor pump tokens:")
    print("1. Go to https://graphql.bitquery.io")
    print("2. Sign up or log in")
    print("3. Get your API token from your profile")

    bitquery_prompt = (
        f" [current: {current_bitquery_token}]: " if current_bitquery_token else ": "
    )
    bitquery_token = input(f"\nEnter your Bitquery API token{bitquery_prompt}").strip()
    if not bitquery_token and current_bitquery_token:
        bitquery_token = current_bitquery_token

    print("\nStep 5: Market Cap Threshold Configuration")
    print("Set the minimum market cap threshold for tokens")
    print("Example: Enter 5000 for 0.0000005 SOL market cap")
    print("         Enter 2500 for 0.00000025 SOL market cap")
    print("The value will be divided by 10^10 for the actual query")

    while True:
        try:
            mcap_prompt = (
                f" [current: {current_min_mcap}]: "
                if current_min_mcap
                else " (default 5000): "
            )
            mcap_input = input(
                f"\nEnter the minimum market cap threshold{mcap_prompt}"
            ).strip()
            if not mcap_input:
                min_mcap = current_min_mcap if current_min_mcap else "5000"
                break
            min_mcap = float(mcap_input)
            if min_mcap <= 0:
                print("Please enter a positive number")
                continue
            # Show the user what their input converts to
            decimal_value = min_mcap / 10000000000
            print(f"This will filter tokens with market cap > {decimal_value:.10f} SOL")
            min_mcap = str(min_mcap)
            break
        except ValueError:
            print("Please enter a valid number")

    print("\nStep 6: Wait Time Configuration")
    while True:
        try:
            wait_time_prompt = (
                f" [current: {os.getenv('WAIT_SECONDS', '5')} seconds]: "
                if os.getenv("WAIT_SECONDS")
                else " (default 5): "
            )
            wait_input = input(
                f"\nEnter the base wait time in seconds{wait_time_prompt}"
            ).strip()
            if not wait_input:
                wait_time = os.getenv("WAIT_SECONDS", "5")
                break
            wait_time = float(wait_input)
            if wait_time <= 0:
                print("Please enter a positive number")
                continue
            wait_time = str(wait_time)
            break
        except ValueError:
            print("Please enter a valid number")

    print("\nStep 7: Token Selection Mode")
    print("Choose how to filter tokens based on 'pump' in their name:")
    print("1. pump    - Only process tokens with 'pump' in the name")
    print("2. non-pump - Only process tokens without 'pump' in the name")
    print("3. all     - Process all tokens regardless of name")

    selection_mode_prompt = (
        f" [current: {current_selection_mode}]: " if current_selection_mode else ": "
    )
    while True:
        mode = (
            input(f"Enter token selection mode{selection_mode_prompt}").strip().lower()
        )
        if not mode and current_selection_mode:
            mode = current_selection_mode
            break
        if mode in ["pump", "non-pump", "all"]:
            break
        print("Please enter 'pump', 'non-pump', or 'all'")

    print("\nStep 8: Token Source Configuration")
    print("Configure which token sources to monitor")

    while True:
        try:
            jupiter_prompt = (
                f" [current: {os.getenv('ENABLE_JUPITER', 'false')}]: "
                if os.getenv("ENABLE_JUPITER")
                else " (default: false): "
            )
            jupiter_input = (
                input(f"\nEnable Jupiter verified tokens? (true/false){jupiter_prompt}")
                .strip()
                .lower()
            )
            if not jupiter_input:
                enable_jupiter = os.getenv("ENABLE_JUPITER", "false")
                break
            if jupiter_input not in ["true", "false"]:
                print("Please enter true or false")
                continue
            enable_jupiter = jupiter_input
            break
        except ValueError:
            print("Please enter true or false")

    while True:
        try:
            pump_prompt = (
                f" [current: {os.getenv('ENABLE_PUMP', 'true')}]: "
                if os.getenv("ENABLE_PUMP")
                else " (default: true): "
            )
            pump_input = (
                input(f"\nEnable pump tokens? (true/false){pump_prompt}")
                .strip()
                .lower()
            )
            if not pump_input:
                enable_pump = os.getenv("ENABLE_PUMP", "true")
                break
            if pump_input not in ["true", "false"]:
                print("Please enter true or false")
                continue
            enable_pump = pump_input
            break
        except ValueError:
            print("Please enter true or false")

    while True:
        try:
            solwatch_prompt = (
                f" [current: {os.getenv('ENABLE_SOLWATCH', 'true')}]: "
                if os.getenv("ENABLE_SOLWATCH")
                else " (default: true): "
            )
            solwatch_input = (
                input(f"\nEnable SolWatch tokens? (true/false){solwatch_prompt}")
                .strip()
                .lower()
            )
            if not solwatch_input:
                enable_solwatch = os.getenv("ENABLE_SOLWATCH", "true")
                break
            if solwatch_input not in ["true", "false"]:
                print("Please enter true or false")
                continue
            enable_solwatch = solwatch_input
            break
        except ValueError:
            print("Please enter true or false")

    # Create .env file
    with open(".env", "w") as f:
        f.write(f"TELEGRAM_API_ID={api_id}\n")
        f.write(f"TELEGRAM_API_HASH={api_hash}\n")
        f.write(f"TELEGRAM_PHONE={phone}\n")
        f.write(f"RECIPIENT_IDS={channel_id}\n")
        f.write(f"TOKEN_AGE_MINUTES={token_age}\n")
        f.write(f"TOKEN_SELECTION_MODE={mode}\n")
        f.write(f"WAIT_SECONDS={wait_time}\n")
        f.write(f"BITQUERY_API_TOKEN={bitquery_token}\n")
        f.write(f"MIN_MCAP_THRESHOLD={min_mcap}\n")
        f.write(f"ENABLE_JUPITER={enable_jupiter}\n")
        f.write(f"ENABLE_PUMP={enable_pump}\n")
        f.write(f"ENABLE_SOLWATCH={enable_solwatch}\n")

    print("\nStep 9: Verification")
    print("Let's verify your setup...")

    # Load the new environment variables
    load_dotenv(override=True)

    try:
        # Test creating a client
        client = TelegramClient("test_session", api_id, api_hash)
        print("\n✅ Telegram API credentials verified!")
    except Exception as e:
        print(f"\n❌ Error with Telegram credentials: {e}")
        return False

    print("\n=== Setup Complete! ===")
    print("\nYour configuration has been saved. The script will now:")
    print(f"1. Monitor for new Jupiter-verified tokens (up to {token_age} minutes old)")
    print("2. Monitor for new pump tokens via Bitquery")
    print("3. Attempt to purchase them through the Trojan bot")
    print("4. Send status updates to your saved messages")
    print("\nWould you like to start the monitor now? (y/n)")

    return input().lower() == "y"


async def handle_trojan_response(event, current_mint):
    """Handle and log responses from Trojan bot"""
    response = event.message.text

    # Always log the message for monitoring purposes
    logger.info(f"\nTrojan Bot Response for {current_mint}:")
    logger.info(response)

    # Skip processing if no current mint or help message
    if not current_mint or TROJAN_HELP_PATTERN in response.lower():
        return

    # Skip processing if the message doesn't contain our current mint address
    if current_mint not in response:
        return

    # Only check for success patterns
    if any(x in response.lower() for x in TROJAN_SUCCESS_PATTERNS):
        conn = sqlite3.connect(TOKENS_DB)
        cursor = conn.cursor()

        try:
            # Mark as bought if successful
            current_time = datetime.now(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
            cursor.execute(
                """
                UPDATE verified_tokens 
                SET is_bought = TRUE,
                    date_bought = ?
                WHERE mint = ? AND is_bought = FALSE
                """,
                (current_time, current_mint),
            )

            if cursor.rowcount > 0:  # Only notify if we actually updated a row
                success_msg = f"🎉 Successfully purchased token!\nMint: {current_mint}"
                await send_personal_message(event.client, success_msg)

            conn.commit()
        finally:
            conn.close()

    # Log the communication
    log_communication(current_mint, "received", response)


def setup_logger():
    """Configure logging with timestamps and levels"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S UTC",
    )
    # Set timezone to UTC for logging
    logging.Formatter.converter = lambda *args: datetime.now(pytz.UTC).timetuple()
    return logging.getLogger("TokenMonitor")


# At the start of main.py, after imports:
logger = setup_logger()


async def main(auto=False):
    # Run setup if needed
    if not os.path.exists(".env") or not all(
        [
            os.getenv("TELEGRAM_API_ID"),
            os.getenv("TELEGRAM_API_HASH"),
            os.getenv("TELEGRAM_PHONE"),
            os.getenv("RECIPIENT_IDS"),
            os.getenv("TOKEN_AGE_MINUTES"),
            os.getenv("BITQUERY_API_TOKEN"),
        ]
    ):
        if not setup_environment(auto):
            print("\nSetup incomplete. Please run the script again when ready.")
            return

    # Load environment variables after setup
    load_dotenv()

    # Now it's safe to get these values
    API_ID = os.getenv("TELEGRAM_API_ID")
    API_HASH = os.getenv("TELEGRAM_API_HASH")
    PHONE_NUMBER = os.getenv("TELEGRAM_PHONE")
    MY_TELEGRAM_ID = os.getenv("RECIPIENT_IDS").split(",")[0]
    TOKEN_AGE = os.getenv("TOKEN_AGE_MINUTES", "1")

    create_database()

    async with TelegramClient("sirouk_session", API_ID, API_HASH) as client:
        current_mint = None

        @client.on(events.NewMessage(from_users=os.getenv("BOT_TG_HANDLE")))
        async def trojan_handler(event):
            await handle_trojan_response(event, current_mint)

        if not await verify_telegram_connection(client):
            return

        logger.info(
            f"Starting main loop - monitoring for new verified tokens (last {TOKEN_AGE} minutes)"
        )

        # Get wait times from environment
        base_wait = float(os.getenv("WAIT_SECONDS", "1"))
        retry_wait = base_wait * 2

        while True:
            try:
                all_tokens = []

                # Fetch tokens based on configuration
                if os.getenv("ENABLE_JUPITER", "false").lower() == "true":
                    jupiter_tokens = fetch_verified_tokens()
                    all_tokens.extend(jupiter_tokens)
                    # print(f"Jupiter tokens: {jupiter_tokens}")
                if os.getenv("ENABLE_PUMP", "true").lower() == "true":
                    bitquery_tokens = fetch_from_bitquery()
                    all_tokens.extend(bitquery_tokens)
                    # print(f"Bitquery tokens: {bitquery_tokens}")

                if os.getenv("ENABLE_SOLWATCH", "true").lower() == "true":
                    solwatch_tokens = fetch_solwatch_tokens()
                    all_tokens.extend(solwatch_tokens)
                    # print(f"SolWatch tokens: {solwatch_tokens}")

                if all_tokens:
                    new_verified_tokens, status_changes = process_tokens(all_tokens)

                    # Notify about new tokens
                    for token in new_verified_tokens:
                        if hasattr(token, "notification_msg"):
                            await send_personal_message(
                                client, token["notification_msg"]
                            )

                    # Get tokens that need processing (new or retry)
                    conn = sqlite3.connect(TOKENS_DB)
                    cursor = conn.cursor()

                    max_retries = int(os.getenv("MAX_TOKEN_RETRIES", "3"))
                    cursor.execute(
                        """
                    SELECT mint, retry_count 
                    FROM verified_tokens 
                    WHERE is_bought = FALSE 
                    AND retry_count < ?
                    ORDER BY date_added ASC
                    """,
                        (max_retries,),
                    )

                    tokens_to_process = cursor.fetchall()
                    conn.close()

                    # Process each token
                    for mint, retry_count in tokens_to_process:
                        current_mint = mint
                        logger.info(
                            f"Processing token (attempt {retry_count + 1}/{max_retries}): {mint}"
                        )

                        # Send token to bot
                        await client.send_message(os.getenv("BOT_TG_HANDLE"), mint)
                        log_communication(mint, "sent", mint)

                        # Immediately increment retry count
                        conn = sqlite3.connect(TOKENS_DB)
                        cursor = conn.cursor()
                        current_time = datetime.now(pytz.UTC).strftime(
                            "%Y-%m-%d %H:%M:%S.%f"
                        )

                        cursor.execute(
                            """
                            UPDATE verified_tokens 
                            SET retry_count = retry_count + 1,
                                last_attempt = ?
                            WHERE mint = ? AND is_bought = FALSE
                            """,
                            (current_time, mint),
                        )
                        conn.commit()
                        conn.close()

                        # Wait before processing next token
                        await asyncio.sleep(retry_wait)

                current_mint = None
                logger.info(f"Waiting {base_wait} seconds before next check")
                await asyncio.sleep(base_wait)

            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                await asyncio.sleep(base_wait)


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args.auto))
