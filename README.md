# Homework Tracker

A personal assignment tracking web app built to run entirely on a Mac as a local server. Built by a nursing student learning software development with AI assistance.

## What It Does

Opens in your browser at `http://localhost:8765` and gives you a clean, real-time view of all your upcoming assignments — both manually added and automatically synced from Canvas LMS. Color-coded urgency indicators, push notifications to your iPhone, and macOS banner alerts keep deadlines visible without having to log into Canvas.

## Features

- **Canvas LMS auto-sync** — pulls all upcoming assignments directly from your Canvas account via the Canvas REST API; runs on launch and on demand
- **Color-coded urgency** — overdue (red), due today (yellow), due within 3 days (orange), upcoming (green)
- **Manual assignment entry** — add assignments that aren't on Canvas via a clean modal dialog
- **Complete/incomplete toggle** — mark assignments done; completed assignments collapse into a separate section
- **Delete assignments** — remove any assignment with a confirmation prompt
- **macOS notification banners** — fires at 3 days out, 1 day out, and the morning each assignment is due via `osascript`
- **iPhone push notifications** — same three milestones delivered to your iPhone via [Bark](https://bark.day.app)
- **Smart deduplication** — notifications never fire twice for the same milestone; Canvas assignments are never duplicated on re-sync
- **Auto-refresh** — assignment list refreshes every 60 seconds; sync status polls every 5 minutes
- **Past-due cleanup** — unsubmitted past-due Canvas assignments are automatically removed on sync
- **Auto-completion** — assignments submitted or graded on Canvas are automatically marked complete
- **CANVAS badge** — Canvas-synced assignments are visually labeled so you always know the source
- **Keyboard shortcuts** — Escape closes modal, Enter saves

## Tech Stack

- **Python 3 (stdlib only)** — no pip, no virtual environment, no third-party packages
- **Web server** — `http.server.HTTPServer` + `BaseHTTPRequestHandler`
- **Frontend** — vanilla HTML, CSS, and JavaScript embedded directly in the Python file; no frameworks, no build step
- **Data storage** — flat JSON file (`assignments.json`); no database required
- **Canvas integration** — Canvas LMS REST API v1 with a personal access token
- **Notifications** — macOS `osascript` for banner alerts; `urllib` HTTP requests to `api.day.app` for iPhone push via Bark
- **Background service** — macOS launchd (`~/Library/LaunchAgents/`) runs the notification checker daily at 7:00 AM

## Project Files

| File | Purpose |
|------|---------|
| `homework_tracker.py` | Main app — HTTP server, REST API, and entire frontend |
| `canvas_sync.py` | Canvas API client — fetches and merges assignments |
| `check_notifications.py` | Daily notification checker run by launchd |
| `1. Run Setup First.command` | One-time setup — registers launchd service, sets permissions |
| `Open Homework Tracker.command` | Daily launcher — double-click to open the app |

## Setup

1. Clone or download this repository into a local folder on your Mac.
2. Create `canvas_config.json` in the project folder (this file is gitignored and must never be committed):
   ```json
   {
     "canvas_url": "https://your-school.instructure.com",
     "token": "<your Canvas personal access token>",
     "bark_key": "<your Bark device key — optional>"
   }
   ```
   Get a Canvas token at: **Canvas → Account → Settings → Approved Integrations → New Access Token**

3. Double-click **`1. Run Setup First.command`** to register the daily notification service.
4. Double-click **`Open Homework Tracker.command`** to launch the app. It opens in your default browser at `http://localhost:8765`.

## Requirements

- macOS (tested on macOS 26 / Darwin 25.x)
- Python 3.9+ (ships with macOS — no install needed)
- A Canvas LMS account with API access
- Bark app on iPhone (optional, for push notifications)

## About

Built for personal use by a nursing student at Palm Beach State College. The entire project was developed using AI-assisted development (Claude by Anthropic) as a learning exercise in software engineering, API integration, and macOS system programming. Zero prior professional software development background.
