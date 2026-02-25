#!/usr/bin/env python3
"""
check_notifications.py
----------------------
Runs every morning at 7:00 AM (via macOS launchd).
Checks each assignment and sends a Mac notification banner if:
  - the assignment is due in exactly 3 days
  - the assignment is due tomorrow
  - the assignment is due today

Once a notification has been sent for a specific milestone it is
recorded in assignments.json so it won't fire again.

If bark_key is set in canvas_config.json, the same notifications are
also pushed to the user's iPhone via the Bark app (free, no account).
"""

import json
import os
import subprocess
import urllib.request
import urllib.parse
from datetime import date


SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_FILE   = os.path.join(SCRIPT_DIR, "assignments.json")
CONFIG_FILE = os.path.join(SCRIPT_DIR, "canvas_config.json")


def load_bark_config() -> tuple[str, str]:
    """Return (bark_key, bark_server) from canvas_config.json, or ('', '') if not set."""
    try:
        with open(CONFIG_FILE, "r") as f:
            cfg = json.load(f)
        key    = cfg.get("bark_key", "").strip()
        server = cfg.get("bark_server", "https://api.day.app").rstrip("/")
        return key, server
    except Exception:
        return "", ""


def send_mac_notification(title: str, message: str) -> None:
    """Display a native macOS notification banner with a sound."""
    def escape(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = (
        f'display notification "{escape(message)}" '
        f'with title "{escape(title)}" '
        f'sound name "Default"'
    )
    subprocess.run(["osascript", "-e", script], capture_output=True)


def send_phone_notification(title: str, message: str, bark_key: str, bark_server: str) -> None:
    """Push a notification to iPhone via the Bark app (https://github.com/Finb/Bark).

    Bark delivers through Apple's APNs — the same mechanism all iOS apps use.
    Requires the free Bark app installed and bark_key set in canvas_config.json.
    Fails silently if the network is unavailable or key is invalid.
    """
    encoded_title   = urllib.parse.quote(title, safe="")
    encoded_message = urllib.parse.quote(message, safe="")
    params = urllib.parse.urlencode({"sound": "default", "group": "HomeworkTracker"})
    url = f"{bark_server}/{bark_key}/{encoded_title}/{encoded_message}?{params}"
    try:
        urllib.request.urlopen(url, timeout=10)
    except Exception:
        pass  # Network failure or invalid key — skip silently


def send_notification(title: str, message: str, bark_key: str, bark_server: str) -> None:
    """Send notification to both Mac and iPhone (if Bark is configured)."""
    send_mac_notification(title, message)
    if bark_key:
        send_phone_notification(title, message, bark_key, bark_server)


def run_checks() -> None:
    if not os.path.exists(DATA_FILE):
        return  # No data file yet — app hasn't been used

    bark_key, bark_server = load_bark_config()

    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return  # File is corrupt or unreadable — skip silently

    assignments = data.get("assignments", [])
    today       = date.today()
    changed     = False

    for assignment in assignments:
        try:
            due = date.fromisoformat(assignment["due_date"])
        except (ValueError, KeyError):
            continue  # Skip malformed entries

        days_until = (due - today).days
        title_name = assignment.get("title", "Assignment")
        course     = assignment.get("course", "")
        sent       = assignment.get("notifications_sent", [])

        # Don't notify for past-due assignments
        if days_until < 0:
            continue

        if days_until == 3 and "3_days" not in sent:
            send_notification(
                f"Due in 3 days \u2014 {course}",
                f"{title_name} is due {due.strftime('%A, %B %d')}",
                bark_key, bark_server,
            )
            sent.append("3_days")
            changed = True

        elif days_until == 1 and "1_day" not in sent:
            send_notification(
                f"Due TOMORROW \u2014 {course}",
                f"{title_name} is due tomorrow!",
                bark_key, bark_server,
            )
            sent.append("1_day")
            changed = True

        elif days_until == 0 and "morning" not in sent:
            send_notification(
                f"Due TODAY \u2014 {course}",
                f"{title_name} is due today. Good luck!",
                bark_key, bark_server,
            )
            sent.append("morning")
            changed = True

        assignment["notifications_sent"] = sent

    if changed:
        try:
            with open(DATA_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except IOError:
            pass  # Can't write — skip silently


if __name__ == "__main__":
    run_checks()
