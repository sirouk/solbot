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

def parse_args():
    parser = argparse.ArgumentParser(description='Solana Token Monitor')
    parser.add_argument('--auto', action='store_true', help='Skip configuration prompts if .env exists')
    return parser.parse_args()

def create_database():
    """Create the SQLite database and tables"""
    conn = sqlite3.connect('tokens.db')
    cursor = conn.cursor()
    
    # Create tokens table
    cursor.execute('''
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
    ''')
    
    # Create communications table
    cursor.execute('''
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
    ''')
    
    conn.commit()
    conn.close()

def log_communication(mint, message_type, message_content):
    """Log communication with Trojan bot"""
    conn = sqlite3.connect('tokens.db')
    cursor = conn.cursor()
    
    cursor.execute('''
    INSERT INTO bot_communications (mint, timestamp, message_type, message_content)
    VALUES (?, ?, ?, ?)
    ''', (mint, datetime.now(), message_type, message_content))
    
    conn.commit()
    conn.close()

async def verify_telegram_connection(client):
    """Test Telegram connection by sending a help command to Trojan bot"""
    try:
        print("Testing Telegram connection...")
        await client.send_message('solana_trojanbot', '/help')
        log_communication(None, 'sent', '/help')
        print("Successfully connected to Telegram and Trojan bot!")
        return True
    except Exception as e:
        print(f"Error connecting to Telegram: {e}")
        return False

async def send_personal_message(client, message):
    """Send a message to your saved messages chat"""
    try:
        # Clean up the channel ID and ensure it's properly formatted
        chat_id = os.getenv('RECIPIENT_IDS').strip()
        chat_id = re.sub(r'.*#', '', chat_id)  # Remove any URL parts
        
        try:
            # Try as integer first
            await client.send_message(int(chat_id), message)
        except ValueError:
            # If not an integer, try as string
            await client.send_message(chat_id, message)
            
    except Exception as e:
        print(f"Error sending message to saved chat: {e}")
        print(f"Message that failed to send: {message}")

def fetch_verified_tokens():
    """Fetch tokens from the Jupiter API and filter by age"""
    try:
        response = requests.get("https://tokens.jup.ag/tokens?tags=verified")
        response.raise_for_status()
        tokens = response.json()
        
        # Get configured token age
        token_age = float(os.getenv('TOKEN_AGE_HOURS', '1'))
        cutoff_time = datetime.now(pytz.UTC) - timedelta(hours=token_age)
        
        # Convert the response to match our expected format and filter by age
        formatted_tokens = []
        for token in tokens:
            created_at = token.get('created_at')
            if created_at:
                try:
                    created_time = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    if created_time > cutoff_time:
                        formatted_tokens.append({
                            'mint': token['address'],
                            'name': token.get('name', ''),
                            'symbol': token.get('symbol', ''),
                            'description': '',  # Jupiter API doesn't provide description
                            'jup_verified': True,  # All tokens from this endpoint are verified
                            'created_at': created_time,
                            'daily_volume': token.get('daily_volume', 0)
                        })
                except ValueError:
                    continue
        
        print(f"\nFetched {len(formatted_tokens)} recent tokens from Jupiter API (last {token_age} hours)")
        return formatted_tokens
    except Exception as e:
        print(f"Error fetching tokens: {e}")
        return []

