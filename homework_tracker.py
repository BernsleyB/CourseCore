#!/usr/bin/env python3
"""
Homework Tracker
Opens in your default browser as a local web app.
Requires no extra software — only the Python that came with your Mac.
"""

import json
import os
import uuid
import webbrowser
import threading
import socket
import time
from datetime import date
import datetime as _dt
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import urllib.request
import urllib.error


# ── Paths & config ────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE  = os.path.join(SCRIPT_DIR, "assignments.json")
PORT       = 8765

# ── Canvas sync state ─────────────────────────────────────────────────────────
_sync_state = {"last_sync": None, "result": None, "error": None, "running": False}


# ── Data helpers ──────────────────────────────────────────────────────────────

def load_assignments():
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f).get("assignments", [])
    except (json.JSONDecodeError, IOError, KeyError):
        return []


def save_assignments(assignments):
    with open(DATA_FILE, "w") as f:
        json.dump({"assignments": assignments}, f, indent=2)


# ── Canvas sync helpers ────────────────────────────────────────────────────────

def _do_canvas_sync():
    """Run a Canvas sync in a background thread; updates _sync_state."""
    _sync_state["running"] = True
    try:
        import canvas_sync
        result = canvas_sync.sync()
        _sync_state["result"]    = result
        _sync_state["error"]     = None
        _sync_state["last_sync"] = _dt.datetime.now().strftime("%I:%M %p").lstrip("0")
    except Exception as e:
        _sync_state["error"] = str(e)
    finally:
        _sync_state["running"] = False


def start_canvas_sync():
    """Kick off a Canvas sync in a daemon thread (no-op if already running)."""
    if _sync_state["running"]:
        return
    threading.Thread(target=_do_canvas_sync, daemon=True).start()


# ── Anthropic AI summarization ────────────────────────────────────────────────

