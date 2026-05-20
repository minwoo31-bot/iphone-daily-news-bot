#!/usr/bin/env python3
"""
Dry-run test script for daily_news_bot
Fetches real news, shortens links, and prints the exact message preview to the console.
"""

import sys
import os

# Set mock environment variables to bypass required checks
os.environ["TELEGRAM_BOT_TOKEN"] = "MOCK_TELEGRAM_BOT_TOKEN"
os.environ["TELEGRAM_CHAT_ID"] = "MOCK_TELEGRAM_CHAT_ID"

# If you have a real GEMINI_API_KEY, you can uncomment the line below or set it in your environment:
# os.environ["GEMINI_API_KEY"] = "YOUR_REAL_API_KEY"

# Import the news bot module
try:
    import daily_news_bot
except ImportError as e:
    print(f"Error importing daily_news_bot: {e}")
    sys.exit(1)

# Intercept the telegram sending function to print instead of making API calls
def mock_telegram_send(token: str, chat_id: str, text: str) -> None:
    print("\n" + "[TELEGRAM MESSAGE PREVIEW]")
    print(text)
    print("=" * 78 + "\n")

# Apply the mock
daily_news_bot.telegram_send = mock_telegram_send

# Let's adjust settings to fetch fewer items for a quick test run (e.g., 3 items instead of 10)
os.environ["MAX_NEWS"] = "3"
os.environ["MAX_SPORTS"] = "2"
os.environ["MAX_ENTERTAINMENT"] = "2"
os.environ["MAX_NARA"] = "2"

if __name__ == "__main__":
    print("Starting real-world dry-run test...")
    print("- Fetching actual RSS feeds...")
    print("- Shortening links with new ad-free providers (da.gd / cleanuri)...")
    
    if not os.getenv("GEMINI_API_KEY"):
        print("- [NOTE] GEMINI_API_KEY is not set. Testing URL shorteners and title-only digest.")
    else:
        print("- [NOTE] GEMINI_API_KEY is set. Attempting live Gemini summarization.")
        
    try:
        exit_code = daily_news_bot.main()
        print(f"Dry run finished successfully (Exit Code: {exit_code}).")
    except Exception as e:
        print(f"\n[ERROR] Execution failed: {e}", file=sys.stderr)
        sys.exit(1)
