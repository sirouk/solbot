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
    time_differences = []  # Store time differences
    
    for token in data:
        # Check if token meets all our criteria:
        # 1. Has minted_at date
        # 2. No authorities set
        # 3. Empty extensions
        # 4. Within time window
        minted_at = token.get('minted_at')
        created_at = token.get('created_at')
        
        if (minted_at and created_at and
            token.get('freeze_authority') is None and
            token.get('mint_authority') is None and
            token.get('permanent_delegate') is None and
            not token.get('extensions')):  # Empty extensions
            
            try:
                created_time = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                minted_time = datetime.fromisoformat(minted_at.replace('Z', '+00:00'))
                
                # Calculate time difference
                time_diff = (created_time - minted_time).total_seconds()
                time_differences.append(time_diff)
                
                # Only include if minted within our time window
                if minted_time > cutoff_time:
                    recent_tokens.append({
                        'address': token.get('address'),
                        'name': token.get('name', ''),
                        'symbol': token.get('symbol', ''),
                        'decimals': token.get('decimals'),
                        'logoURI': token.get('logoURI'),
                        'tags': token.get('tags', []),
                        'daily_volume': token.get('daily_volume'),
                        'created_at': created_at,
                        'minted_at': minted_at,
                        'time_diff_seconds': time_diff
                    })
            except ValueError as e:
                print(f"Error parsing date for token {token.get('symbol')}: {e}")
                continue
    
    # Sort by created_at date, newest first
    recent_tokens.sort(key=lambda x: x['created_at'], reverse=True)
    
    # Print time difference statistics
    if time_differences:
        avg_diff = sum(time_differences) / len(time_differences)
        print(f"\nAnalysis of minted vs created times:")
        print(f"Number of tokens with both dates: {len(time_differences)}")
        print(f"Average time difference: {avg_diff:.2f} seconds ({avg_diff/60:.2f} minutes)")
        print(f"Min difference: {min(time_differences):.2f} seconds")
        print(f"Max difference: {max(time_differences):.2f} seconds")
    
    return recent_tokens

def format_volume(volume):
    """Format volume with appropriate suffix (K, M, B) or 'No volume' if None or 0"""
    if volume is None or volume == 0:
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
    # verified,unknown,community,strict,lst,birdeye-trending,clone,
    api_url = "https://tokens.jup.ag/tokens?tags=verified,community,strict,lst,birdeye-trending,clone,pump"
    data = fetch_json_data(api_url)
    
    if data:
        print("Successfully fetched data!")
        
        # Print raw data for first token
        print("\nExample of raw token data from API:")
        print(json.dumps(data[0], indent=2))
        
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
            timestamp = datetime.now(pytz.UTC).strftime("%Y%m%d_%H%M%S")
            filename = f"recent_tokens_{timestamp}.json"
            save_json_data(recent_tokens, filename)
        else:
            print(f"\nNo new tokens found in the last {token_age} hour{'s' if token_age != 1 else ''}.")

if __name__ == "__main__":
    main() 