import requests
import time
from datetime import datetime
import json
import os
import dotenv

dotenv.load_dotenv()


class SolanaTokenMonitor:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://pro-api.solscan.io/v2.0"
        self.headers = {"token": api_key, "Accept": "application/json"}

    def get_latest_tokens(self, page_size=100):
        """Fetch the latest minted tokens"""
        url = f"{self.base_url}/token/list"
        params = {
            "sort_by": "created_time",
            "sort_order": "desc",
            "page": 1,
            "page_size": page_size,
        }

        response = requests.get(url, headers=self.headers, params=params)
        if response.status_code == 200:
            return response.json()["data"]
        else:
            print(f"Error fetching token list: {response.status_code}")
            return None

    def get_token_metadata(self, token_address):
        """Fetch metadata for a specific token"""
        url = f"{self.base_url}/token/meta"
        params = {"address": token_address}

        response = requests.get(url, headers=self.headers, params=params)
        if response.status_code == 200:
            return response.json()["data"]
        else:
            print(f"Error fetching token metadata: {response.status_code}")
            return None

    def get_token_authorities(self, token_address):
        """Fetch token accounts to determine authorities"""
        url = f"{self.base_url}/account/token-accounts"
        params = {"address": token_address}

        response = requests.get(url, headers=self.headers, params=params)
        if response.status_code == 200:
            return response.json()["data"]
        else:
            print(f"Error fetching token authorities: {response.status_code}")
            return None

    def analyze_token(self, token_address):
        """Analyze a specific token's details"""
        metadata = self.get_token_metadata(token_address)
        authorities = self.get_token_authorities(token_address)

        if metadata and authorities:
            analysis = {
                "token_name": metadata.get("name"),
                "token_symbol": metadata.get("symbol"),
                "creator": metadata.get("creator"),
                "creation_time": datetime.fromtimestamp(
                    metadata.get("created_time", 0)
                ),
                "total_supply": metadata.get("supply"),
                "decimals": metadata.get("decimals"),
                "authorities": [],
            }

            for account in authorities:
                analysis["authorities"].append(
                    {
                        "owner": account.get("owner"),
                        "amount": account.get("amount"),
                        "token_account": account.get("token_account"),
                    }
                )

            return analysis
        return None


def main():
    # Replace with your API key
    api_key = f"{os.getenv('SOLSCAN_API_KEY')}"
    monitor = SolanaTokenMonitor(api_key)

    # Get latest tokens
    latest_tokens = monitor.get_latest_tokens(1)
    if latest_tokens and len(latest_tokens) > 0:
        latest_token = latest_tokens[0]
        print(
            f"\nAnalyzing latest token: {latest_token['name']} ({latest_token['symbol']})"
        )

        # Analyze the token
        analysis = monitor.analyze_token(latest_token["address"])
        if analysis:
            print("\nToken Analysis:")
            print(f"Name: {analysis['token_name']}")
            print(f"Symbol: {analysis['token_symbol']}")
            print(f"Creator: {analysis['creator']}")
            print(f"Created: {analysis['creation_time']}")
            print(f"Total Supply: {analysis['total_supply']}")
            print(f"Decimals: {analysis['decimals']}")
            print("\nAuthorities:")
            for auth in analysis["authorities"]:
                print(f"Owner: {auth['owner']}")
                print(f"Amount: {auth['amount']}")
                print(f"Token Account: {auth['token_account']}")
                print("---")


if __name__ == "__main__":
    main()
