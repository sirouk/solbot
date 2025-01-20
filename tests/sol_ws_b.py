import asyncio
import json
import websockets
import logging
import base64
import signal
import sys
import os
import dotenv



# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Constants
dotenv.load_dotenv()
SOLANA_MAINNET_WS = os.getenv("SOLANA_MAINNET_WS", "wss://api.mainnet-beta.solana.com")
TOKEN_PROGRAM_ID = os.getenv("TOKEN_PROGRAM_ID", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

async def unsubscribe_all(websocket):
    unsubscribe_message = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "unsubscribeAll"
    }
    
    await websocket.send(json.dumps(unsubscribe_message))
    unsubscribe_response = await websocket.recv()
    logger.info(f"Unsubscribe response: {unsubscribe_response}")

async def subscribe_to_program():
    async with websockets.connect(SOLANA_MAINNET_WS) as websocket:
        # First unsubscribe from any existing subscriptions
        await unsubscribe_all(websocket)

        # Now subscribe to Token Program account changes
        subscribe_message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "accountSubscribe",
            "params": [
                TOKEN_PROGRAM_ID,  # The Token Program ID
                {
                    "encoding": "jsonParsed",
                    "commitment": "finalized",
                    "filters": [
                        {
                            "dataSize": 165  # Size of token accounts
                        },
                        {
                            "mentions": [TOKEN_PROGRAM_ID]
                        }
                    ]
                }
            ]
        }

        await websocket.send(json.dumps(subscribe_message))
        subscription_response = await websocket.recv()
        subscription_data = json.loads(subscription_response)
        
        # The subscription response just gives us the subscription id
        subscription_id = subscription_data.get("result")
        logger.info(f"Subscribed with id: {subscription_id}")
        
        # Get the current slot from the first message we receive
        initial_message = await websocket.recv()
        initial_data = json.loads(initial_message)
        initial_slot = initial_data.get("params", {}).get("result", {}).get("context", {}).get("slot", 0)
        logger.info(f"Starting at slot: {initial_slot}")

        try:
            while True:
                try:
                    message = await websocket.recv()
                    data = json.loads(message)
                    print(data)
                    sys.exit()
                    
                    if "params" in data:
                        result = data["params"]["result"]
                        if "value" in result and "account" in result["value"]:
                            account_data = result["value"]["account"]
                            
                            # Check if this is parsed SPL token data
                            if "data" in account_data and "parsed" in account_data["data"]:
                                parsed_data = account_data["data"]["parsed"]
                                
                                # Check if this is a token account
                                if parsed_data["type"] == "account":
                                    token_info = parsed_data["info"]
                                    amount = int(token_info["tokenAmount"]["amount"])
                                    mint = token_info["mint"]
                                    token_address = result['value']['pubkey']
                                    
                                    # Check if the token address or mint contains 'pump'
                                    is_pump_token = "pump" in token_address.lower() or "pump" in mint.lower()
                                    
                                    logger.info(f"🔔 Token Account Update Detected!")
                                    logger.info(f"Token Account: {token_address}")
                                    logger.info(f"Mint Address: {mint}")
                                    logger.info(f"Amount: {amount}")
                                    logger.info(f"Decimals: {token_info['tokenAmount']['decimals']}")
                                    
                                    if is_pump_token:
                                        logger.info(f"🎉 Friendly Indicator: Contains 'pump' in name/address")
                                    
                                    logger.info("-" * 50)

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
    logger.info("Starting Solana Token Program monitor...")
    asyncio.run(main()) 