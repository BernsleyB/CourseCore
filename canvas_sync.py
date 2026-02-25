#!/usr/bin/env python3
"""
canvas_sync.py
--------------
Fetches upcoming assignments from Canvas LMS and merges them into
assignments.json.  Uses only the Python standard library — no pip required.

How it works
  1. Read canvas_config.json for the Canvas URL and API token.
  2. Fetch every active student enrollment (paginated).
  3. For each course, fetch upcoming assignments (paginated).
  4. For each course, fetch submitted assignments to detect completions.
  5. Convert Canvas UTC due dates to local time, keep YYYY-MM-DD.
  6. Merge into assignments.json:
       • Canvas assignments are keyed by canvas_id so they never duplicate.
       • Manually-added assignments (no canvas_id) are never touched.
       • Assignments submitted on Canvas are automatically marked completed.
       • Already-completed assignments are kept (never deleted by sync).
       • Assignments that disappeared from Canvas and are not submitted/
         completed are removed (past-due without submission).
       • notifications_sent and completed are preserved when a Canvas row
         already exists.
"""

import json
import os
import uuid
from datetime import datetime, date
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_FILE   = os.path.join(SCRIPT_DIR, "assignments.json")
CONFIG_FILE = os.path.join(SCRIPT_DIR, "canvas_config.json")


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config():
    with open(CONFIG_FILE, "r") as f:
        cfg = json.load(f)
    return cfg["canvas_url"].rstrip("/"), cfg["token"]


# ── Canvas HTTP helpers ───────────────────────────────────────────────────────

def _get_all(base_url: str, token: str, path: str) -> list:
    """Fetch every page of a Canvas API endpoint and return merged list."""
    results = []
    url = f"{base_url}{path}"
    while url:
        req = Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urlopen(req, timeout=20) as resp:
                page = json.loads(resp.read().decode())
                if isinstance(page, list):
                    results.extend(page)
                # Follow the Link: <…>; rel="next" header for pagination
                link_header = resp.headers.get("Link", "")
                url = None
                for part in link_header.split(","):
                    part = part.strip()
                    if 'rel="next"' in part:
                        url = part.split(";")[0].strip().strip("<>")
                        break
        except HTTPError as e:
            if e.code == 401:
                raise RuntimeError(
                    "Canvas API token is invalid or expired. "
                    "Update the token in canvas_config.json."
                )
            raise RuntimeError(f"Canvas HTTP {e.code}: {e.reason}")
        except URLError as e:
            raise RuntimeError(
                f"Could not reach Canvas ({base_url}). "
                f"Check your internet connection. Detail: {e.reason}"
            )
    return results


# ── Date helpers ──────────────────────────────────────────────────────────────

def _parse_due_date(due_at: str):
    """
    Convert a Canvas UTC timestamp ("2026-02-25T23:59:00Z") to a local
    YYYY-MM-DD string.  Returns None if no due date is set.
    """
    if not due_at:
        return None
    try:
        # Canvas uses Z for UTC or an explicit offset like -05:00
        dt = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        local_dt = dt.astimezone()          # convert to system local timezone
        return local_dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


# ── Local data helpers ────────────────────────────────────────────────────────

def _load_assignments() -> list:
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f).get("assignments", [])
    except (json.JSONDecodeError, IOError):
        return []


def _save_assignments(assignments: list) -> None:
    with open(DATA_FILE, "w") as f:
        json.dump({"assignments": assignments}, f, indent=2)


# ── Main sync logic ───────────────────────────────────────────────────────────