def process_tokens(tokens):
    """Process tokens and return new verified tokens and status changes"""
    conn = sqlite3.connect('tokens.db')
    cursor = conn.cursor()
    
    new_verified_tokens = []
    status_changes = []
    
    # Get configured token age for cleanup
    token_age = float(os.getenv('TOKEN_AGE_HOURS', '1'))
    cutoff_time = datetime.now(pytz.UTC) - timedelta(hours=token_age)
    
    # Clean up old tokens that were never bought
    cursor.execute('''
    DELETE FROM verified_tokens 
    WHERE is_bought = FALSE 
    AND date_added < ? 
    AND retry_count >= 3
    ''', (cutoff_time,))
    
    for token in tokens:
        mint = token['mint']
        is_verified = token.get('jup_verified', False)
        created_at = token['created_at']
        
        # Only process tokens within our monitoring window
        if created_at > cutoff_time:
            # Check if token exists
            cursor.execute('SELECT jup_verified, is_bought, retry_count FROM verified_tokens WHERE mint = ?', (mint,))
            result = cursor.fetchone()
            
            if result is None:
                # New token
                if is_verified:
                    new_verified_tokens.append(token)
                    # Format message for new token notification
                    volume_str = f"${token['daily_volume']:,.2f}" if token.get('daily_volume') else "No volume data"
                    created_at_str = token['created_at'].strftime('%Y-%m-%d %H:%M:%S UTC')
                    token['notification_msg'] = (
                        f"🆕 New Token Listed!\n"
                        f"Symbol: {token.get('symbol', 'N/A')}\n"
                        f"Name: {token.get('name', 'N/A')}\n"
                        f"Address: {mint}\n"
                        f"Listed: {created_at_str}\n"
                        f"Daily Volume: {volume_str}"
                    )
                
                cursor.execute('''
                INSERT INTO verified_tokens 
                (mint, name, symbol, description, jup_verified, date_added, retry_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    mint,
                    token.get('name', ''),
                    token.get('symbol', ''),
                    token.get('description', ''),
                    is_verified,
                    token['created_at'],
                    0
                ))
            else:
                old_status, is_bought, retry_count = result
                
                if old_status != is_verified:
                    status_changes.append({
                        'mint': mint,
                        'name': token.get('name', ''),
                        'old_status': old_status,
                        'new_status': is_verified
                    })
                    
                    cursor.execute('''
                    UPDATE verified_tokens 
                    SET jup_verified = ?
                    WHERE mint = ?
                    ''', (is_verified, mint))
    
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
    current_api_id = os.getenv('TELEGRAM_API_ID', '')
    current_api_hash = os.getenv('TELEGRAM_API_HASH', '')
    current_phone = os.getenv('TELEGRAM_PHONE', '')
    current_channel_id = os.getenv('RECIPIENT_IDS', '')
    current_token_age = os.getenv('TOKEN_AGE_HOURS', '1')

    # Check if .env exists
    if os.path.exists('.env'):
        if auto:
            return True  # Skip reconfiguration prompt in auto mode
        print("Existing .env file found. Would you like to reconfigure? (y/n)")
        if input().lower() != 'y':
            return True  # Return True to indicate we should continue with existing config

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

    phone_prompt = f" [current: {current_phone}]: " if current_phone else " (with country code, e.g., +1234567890): "
    phone = input(f"Enter your phone number{phone_prompt}").strip()
    if not phone and current_phone:
        phone = current_phone

    print("\nStep 2: Telegram Channel Setup")
    print("Now, let's set up your notification channel:")
    print("1. Open Telegram")
    print("2. Go to your saved messages chat")
    print("3. Forward any message from there to @userinfobot")
    print("4. Copy the ID number it gives you")
    
    channel_id_prompt = f" [current: {current_channel_id}]: " if current_channel_id else ": "
    channel_id = input(f"\nEnter your Telegram chat ID{channel_id_prompt}").strip()
    if not channel_id and current_channel_id:
        channel_id = current_channel_id
    
    # Clean up the channel ID (remove any URL parts if they paste the full URL)
    channel_id = re.sub(r'.*#', '', channel_id)

    print("\nStep 3: Token Age Configuration")
    while True:
        try:
            age_prompt = f" [current: {current_token_age} hours]: " if current_token_age else " (default 1): "
            age_input = input(f"\nEnter the maximum age of tokens to monitor{age_prompt}").strip()
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

    # Create .env file
    with open('.env', 'w') as f:
        f.write(f'TELEGRAM_API_ID={api_id}\n')
        f.write(f'TELEGRAM_API_HASH={api_hash}\n')
        f.write(f'TELEGRAM_PHONE={phone}\n')
        f.write(f'RECIPIENT_IDS={channel_id}\n')
        f.write(f'TOKEN_AGE_HOURS={token_age}\n')

    print("\nStep 4: Verification")
    print("Let's verify your setup...")
    
    # Load the new environment variables
    load_dotenv(override=True)
    
    try:
        # Test creating a client
        client = TelegramClient('test_session', api_id, api_hash)
        print("\n✅ Telegram API credentials verified!")
    except Exception as e:
        print(f"\n❌ Error with Telegram credentials: {e}")
        return False

    print("\n=== Setup Complete! ===")
    print("\nYour configuration has been saved. The script will now:")
    print(f"1. Monitor for new Jupiter-verified tokens (up to {token_age} hours old)")
    print("2. Attempt to purchase them through the Trojan bot")
    print("3. Send status updates to your saved messages")
    print("\nWould you like to start the monitor now? (y/n)")
    
    return input().lower() == 'y'

async def handle_trojan_response(event, current_mint):
    """Handle and log responses from Trojan bot"""
    response = event.message.text
    
    # Skip processing help message
    if "How do I use Trojan?" in response:
        return
        
    print(f"\nTrojan Bot Response for {current_mint}:")
    print(response)
    
    # Check if response indicates a transaction was sent
    if "Transaction sent" in response:
        # Wait for message edit with transaction result
        try:
            # Wait up to 60 seconds for transaction confirmation
            for _ in range(12):  # 12 * 5 seconds = 60 seconds total
                await asyncio.sleep(5)
                # Get the updated message
                message = await event.client.get_messages(event.chat_id, ids=[event.message.id])
                if message and message[0].text != response:
                    # Message was edited, process the new response
                    response = message[0].text
                    print(f"\nUpdated Trojan Bot Response:")
                    print(response)
                    break
        except Exception as e:
            print(f"Error checking transaction status: {e}")
    
    # Check if response indicates a failure
    is_failure = any(x in response.lower() for x in [
        "insufficient balance",
        "error",
        "failed",
        "🔴",
        "token not found",
        "transaction failed"
    ])
    
    # Check if response indicates success
    is_success = any(x in response.lower() for x in [
        "buy success",
    ])
    
    if current_mint:
        conn = sqlite3.connect('tokens.db')
        cursor = conn.cursor()
        
        try:
            if is_failure:
                # Increment retry count
                cursor.execute('''
                UPDATE verified_tokens 
                SET retry_count = retry_count + 1,
                    last_attempt = ?
                WHERE mint = ?
                ''', (datetime.now(), current_mint))
                
                # Check if we've hit max retries
                cursor.execute('SELECT retry_count FROM verified_tokens WHERE mint = ?', (current_mint,))
                retry_count = cursor.fetchone()[0]
                print(f"Current retry count for {current_mint}: {retry_count}")
                
                if retry_count >= 3:
                    failure_msg = (
                        f"⚠️ Maximum retries reached for token!\n"
                        f"Mint: {current_mint}\n"
                        f"Last error: {response}"
                    )
                    await send_personal_message(event.client, failure_msg)
            elif is_success:
                # Success - mark as bought
                cursor.execute('''
                UPDATE verified_tokens 
                SET is_bought = TRUE,
                    date_bought = ?
                WHERE mint = ?
                ''', (datetime.now(), current_mint))
                
                success_msg = (
                    f"🎉 Successfully purchased token!\n"
                    f"Mint: {current_mint}"
                )
                await send_personal_message(event.client, success_msg)
            
            conn.commit()
        finally:
            conn.close()
    
    # Log the communication
    log_communication(current_mint, 'received', response)

async def main(auto=False):
    # Run setup if needed
    if not os.path.exists('.env') or not all([
        os.getenv('TELEGRAM_API_ID'),
        os.getenv('TELEGRAM_API_HASH'),
        os.getenv('TELEGRAM_PHONE'),
        os.getenv('RECIPIENT_IDS'),
        os.getenv('TOKEN_AGE_HOURS')
    ]):
        if not setup_environment(auto):
            print("\nSetup incomplete. Please run the script again when ready.")
            return

    # Load environment variables after setup
    load_dotenv()
    
    # Now it's safe to get these values
    API_ID = os.getenv('TELEGRAM_API_ID')
    API_HASH = os.getenv('TELEGRAM_API_HASH')
    PHONE_NUMBER = os.getenv('TELEGRAM_PHONE')
    MY_TELEGRAM_ID = os.getenv('RECIPIENT_IDS').split(',')[0]
    TOKEN_AGE = os.getenv('TOKEN_AGE_HOURS', '1')

    create_database()
    
    async with TelegramClient('sirouk_session', API_ID, API_HASH) as client:
        current_mint = None
        
        @client.on(events.NewMessage(from_users='solana_trojanbot'))
        async def trojan_handler(event):
            await handle_trojan_response(event, current_mint)
        
        if not await verify_telegram_connection(client):
            return
        
        print(f"\nStarting main loop - monitoring for new verified tokens (last {TOKEN_AGE} hours)...")
        
        while True:
            try:
                tokens = fetch_verified_tokens()
                
                if tokens:
                    new_verified_tokens, status_changes = process_tokens(tokens)
                    
                    # Notify about new tokens
                    for token in new_verified_tokens:
                        if hasattr(token, 'notification_msg'):
                            await send_personal_message(client, token['notification_msg'])
                    
                    # Get tokens that need processing (new or retry)
                    conn = sqlite3.connect('tokens.db')
                    cursor = conn.cursor()
                    
                    cursor.execute('''
                    SELECT mint, retry_count 
                    FROM verified_tokens 
                    WHERE is_bought = FALSE 
                    AND retry_count < 3 
                    AND jup_verified = TRUE
                    ORDER BY date_added ASC
                    ''')
                    
                    tokens_to_process = cursor.fetchall()
                    conn.close()
                    
                    # Process each token
                    for mint, retry_count in tokens_to_process:
                        current_mint = mint
                        print(f"\nProcessing token (attempt {retry_count + 1}/3):")
                        print(f"Mint: {mint}")
                        await client.send_message('solana_trojanbot', mint)
                        await asyncio.sleep(5)  # Wait for response
                
                current_mint = None
                print("\nWaiting 15 seconds before next check...")
                await asyncio.sleep(15)
                
            except Exception as e:
                print(f"\nError in main loop: {e}")
                await asyncio.sleep(15)

if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args.auto)) 