def _call_anthropic(api_key: str, assignment: dict) -> dict:
    """Call Anthropic API to summarize an assignment. Returns dict with three fields."""
    title    = assignment.get("title",    "Unknown Assignment")
    course   = assignment.get("course",   "Unknown Course")
    due_date = assignment.get("due_date", "Unknown")

    prompt = (
        "You are a helpful academic assistant for a nursing/science student at "
        "Palm Beach State College.\n\n"
        f"Assignment: {title}\n"
        f"Course: {course}\n"
        f"Due Date: {due_date}\n\n"
        "Based on the assignment title and course, respond with a JSON object "
        "containing exactly these three keys:\n"
        "- \"what_its_asking\": What this assignment is likely asking the student "
        "to do (1-3 sentences)\n"
        "- \"concepts_tested\": The academic concepts or skills this assignment "
        "probably tests (1-3 sentences)\n"
        "- \"suggested_approach\": A practical suggested approach for completing "
        "this assignment (2-4 sentences)\n\n"
        "Respond with only valid JSON — no markdown fences, no extra text."
    )

    payload = json.dumps({
        "model":      "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "messages":   [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload, method="POST",
    )
    req.add_header("Content-Type",      "application/json")
    req.add_header("x-api-key",         api_key)
    req.add_header("anthropic-version", "2023-06-01")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            text   = result["content"][0]["text"].strip()
            # Strip markdown fences in case they appear anyway
            if text.startswith("```"):
                parts = text.split("```")
                text = parts[1] if len(parts) > 1 else text
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {
                    "what_its_asking":    text,
                    "concepts_tested":    "",
                    "suggested_approach": "",
                }
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            msg = json.loads(body).get("error", {}).get("message", body)
        except Exception:
            msg = body
        raise Exception(f"Anthropic API error ({e.code}): {msg}")


# ── Embedded HTML/CSS/JS ──────────────────────────────────────────────────────

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Homework Tracker</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', system-ui, sans-serif;
    background: #f0f4f8;
    color: #1e293b;
    min-height: 100vh;
  }

  /* ── Top bar ── */
  .topbar {
    background: #1e40af;
    color: white;
    padding: 14px 28px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    position: sticky;
    top: 0;
    z-index: 100;
    box-shadow: 0 2px 10px rgba(0,0,0,0.25);
  }
  .topbar h1 { font-size: 20px; font-weight: 700; letter-spacing: -0.3px; }
  .btn-add {
    background: white; color: #1e40af;
    border: none; padding: 8px 18px;
    border-radius: 8px; font-size: 14px; font-weight: 700;
    cursor: pointer; font-family: inherit;
    transition: background 0.15s;
  }
  .btn-add:hover { background: #e0e7ff; }

  /* ── Legend ── */
  .legend {
    background: white;
    padding: 8px 28px;
    display: flex; gap: 22px; flex-wrap: wrap;
    font-size: 12px; color: #64748b;
    border-bottom: 1px solid #e2e8f0;
  }
  .legend-item { display: flex; align-items: center; gap: 6px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; flex-shrink: 0; }

  /* ── Main area ── */
  .main { padding: 20px 28px; max-width: 1040px; margin: 0 auto; }

  .col-headers {
    display: grid;
    grid-template-columns: 32px 2fr 1.3fr 1fr 1.1fr 36px 44px;
    padding: 8px 16px;
    background: #dde4ef;
    border-radius: 10px 10px 0 0;
    font-size: 11px; font-weight: 700;
    color: #475569; text-transform: uppercase; letter-spacing: 0.06em;
    gap: 8px;
  }

  .rows { border-radius: 0 0 10px 10px; overflow: hidden; }

  /* ── Rows ── */
  .row {
    display: grid;
    grid-template-columns: 32px 2fr 1.3fr 1fr 1.1fr 36px 44px;
    padding: 13px 16px;
    align-items: center;
    border-bottom: 1px solid rgba(0,0,0,0.06);
    gap: 8px;
    transition: filter 0.15s;
  }
  .row:last-child { border-bottom: none; border-radius: 0 0 10px 10px; }
  .row:hover { filter: brightness(0.97); }
  .row.overdue  { background: #fee2e2; }
  .row.today    { background: #fef3c7; }
  .row.soon     { background: #fff7ed; }
  .row.upcoming { background: #f0fdf4; }

  .title  { font-weight: 600; font-size: 15px; word-break: break-word; }
  .course { font-size: 14px; color: #475569; word-break: break-word; }
  .due    { font-size: 14px; color: #475569; }
  .status { font-size: 13px; font-weight: 700; }
  .status.overdue  { color: #dc2626; }
  .status.today    { color: #d97706; }
  .status.soon     { color: #ea580c; }
  .status.upcoming { color: #16a34a; }

  .btn-delete {
    background: transparent; border: none;
    color: #cbd5e1; font-size: 17px;
    cursor: pointer; padding: 4px 8px; border-radius: 6px;
    line-height: 1; transition: color 0.15s, background 0.15s;
    justify-self: end;
  }
  .btn-delete:hover { color: #dc2626; background: rgba(220,38,38,0.1); }

  /* ── Completion toggle ── */
  .btn-complete {
    width: 22px; height: 22px; border-radius: 50%;
    border: 2px solid #c8d3de; background: transparent;
    cursor: pointer; padding: 0;
    font-size: 12px; font-weight: 800; color: transparent;
    justify-self: center; align-self: center;
    transition: border-color 0.15s, background 0.15s, color 0.15s;
    font-family: inherit; display: inline-flex;
    align-items: center; justify-content: center;
  }
  .btn-complete:hover { border-color: #22c55e; background: #f0fdf4; color: #16a34a; }
  .btn-complete.done  { border-color: #16a34a; background: #16a34a; color: white; }

  /* ── Completed rows ── */
  .row.done-row { background: #f8fafc !important; }
  .row.done-row:hover { filter: none; }
  .row.done-row .title { text-decoration: line-through; color: #94a3b8; font-weight: 400; }
  .row.done-row .course,
  .row.done-row .due   { color: #b0bec5; }
  .status.done-status  { color: #94a3b8; font-weight: 600; }

  /* ── Tabs bar ── */
  .tabs-bar {
    background: white;
    border-bottom: 1px solid #e2e8f0;
    padding: 0 28px;
    display: flex;
    overflow-x: auto;
    scrollbar-width: none;
  }
  .tabs-bar::-webkit-scrollbar { display: none; }
  .tab {
    padding: 10px 14px;
    font-size: 13px; font-weight: 600;
    color: #64748b;
    cursor: pointer;
    border: none;
    border-bottom: 2px solid transparent;
    background: none;
    font-family: inherit;
    transition: color 0.15s, border-bottom-color 0.15s;
    user-select: none;
    flex-shrink: 0;
    display: flex; align-items: center; gap: 5px;
    max-width: 200px;
  }
  .tab:hover { color: #1e40af; }
  .tab.active { color: #1e40af; border-bottom-color: #1e40af; }
  .tab-label {
    overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; min-width: 0;
  }
  .tab-count {
    display: inline-block;
    background: #e2e8f0; color: #64748b;
    border-radius: 10px; padding: 0 6px;
    font-size: 10px; font-weight: 700;
    flex-shrink: 0; min-width: 18px; text-align: center;
  }
  .tab.active .tab-count { background: #1e40af; color: white; }

  /* ── Empty state ── */
  .empty {
    text-align: center; padding: 72px 20px;
    color: #94a3b8; background: white;
    border-radius: 0 0 10px 10px;
  }
  .empty h2 { font-size: 22px; margin-bottom: 8px; font-weight: 600; }
  .empty p  { font-size: 15px; }

  /* ── Modal ── */
  .overlay {
    display: none; position: fixed; inset: 0;
    background: rgba(15,23,42,0.55);
    z-index: 200;
    align-items: center; justify-content: center;
  }
  .overlay.open { display: flex; }

  .modal {
    background: white; border-radius: 16px; overflow: hidden;
    width: 100%; max-width: 450px;
    box-shadow: 0 24px 64px rgba(0,0,0,0.35);
    animation: pop-in 0.18s ease;
  }
  @keyframes pop-in {
    from { transform: scale(0.95) translateY(-12px); opacity: 0; }
    to   { transform: scale(1)    translateY(0);     opacity: 1; }
  }
  .modal-hdr {
    background: #1e40af; color: white;
    padding: 15px 20px; font-size: 16px; font-weight: 700;
  }
  .modal-body { padding: 20px 20px 8px; }

  .field { margin-bottom: 16px; }
  .field label {
    display: block; font-size: 13px; font-weight: 700;
    color: #374151; margin-bottom: 6px;
  }
  .field input {
    width: 100%; padding: 10px 13px;
    border: 1.5px solid #d1d5db; border-radius: 8px;
    font-size: 15px; font-family: inherit; outline: none;
    transition: border-color 0.18s;
  }
  .field input:focus { border-color: #1e40af; }

  .date-row { display: flex; gap: 10px; }
  .date-row select {
    flex: 1; padding: 10px 8px;
    border: 1.5px solid #d1d5db; border-radius: 8px;
    font-size: 14px; font-family: inherit; outline: none;
    background: white; cursor: pointer;
    transition: border-color 0.18s;
  }
  .date-row select:focus { border-color: #1e40af; }

  .modal-ftr {
    padding: 12px 20px 20px;
    display: flex; gap: 10px;
  }
  .btn-save {
    flex: 1; background: #1e40af; color: white;
    border: none; padding: 12px; border-radius: 8px;
    font-size: 15px; font-weight: 700;
    cursor: pointer; font-family: inherit;
    transition: background 0.15s;
  }
  .btn-save:hover { background: #1e3a8a; }
  .btn-cancel {
    background: #f1f5f9; color: #475569;
    border: none; padding: 12px 20px; border-radius: 8px;
    font-size: 15px; font-weight: 600;
    cursor: pointer; font-family: inherit;
    transition: background 0.15s;
  }
  .btn-cancel:hover { background: #e2e8f0; }

  /* ── Canvas sync button ── */
  .btn-sync {
    background: rgba(255,255,255,0.15); color: white;
    border: 1.5px solid rgba(255,255,255,0.45); padding: 7px 15px;
    border-radius: 8px; font-size: 13px; font-weight: 600;
    cursor: pointer; font-family: inherit;
    transition: background 0.15s;
  }
  .btn-sync:hover    { background: rgba(255,255,255,0.28); }
  .btn-sync:disabled { opacity: 0.5; cursor: default; }

  /* ── Sync status bar ── */
  .sync-bar {
    background: #f8fafc; border-bottom: 1px solid #e2e8f0;
    padding: 5px 28px; font-size: 12px; color: #64748b;
    display: flex; align-items: center; gap: 6px; min-height: 28px;
  }
  .sync-bar.syncing { color: #1e40af; }
  .sync-bar.error   { color: #dc2626; background: #fef2f2; }

  /* ── Canvas source badge ── */
  .canvas-badge {
    display: inline-block;
    background: #e0e7ff; color: #3730a3;
    border-radius: 4px; padding: 1px 5px;
    font-size: 10px; font-weight: 700;
    letter-spacing: 0.04em; margin-left: 6px;
    vertical-align: middle; line-height: 1.4;
  }

  /* ── AI Summarize button ── */
  .btn-summarize {
    background: transparent; border: none;
    color: #a5b4fc; font-size: 15px;
    cursor: pointer; padding: 4px 6px; border-radius: 6px;
    line-height: 1; transition: color 0.15s, background 0.15s;
    justify-self: end;
  }
  .btn-summarize:hover { color: #4f46e5; background: rgba(79,70,229,0.1); }

  /* ── Summary modal ── */
  .summary-modal {
    background: white; border-radius: 16px; overflow: hidden;
    width: 100%; max-width: 560px;
    box-shadow: 0 24px 64px rgba(0,0,0,0.35);
    animation: pop-in 0.18s ease;
  }
  .summary-hdr {
    background: #4338ca; color: white;
    padding: 15px 20px;
    display: flex; justify-content: space-between; align-items: flex-start;
  }
  .summary-hdr-title    { font-size: 16px; font-weight: 700; }
  .summary-hdr-subtitle { font-size: 12px; opacity: 0.75; margin-top: 3px; }
  .summary-close {
    background: rgba(255,255,255,0.15); border: none; color: white;
    width: 28px; height: 28px; border-radius: 6px; flex-shrink: 0;
    font-size: 16px; cursor: pointer; font-family: inherit;
    display: flex; align-items: center; justify-content: center;
    transition: background 0.15s; margin-left: 12px;
  }
  .summary-close:hover { background: rgba(255,255,255,0.28); }

  .summary-body { padding: 20px; max-height: 70vh; overflow-y: auto; }
  .summary-loading {
    text-align: center; padding: 40px 20px; color: #64748b;
  }
  .summary-loading .spin-icon {
    font-size: 28px; margin-bottom: 12px;
    display: inline-block;
    animation: spin-anim 1s linear infinite;
  }
  @keyframes spin-anim {
    from { transform: rotate(0deg); }
    to   { transform: rotate(360deg); }
  }
  .summary-section { margin-bottom: 16px; }
  .summary-section:last-child { margin-bottom: 0; }
  .summary-section-label {
    font-size: 11px; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.07em; color: #4338ca; margin-bottom: 6px;
  }
  .summary-section-text {
    font-size: 14px; color: #1e293b; line-height: 1.6;
    background: #f8fafc; border-radius: 8px; padding: 10px 14px;
    border-left: 3px solid #c7d2fe;
    white-space: pre-wrap; word-break: break-word;
  }
  .summary-error {
    background: #fef2f2; color: #dc2626;
    border-radius: 8px; padding: 14px; font-size: 14px;
  }
</style>
</head>
<body>

<div class="topbar">
  <h1>Homework Tracker</h1>
  <div style="display:flex;gap:10px;align-items:center;">
    <button class="btn-sync" id="btn-sync" onclick="syncCanvas()">&#x27F3; Sync Canvas</button>
    <button class="btn-add" onclick="openModal()">+ Add Assignment</button>
  </div>
</div>

<div class="legend">
  <div class="legend-item"><span class="dot" style="background:#ef4444"></span>Overdue</div>
  <div class="legend-item"><span class="dot" style="background:#f59e0b"></span>Due today</div>
  <div class="legend-item"><span class="dot" style="background:#f97316"></span>Due within 3 days</div>
  <div class="legend-item"><span class="dot" style="background:#22c55e"></span>Upcoming</div>
  <div class="legend-item"><span class="dot" style="background:#94a3b8"></span>Completed</div>
</div>

<div class="sync-bar" id="sync-bar">Connecting to Canvas&hellip;</div>

<div class="tabs-bar" id="tabs-bar"></div>

<div class="main">
  <div class="col-headers">
    <span></span>
    <span>Assignment</span>
    <span>Course</span>
    <span>Due Date</span>
    <span>Status</span>
    <span></span>
    <span></span>
  </div>
  <div class="rows" id="rows">
    <div class="empty"><h2>No assignments yet!</h2><p>Click '+ Add Assignment' to get started.</p></div>
  </div>

</div>

<!-- Summary Modal -->
<div class="overlay" id="summary-overlay" onclick="summaryOverlayClick(event)">
  <div class="summary-modal">
    <div class="summary-hdr">
      <div>
        <div class="summary-hdr-title"  id="summary-modal-title">AI Summary</div>
        <div class="summary-hdr-subtitle" id="summary-modal-course"></div>
      </div>
      <button class="summary-close" onclick="closeSummaryModal()">&#x2715;</button>
    </div>
    <div class="summary-body" id="summary-body"></div>
  </div>
</div>

<!-- Add Assignment Modal -->
<div class="overlay" id="overlay" onclick="overlayClick(event)">
  <div class="modal">
    <div class="modal-hdr">Add New Assignment</div>
    <div class="modal-body">
      <div class="field">
        <label for="inp-title">Assignment Title</label>
        <input id="inp-title" type="text" placeholder="e.g. Care Plan Assignment" autocomplete="off">
      </div>
      <div class="field">
        <label for="inp-course">Course Name</label>
        <input id="inp-course" type="text" placeholder="e.g. NURS 201" autocomplete="off">
      </div>
      <div class="field">
        <label>Due Date</label>
        <div class="date-row">
          <select id="sel-month"></select>
          <select id="sel-day"></select>
          <select id="sel-year"></select>
        </div>
      </div>
    </div>
    <div class="modal-ftr">
      <button class="btn-cancel" onclick="closeModal()">Cancel</button>
      <button class="btn-save"   onclick="saveAssignment()">Save Assignment</button>
    </div>
  </div>
</div>

<script>
// ── Date dropdowns ─────────────────────────────────────────────────────────
const MONTHS = ['January','February','March','April','May','June',
                'July','August','September','October','November','December'];

function initDates() {
  const now = new Date();
  const sm = document.getElementById('sel-month');
  const sy = document.getElementById('sel-year');

  MONTHS.forEach((m, i) => sm.add(new Option(m, i + 1)));
  sm.value = now.getMonth() + 1;

  for (let y = now.getFullYear(); y <= now.getFullYear() + 3; y++)
    sy.add(new Option(y, y));
  sy.value = now.getFullYear();

  refreshDays(now.getDate());
  sm.addEventListener('change', () => refreshDays());
  sy.addEventListener('change', () => refreshDays());
}

function refreshDays(selectDay) {
  const sd   = document.getElementById('sel-day');
  const mon  = +document.getElementById('sel-month').value;
  const yr   = +document.getElementById('sel-year').value;
  const prev = selectDay || +sd.value || 1;
  const max  = new Date(yr, mon, 0).getDate();

  sd.innerHTML = '';
  for (let d = 1; d <= max; d++) sd.add(new Option(d, d));
  sd.value = Math.min(prev, max);
}

// ── Modal ──────────────────────────────────────────────────────────────────
function openModal() {
  document.getElementById('overlay').classList.add('open');
  document.getElementById('inp-title').focus();
}
function closeModal() {
  document.getElementById('overlay').classList.remove('open');
  document.getElementById('inp-title').value  = '';
  document.getElementById('inp-course').value = '';
}
function overlayClick(e) { if (e.target.id === 'overlay') closeModal(); }

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeModal(); closeSummaryModal(); }
  if (e.key === 'Enter' && document.getElementById('overlay').classList.contains('open'))
    saveAssignment();
});

// ── Save ───────────────────────────────────────────────────────────────────
async function saveAssignment() {
  const title  = document.getElementById('inp-title').value.trim();
  const course = document.getElementById('inp-course').value.trim();
  const month  = String(document.getElementById('sel-month').value).padStart(2,'0');
  const day    = String(document.getElementById('sel-day').value).padStart(2,'0');
  const year   = document.getElementById('sel-year').value;

  if (!title)  { alert('Please enter an assignment title.');  return; }
  if (!course) { alert('Please enter a course name.'); return; }

  const dueDate = `${year}-${month}-${day}`;
  const todayStr = new Date().toISOString().split('T')[0];
  if (dueDate < todayStr)
    if (!confirm(`That due date (${dueDate}) is in the past. Save it anyway?`)) return;

  const resp = await fetch('/api/assignments', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, course, due_date: dueDate })
  });

  if (resp.ok) { closeModal(); loadAssignments(); }
  else alert('Something went wrong. Please try again.');
}

// ── Delete ─────────────────────────────────────────────────────────────────
async function del(id, title) {
  if (!confirm(`Delete "${title}"?\n\nThis cannot be undone.`)) return;
  const resp = await fetch(`/api/assignments/${id}`, { method: 'DELETE' });
  if (resp.ok) loadAssignments();
}

// ── Toggle complete ────────────────────────────────────────────────────────
async function toggleComplete(id, currentlyDone) {
  const resp = await fetch(`/api/assignments/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ completed: !currentlyDone })
  });
  if (resp.ok) loadAssignments();
}

// ── Tabs ───────────────────────────────────────────────────────────────────
let activeTab = 'all';

function shortLabel(course) {
  return course
    .replace(/^(Online|Hybrid)\s*-\s*/i, '')
    .replace(/\s*\([A-Z]{2,10}\d{3,5}[A-Z]?\s*-\s*\d+\)\s*$/i, '')
    .trim() || course;
}

function buildTabs(list) {
  const active  = list.filter(a => !a.completed);
  const done    = list.filter(a =>  a.completed);
  const courses = [...new Set(active.map(a => a.course))].sort();

  const tabs = [
    { id: 'all',       display: 'All Assignments', count: active.length },
    ...courses.map(c => ({
      id:      'course:' + c,
      display: shortLabel(c),
      title:   c,
      count:   active.filter(a => a.course === c).length
    })),
    { id: 'completed', display: 'Completed', count: done.length },
  ];

  if (!tabs.some(t => t.id === activeTab)) activeTab = 'all';

  const bar = document.getElementById('tabs-bar');
  bar.innerHTML = tabs.map(t =>
    `<button class="tab${activeTab === t.id ? ' active' : ''}" data-tab-id="${esc(t.id)}" title="${esc(t.title || t.display)}"><span class="tab-label">${esc(t.display)}</span><span class="tab-count">${t.count}</span></button>`
  ).join('');

  bar.querySelectorAll('.tab').forEach(btn => {
    btn.addEventListener('click', () => {
      activeTab = btn.getAttribute('data-tab-id');
      loadAssignments();
    });
  });
}

// ── Rendering ──────────────────────────────────────────────────────────────
function urgency(d) {
  if (d < 0)   return 'overdue';
  if (d === 0) return 'today';
  if (d <= 3)  return 'soon';
  return 'upcoming';
}
function statusText(d) {
  if (d < 0)   return `Overdue by ${Math.abs(d)} day${Math.abs(d)!==1?'s':''}`;
  if (d === 0) return 'Due TODAY!';
  if (d === 1) return 'Due tomorrow';
  return `${d} days left`;
}
function esc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function renderRow(a, today) {
  const due   = new Date(a.due_date + 'T00:00:00');
  const days  = Math.round((due - today) / 86400000);
  const fmt   = due.toLocaleDateString('en-US', {month:'short',day:'numeric',year:'numeric'});
  const safeT = esc(a.title);
  const safeC = esc(a.course);
  const isCompleted = !!a.completed;

  const rowClass     = isCompleted ? 'done-row' : urgency(days);
  const statusLabel  = isCompleted ? 'Completed' : statusText(days);
  const statusClass  = isCompleted ? 'done-status' : urgency(days);
  const toggleTitle  = isCompleted ? 'Mark as incomplete' : 'Mark as complete';
  const badge        = a.source === 'canvas' ? '<span class="canvas-badge">CANVAS</span>' : '';

  return `<div class="row ${rowClass}">
    <button class="btn-complete${isCompleted ? ' done' : ''}" onclick="toggleComplete('${esc(a.id)}',${isCompleted})" title="${toggleTitle}">&#x2713;</button>
    <span class="title">${safeT}</span>
    <span class="course">${safeC}${badge}</span>
    <span class="due">${fmt}</span>
    <span class="status ${statusClass}">${statusLabel}</span>
    <button class="btn-summarize" onclick="summarize('${esc(a.id)}')" title="AI Summary">&#x2726;</button>
    <button class="btn-delete" onclick="del('${esc(a.id)}','${safeT}')" title="Delete">&#x2715;</button>
  </div>`;
}

async function loadAssignments() {
  const resp = await fetch('/api/assignments');
  const data = await resp.json();
  const list = (data.assignments || []).sort((a,b) => a.due_date.localeCompare(b.due_date));
  const today = new Date(); today.setHours(0,0,0,0);

  buildTabs(list);

  let toShow;
  if (activeTab === 'completed') {
    toShow = list.filter(a => a.completed);
  } else if (activeTab.startsWith('course:')) {
    const course = activeTab.slice(7);
    toShow = list.filter(a => !a.completed && a.course === course);
  } else {
    toShow = list.filter(a => !a.completed);
  }

  const box = document.getElementById('rows');
  if (!toShow.length) {
    let msg;
    if (activeTab === 'completed') {
      msg = '<div class="empty"><h2>No completed assignments</h2><p>Completed assignments will appear here.</p></div>';
    } else if (activeTab !== 'all') {
      msg = '<div class="empty"><h2>All caught up!</h2><p>No active assignments for this course.</p></div>';
    } else if (list.some(a => a.completed)) {
      msg = '<div class="empty"><h2>All caught up! &#x2713;</h2><p>All assignments are completed &mdash; see the Completed tab.</p></div>';
    } else {
      msg = '<div class="empty"><h2>No assignments yet!</h2><p>Click \'+ Add Assignment\' to get started.</p></div>';
    }
    box.innerHTML = msg;
  } else {
    box.innerHTML = toShow.map(a => renderRow(a, today)).join('');
  }
}

// ── Canvas Sync ─────────────────────────────────────────────────────────────
async function syncCanvas() {
  const btn = document.getElementById('btn-sync');
  btn.disabled    = true;
  btn.textContent = '\u27F3 Syncing\u2026';
  try {
    await fetch('/api/sync', { method: 'POST' });
    pollSyncStatus();
  } catch(e) {
    btn.disabled    = false;
    btn.textContent = '\u27F3 Sync Canvas';
    const bar = document.getElementById('sync-bar');
    bar.className   = 'sync-bar error';
    bar.textContent = 'Network error \u2014 could not reach Canvas';
  }
}

async function pollSyncStatus() {
  try {
    const resp = await fetch('/api/sync-status');
    const s    = await resp.json();
    const bar  = document.getElementById('sync-bar');
    const btn  = document.getElementById('btn-sync');

    if (s.running) {
      bar.className   = 'sync-bar syncing';
      bar.textContent = '\u27F3 Syncing with Canvas\u2026';
      setTimeout(pollSyncStatus, 1200);
      return;
    }

    btn.disabled    = false;
    btn.textContent = '\u27F3 Sync Canvas';

    if (s.error) {
      bar.className   = 'sync-bar error';
      bar.textContent = 'Canvas sync error: ' + s.error;
    } else if (s.last_sync && s.result) {
      const r     = s.result;
      const aWord = r.total_canvas === 1 ? 'assignment' : 'assignments';
      const cWord = r.courses      === 1 ? 'course'     : 'courses';
      const cDone = r.auto_completed > 0 ? ` \u2014 ${r.auto_completed} auto-completed` : '';
      bar.className   = 'sync-bar';
      bar.textContent = `Canvas synced at ${s.last_sync} \u2014 ${r.total_canvas} ${aWord} across ${r.courses} ${cWord}${cDone}`;
    } else if (s.running === false && s.last_sync === null) {
      bar.className   = 'sync-bar syncing';
      bar.textContent = '\u27F3 Syncing with Canvas\u2026';
      setTimeout(pollSyncStatus, 1200);
      return;
    }
    loadAssignments();
  } catch(e) { /* server may still be starting */ }
}

// ── AI Summarize ────────────────────────────────────────────────────────────
function closeSummaryModal() {
  document.getElementById('summary-overlay').classList.remove('open');
}
function summaryOverlayClick(e) {
  if (e.target.id === 'summary-overlay') closeSummaryModal();
}

async function summarize(id) {
  document.getElementById('summary-modal-title').textContent  = 'Analyzing\u2026';
  document.getElementById('summary-modal-course').textContent = '';
  document.getElementById('summary-body').innerHTML =
    '<div class="summary-loading">' +
    '<div class="spin-icon">\u27F3</div>' +
    '<p>Asking Claude to analyze this assignment\u2026</p></div>';
  document.getElementById('summary-overlay').classList.add('open');

  try {
    const resp = await fetch('/api/summarize/' + id, { method: 'POST' });
    const data = await resp.json();
    const body = document.getElementById('summary-body');

    if (!resp.ok || data.error) {
      body.innerHTML = '<div class="summary-error">' + esc(data.error || 'Unknown error') + '</div>';
      return;
    }

    document.getElementById('summary-modal-title').textContent  = data.title  || 'AI Summary';
    document.getElementById('summary-modal-course').textContent = data.course || '';

    body.innerHTML =
      '<div class="summary-section">' +
        '<div class="summary-section-label">What it\'s asking for</div>' +
        '<div class="summary-section-text">' + esc(data.what_its_asking    || '') + '</div>' +
      '</div>' +
      '<div class="summary-section">' +
        '<div class="summary-section-label">Concepts being tested</div>' +
        '<div class="summary-section-text">' + esc(data.concepts_tested    || '') + '</div>' +
      '</div>' +
      '<div class="summary-section">' +
        '<div class="summary-section-label">Suggested approach</div>' +
        '<div class="summary-section-text">' + esc(data.suggested_approach || '') + '</div>' +
      '</div>';
  } catch(e) {
    document.getElementById('summary-body').innerHTML =
      '<div class="summary-error">Network error \u2014 could not reach server.</div>';
  }
}

initDates();
loadAssignments();
pollSyncStatus();                        // kick off on page load
setInterval(loadAssignments,  60000);    // refresh list every minute
setInterval(pollSyncStatus,  300000);    // re-check Canvas status every 5 min
</script>
</body>
</html>"""


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # silence server logs in terminal

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/assignments":
            self._send_json({"assignments": load_assignments()})

        elif path == "/api/sync-status":
            self._send_json({
                "running":   _sync_state["running"],
                "last_sync": _sync_state["last_sync"],
                "result":    _sync_state["result"],
                "error":     _sync_state["error"],
            })

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/sync":
            start_canvas_sync()
            self._send_json({"ok": True, "message": "Sync started"})

        elif path == "/api/assignments":
            length = int(self.headers.get("Content-Length", 0))
            try:
                data       = json.loads(self.rfile.read(length))
                assignment = {
                    "id":                 str(uuid.uuid4()),
                    "title":              str(data["title"]),
                    "course":             str(data["course"]),
                    "due_date":           str(data["due_date"]),
                    "notifications_sent": [],
                    "completed":          False,
                }
                assignments = load_assignments()
                assignments.append(assignment)
                save_assignments(assignments)
                self._send_json({"ok": True})
            except Exception as err:
                self._send_json({"error": str(err)}, 400)
        elif path.startswith("/api/summarize/"):
            aid        = path[len("/api/summarize/"):]
            assignments = load_assignments()
            assignment  = next((a for a in assignments if a["id"] == aid), None)
            if assignment is None:
                self._send_json({"error": "Assignment not found."}, 404)
                return

            config_path = os.path.join(SCRIPT_DIR, "canvas_config.json")
            try:
                with open(config_path) as f:
                    config = json.load(f)
            except (IOError, json.JSONDecodeError):
                self._send_json(
                    {"error": "canvas_config.json not found or invalid. "
                              "Add your anthropic_key to it."}, 500)
                return

            api_key = config.get("anthropic_key", "").strip()
            if not api_key:
                self._send_json(
                    {"error": "No Anthropic API key configured. "
                              "Add \"anthropic_key\": \"<your-key>\" to canvas_config.json."}, 400)
                return

            try:
                summary = _call_anthropic(api_key, assignment)
                self._send_json({
                    "ok":                 True,
                    "title":              assignment.get("title",  ""),
                    "course":             assignment.get("course", ""),
                    "what_its_asking":    summary.get("what_its_asking",    ""),
                    "concepts_tested":    summary.get("concepts_tested",    ""),
                    "suggested_approach": summary.get("suggested_approach", ""),
                })
            except Exception as err:
                self._send_json({"error": str(err)}, 500)

        else:
            self.send_response(404)
            self.end_headers()

    def do_PATCH(self):
        path = urlparse(self.path).path

        if path.startswith("/api/assignments/"):
            aid    = path[len("/api/assignments/"):]
            length = int(self.headers.get("Content-Length", 0))
            try:
                data        = json.loads(self.rfile.read(length))
                assignments = load_assignments()
                for a in assignments:
                    if a["id"] == aid:
                        if "completed" in data:
                            a["completed"] = bool(data["completed"])
                        break
                save_assignments(assignments)
                self._send_json({"ok": True})
            except Exception as err:
                self._send_json({"error": str(err)}, 400)
        else:
            self.send_response(404)
            self.end_headers()

    def do_DELETE(self):
        path = urlparse(self.path).path

        if path.startswith("/api/assignments/"):
            aid         = path[len("/api/assignments/"):]
            assignments = [a for a in load_assignments() if a["id"] != aid]
            save_assignments(assignments)
            self._send_json({"ok": True})
        else:
            self.send_response(404)
            self.end_headers()


# ── Server startup ────────────────────────────────────────────────────────────

def server_already_running():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", PORT)) == 0


def run():
    url = f"http://localhost:{PORT}"

    if server_already_running():
        # App already open — just bring the browser tab to focus
        print("Homework Tracker is already running. Opening browser...")
        webbrowser.open(url)
        return

    server = HTTPServer(("localhost", PORT), Handler)

    # Open the browser half a second after the server starts
    def open_browser():
        time.sleep(0.5)
        webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()

    # Auto-sync Canvas assignments as soon as the app launches
    threading.Thread(target=_do_canvas_sync, daemon=True).start()

    print()
    print("=" * 44)
    print("  Homework Tracker is running!")
    print(f"  {url}")
    print("=" * 44)
    print()
    print("  Your browser should open automatically.")
    print("  If it doesn't, paste the link above into Safari.")
    print()
    print("  Keep this window open while using the app.")
    print("  Close it (or press Ctrl+C) to quit.")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nHomework Tracker stopped. Goodbye!")


if __name__ == "__main__":
    run()
