# Homework Tracker — Complete Project Context

## What the App Does

A personal homework/assignment tracking web app that runs entirely on the user's Mac as a local server. It opens in the default browser at `http://localhost:8765`. The user can manually add assignments, and the app also auto-syncs assignments directly from Canvas LMS. It shows color-coded urgency (overdue, due today, due in 3 days, upcoming), lets the user mark assignments complete, and sends both native macOS notification banners and iPhone push notifications via Bark at 7:00 AM — 3 days before, 1 day before, and the morning each assignment is due. No internet hosting, no database, no npm, no pip — everything runs on the Python that ships with macOS.

## Who It's For

The user is a student at **Palm Beach State College** (Canvas instance: `https://palmbeachstate.instructure.com`). They are currently enrolled in a nursing/science curriculum (Spring 2026 semester). Current active courses visible in `assignments.json`:
- Hybrid - Anatomy and Physiology 2 Lab (BSC2086L-42)
- Online - Anatomy and Physiology 2 (BSC2086-49)
- Online - Elements of Nutrition (HUN1201-32)
- Online - Human Growth and Development (DEP2004-49)
- Online - Introduction to Psychology (PSY2012-45)
- Online - Microbiology (MCB2010-19)

---

## Version Control

- **Git:** Repository is initialized. Remote is at `https://github.com/BernsleyB/CourseCore`.
- **Gitignored files:** `canvas_config.json` and `assignments.json` are excluded from version control for security. `canvas_config.json` contains a live Canvas API token and Bark device key; `assignments.json` contains personal academic data. Never commit either file.

---

## Every File and What It Does

### `homework_tracker.py`
The main application. Starts a Python `http.server.HTTPServer` on port 8765. Serves:
- The entire frontend (HTML/CSS/JS) as a single embedded string (`HTML_PAGE`) via GET `/`
- REST API endpoints:
  - `GET /api/assignments` — returns all assignments from `assignments.json`
  - `POST /api/assignments` — adds a new manually-created assignment (generates a UUID, stores title/course/due_date/completed/notifications_sent)
  - `PATCH /api/assignments/<id>` — toggles the `completed` field
  - `DELETE /api/assignments/<id>` — removes an assignment
  - `POST /api/sync` — triggers a Canvas sync in a background thread
  - `GET /api/sync-status` — returns the current sync state (running, last_sync time, result summary, error)

The frontend is fully self-contained inside the Python file — no separate HTML/CSS/JS files exist. On launch, the app checks if port 8765 is already in use; if so, it just opens the browser without starting a second server. It also auto-triggers a Canvas sync in a daemon thread immediately on startup.

The UI features:
- Sticky blue topbar with "Sync Canvas" and "+ Add Assignment" buttons
- Color-coded legend (red=overdue, yellow=today, orange=within 3 days, green=upcoming, gray=completed)
- Sync status bar below the legend showing last sync time and result summary
- Sortable assignment list (sorted by due date ascending)
- Active assignments and a collapsible "Completed" section
- Add assignment modal with title, course, and date dropdowns (month/day/year)
- Per-row circle complete button and X delete button
- "CANVAS" badge on Canvas-synced assignments
- Auto-refreshes assignment list every 60 seconds; polls Canvas sync status every 5 minutes

### `canvas_sync.py`
Handles all communication with the Canvas REST API. Uses only stdlib (`urllib`, `json`, `datetime`). No pip packages required.

Logic:
1. Reads `canvas_config.json` for the Canvas base URL and Bearer token
2. Fetches all active student course enrollments (paginated via Link header)
3. For each course, fetches `bucket=upcoming` assignments (not yet submitted, not past due)
4. For each course, fetches `bucket=submitted` and `bucket=graded` assignments to detect completions
5. Converts Canvas UTC timestamps to local timezone using `datetime.astimezone()`
6. Merges into `assignments.json`:
   - Canvas assignments are keyed by `canvas_id` — never duplicated
   - Manually-added assignments (no `canvas_id`) are never touched by sync
   - Submitted/graded assignments are auto-marked `completed: true`
   - Assignments that disappeared from Canvas (past-due, not submitted) are removed
   - `notifications_sent` and `completed` fields are preserved when a Canvas row already exists

