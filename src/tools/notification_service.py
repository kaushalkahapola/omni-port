#!/usr/bin/env python3
"""
Notification service for sending test results to Telegram and Discord.

Configuration:
  - TELEGRAM_BOT_TOKEN: Bot token (from @BotFather)
  - TELEGRAM_CHAT_ID: Chat/Channel ID (use get_chat_id.py to find it)
  - DISCORD_WEBHOOK_URL: Webhook URL (from Server Settings → Integrations)
"""

import os
import json
import requests
from typing import Optional, Dict, Any
from datetime import datetime
from pathlib import Path

# Load .env file if it exists
try:
    from dotenv import load_dotenv
    env_file = Path(__file__).parent.parent.parent / ".env"
    if env_file.exists():
        load_dotenv(env_file)
except ImportError:
    # dotenv not available, fall back to os.getenv only
    pass


class TelegramNotifier:
    """Send formatted test result summaries to Telegram."""

    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"

    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send plain text message to Telegram."""
        if not self.bot_token or not self.chat_id:
            print("⚠️  Telegram credentials not configured. Skipping notification.")
            return False

        try:
            response = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            response.raise_for_status()
            return True
        except Exception as e:
            print(f"❌ Failed to send Telegram message: {e}")
            return False

    def format_summary(
        self,
        patch_type: str,
        repo: str,
        status: str,
        build_passed: bool,
        tests_passed: bool,
        notes: str = "",
    ) -> str:
        """Format a single patch result for Telegram."""
        emoji = "✅" if status == "success" else "❌" if status == "failed" else "⚠️"
        build_emoji = "✅" if build_passed else "❌"
        test_emoji = "✅" if tests_passed else "❌"

        text = f"""
{emoji} <b>Patch Result: {patch_type}</b>
<b>Repo:</b> {repo}
<b>Status:</b> {status.upper()}

<b>Build:</b> {build_emoji}
<b>Tests:</b> {test_emoji}
<b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
        if notes:
            text += f"\n<b>Notes:</b> {notes}\n"

        return text.strip()

    def send_pipeline_summary(self, results: Dict[str, Any]) -> bool:
        """Send formatted pipeline summary."""
        total = len(results.get("patches", []))
        passed = sum(1 for p in results.get("patches", []) if p.get("status") == "success")
        failed = total - passed

        text = f"""
<b>🔄 Pipeline Run Complete</b>

<b>Results:</b>
✅ Passed: {passed}/{total}
❌ Failed: {failed}/{total}
⏱️ Time: {results.get("timestamp", "N/A")}

<b>By Type:</b>
"""

        for patch_type in ["TYPE-I", "TYPE-II", "TYPE-III", "TYPE-IV", "TYPE-V"]:
            patches = [p for p in results.get("patches", []) if patch_type in p.get("type", "")]
            if patches:
                type_passed = sum(1 for p in patches if p.get("status") == "success")
                text += f"\n{patch_type}: {type_passed}/{len(patches)} ✅"

        text += f"\n\n📊 Full results: Check your results folder"

        return self.send_message(text)

    def send_markdown_file(self, file_path: Path) -> bool:
        """Read a markdown file and send its content to Telegram."""
        if not file_path.exists():
            print(f"⚠️  Markdown file not found: {file_path}")
            return False

        try:
            content = file_path.read_text(encoding="utf-8")
            # Telegram has a 4096 character limit for messages
            if len(content) > 4000:
                content = content[:4000] + "\n\n... (truncated)"

            # Use HTML mode as it's more reliable than Markdown for raw text with tables
            # We wrap everything in <pre> to maintain the table formatting from the MD
            # and escape common HTML entities.
            from html import escape
            html_content = f"<pre>{escape(content)}</pre>"

            return self.send_message(html_content, parse_mode="HTML")
        except Exception as e:
            print(f"❌ Failed to send markdown file: {e}")
            return False


class DiscordNotifier:
    """Send formatted test result summaries to Discord."""

    def __init__(self, webhook_url: Optional[str] = None):
        self.webhook_url = webhook_url or os.getenv("DISCORD_WEBHOOK_URL")

    def send_message(self, embed: Dict[str, Any]) -> bool:
        """Send embed message to Discord webhook."""
        if not self.webhook_url:
            print("⚠️  Discord webhook not configured. Skipping notification.")
            return False

        try:
            response = requests.post(
                self.webhook_url,
                json={"embeds": [embed]},
                timeout=10,
            )
            response.raise_for_status()
            return True
        except Exception as e:
            print(f"❌ Failed to send Discord message: {e}")
            return False

    def send_pipeline_summary(self, results: Dict[str, Any]) -> bool:
        """Send formatted pipeline summary."""
        total = len(results.get("patches", []))
        passed = sum(1 for p in results.get("patches", []) if p.get("status") == "success")
        failed = total - passed

        embed = {
            "title": "🔄 Pipeline Run Complete",
            "color": 0x00FF00 if failed == 0 else 0xFF6600,
            "fields": [
                {"name": "Passed", "value": f"✅ {passed}/{total}", "inline": True},
                {"name": "Failed", "value": f"❌ {failed}/{total}", "inline": True},
                {"name": "Timestamp", "value": results.get("timestamp", "N/A"), "inline": False},
            ],
        }

        return self.send_message(embed)


def send_test_notification(
    patch_type: str,
    repo: str,
    status: str,
    build_passed: bool,
    tests_passed: bool,
    notes: str = "",
    use_telegram: bool = True,
    use_discord: bool = False,
) -> bool:
    """Send a single patch test notification to configured services."""
    success = True

    if use_telegram:
        telegram = TelegramNotifier()
        msg = telegram.format_summary(patch_type, repo, status, build_passed, tests_passed, notes)
        if not telegram.send_message(msg):
            success = False

    if use_discord:
        # TODO: Implement Discord embed formatting for single patch
        pass

    return success


def send_pipeline_summary_notification(
    results: Dict[str, Any],
    use_telegram: bool = True,
    use_discord: bool = False,
) -> bool:
    """Send pipeline summary notification to configured services."""
    success = True

    if use_telegram:
        telegram = TelegramNotifier()
        if not telegram.send_pipeline_summary(results):
            success = False

    if use_discord:
        discord = DiscordNotifier()
        if not discord.send_pipeline_summary(results):
            success = False

    return success


if __name__ == "__main__":
    # Test: Send a sample message
    telegram = TelegramNotifier()
    test_msg = telegram.format_summary(
        patch_type="TYPE-I",
        repo="elasticsearch",
        status="success",
        build_passed=True,
        tests_passed=True,
        notes="All tests passed!",
    )
    print("Sample message:")
    print(test_msg)
    print("\nSending to Telegram...")
    if telegram.send_message(test_msg):
        print("✅ Message sent successfully!")
    else:
        print("❌ Failed to send message")
