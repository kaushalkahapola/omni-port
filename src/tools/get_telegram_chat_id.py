#!/usr/bin/env python3
"""
Helper script to find your Telegram chat ID for the notification service.

Usage:
  python src/tools/get_telegram_chat_id.py <BOT_TOKEN>

The script will:
1. Start a bot listener
2. Tell you to send a message to your bot
3. Print out your chat ID
4. You can then add it to .env
"""

import sys
import requests
import time
import os
from typing import Optional
from pathlib import Path

# Load .env file if it exists
try:
    from dotenv import load_dotenv
    env_file = Path(__file__).parent.parent.parent / ".env"
    if env_file.exists():
        load_dotenv(env_file)
except ImportError:
    pass


def get_updates(bot_token: str, offset: int = 0) -> dict:
    """Get updates from Telegram bot."""
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    try:
        response = requests.get(url, params={"offset": offset}, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"❌ Failed to get updates: {e}")
        return {"ok": False, "result": []}


def find_chat_id(bot_token: str) -> Optional[int]:
    """Listen for messages and extract chat ID."""
    print("\n📱 Telegram Chat ID Finder")
    print("=" * 50)
    print(f"\n✅ Bot token is valid. Listening for messages...\n")
    print("📢 Steps:")
    print("1. Open your Telegram group/channel")
    print("2. Send a message: /start or just type anything")
    print("3. This script will capture your chat ID\n")
    print("Waiting for message (timeout: 60 seconds)...\n")

    offset = 0
    start_time = time.time()
    timeout = 60

    while time.time() - start_time < timeout:
        try:
            updates = get_updates(bot_token, offset)

            if updates.get("ok") and updates.get("result"):
                for update in updates["result"]:
                    message = update.get("message", {})
                    chat = message.get("chat", {})
                    chat_id = chat.get("id")
                    chat_type = chat.get("type")
                    chat_title = chat.get("title") or chat.get("username") or "Unknown"

                    if chat_id:
                        print(f"\n✅ Found chat!")
                        print(f"   Chat ID: {chat_id}")
                        print(f"   Type: {chat_type}")
                        print(f"   Name: {chat_title}")
                        print(f"\n📝 Add this to your .env file:")
                        print(f"   TELEGRAM_CHAT_ID={chat_id}")
                        return chat_id

                    offset = update["update_id"] + 1

            time.sleep(1)

        except Exception as e:
            print(f"⚠️  Error: {e}")
            time.sleep(1)

    print("❌ Timeout: No message received")
    return None


def main():
    bot_token = sys.argv[1] if len(sys.argv) > 1 else os.getenv("TELEGRAM_BOT_TOKEN")

    if not bot_token:
        print("Usage: python src/tools/get_telegram_chat_id.py <BOT_TOKEN>")
        print("Alternatively, set TELEGRAM_BOT_TOKEN in your .env file")
        sys.exit(1)

    # Verify token
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{bot_token}/getMe", timeout=5
        )
        if not response.ok:
            print(f"❌ Invalid bot token")
            sys.exit(1)
    except Exception as e:
        print(f"❌ Failed to verify token: {e}")
        sys.exit(1)

    find_chat_id(bot_token)


if __name__ == "__main__":
    main()
