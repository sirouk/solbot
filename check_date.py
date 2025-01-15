from datetime import datetime, timedelta
import zoneinfo

def check_timestamp(timestamp_str, hours=3):
    """Check if a timestamp is in the future and provide analysis"""
    try:
        # Parse the input timestamp
        token_date = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
        current_time = datetime.now(zoneinfo.ZoneInfo('UTC'))
        cutoff_time = current_time - timedelta(hours=hours)

        print("Token Validation:")
        print("-" * 50)
        print(f"Input timestamp: {timestamp_str}")
        print(f"Parsed date (UTC): {token_date}")
        print(f"Current time (UTC): {current_time}")
        print(f"Cutoff time (UTC): {cutoff_time}")
        print(f"Time until/since: {token_date - current_time}")
        print(f"Is in future? {token_date > current_time}")
        print(f"Within last {hours} hours? {token_date > cutoff_time}")

    except ValueError as e:
        print(f"Error parsing timestamp: {e}")
        print("Please provide timestamp in format: YYYY-MM-DDTHH:MM:SS.uuuuuuZ")

if __name__ == "__main__":
    timestamp = input("Enter timestamp to check (e.g., 2025-01-15T01:30:01.503676Z): ").strip()
    hours = float(input("Enter hours to check against (default 3): ") or "3")
    check_timestamp(timestamp, hours) 