#!/bin/bash
# ============================================================
#  Homework Tracker — Launcher
#  Double-click this file any time you want to open the app.
# ============================================================

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Find Python 3
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
    osascript -e 'display dialog "Python 3 is not installed.\n\nPlease visit python.org, download Python 3, install it, and try again." buttons {"OK"} default button "OK" with icon caution with title "Homework Tracker"'
    exit 1
fi

# Launch the app (the Terminal window will close once the app opens)
"$PYTHON_PATH" "$SCRIPT_DIR/homework_tracker.py"
