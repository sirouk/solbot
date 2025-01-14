import requests
import json
from datetime import datetime, timedelta
import pytz
import argparse

def fetch_json_data(url):
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"Error fetching data: {e}")
        return None

def filter_recent_tokens(data, hours=1):
    # Use UTC for consistency
    current_time = datetime.now(pytz.UTC)
    cutoff_time = current_time - timedelta(hours=hours)
    
    recent_tokens = []
    for token in data:
        # Check if the token has a 'created_at' field
        created_at = token.get('created_at')
        if created_at:
            try:
                # Parse the ISO format datetime string (already UTC)
                listed_time = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                if listed_time > cutoff_time:
                    # Handle daily volume that might be None
                    daily_volume = token.get('daily_volume')
                    if daily_volume is None:
                        daily_volume = 0
                    
                    recent_tokens.append({
                        'address': token.get('address', 'N/A'),
                        'symbol': token.get('symbol', 'N/A'),
                        'name': token.get('name', 'N/A'),
                        'created_at': listed_time.strftime('%Y-%m-%d %H:%M:%S UTC'),
                        'daily_volume': daily_volume
                    })
            except ValueError as e:
                print(f"Error parsing date for token {token.get('symbol')}: {e}")
                continue
    
    # Sort by created_at date, newest first
    recent_tokens.sort(key=lambda x: x['created_at'], reverse=True)
    return recent_tokens

def format_volume(volume):
    """Format volume with appropriate suffix (K, M, B) or 'No volume' if 0"""
    if volume == 0:
        return "No volume data"
    elif volume < 1000:
        return f"${volume:.2f}"
    elif volume < 1000000:
        return f"${volume/1000:.2f}K"
    elif volume < 1000000000:
        return f"${volume/1000000:.2f}M"
    else:
        return f"${volume/1000000000:.2f}B"

def save_json_data(data, filename):
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"Data successfully saved to {filename}")
    except Exception as e:
        print(f"Error saving data: {e}")

def parse_args():
    parser = argparse.ArgumentParser(description='Fetch and filter recent Jupiter tokens')
    parser.add_argument('--auto', action='store_true', help='Use default age (1 hour) without prompting')
    parser.add_argument('--age', type=float, help='Age in hours to filter tokens')
    return parser.parse_args()

def main():
    args = parse_args()
    
    # Determine token age
    token_age = args.age
    if not args.auto and token_age is None:
        while True:
            try:
                age_input = input("Enter the maximum age of tokens to fetch (in hours, default 1): ").strip()
                if not age_input:
                    token_age = 1
                    break
                token_age = float(age_input)
                if token_age <= 0:
                    print("Please enter a positive number")
                    continue
                break
            except ValueError:
                print("Please enter a valid number")
    elif token_age is None:
        token_age = 1

    api_url = "https://tokens.jup.ag/tokens?tags=verified"
    data = fetch_json_data(api_url)
    
    if data:
        print("Successfully fetched data!")
        recent_tokens = filter_recent_tokens(data, hours=token_age)
        
        if recent_tokens:
            print(f"\nFound {len(recent_tokens)} tokens listed in the last {token_age} hour{'s' if token_age != 1 else ''}:")
            for token in recent_tokens:
                print(f"\nSymbol: {token['symbol']}")
                print(f"Name: {token['name']}")
                print(f"Address: {token['address']}")
                print(f"Created at: {token['created_at']}")
                print(f"Daily Volume: {format_volume(token['daily_volume'])}")
            
            # Save to file
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"recent_tokens_{timestamp}.json"
            save_json_data(recent_tokens, filename)
        else:
            print(f"\nNo new tokens found in the last {token_age} hour{'s' if token_age != 1 else ''}.")

if __name__ == "__main__":
    main() 