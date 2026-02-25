#!/bin/bash
# ============================================================
#  Homework Tracker — One-Time Setup
#  Double-click this file to set everything up.
#  You only need to run this once!
# ============================================================

# Find the folder this script lives in
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_NAME="com.homeworktracker.checker"
PLIST_FILE="$PLIST_DIR/$PLIST_NAME.plist"

clear
echo "============================================"
echo "     Homework Tracker — Setup"
echo "============================================"
echo ""

# ── Step 1: Find Python ──────────────────────────────────────
echo "Step 1: Looking for Python 3..."

PYTHON_PATH=""
for cmd in /usr/bin/python3 /usr/local/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do
    if [ -x "$cmd" ]; then
        VERSION=$("$cmd" --version 2>&1)
        if [[ $VERSION == Python\ 3* ]]; then
            PYTHON_PATH="$cmd"
            break
        fi
    fi
done

# Fallback: search PATH
if [ -z "$PYTHON_PATH" ]; then
    FOUND=$(command -v python3 2>/dev/null)
    if [ -n "$FOUND" ]; then
        VERSION=$("$FOUND" --version 2>&1)
        if [[ $VERSION == Python\ 3* ]]; then
            PYTHON_PATH="$FOUND"
        fi
    fi
fi

if [ -z "$PYTHON_PATH" ]; then
    echo ""
    echo "  ERROR: Python 3 was not found on this Mac."
    echo ""
    echo "  Please:"
    echo "  1. Go to  https://www.python.org/downloads/"
    echo "  2. Download and install the latest Python 3"
    echo "  3. Run this setup script again"
    echo ""
    read -p "Press Enter to close..."
    exit 1
fi

echo "  Found: $PYTHON_PATH ($VERSION)"
echo ""

# ── Step 2: Create the LaunchAgents folder if needed ────────
echo "Step 2: Preparing notification service folder..."
mkdir -p "$PLIST_DIR"
echo "  Done."
echo ""

# ── Step 3: Write the launchd plist ─────────────────────────
echo "Step 3: Installing daily 7:00 AM reminder service..."

cat > "$PLIST_FILE" << PLIST_CONTENT
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.homeworktracker.checker</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON_PATH</string>
        <string>$SCRIPT_DIR/check_notifications.py</string>
    </array>

    <!-- Run every day at 7:00 AM -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>   <integer>7</integer>
        <key>Minute</key> <integer>0</integer>
    </dict>

    <!-- Also run once when you log in (catches missed days) -->
    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>$SCRIPT_DIR/service_output.log</string>
    <key>StandardErrorPath</key>
    <string>$SCRIPT_DIR/service_error.log</string>
</dict>
</plist>
PLIST_CONTENT

chmod 644 "$PLIST_FILE"
echo "  Service file created."
echo ""

# ── Step 4: Load (activate) the service ─────────────────────
echo "Step 4: Activating the service..."

# Unload first in case it was previously loaded
launchctl unload "$PLIST_FILE" 2>/dev/null

launchctl load "$PLIST_FILE" 2>/dev/null
LOAD_STATUS=$?

if [ $LOAD_STATUS -eq 0 ]; then
    echo "  Service activated! It will run every morning at 7:00 AM."
else
    echo "  Note: Could not activate automatically."
    echo "  Your app will still work; notifications will fire at login."
fi
echo ""

# ── Step 5: Make scripts executable ─────────────────────────
echo "Step 5: Setting file permissions..."
chmod +x "$SCRIPT_DIR/check_notifications.py"
chmod +x "$SCRIPT_DIR/homework_tracker.py"
chmod +x "$SCRIPT_DIR/Open Homework Tracker.command"
echo "  Done."
echo ""

# ── Step 6: Send a test notification ────────────────────────
echo "Step 6: Sending a test notification..."
osascript -e 'display notification "Your homework reminders are now active! You will be notified at 7 AM, 3 days before, 1 day before, and the morning things are due." with title "Homework Tracker is ready!" sound name "Default"'
echo "  Check your top-right corner for a notification banner."
echo ""

# ── Done ─────────────────────────────────────────────────────
echo "============================================"
echo "  Setup complete!"
echo "============================================"
echo ""
echo "  To open the app:"
echo "  Double-click 'Open Homework Tracker.command'"
echo "  in this same folder."
echo ""
echo "  Notifications fire automatically at 7:00 AM:"
echo "    - 3 days before something is due"
echo "    - 1 day before something is due"
echo "    - The morning it is due"
echo ""
read -p "Press Enter to close this window..."
