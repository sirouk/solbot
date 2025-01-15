from datetime import datetime
import zoneinfo

def check_timestamp(timestamp_str):
    """Check if a timestamp is in the future and provide analysis"""
    try:
        # Parse the input timestamp
        token_date = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
        current_time = datetime.now(zoneinfo.ZoneInfo('UTC'))

        print("Token Validation:")
        print("-" * 50)
        print(f"Input timestamp: {timestamp_str}")
        print(f"Parsed date (UTC): {token_date}")
        print(f"Current time (UTC): {current_time}")
        print(f"Time until/since: {token_date - current_time}")
        print(f"Is in future? {token_date > current_time}")

    except ValueError as e:
        print(f"Error parsing timestamp: {e}")
        print("Please provide timestamp in format: YYYY-MM-DDTHH:MM:SS.uuuuuuZ")

if __name__ == "__main__":
    timestamp = input("Enter timestamp to check (e.g., 2025-01-15T01:30:01.503676Z): ").strip()
    check_timestamp(timestamp) 