Returns a summary dict: `{ok, added, updated, removed, auto_completed, total_canvas, courses}`

Can also be run standalone: `python3 canvas_sync.py`

### `check_notifications.py`
Runs as a background macOS service via launchd. Checks `assignments.json` daily and sends both native macOS notification banners (via `osascript`) and iPhone push notifications (via Bark). Fires at:
- Exactly 3 days before due → "Due in 3 days — {Course}" / "{title} is due {weekday, Month Day}"
- Exactly 1 day before due → "Due TOMORROW — {Course}"
- The morning of due date → "Due TODAY — {Course}"

Each milestone is recorded in `notifications_sent` on the assignment object so it never fires twice. Skips past-due and completed assignments (though doesn't explicitly check `completed` — only skips `days_until < 0`).

**Bark integration:** If `bark_key` is present in `canvas_config.json`, each notification is also sent to the user's iPhone via the Bark app. Bark uses a **path-based URL format** — `https://api.day.app/{key}/{title}/{body}` — NOT query parameters. This is critical: using query parameters (`?title=...&body=...`) does not work correctly with Bark. The URL components are percent-encoded using `urllib.parse.quote`.

### `canvas_config.json`
Stores the Canvas base URL, API access token, and Bark push notification key. Format:
```json
{
  "canvas_url": "https://palmbeachstate.instructure.com",
  "token": "<Canvas API token here>",
  "bark_key": "<Bark device key here>"
}
```
**Security note:** This file contains a live Canvas API token and Bark device key. Do not commit it, do not display its contents in responses. The token grants read access to the student's Canvas account. The `bark_key` is optional — if absent, Bark notifications are silently skipped.

### `assignments.json`
The app's flat-file database. Structure:
```json
{
  "assignments": [
    {
      "id": "<uuid>",
      "canvas_id": 12345678,        // present only on Canvas-synced rows
      "title": "Assignment Name",
      "course": "Course Name",
      "due_date": "YYYY-MM-DD",
      "notifications_sent": [],     // e.g. ["3_days", "1_day", "morning"]
      "source": "canvas",           // present only on Canvas-synced rows
      "completed": false
    }
  ]
}
```
Manually-added assignments have `id`, `title`, `course`, `due_date`, `notifications_sent`, `completed`. No `canvas_id` or `source` field.

### `1. Run Setup First.command`
A double-clickable bash script for one-time setup. Runs in Terminal when the user double-clicks it in Finder. Does:
1. Finds Python 3 (checks `/usr/bin/python3`, `/usr/local/bin/python3`, fallback to `$PATH`)
2. Creates `~/Library/LaunchAgents/` if needed
3. Writes a launchd `.plist` file (`com.homeworktracker.checker.plist`) to `~/Library/LaunchAgents/`
4. Loads the plist with `launchctl load` (registers the daily 7 AM notification job)
5. Sets executable permissions on all scripts
6. Sends a test macOS notification confirming setup

### `Open Homework Tracker.command`
The daily-use double-clickable launcher. Finds Python 3 and runs `homework_tracker.py`. If Python isn't found, shows a dialog via `osascript`.

### `service_output.log` / `service_error.log`
Log files written by the launchd service (stdout/stderr of `check_notifications.py`). Currently empty (service running cleanly). These are in the app folder — useful for debugging notification issues.

---

## Tech Stack

- **Language:** Python 3 (stdlib only — no pip, no virtual env, no dependencies)
- **Web server:** `http.server.HTTPServer` + `BaseHTTPRequestHandler`
- **Frontend:** Vanilla HTML/CSS/JS, embedded as a Python string in `homework_tracker.py`
- **Data storage:** `assignments.json` (flat JSON file, no database)
- **Canvas integration:** Canvas LMS REST API v1, authenticated with a Bearer token
- **Notifications:** macOS `osascript` via `subprocess.run(["osascript", "-e", ...])` + iPhone push via Bark (`urllib` HTTP request to `api.day.app`)
- **Background service:** macOS launchd (`~/Library/LaunchAgents/com.homeworktracker.checker.plist`)
- **Design system:** Custom CSS using Apple's system font stack (`-apple-system, BlinkMacSystemFont, 'SF Pro Text'`), Tailwind-inspired color palette (blue-700 for the header: `#1e40af`)

---

## Current Features (Built and Working)

1. **Manual assignment creation** — modal with title, course, and date dropdowns (no `<input type="date">` to ensure cross-browser/macOS compatibility)
2. **Canvas auto-sync** — syncs on app launch + manual "Sync Canvas" button
3. **Sync status bar** — shows "Syncing…", last sync time, summary (X assignments across Y courses, Z auto-completed)
4. **Color-coded urgency** — overdue (red), today (yellow), soon/within 3 days (orange), upcoming (green)
5. **Status labels** — "Overdue by N days", "Due TODAY!", "Due tomorrow", "N days left"
6. **Complete/incomplete toggle** — circle button per row; completed rows move to collapsed section
7. **Collapsible completed section** — count badge, collapse/expand arrow
8. **Delete assignments** — X button with confirmation dialog
9. **Canvas source badge** — blue "CANVAS" badge on synced rows
10. **Duplicate prevention** — Canvas assignments keyed by `canvas_id`, never duplicated on re-sync
11. **Auto-completion from Canvas** — submitted/graded assignments auto-marked complete on sync
12. **Past-due cleanup** — unsubmitted past-due Canvas assignments removed on sync
13. **macOS notifications** — daily 7 AM banners for 3 days out, 1 day out, and day-of
14. **Smart deduplication of notifications** — `notifications_sent` array prevents re-firing
15. **Server port check** — prevents duplicate servers; reopens browser if already running
16. **Auto-refresh** — assignment list refreshes every 60s; Canvas status checked every 5 min
17. **Keyboard shortcuts** — Escape closes modal; Enter saves when modal is open
18. **Past-date warning** — alerts user if they try to add an assignment with a past due date
19. **One-time setup script** — registers launchd service, sets permissions, sends test notification
20. **Double-click launcher** — `Open Homework Tracker.command` for daily use
21. **Tabbed interface** — tabs to switch between views/sections within the app
22. **Mark complete feature** — ability to mark assignments as complete with visual feedback
23. **iPhone push notifications via Bark** — sends push notifications to iPhone at the same 3-day, 1-day, and day-of milestones as macOS banners; uses path-based URL format (`api.day.app/{key}/{title}/{body}`); Bark key stored in `canvas_config.json`; gracefully skipped if key is absent

---

## Planned Features (In Priority Order)

1. **Edit existing assignments** — clicking a row should open a pre-filled modal to edit title, course, or due date. Canvas-synced rows might be read-only or warn that changes will be overwritten on next sync.
2. **Due time support** — Canvas assignments have specific due times (e.g., 11:59 PM). Store and display `due_time` alongside `due_date`. Show time in the Due Date column.
3. **Filter / search bar** — filter by course name or search by assignment title. Particularly useful as Canvas imports many assignments.
4. **Course color coding** — assign a color to each course name (auto-generated or user-selectable) so rows are visually grouped by course.
5. **Notes/details field** — an optional free-text notes field per assignment for storing info like "chapter 4 only" or a link to the assignment.
6. **Priority levels** — Low/Medium/High priority tag per assignment, with its own sort weight.
7. **Scheduled Canvas auto-sync** — re-sync every X hours automatically (not just on app launch and manual button), so new assignments appear without user action.
8. **Notification time customization** — let user configure what time of day notifications fire and which milestones (3 days, 1 day, day-of) to enable.

---

## Design Preferences

- **Aesthetic:** Clean, professional, macOS-native feel. Not toy-like. Inspired by Apple's design language.
- **Colors:** Primary blue `#1e40af` (Tailwind blue-700). Use the Tailwind color palette scale as a reference.
- **Font:** Always use `-apple-system, BlinkMacSystemFont, 'SF Pro Text', system-ui, sans-serif`. Never specify a third-party font.
- **No dependencies:** The entire project must run with zero installs. No pip, no npm, no Homebrew required. If a feature needs a library, it must be in Python's stdlib or implementable in vanilla JS.
- **Single-file frontend:** The HTML/CSS/JS lives entirely inside `homework_tracker.py` as the `HTML_PAGE` string. Do not split it into separate files.
- **Compact, dense layout:** Information-dense rows, not lots of whitespace. More like a spreadsheet than a card grid.
- **No extra chrome:** No sidebars, no hamburger menus, no settings pages unless truly necessary.
- **Modal for input:** Keep the add/edit flow in a modal overlay, not a separate page.
- **Subtle interactions:** Hover effects using `filter: brightness()` or light background changes. Smooth CSS transitions (0.15s). No jarring animations except the modal pop-in.

---

## Important Technical Notes

### Python Version Compatibility

- **Minimum required: Python 3.9**
- `canvas_sync.py` uses `dict[int, str]` and similar built-in generic type annotations (PEP 585), which require Python 3.9+. Do not use `typing.Dict`, `typing.List`, etc. — the modern syntax is intentional and correct for 3.9+.
- `datetime.fromisoformat()` with timezone offsets works in Python 3.7+. The `.replace("Z", "+00:00")` workaround in `canvas_sync.py:_parse_due_date()` handles Canvas's UTC "Z" suffix, which `fromisoformat()` only natively supports starting in Python 3.11. The workaround is correct and must be kept.
- The Mac ships with Python 3.9 at `/usr/bin/python3`. Do not assume a newer version is present.
- Never use `match`/`case` statements (Python 3.10+) or `str.removeprefix`/`str.removesuffix` (Python 3.9 — actually these are fine). Be conservative.

### macOS 26 (Darwin 25.x) Compatibility

The user is running **macOS 26 / Darwin 25.2.0** (the version released in 2025 where Apple switched to numeric macOS naming, post-Sequoia).

**Known issue: `launchctl load` / `launchctl unload` are deprecated.** On macOS 12+ (and increasingly enforced on later versions), the legacy `launchctl load <plist>` syntax is deprecated. On macOS 26, it may fail silently or return non-zero. The correct modern commands are:
```bash
launchctl bootstrap gui/$(id -u) "$PLIST_FILE"
launchctl bootout  gui/$(id -u) "$PLIST_FILE"
```
The current `1. Run Setup First.command` still uses the old syntax. If the notification service isn't firing, this is the likely cause. The plist loads at login via `RunAtLoad true` as a fallback, so the app still works; it just may not register correctly for the daily 7 AM schedule via launchctl.

**Notification permissions:** macOS has strict notification permission requirements. The `osascript display notification` method requires the app (Script Editor / osascript) to have notification permission in System Settings → Notifications. On a fresh system or after a macOS upgrade, the user may need to grant this permission manually.

**Port 8765:** No known conflicts on macOS 26. The app correctly checks if the port is already occupied before starting a second server.

### Canvas API Notes

- The Canvas API token is a personal access token (not OAuth). It can expire or be revoked.
- If sync fails with "Canvas API token is invalid or expired", the user needs to generate a new token at `https://palmbeachstate.instructure.com/profile/settings` under "Approved Integrations".
- The token is stored in plaintext in `canvas_config.json`. Never log or display it.
- Pagination is handled via the `Link: <url>; rel="next"` header — this is correct Canvas behavior and must be preserved.
- `bucket=upcoming` from the Canvas API returns only assignments that are not yet submitted and not past due. This is the primary source. `bucket=submitted` and `bucket=graded` are used to detect completions.
- Course names from Canvas are long (e.g., "Online - Anatomy and Physiology 2 (AA) (2026 Spring 12 Weeks - BSC2086-49)"). This is the actual Canvas course name and is stored verbatim.

### Data Integrity

- Manually-added assignments (no `canvas_id`) are **never modified or deleted by Canvas sync**. This is a design invariant — do not break it.
- The `completed` and `notifications_sent` fields on Canvas rows are **never overwritten by sync**. Only `title`, `course`, `due_date`, and `source` are refreshed from Canvas.
- UUIDs are generated with `uuid.uuid4()` and stored as strings. They are the stable identity key for manually-added assignments.
- `canvas_id` (integer) is the stable identity key for Canvas assignments.

### No External Dependencies — Ever

The entire stack must work on a fresh Mac with no additional software installed. Every feature must be implementable using:
- Python stdlib (http.server, json, uuid, datetime, subprocess, threading, socket, urllib)
- Vanilla JavaScript (no React, no Vue, no jQuery)
- Native macOS tools (osascript, launchctl)

If you ever feel tempted to `import requests` or `import flask`, stop and use `urllib` and `http.server` instead.