def sync() -> dict:
    """
    Pull upcoming Canvas assignments and merge into local data.
    Submitted assignments are automatically marked as completed.

    Returns a summary dict:
        ok              – True on success
        added           – new Canvas assignments written
        updated         – existing Canvas rows refreshed
        removed         – Canvas rows removed (past-due, not submitted)
        auto_completed  – Canvas rows auto-marked complete (submitted on Canvas)
        total_canvas    – total Canvas rows now in assignments.json
        courses         – number of courses checked
    """
    canvas_url, token = _load_config()
    today_str = date.today().isoformat()

    # ── 1. Fetch active courses ──────────────────────────────────────────────
    raw_courses = _get_all(
        canvas_url, token,
        "/api/v1/courses"
        "?enrollment_state=active"
        "&enrollment_type=student"
        "&state[]=available"
        "&per_page=50"
    )

    course_map: dict[int, str] = {}
    for c in raw_courses:
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        if not cid:
            continue
        name = c.get("name") or c.get("course_code") or f"Course {cid}"
        course_map[cid] = name

    # ── 2. Fetch upcoming and submitted assignments per course ────────────────
    canvas_upcoming: list[dict] = []
    submitted_canvas_ids: set   = set()

    for cid, cname in course_map.items():
        # Upcoming (not yet due, not submitted)
        try:
            raw = _get_all(
                canvas_url, token,
                f"/api/v1/courses/{cid}/assignments"
                f"?bucket=upcoming"
                f"&per_page=100"
                f"&order_by=due_at"
            )
        except RuntimeError:
            raw = []  # skip courses we can't access

        for a in raw:
            if not isinstance(a, dict):
                continue
            due_date = _parse_due_date(a.get("due_at"))
            if not due_date:
                continue                  # skip undated assignments
            if due_date < today_str:
                continue                  # skip anything already past
            canvas_upcoming.append({
                "canvas_id": a["id"],
                "title":     a.get("name", "Unnamed Assignment"),
                "course":    cname,
                "due_date":  due_date,
            })

        # Submitted (turned in but not yet graded)
        try:
            submitted_raw = _get_all(
                canvas_url, token,
                f"/api/v1/courses/{cid}/assignments"
                f"?bucket=submitted"
                f"&per_page=100"
            )
            for a in submitted_raw:
                if isinstance(a, dict) and a.get("id"):
                    submitted_canvas_ids.add(a["id"])
        except RuntimeError:
            pass  # submitted bucket may not be accessible; continue

        # Graded (submitted and graded — also counts as done)
        try:
            graded_raw = _get_all(
                canvas_url, token,
                f"/api/v1/courses/{cid}/assignments"
                f"?bucket=graded"
                f"&per_page=100"
            )
            for a in graded_raw:
                if isinstance(a, dict) and a.get("id"):
                    submitted_canvas_ids.add(a["id"])
        except RuntimeError:
            pass  # graded bucket may not be accessible; continue

    # ── 3. Merge into local data ─────────────────────────────────────────────
    existing = _load_assignments()

    # Separate manually-added rows from Canvas rows
    manual: list[dict]            = []
    by_canvas_id: dict[int, dict] = {}

    for row in existing:
        if "canvas_id" in row:
            by_canvas_id[row["canvas_id"]] = row
        else:
            manual.append(row)

    upcoming_canvas_ids = {a["canvas_id"] for a in canvas_upcoming}
    added = updated = removed = auto_completed = 0
    new_canvas_rows: list[dict] = []
    processed_ids: set          = set()

    # Process upcoming (active) assignments
    for ca in canvas_upcoming:
        cid = ca["canvas_id"]
        processed_ids.add(cid)
        if cid in by_canvas_id:
            row = by_canvas_id[cid]
            changed = (
                row.get("title")    != ca["title"]    or
                row.get("course")   != ca["course"]   or
                row.get("due_date") != ca["due_date"]
            )
            row["title"]    = ca["title"]
            row["course"]   = ca["course"]
            row["due_date"] = ca["due_date"]
            row["source"]   = "canvas"
            # completed and notifications_sent are preserved (not touched)
            new_canvas_rows.append(row)
            if changed:
                updated += 1
        else:
            new_canvas_rows.append({
                "id":                 str(uuid.uuid4()),
                "canvas_id":          cid,
                "title":              ca["title"],
                "course":             ca["course"],
                "due_date":           ca["due_date"],
                "notifications_sent": [],
                "source":             "canvas",
                "completed":          False,
            })
            added += 1

    # Process previously-tracked Canvas rows no longer in upcoming
    for cid, row in by_canvas_id.items():
        if cid in processed_ids:
            continue  # already handled above

        if cid in submitted_canvas_ids:
            # Assignment was submitted on Canvas — auto-mark as completed
            if not row.get("completed"):
                row["completed"] = True
                auto_completed  += 1
            new_canvas_rows.append(row)
        elif row.get("completed"):
            # Keep rows that were already marked complete (manually or previously auto)
            new_canvas_rows.append(row)
        else:
            # Past-due, not submitted, not completed — remove
            removed += 1

    # Final list: keep manual rows, replace canvas rows with fresh data
    _save_assignments(manual + new_canvas_rows)

    return {
        "ok":             True,
        "added":          added,
        "updated":        updated,
        "removed":        removed,
        "auto_completed": auto_completed,
        "total_canvas":   len(new_canvas_rows),
        "courses":        len(course_map),
    }


# ── CLI usage ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        r = sync()
        done_msg = f", {r['auto_completed']} auto-completed" if r['auto_completed'] else ""
        print(
            f"Sync complete: {r['added']} added, {r['updated']} updated, "
            f"{r['removed']} removed{done_msg} across {r['courses']} courses."
        )
    except Exception as e:
        print(f"Sync failed: {e}")
