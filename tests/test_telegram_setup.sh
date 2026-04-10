#!/bin/bash
# Quick Telegram setup verification script

set -e

cd "$(dirname "$0")/.."

echo "═══════════════════════════════════════════════════════════"
echo "📱 Telegram Setup Verification"
echo "═══════════════════════════════════════════════════════════"

# Check .env exists
if [ ! -f ".env" ]; then
    echo "❌ .env file not found"
    exit 1
fi

# Extract Telegram config
BOT_TOKEN=$(grep "TELEGRAM_BOT_TOKEN=" .env | cut -d'=' -f2 | xargs)
CHAT_ID=$(grep "TELEGRAM_CHAT_ID=" .env | cut -d'=' -f2 | xargs)

echo ""
echo "📋 Current Configuration:"
echo "  Bot Token: ${BOT_TOKEN:0:20}..."
echo "  Chat ID:   $CHAT_ID"

if [ "$CHAT_ID" = "-" ] || [ -z "$CHAT_ID" ]; then
    echo ""
    echo "⚠️  Chat ID is not set!"
    echo ""
    echo "📝 Steps to fix:"
    echo "  1. Run: python src/tools/get_telegram_chat_id.py '$BOT_TOKEN'"
    echo "  2. Open your Telegram: https://t.me/+_57iSl51lrkyZjA9"
    echo "  3. Send a message in the group"
    echo "  4. Copy the Chat ID from the script output"
    echo "  5. Update .env with: TELEGRAM_CHAT_ID=<your-id>"
    echo ""
    exit 1
fi

echo ""
echo "🧪 Testing message delivery..."
python3 -c "
import sys
sys.path.insert(0, '.')
from src.tools.notification_service import TelegramNotifier

telegram = TelegramNotifier()
msg = telegram.format_summary(
    patch_type='TEST',
    repo='test',
    status='success',
    build_passed=True,
    tests_passed=True,
    notes='Setup verification'
)
print('Sample message to be sent:')
print(msg)
print('')
if telegram.send_message(msg):
    print('✅ Test message sent successfully!')
    print('📱 Check your Telegram: https://t.me/+_57iSl51lrkyZjA9')
else:
    print('❌ Failed to send test message')
    sys.exit(1)
"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "✅ Setup verified! You're ready to run tests with notifications:"
echo ""
echo "   python tests/run_with_telegram_notifications.py"
echo ""
echo "═══════════════════════════════════════════════════════════"
