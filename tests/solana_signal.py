import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
import aiohttp
import base58
from typing import List, Dict, Any
import os
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S UTC",
)
logger = logging.getLogger("SolanaSignal")

# Constants
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
WSOL_ADDRESS = "So11111111111111111111111111111111111111112"
RAYDIUM_PROGRAM_ID = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"  # Raydium SwapV2
ORCA_PROGRAM_ID = "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP"  # Orca Whirlpool

# RPC Configuration
DEFAULT_RPC_URLS = [
    "https://api.mainnet-beta.solana.com",
    "https://mainnet.rpcpool.com",
    "https://free.rpcpool.com",
    "https://solana-api.projectserum.com",
    "https://rpc.ankr.com/solana",
    "https://solana.getblock.io/mainnet-beta",
    "http://127.0.0.1:8899",  # Keep local RPC as backup
]


class SolanaSignal:
    def __init__(self, rpc_url: str = None):
        """Initialize with multiple RPC endpoints"""
        # If a specific RPC URL is provided, add it to the front of the list
        self.rpc_urls = DEFAULT_RPC_URLS.copy()
        if rpc_url:
            if rpc_url not in self.rpc_urls:
                self.rpc_urls.insert(0, rpc_url)

        self.current_rpc_index = 0
        self.session = None
        self.request_id = 0
        self.last_request_time = 0
        self.min_request_interval = 1.0  # 1 second between requests
        self.rate_limit_backoff = 5.0  # 5 seconds when rate limited
        self.endpoints_health = {url: 1.0 for url in self.rpc_urls}
        self.last_rotation_time = time.time()
        self.rotation_cooldown = 1.0  # Minimum time between rotations
        logger.info(
            f"Initialized with {len(self.rpc_urls)} RPC endpoints: {self.rpc_urls}"
        )

    @property
    def current_rpc_url(self) -> str:
        return self.rpc_urls[self.current_rpc_index]

    def rotate_rpc_url(self, error_code: int = None):
        """Rotate to next RPC endpoint with cooldown and health tracking"""
        now = time.time()
        current_url = self.current_rpc_url

        # Check if enough time has passed since last rotation
        if now - self.last_rotation_time < self.rotation_cooldown:
            return

        # Update health scores
        if error_code == 429:  # Rate limit error
            self.endpoints_health[current_url] *= 0.5
        elif error_code == 410:  # Method disabled
            self.endpoints_health[current_url] = 0  # Mark as unusable
        elif error_code == 403:  # Access forbidden
            self.endpoints_health[current_url] = 0  # Mark as unusable
        elif error_code:  # Other errors
            self.endpoints_health[current_url] *= 0.8

        # Find next usable endpoint
        original_index = self.current_rpc_index
        while True:
            # Move to next endpoint
            self.current_rpc_index = (self.current_rpc_index + 1) % len(self.rpc_urls)

            # If we've tried all endpoints, reset health scores and break
            if self.current_rpc_index == original_index:
                self.endpoints_health = {url: 1.0 for url in self.rpc_urls}
                break

            # If this endpoint is usable (health > 0), use it
            if self.endpoints_health[self.current_rpc_url] > 0:
                break

        # Update rotation time
        self.last_rotation_time = now

        logger.info(
            f"Rotating from {current_url} to {self.current_rpc_url} (Health scores: {self.endpoints_health})"
        )

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def _rpc_request(self, method: str, params: List[Any] = None) -> Dict:
        """Make an RPC request to the Solana node with rate limiting and failover"""
        self.request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method,
            "params": params or [],
        }

        # Rate limiting
        now = time.time()
        time_since_last = now - self.last_request_time
        if time_since_last < self.min_request_interval:
            await asyncio.sleep(self.min_request_interval - time_since_last)

        self.last_request_time = time.time()

        # Try each RPC endpoint
        attempts = 0
        max_attempts = len(self.rpc_urls) * 2  # Allow two rounds of attempts

        while attempts < max_attempts:
            current_url = self.rpc_urls[self.current_rpc_index]
            try:
                logger.info(f"Request {self.request_id} to {current_url}: {method}")
                async with self.session.post(
                    current_url, json=payload, timeout=30
                ) as response:
                    result = await response.json()

                    if "error" in result:
                        error_code = result["error"].get("code", 0)
                        if error_code == 429:  # Rate limited
                            logger.warning(
                                f"Rate limited by {current_url}, rotating..."
                            )
                            self.rotate_rpc_url(429)
                            await asyncio.sleep(self.rate_limit_backoff)
                        elif error_code == 403:  # Access forbidden
                            logger.warning(
                                f"Access forbidden by {current_url}, rotating..."
                            )
                            self.rotate_rpc_url(403)
                        elif error_code == 410:  # Gone/Disabled
                            logger.warning(
                                f"Method disabled by {current_url}, rotating..."
                            )
                            self.rotate_rpc_url(410)
                        elif error_code in [-32002, -32602]:  # Other recoverable errors
                            logger.warning(
                                f"Error {error_code} from {current_url}, rotating..."
                            )
                            self.rotate_rpc_url(error_code)
                        else:  # Unrecoverable error
                            logger.error(
                                f"Failed request {self.request_id}: {result['error']}"
                            )
                            self.rotate_rpc_url(error_code)  # Rotate on any error
                            return None
                    else:
                        logger.info(f"Success {self.request_id} from {current_url}")
                        # Success - improve health score
                        self.endpoints_health[current_url] = min(
                            self.endpoints_health[current_url] * 1.2, 1.0
                        )
                        return result["result"]

            except Exception as e:
                logger.error(f"Network error with {current_url}")
                self.rotate_rpc_url(-1)  # Rotate on network errors

            attempts += 1
            await asyncio.sleep(0.1)  # Small delay between attempts

        logger.error(f"Request {self.request_id} failed on all endpoints")
        return None

    async def get_recent_token_mints(self, limit: int = 5) -> List[Dict]:
        """Get recently minted tokens and their authority information"""
        try:
            # Get recent signatures with memcmp filter for initializeMint
            signatures = await self._rpc_request(
                "getSignaturesForAddress",
                [
                    TOKEN_PROGRAM_ID,
                    {
                        "limit": limit
                        * 2,  # Reduced multiplier since we're more targeted
                        "commitment": "confirmed",
                        "filters": [
                            {
                                "memcmp": {
                                    "offset": 0,
                                    "bytes": "3",  # initializeMint instruction discriminator
                                }
                            }
                        ],
                    },
                ],
            )

            if not signatures:
                return []

            recent_tokens = []
            for sig in signatures:
                try:
                    # Get parsed transaction
                    tx = await self._rpc_request(
                        "getTransaction",
                        [
                            sig["signature"],
                            {
                                "encoding": "jsonParsed",
                                "maxSupportedTransactionVersion": 0,
                            },
                        ],
                    )

                    if not tx or "error" in tx:
                        continue

                    # Find the mint instruction and extract mint address
                    mint_address = None
                    for ix in tx["transaction"]["message"]["instructions"]:
                        if ix["programId"] == TOKEN_PROGRAM_ID:
                            if ix.get("parsed", {}).get("type") == "initializeMint":
                                mint_address = ix["parsed"]["info"]["mint"]
                                break

                    if not mint_address:
                        continue

                    # Get token info
                    token_info = await self.get_token_info(mint_address)
                    if token_info:
                        # Add transaction context
                        token_info.update(
                            {
                                "created_at": tx["blockTime"],
                                "creator": tx["transaction"]["message"]["accountKeys"][
                                    0
                                ],
                                "transaction": sig["signature"],
                                "mint_authority": tx["transaction"]["message"][
                                    "accountKeys"
                                ][0],  # Usually the creator
                            }
                        )

                        # Print immediate feedback
                        print(
                            f"\nFound new token mint: {token_info['symbol']} ({mint_address})"
                        )
                        print(f"Creator: {token_info['creator']}")
                        print(
                            f"Authorities: {json.dumps(token_info['authorities'], indent=2)}"
                        )

                        recent_tokens.append(token_info)
                        if len(recent_tokens) >= limit:
                            return recent_tokens

                except Exception as e:
                    logger.error(
                        f"Error processing transaction {sig['signature']}: {e}"
                    )
                    continue

            return recent_tokens

        except Exception as e:
            logger.error(f"Error getting recent token mints: {e}")
            return []

    async def get_token_info(self, mint_address: str) -> Dict:
        """Get comprehensive token information"""
        try:
            # Get token account info
            account_info = await self._rpc_request(
                "getAccountInfo",
                [mint_address, {"encoding": "jsonParsed", "commitment": "confirmed"}],
            )

            if not account_info or "parsed" not in account_info["data"]:
                return None

            token_data = account_info["data"]["parsed"]["info"]

            # Get metadata if available
            metadata = await self.get_token_metadata(mint_address)

            # Build comprehensive token info
            token_info = {
                "address": mint_address,
                "name": metadata.get("name", ""),
                "symbol": metadata.get("symbol", ""),
                "supply": token_data.get("supply", "0"),
                "decimals": token_data.get("decimals", 0),
                "authorities": {
                    "freeze": token_data.get("freezeAuthority"),
                    "mint": token_data.get("mintAuthority"),
                },
                "is_initialized": token_data.get("isInitialized", False),
            }

            return token_info

        except Exception as e:
            logger.error(f"Error getting token info for {mint_address}: {e}")
            return None

    async def get_token_metadata(self, mint_address: str) -> Dict:
        """Get token metadata using getAccountInfo"""
        try:
            # Get metadata account address using seeds
            metadata_seeds = [
                "metadata",
                "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s",
                mint_address,
            ]
            metadata_address = await self._find_metadata_address(metadata_seeds)

            if not metadata_address:
                return {}

            # Get metadata account info
            metadata = await self._rpc_request(
                "getAccountInfo",
                [metadata_address, {"encoding": "base64", "commitment": "confirmed"}],
            )

            if not metadata or not metadata.get("data", []):
                return {}

            # Parse metadata
            try:
                data = base58.b58decode(metadata["data"][0])
                name_len = int.from_bytes(data[4:8], byteorder="little")
                name = data[8 : 8 + name_len].decode("utf-8")

                symbol_len = int.from_bytes(
                    data[8 + name_len : 12 + name_len], byteorder="little"
                )
                symbol = data[12 + name_len : 12 + name_len + symbol_len].decode(
                    "utf-8"
                )

                return {"name": name, "symbol": symbol}
            except:
                return {}

        except Exception as e:
            logger.error(f"Error getting token metadata for {mint_address}: {e}")
            return {}

    async def _find_metadata_address(self, seeds: List[str]) -> str:
        """Find PDA for metadata account"""
        try:
            seeds_bytes = [s.encode() if isinstance(s, str) else s for s in seeds]
            metadata_program = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"

            # Find PDA
            pda = await self._rpc_request(
                "getProgramAccounts",
                [
                    metadata_program,
                    {
                        "filters": [
                            {
                                "memcmp": {
                                    "offset": 0,
                                    "bytes": base58.b58encode(seeds_bytes[0]).decode(),
                                }
                            }
                        ],
                        "encoding": "base64",
                    },
                ],
            )

            if pda and len(pda) > 0:
                return pda[0]["pubkey"]
            return None

        except Exception as e:
            logger.error(f"Error finding metadata address: {e}")
            return None

    async def get_dex_trades(self, lookback_slots: int = 1000) -> List[Dict]:
        """Get recent DEX trades from Raydium and Orca"""
        try:
            all_trades = []

            # Get Raydium trades (reduced from 50 to 3)
            raydium_sigs = await self._rpc_request(
                "getSignaturesForAddress",
                [RAYDIUM_PROGRAM_ID, {"limit": 3, "commitment": "confirmed"}],
            )

            # Get Orca trades (reduced from 50 to 3)
            orca_sigs = await self._rpc_request(
                "getSignaturesForAddress",
                [ORCA_PROGRAM_ID, {"limit": 3, "commitment": "confirmed"}],
            )

            # Process all signatures
            for sig_list in [raydium_sigs, orca_sigs]:
                if not sig_list:
                    continue

                for sig in sig_list:
                    try:
                        # Get transaction details
                        tx = await self._rpc_request(
                            "getTransaction",
                            [
                                sig["signature"],
                                {
                                    "encoding": "jsonParsed",
                                    "maxSupportedTransactionVersion": 0,
                                },
                            ],
                        )

                        if not tx or not tx.get("meta", {}).get("innerInstructions"):
                            continue

                        # Process transaction
                        trade = await self._process_dex_transaction(tx)
                        if trade:
                            all_trades.append(trade)

                    except Exception as e:
                        logger.error(
                            f"Error processing DEX transaction {sig['signature']}: {e}"
                        )
                        continue

            return all_trades

        except Exception as e:
            logger.error(f"Error getting DEX trades: {e}")
            return []

    async def _process_dex_transaction(self, tx: Dict) -> Dict:
        """Process a DEX transaction to extract trade details"""
        try:
            # Find the DEX instruction
            dex_ix = None
            for ix in tx["transaction"]["message"]["instructions"]:
                if ix["programId"] in [RAYDIUM_PROGRAM_ID, ORCA_PROGRAM_ID]:
                    dex_ix = ix
                    break

            if not dex_ix:
                return None

            # Get token accounts involved in the trade
            token_accounts = []
            for acc in dex_ix["accounts"]:
                token_info = await self.get_token_info(acc)
                if token_info:
                    token_accounts.append(token_info)

            if not token_accounts:
                return None

            return {
                "signature": tx["transaction"]["signatures"][0],
                "block_time": tx["blockTime"],
                "dex": "Raydium"
                if dex_ix["programId"] == RAYDIUM_PROGRAM_ID
                else "Orca",
                "tokens": token_accounts,
                "success": tx["meta"]["status"].get("Ok") is not None,
            }

        except Exception as e:
            logger.error(f"Error processing DEX transaction: {e}")
            return None

    async def calculate_market_cap(self, token_address: str) -> float:
        """Calculate token's market cap using getTokenLargestAccounts"""
        try:
            # Get token supply and largest accounts
            accounts = await self._rpc_request(
                "getTokenLargestAccounts", [token_address, {"commitment": "confirmed"}]
            )

            if not accounts or not accounts.get("value"):
                return 0

            total_supply = sum(float(acc["amount"]) for acc in accounts["value"])
            return total_supply

        except Exception as e:
            logger.error(f"Error calculating market cap for {token_address}: {e}")
            return 0

    async def get_token_accounts(self, mint_address: str = None) -> List[Dict]:
        """Get token accounts using getProgramAccounts with filters"""
        try:
            filters = [
                {"dataSize": 165},  # Token account size
            ]

            if mint_address:
                # Filter for specific mint
                filters.append({"memcmp": {"offset": 0, "bytes": mint_address}})

            accounts = await self._rpc_request(
                "getProgramAccounts",
                [
                    TOKEN_PROGRAM_ID,
                    {
                        "encoding": "base64",
                        "dataSlice": {"offset": 64, "length": 8},
                        "filters": filters,
                    },
                ],
            )

            if not accounts:
                return []

            # Filter out zero balance accounts
            non_zero = []
            for acc in accounts:
                try:
                    data = base58.b58decode(acc["account"]["data"][0])
                    balance = int.from_bytes(data, byteorder="little")
                    if balance > 0:
                        non_zero.append(
                            {
                                "address": acc["pubkey"],
                                "balance": balance,
                                "mint": mint_address,
                            }
                        )
                except Exception as e:
                    logger.error(f"Error processing account {acc['pubkey']}: {e}")
                    continue

            return non_zero

        except Exception as e:
            if "disabled" in str(e).lower():
                logger.warning("getProgramAccounts is disabled on this RPC endpoint")
            else:
                logger.error(f"Error getting token accounts: {e}")
            return []

    async def scan_new_tokens(self, min_holders: int = 10) -> List[Dict]:
        """Scan for new tokens with significant holder count"""
        try:
            # Get all token accounts
            accounts = await self.get_token_accounts()

            if not accounts:
                return []

            # Group by mint address
            mints = {}
            for acc in accounts:
                mint = acc["mint"]
                if mint not in mints:
                    mints[mint] = []
                mints[mint].append(acc)

            # Filter mints by holder count
            interesting_tokens = []
            for mint, holders in mints.items():
                if len(holders) >= min_holders:
                    # Get token info
                    token_info = await self.get_token_info(mint)
                    if token_info:
                        token_info["holder_count"] = len(holders)
                        token_info["total_balance"] = sum(h["balance"] for h in holders)
                        interesting_tokens.append(token_info)
                        print(f"\nFound token with {len(holders)} holders:")
                        print(f"Symbol: {token_info.get('symbol', 'Unknown')}")
                        print(f"Address: {mint}")
                        print(f"Total Balance: {token_info['total_balance']}")

            return interesting_tokens

        except Exception as e:
            logger.error(f"Error scanning new tokens: {e}")
            return []


async def main():
    # Load environment variables
    load_dotenv()

    # Get RPC URL from environment or use default list
    rpc_url = os.getenv("SOLANA_RPC_URL")

    async with SolanaSignal(rpc_url) as signal:
        while True:
            try:
                # Scan for new tokens with at least 5 holders
                tokens = await signal.scan_new_tokens(min_holders=5)

                if tokens:
                    print("\nFound tokens with significant holder count:")
                    for token in tokens:
                        print("\n=================")
                        print(f"Symbol: {token.get('symbol', 'Unknown')}")
                        print(f"Name: {token.get('name', 'Unknown')}")
                        print(f"Address: {token['address']}")
                        print(f"Holder Count: {token['holder_count']}")
                        print(f"Total Supply: {token['supply']}")
                        print(f"Decimals: {token['decimals']}")
                        print("Authorities:")
                        print(f"  Mint: {token['authorities']['mint']}")
                        print(f"  Freeze: {token['authorities']['freeze']}")

                # Wait between scans
                await asyncio.sleep(30)  # Longer delay since we're scanning more data

            except Exception as e:
                logger.error(f"Main loop error: {e}")
                await asyncio.sleep(10)
                continue


if __name__ == "__main__":
    import time

    asyncio.run(main())
