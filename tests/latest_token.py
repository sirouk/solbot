import requests
import json
from datetime import datetime
import pytz

def fetch_json_data(url):
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"Error fetching data: {e}")
        return None

def get_latest_token(data):
    latest_token = None
    latest_time = None
    
    for token in data:
        minted_at = token.get('minted_at')
        created_at = token.get('created_at')
        
        if (minted_at and
            token.get('freeze_authority') is None and
            token.get('mint_authority') is None and
            token.get('permanent_delegate') is None):
            
            try:
                minted_at = datetime.fromisoformat(minted_at.replace('Z', '+00:00'))
                
                if latest_time is None or minted_at > latest_time:
                    latest_time = minted_at
                    latest_token = token
            except ValueError:
                continue
    
    return latest_token

def print_token_details(token):
    if not token:
        print("No token found matching the criteria.")
        return
        
    print("\nLatest Token Details:")
    print(json.dumps(token, indent=2))
    # Current time in UTC
    print(f"Current time in UTC: {datetime.now(pytz.UTC)}")

def main():
    api_url = "https://tokens.jup.ag/tokens?tags=verified,community,strict,lst,birdeye-trending,clone,pump"
    data = fetch_json_data(api_url)
    
    if data:
        print("Successfully fetched data!")
        latest_token = get_latest_token(data)
        print_token_details(latest_token)

if __name__ == "__main__":
    main() 