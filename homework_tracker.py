#!/usr/bin/env python3
"""
Homework Tracker
Opens in your default browser as a local web app.
Requires no extra software — only the Python that came with your Mac.
"""

import json
import os
import re
import uuid
import webbrowser
import threading
import socket
import time
import tempfile
import shutil
import zipfile
import xml.etree.ElementTree as ET
from datetime import date
import datetime as _dt
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import urllib.request
import urllib.error
import base64


# ── Paths & config ────────────────────────────────────────────────────────────

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_FILE    = os.path.join(SCRIPT_DIR, "assignments.json")
SYLLABI_FILE = os.path.join(SCRIPT_DIR, "syllabi.json")
PORT         = 8765

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
    data = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            data = {}
    data["assignments"] = assignments
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_syllabi() -> dict:
    if not os.path.exists(SYLLABI_FILE):
        return {}
    try:
        with open(SYLLABI_FILE, "r") as f:
            return json.load(f).get("syllabi", {})
    except (json.JSONDecodeError, IOError):
        return {}


def save_syllabi(syllabi: dict) -> None:
    with open(SYLLABI_FILE, "w") as f:
        json.dump({"syllabi": syllabi}, f, indent=2)


def _slugify_course(course: str) -> str:
    """URL-safe slug for a course name — used for DELETE routing."""
    slug = re.sub(r'[^a-z0-9]+', '_', course.lower())
    return slug.strip('_')


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

def _call_anthropic(api_key: str, assignment: dict, announcements: list = None, file_texts: list = None) -> dict:
    """Call Anthropic API to summarize an assignment. Returns dict with three fields."""
    title       = assignment.get("title",       "Unknown Assignment")
    course      = assignment.get("course",      "Unknown Course")
    due_date    = assignment.get("due_date",    "Unknown")
    description = (assignment.get("description") or "").strip()

    prompt = (
        "You are a helpful academic assistant for a nursing/science student at "
        "Palm Beach State College.\n\n"
        f"Assignment: {title}\n"
        f"Course: {course}\n"
        f"Due Date: {due_date}\n"
    )

    if description:
        prompt += f"\nAssignment Instructions:\n{description[:3000]}\n"

    if announcements:
        ann_lines = []
        for a in announcements[:3]:
            t   = a.get("title", "").strip()
            msg = (a.get("message") or "").strip()[:500]
            dt  = (a.get("posted_at") or "")[:10]
            ann_lines.append(f"- [{dt}] {t}: {msg}" if dt else f"- {t}: {msg}")
        prompt += "\nRecent Course Announcements:\n" + "\n".join(ann_lines) + "\n"

    if file_texts:
        prompt += "\nAttached Course Materials:\n"
        for f in file_texts:
            prompt += f"\n[File: {f['filename']}]\n{f['text']}\n"

    prompt += (
        "\nBased on all available context above, respond with a JSON object "
        "containing exactly these three keys:\n"
        "- \"what_its_asking\": What this assignment is asking the student "
        "to do (1-3 sentences)\n"
        "- \"concepts_tested\": The academic concepts or skills this assignment "
        "tests (1-3 sentences)\n"
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


def _call_anthropic_syllabus(api_key: str, course: str, pdf_text: str) -> dict:
    """Call Anthropic API to extract structured syllabus data. Returns structured dict."""
    _empty = {
        "professor":   {"name": None, "email": None, "phone": None,
                        "office_hours": None, "office_location": None},
        "assignments": [],
        "exams":       [],
        "policies":    {"attendance": None, "late_work": None},
        "schedule":    [],
    }

    prompt = (
        f"Course: {course}\n"
        f"Syllabus Text: {pdf_text[:8000]}\n\n"
        "Respond with a single valid JSON object (no markdown fences):\n"
        "{\n"
        '  "professor": {"name":..., "email":..., "phone":..., "office_hours":..., "office_location":...},\n'
        '  "assignments": [{"name":..., "weight":"25% or 100 pts", "description":...}],\n'
        '  "exams": [{"name":..., "date":"YYYY-MM-DD or text as written", "topics":...}],\n'
        '  "policies": {"attendance":"2-4 sentence summary or null", "late_work":"...or null"},\n'
        '  "schedule": [{"week":"Week 1 (Jan 13)", "topics":"..."}]\n'
        "}\n"
        "Rules:\n"
        "- assignments: ONLY graded items with weights (no readings)\n"
        "- exams: ALL exams AND quizzes found anywhere in the syllabus\n"
        "- schedule: [] if no weekly schedule found\n"
        "- null for any field not present in the syllabus"
    )

    payload = json.dumps({
        "model":      "claude-sonnet-4-20250514",
        "max_tokens": 2048,
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
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
            text   = result["content"][0]["text"].strip()
            if text.startswith("```"):
                parts = text.split("```")
                text = parts[1] if len(parts) > 1 else text
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return _empty
    except Exception:
        return _empty


# ── Canvas file extraction helpers ───────────────────────────────────────────

def _extract_canvas_file_ids(html: str) -> list:
    """Extract Canvas file IDs from assignment description HTML."""
    if not html:
        return []
    ids = []
    # data-api-endpoint="…/files/12345" (Canvas Rich Content API attribute)
    for m in re.finditer(r'data-api-endpoint="[^"]*?/files/(\d+)"', html):
        ids.append(int(m.group(1)))
    # href="…/files/12345/" (direct download links)
    for m in re.finditer(r'href="[^"]*?/files/(\d+)(?:/[^"]*?)?"', html):
        fid = int(m.group(1))
        if fid not in ids:
            ids.append(fid)
    return ids


def _download_canvas_file(canvas_url: str, token: str, file_id: int, dest_dir: str):
    """
    Fetch file metadata from Canvas, then download the file to dest_dir.
    Returns {"path": ..., "filename": ..., "content_type": ...} or None on error.
    """
    try:
        meta_url = f"{canvas_url}/api/v1/files/{file_id}"
        req = urllib.request.Request(
            meta_url,
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            meta = json.loads(resp.read())

        filename     = meta.get("display_name", f"file_{file_id}")
        download_url = meta.get("url", "")
        content_type = meta.get("content-type", "")
        size         = meta.get("size", 0)

        if not download_url:
            return None
        if size > 10 * 1024 * 1024:   # skip files > 10 MB
            return None

        # S3 pre-signed URL — no auth header needed
        dest_path = os.path.join(dest_dir, filename)
        urllib.request.urlretrieve(download_url, dest_path)
        return {"path": dest_path, "filename": filename, "content_type": content_type}

    except Exception:
        return None


def _extract_pdf_text(path: str, limit: int = 5000) -> str:
    """Extract text from a PDF. Tries PyMuPDF first, falls back to raw scan."""
    try:
        import fitz  # PyMuPDF
        doc  = fitz.open(path)
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
        return text[:limit]
    except ImportError:
        pass
    except Exception:
        return ""

    # Fallback: scan raw bytes for PDF text operators
    try:
        with open(path, "rb") as f:
            raw = f.read()
        text_parts = []
        for m in re.finditer(rb'\(([^)]{1,500})\)\s*Tj', raw):
            try:
                text_parts.append(m.group(1).decode("latin-1"))
            except Exception:
                pass
        return " ".join(text_parts)[:limit]
    except Exception:
        return ""


def _render_pdf_pages_coregraphics(pdf_path: str, max_pages: int, tmp_dir: str,
                                    scale: float = 1.5) -> list:
    """Render PDF pages to PNG files using macOS CoreGraphics (no pip required).
    Returns list of file paths for successfully rendered pages."""
    import ctypes
    import ctypes.util

    try:
        cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        cg = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
        ii = ctypes.CDLL('/System/Library/Frameworks/ImageIO.framework/ImageIO')

        class CGRect(ctypes.Structure):
            _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double),
                        ('width', ctypes.c_double), ('height', ctypes.c_double)]

        cf.CFURLCreateFromFileSystemRepresentation.restype  = ctypes.c_void_p
        cf.CFURLCreateFromFileSystemRepresentation.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_ssize_t, ctypes.c_bool]
        cf.CFStringCreateWithCString.restype  = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFRelease.argtypes = [ctypes.c_void_p]

        cg.CGPDFDocumentCreateWithURL.restype  = ctypes.c_void_p
        cg.CGPDFDocumentCreateWithURL.argtypes = [ctypes.c_void_p]
        cg.CGPDFDocumentGetNumberOfPages.restype  = ctypes.c_size_t
        cg.CGPDFDocumentGetNumberOfPages.argtypes = [ctypes.c_void_p]
        cg.CGPDFDocumentGetPage.restype  = ctypes.c_void_p
        cg.CGPDFDocumentGetPage.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        cg.CGPDFPageGetBoxRect.restype  = CGRect
        cg.CGPDFPageGetBoxRect.argtypes = [ctypes.c_void_p, ctypes.c_int]
        cg.CGColorSpaceCreateDeviceRGB.restype  = ctypes.c_void_p
        cg.CGColorSpaceCreateDeviceRGB.argtypes = []
        cg.CGBitmapContextCreate.restype  = ctypes.c_void_p
        cg.CGBitmapContextCreate.argtypes = [
            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t,
            ctypes.c_size_t, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_uint32]
        cg.CGContextSetRGBFillColor.argtypes = [
            ctypes.c_void_p, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
        cg.CGContextFillRect.argtypes   = [ctypes.c_void_p, CGRect]
        cg.CGContextScaleCTM.argtypes   = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]
        cg.CGContextDrawPDFPage.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cg.CGBitmapContextCreateImage.restype  = ctypes.c_void_p
        cg.CGBitmapContextCreateImage.argtypes = [ctypes.c_void_p]

        ii.CGImageDestinationCreateWithURL.restype  = ctypes.c_void_p
        ii.CGImageDestinationCreateWithURL.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
        ii.CGImageDestinationAddImage.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        ii.CGImageDestinationFinalize.restype  = ctypes.c_bool
        ii.CGImageDestinationFinalize.argtypes = [ctypes.c_void_p]

        p    = pdf_path.encode('utf-8')
        url  = cf.CFURLCreateFromFileSystemRepresentation(None, p, len(p), False)
        doc  = cg.CGPDFDocumentCreateWithURL(url)
        cf.CFRelease(url)
        if not doc:
            return []

        n_pages = min(int(cg.CGPDFDocumentGetNumberOfPages(doc)), max_pages)
        cs      = cg.CGColorSpaceCreateDeviceRGB()
        kpng    = cf.CFStringCreateWithCString(None, b'public.png', 0x08000100)
        paths   = []

        for pg in range(1, n_pages + 1):
            page = cg.CGPDFDocumentGetPage(doc, pg)
            if not page:
                continue
            rect = cg.CGPDFPageGetBoxRect(page, 1)  # kCGPDFMediaBox=1
            w    = max(1, int(rect.width  * scale))
            h    = max(1, int(rect.height * scale))

            ctx  = cg.CGBitmapContextCreate(None, w, h, 8, w * 4, cs, 1)  # kCGImageAlphaLast=1
            if not ctx:
                continue

            bg = CGRect(0, 0, w, h)
            cg.CGContextSetRGBFillColor(ctx, 1.0, 1.0, 1.0, 1.0)
            cg.CGContextFillRect(ctx, bg)
            cg.CGContextScaleCTM(ctx, scale, scale)
            cg.CGContextDrawPDFPage(ctx, page)

            img = cg.CGBitmapContextCreateImage(ctx)
            if not img:
                continue

            out_path  = os.path.join(tmp_dir, f'page_{pg}.png')
            out_bytes = out_path.encode('utf-8')
            ourl      = cf.CFURLCreateFromFileSystemRepresentation(
                None, out_bytes, len(out_bytes), False)
            dest = ii.CGImageDestinationCreateWithURL(ourl, kpng, 1, None)
            ii.CGImageDestinationAddImage(dest, img, None)
            ok = ii.CGImageDestinationFinalize(dest)
            cf.CFRelease(ourl)

            if ok and os.path.exists(out_path):
                paths.append(out_path)

        return paths

    except Exception:
        return []


def _pdf_page_images(pdf_path: str, max_pages: int = 4) -> list:
    """Return a list of (image_bytes, media_type) for up to max_pages pages.

    Strategy 1: PyMuPDF (fitz) if installed — renders at 1.5× scale to PNG.
    Strategy 2: macOS CoreGraphics via ctypes — stdlib-only, always available on Mac.
    """
    # ── Strategy 1: PyMuPDF ──────────────────────────────────────────────────
    try:
        import fitz  # noqa: PyMuPDF
        doc    = fitz.open(pdf_path)
        mat    = fitz.Matrix(1.5, 1.5)
        images = []
        for page in list(doc)[:max_pages]:
            pix = page.get_pixmap(matrix=mat)
            images.append((pix.tobytes('png'), 'image/png'))
        doc.close()
        if images:
            return images
    except ImportError:
        pass
    except Exception:
        pass

    # ── Strategy 2: CoreGraphics via ctypes ──────────────────────────────────
    tmp_dir = tempfile.mkdtemp()
    try:
        paths = _render_pdf_pages_coregraphics(pdf_path, max_pages, tmp_dir)
        images = []
        for p in paths:
            with open(p, 'rb') as f:
                images.append((f.read(), 'image/png'))
        return images
    except Exception:
        return []
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _call_anthropic_syllabus_vision(api_key: str, course: str, page_images: list) -> dict:
    """Extract structured syllabus data from page images using Claude vision API."""
    _empty = {
        "professor":   {"name": None, "email": None, "phone": None,
                        "office_hours": None, "office_location": None},
        "assignments": [],
        "exams":       [],
        "policies":    {"attendance": None, "late_work": None},
        "schedule":    [],
    }

    content = []
    for img_bytes, media_type in page_images:
        content.append({
            "type": "image",
            "source": {
                "type":       "base64",
                "media_type": media_type,
                "data":       base64.b64encode(img_bytes).decode('ascii'),
            }
        })

    content.append({
        "type": "text",
        "text": (
            f"The images above are pages from a course syllabus for: {course}\n\n"
            "Extract the syllabus information and respond with a single valid JSON object "
            "(no markdown fences):\n"
            "{\n"
            '  "professor": {"name":..., "email":..., "phone":..., "office_hours":..., "office_location":...},\n'
            '  "assignments": [{"name":..., "weight":"25% or 100 pts", "description":...}],\n'
            '  "exams": [{"name":..., "date":"YYYY-MM-DD or text as written", "topics":...}],\n'
            '  "policies": {"attendance":"2-4 sentence summary or null", "late_work":"...or null"},\n'
            '  "schedule": [{"week":"Week 1 (Jan 13)", "topics":"..."}]\n'
            "}\n"
            "Rules:\n"
            "- assignments: ONLY graded items with weights (no readings)\n"
            "- exams: ALL exams AND quizzes found anywhere in the syllabus\n"
            "- schedule: [] if no weekly schedule found\n"
            "- null for any field not present in the syllabus"
        )
    })

    payload = json.dumps({
        "model":      "claude-sonnet-4-20250514",
        "max_tokens": 2048,
        "messages":   [{"role": "user", "content": content}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload, method="POST",
    )
    req.add_header("Content-Type",      "application/json")
    req.add_header("x-api-key",         api_key)
    req.add_header("anthropic-version", "2023-06-01")

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
            text   = result["content"][0]["text"].strip()
            if text.startswith("```"):
                parts = text.split("```")
                text  = parts[1] if len(parts) > 1 else text
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return _empty
    except Exception:
        return _empty


def _extract_docx_text(path: str) -> str:
    """Extract text from a .docx file. Tries python-docx first, falls back to XML."""
    try:
        import docx
        doc  = docx.Document(path)
        text = "\n".join(p.text for p in doc.paragraphs)
        return text[:5000]
    except ImportError:
        pass
    except Exception:
        return ""

    # Fallback: parse word/document.xml directly
    try:
        ns   = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        with zipfile.ZipFile(path, "r") as z:
            xml_bytes = z.read("word/document.xml")
        root  = ET.fromstring(xml_bytes)
        texts = [el.text for el in root.iter(f"{{{ns}}}t") if el.text]
        return " ".join(texts)[:5000]
    except Exception:
        return ""


def _extract_pptx_text(path: str) -> str:
    """Extract text from a .pptx file using stdlib only."""
    try:
        ns   = "http://schemas.openxmlformats.org/drawingml/2006/main"
        texts = []
        with zipfile.ZipFile(path, "r") as z:
            slide_names = sorted(
                n for n in z.namelist()
                if re.match(r"ppt/slides/slide\d+\.xml", n)
            )
            for sname in slide_names:
                xml_bytes = z.read(sname)
                root = ET.fromstring(xml_bytes)
                for el in root.iter(f"{{{ns}}}t"):
                    if el.text:
                        texts.append(el.text)
        return " ".join(texts)[:5000]
    except Exception:
        return ""


def _extract_file_text(path: str, filename: str, content_type: str) -> str:
    """Dispatcher: extract text from a downloaded file based on type."""
    fn  = filename.lower()
    ct  = content_type.lower()

    if fn.endswith(".pdf") or "pdf" in ct:
        return _extract_pdf_text(path)
    if fn.endswith(".docx") or "word" in ct or "openxmlformats" in ct:
        return _extract_docx_text(path)
    if fn.endswith(".pptx") or "presentation" in ct:
        return _extract_pptx_text(path)
    if fn.endswith((".txt", ".md", ".csv")):
        try:
            with open(path, "r", errors="replace") as f:
                return f.read(5000)
        except Exception:
            return ""
    return ""


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

  /* ── Syllabi page ──────────────────────────────────────────────────────── */
  .syl-card {
    background: white; border: 1px solid #e2e8f0; border-radius: 10px;
    margin: 0 16px 16px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.06);
  }
  .syl-card-hdr {
    background: #1e40af; color: white; padding: 12px 16px;
    display: flex; align-items: center; justify-content: space-between;
  }
  .syl-card-title { font-size: 15px; font-weight: 700; margin-bottom: 2px; }
  .syl-card-meta  { font-size: 12px; opacity: 0.75; }
  .syl-body { padding: 16px; }
  .syl-section { margin-bottom: 16px; }
  .syl-section:last-child { margin-bottom: 0; }
  .syl-section-hdr {
    font-size: 11px; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.07em; color: #4338ca; margin-bottom: 8px;
  }
  .syl-table {
    width: 100%; border-collapse: collapse; font-size: 13px;
  }
  .syl-table th {
    text-align: left; padding: 5px 10px; background: #f1f5f9;
    font-size: 11px; font-weight: 600; color: #64748b;
    border-bottom: 1px solid #e2e8f0;
  }
  .syl-table td {
    padding: 6px 10px; border-bottom: 1px solid #f1f5f9;
    vertical-align: top; color: #1e293b;
  }
  .syl-table tr:last-child td { border-bottom: none; }
  .syl-prof-grid {
    display: grid; grid-template-columns: 110px 1fr;
    gap: 4px 12px; font-size: 13px; background: #f8fafc;
    border-radius: 8px; padding: 10px 14px;
  }
  .syl-prof-key { color: #64748b; font-weight: 500; }
  .syl-prof-val { color: #1e293b; }
  .syl-policy-text {
    font-size: 14px; color: #1e293b; line-height: 1.6;
    background: #f8fafc; border-radius: 8px; padding: 10px 14px;
    border-left: 3px solid #c7d2fe; white-space: pre-wrap; word-break: break-word;
    margin-bottom: 8px;
  }
  .btn-reupload {
    background: transparent; border: 1px solid rgba(255,255,255,0.5);
    color: white; padding: 4px 10px; border-radius: 6px;
    font-size: 12px; font-weight: 600; cursor: pointer;
    font-family: inherit; transition: background 0.15s; white-space: nowrap;
  }
  .btn-reupload:hover { background: rgba(255,255,255,0.15); }

  /* ── Syllabus upload modal ─────────────────────────────────────────────── */
  .syl-upload-modal {
    background: white; border-radius: 14px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.25);
    width: min(460px, 94vw); overflow: hidden;
  }
  .syl-upload-hdr {
    background: #1e40af; color: white; padding: 16px 20px;
    font-size: 17px; font-weight: 700;
  }
  .syl-upload-body { padding: 20px; }
  .syl-upload-ftr {
    display: flex; justify-content: flex-end; gap: 10px;
    padding: 14px 20px; border-top: 1px solid #e2e8f0; background: #f8fafc;
  }
  .file-drop-zone {
    border: 2px dashed #cbd5e1; border-radius: 8px;
    padding: 20px; text-align: center; color: #64748b;
    font-size: 14px; cursor: pointer; transition: all 0.15s;
    background: #f8fafc; word-break: break-all;
  }
  .file-drop-zone:hover { border-color: #1e40af; color: #1e40af; background: #eff6ff; }
  .syl-upload-status { margin-top: 10px; font-size: 13px; min-height: 0; }
  .syl-upload-status.loading { color: #1e40af; }
  .syl-upload-status.error   { color: #dc2626; }
  .syl-upload-status.success { color: #16a34a; }
  .syl-course-other {
    margin-top: 8px; width: 100%; box-sizing: border-box;
    padding: 8px 10px; border: 1px solid #cbd5e1; border-radius: 8px;
    font-family: inherit; font-size: 14px; color: #1e293b;
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

<div class="main" id="assignments-main">
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

<!-- Syllabi page (hidden until Syllabi tab is active) -->
<div id="syllabi-main" class="main" style="display:none">
  <div id="syllabi-content"></div>
</div>

<!-- Syllabus Upload Modal -->
<div class="overlay" id="syl-upload-overlay" onclick="sylUploadOverlayClick(event)">
  <div class="syl-upload-modal">
    <div class="syl-upload-hdr">Upload Syllabus PDF</div>
    <div class="syl-upload-body">
      <div class="field">
        <label for="syl-course-sel">Course</label>
        <select id="syl-course-sel" onchange="handleCourseSelChange()" style="width:100%;box-sizing:border-box;padding:8px 10px;border:1px solid #cbd5e1;border-radius:8px;font-family:inherit;font-size:14px;color:#1e293b;background:white;"></select>
        <input type="text" id="syl-course-other" class="syl-course-other" placeholder="Enter course name" style="display:none">
      </div>
      <div class="field">
        <label>Syllabus PDF</label>
        <div class="file-drop-zone" id="file-drop-zone" onclick="document.getElementById('syl-file-input').click()">Click to select PDF</div>
        <input type="file" id="syl-file-input" accept=".pdf" style="display:none" onchange="handleFileSelect(this)">
      </div>
      <div class="syl-upload-status" id="syl-upload-status"></div>
    </div>
    <div class="syl-upload-ftr">
      <button class="btn-cancel" onclick="closeSylUploadModal()">Cancel</button>
      <button class="btn-save" id="btn-syl-upload" onclick="doUploadSyllabus()">Upload &amp; Analyze</button>
    </div>
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
  if (e.key === 'Escape') { closeModal(); closeSummaryModal(); closeSylUploadModal(); }
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
let _syllabiData    = {};
let _syllabiCount   = 0;
let _selectedSylFile = null;
let _assignmentList  = [];

function shortLabel(course) {
  return course
    .replace(/^(Online|Hybrid)\s*-\s*/i, '')
    .replace(/\s*\([A-Z]{2,10}\d{3,5}[A-Z]?\s*-\s*\d+\)\s*$/i, '')
    .trim() || course;
}

function buildTabs(list) {
  _assignmentList = list;
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
    { id: 'completed', display: 'Completed',   count: done.length },
    { id: 'syllabi',   display: 'Syllabi',     count: _syllabiCount },
  ];

  if (activeTab !== 'syllabi' && !tabs.some(t => t.id === activeTab)) activeTab = 'all';

  const bar = document.getElementById('tabs-bar');
  bar.innerHTML = tabs.map(t =>
    `<button class="tab${activeTab === t.id ? ' active' : ''}" data-tab-id="${esc(t.id)}" title="${esc(t.title || t.display)}"><span class="tab-label">${esc(t.display)}</span><span class="tab-count">${t.count}</span></button>`
  ).join('');

  bar.querySelectorAll('.tab').forEach(btn => {
    btn.addEventListener('click', () => {
      activeTab = btn.getAttribute('data-tab-id');
      if (activeTab === 'syllabi') {
        bar.querySelectorAll('.tab').forEach(b =>
          b.classList.toggle('active', b.getAttribute('data-tab-id') === 'syllabi')
        );
        showPage('syllabi');
      } else {
        showPage('assignments');
        loadAssignments();
      }
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
  if (activeTab === 'syllabi') return;

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

// ── Syllabi ─────────────────────────────────────────────────────────────────
function showPage(page) {
  const assignMain = document.getElementById('assignments-main');
  const sylMain    = document.getElementById('syllabi-main');
  if (page === 'syllabi') {
    assignMain.style.display = 'none';
    sylMain.style.display    = 'block';
    loadSyllabi();
  } else {
    assignMain.style.display = 'block';
    sylMain.style.display    = 'none';
  }
}

async function loadSyllabi() {
  try {
    const resp = await fetch('/api/syllabi');
    const data = await resp.json();
    _syllabiData  = data.syllabi || {};
    _syllabiCount = Object.keys(_syllabiData).length;
    const badge = document.querySelector('[data-tab-id="syllabi"] .tab-count');
    if (badge) badge.textContent = _syllabiCount;
    renderSyllabiPage(_syllabiData);
  } catch(e) {
    document.getElementById('syllabi-content').innerHTML =
      '<div class="empty"><p>Error loading syllabi.</p></div>';
  }
}

function renderSyllabiPage(syllabi) {
  const container = document.getElementById('syllabi-content');
  const count = Object.keys(syllabi).length;
  if (count === 0) {
    container.innerHTML =
      '<div class="empty"><h2>No syllabi yet</h2>' +
      '<p>Upload a syllabus PDF to extract course information.</p>' +
      '<button class="btn-add" onclick="openSylUploadModal()" style="margin-top:12px">+ Upload First Syllabus</button></div>';
    return;
  }
  let html =
    '<div style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px 8px;">' +
    `<span style="font-size:13px;color:#64748b">${count} course${count !== 1 ? 's' : ''}</span>` +
    '<button class="btn-add" onclick="openSylUploadModal()">+ Upload Syllabus</button></div>';
  for (const [course, sdata] of Object.entries(syllabi)) {
    html += renderSyllabus(course, sdata);
  }
  container.innerHTML = html;
}

function renderSyllabus(course, data) {
  const prof = data.professor || {};
  const uploadedDate = data.uploaded_at
    ? new Date(data.uploaded_at).toLocaleDateString('en-US', {month:'short',day:'numeric',year:'numeric'})
    : '';
  const filename = data.filename || '';

  let html = '<div class="syl-card">' +
    '<div class="syl-card-hdr">' +
    '<div><div class="syl-card-title">' + esc(shortLabel(course)) + '</div>' +
    '<div class="syl-card-meta">' + (uploadedDate ? esc(uploadedDate) + ' &bull; ' : '') + esc(filename) + '</div></div>' +
    '<button class="btn-reupload" onclick="openSylUploadModal(\'' + esc(course).replace(/'/g,'\\\'') + '\')">Re-upload</button>' +
    '</div><div class="syl-body">';

  // Professor
  const profFields = [
    ['Name', prof.name], ['Email', prof.email], ['Phone', prof.phone],
    ['Office Hours', prof.office_hours], ['Location', prof.office_location],
  ].filter(([,v]) => v);
  if (profFields.length) {
    html += '<div class="syl-section"><div class="syl-section-hdr">Professor</div>' +
      '<div class="syl-prof-grid">';
    for (const [label, value] of profFields) {
      html += '<span class="syl-prof-key">' + esc(label) + '</span><span class="syl-prof-val">' + esc(value) + '</span>';
    }
    html += '</div></div>';
  }

  // Graded Assignments
  const graded = data.graded_assignments || [];
  if (graded.length) {
    html += '<div class="syl-section"><div class="syl-section-hdr">Graded Assignments</div>' +
      '<table class="syl-table"><thead><tr><th>Name</th><th>Weight</th><th>Description</th></tr></thead><tbody>';
    for (const item of graded) {
      html += '<tr><td>' + esc(item.name||'') + '</td><td>' + esc(item.weight||'') + '</td><td>' + esc(item.description||'') + '</td></tr>';
    }
    html += '</tbody></table></div>';
  }

  // Exams & Quizzes
  const exams = data.exams || [];
  if (exams.length) {
    html += '<div class="syl-section"><div class="syl-section-hdr">Exams &amp; Quizzes</div>' +
      '<table class="syl-table"><thead><tr><th>Name</th><th>Date</th><th>Topics</th></tr></thead><tbody>';
    for (const item of exams) {
      html += '<tr><td>' + esc(item.name||'') + '</td><td>' + esc(item.date||'') + '</td><td>' + esc(item.topics||'') + '</td></tr>';
    }
    html += '</tbody></table></div>';
  }

  // Policies
  const policies = data.policies || {};
  if (policies.attendance || policies.late_work) {
    html += '<div class="syl-section"><div class="syl-section-hdr">Policies</div>';
    if (policies.attendance) {
      html += '<div class="summary-section-label" style="margin-top:8px">Attendance</div>' +
        '<div class="syl-policy-text">' + esc(policies.attendance) + '</div>';
    }
    if (policies.late_work) {
      html += '<div class="summary-section-label" style="margin-top:8px">Late Work</div>' +
        '<div class="syl-policy-text">' + esc(policies.late_work) + '</div>';
    }
    html += '</div>';
  }

  // Schedule
  const schedule = data.schedule || [];
  if (schedule.length) {
    html += '<div class="syl-section"><div class="syl-section-hdr">Schedule</div>' +
      '<table class="syl-table"><thead><tr><th>Week</th><th>Topics</th></tr></thead><tbody>';
    for (const item of schedule) {
      html += '<tr><td>' + esc(String(item.week||'')) + '</td><td>' + esc(item.topics||'') + '</td></tr>';
    }
    html += '</tbody></table></div>';
  }

  html += '</div></div>';
  return html;
}

// ── Syllabus upload modal ────────────────────────────────────────────────────
function openSylUploadModal(preselectedCourse) {
  const sel = document.getElementById('syl-course-sel');
  sel.innerHTML = '<option value="">-- Select Course --</option>';
  const courses = [...new Set(_assignmentList.map(a => a.course))].sort();
  for (const c of courses) {
    const opt = new Option(shortLabel(c), c);
    opt.title = c;
    sel.add(opt);
  }
  const other = new Option('Other (type manually)', '__other__');
  sel.add(other);

  if (preselectedCourse) sel.value = preselectedCourse;

  _selectedSylFile = null;
  document.getElementById('file-drop-zone').textContent = 'Click to select PDF';
  document.getElementById('syl-upload-status').innerHTML = '';
  document.getElementById('syl-upload-status').className = 'syl-upload-status';
  document.getElementById('syl-file-input').value = '';
  document.getElementById('syl-course-other').style.display = 'none';
  document.getElementById('btn-syl-upload').disabled = false;
  document.getElementById('syl-upload-overlay').classList.add('open');
}

function closeSylUploadModal() {
  document.getElementById('syl-upload-overlay').classList.remove('open');
  _selectedSylFile = null;
}

function sylUploadOverlayClick(e) {
  if (e.target.id === 'syl-upload-overlay') closeSylUploadModal();
}

function handleCourseSelChange() {
  const sel = document.getElementById('syl-course-sel');
  document.getElementById('syl-course-other').style.display =
    sel.value === '__other__' ? 'block' : 'none';
}

function handleFileSelect(input) {
  if (input.files && input.files[0]) {
    _selectedSylFile = input.files[0];
    document.getElementById('file-drop-zone').textContent = _selectedSylFile.name;
  }
}

async function doUploadSyllabus() {
  const sel = document.getElementById('syl-course-sel');
  let course = sel.value;
  if (course === '__other__') {
    course = document.getElementById('syl-course-other').value.trim();
  }
  if (!course) { alert('Please select or enter a course name.'); return; }
  if (!_selectedSylFile) { alert('Please select a PDF file.'); return; }

  const status = document.getElementById('syl-upload-status');
  status.className = 'syl-upload-status loading';
  status.textContent = 'Uploading and analyzing\u2026 This may take a moment.';
  document.getElementById('btn-syl-upload').disabled = true;

  const reader = new FileReader();
  reader.onload = async (e) => {
    const base64 = e.target.result.split(',')[1];
    try {
      const resp = await fetch('/api/syllabi/upload', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ course: course, filename: _selectedSylFile.name, pdf_base64: base64 })
      });
      const data = await resp.json();
      if (!resp.ok || data.error) {
        status.className = 'syl-upload-status error';
        status.textContent = 'Error: ' + (data.error || 'Upload failed');
        document.getElementById('btn-syl-upload').disabled = false;
        return;
      }
      status.className = 'syl-upload-status success';
      status.textContent = 'Syllabus analyzed successfully!';
      setTimeout(() => { closeSylUploadModal(); loadSyllabi(); }, 800);
    } catch(err) {
      status.className = 'syl-upload-status error';
      status.textContent = 'Network error: ' + err.message;
      document.getElementById('btn-syl-upload').disabled = false;
    }
  };
  reader.onerror = () => {
    status.className = 'syl-upload-status error';
    status.textContent = 'Error reading file.';
    document.getElementById('btn-syl-upload').disabled = false;
  };
  reader.readAsDataURL(_selectedSylFile);
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

// Prime the syllabi tab count on startup
fetch('/api/syllabi').then(r=>r.json()).then(d=>{
  _syllabiCount = Object.keys(d.syllabi||{}).length;
  const t = document.querySelector('[data-tab-id="syllabi"] .tab-count');
  if (t) t.textContent = _syllabiCount;
}).catch(()=>{});
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

        elif path == "/api/syllabi":
            self._send_json({"syllabi": load_syllabi()})

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
            aid = path[len("/api/summarize/"):]
            # Load full data to get assignments and announcements together
            try:
                with open(DATA_FILE, "r") as f:
                    full_data = json.load(f)
            except (IOError, json.JSONDecodeError):
                full_data = {}
            assignments      = full_data.get("assignments", [])
            all_announcements = full_data.get("announcements", {})

            assignment = next((a for a in assignments if a["id"] == aid), None)
            if assignment is None:
                self._send_json({"error": "Assignment not found."}, 404)
                return

            course_announcements = all_announcements.get(assignment.get("course", ""), [])

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

            canvas_url   = config.get("canvas_url", "").strip()
            canvas_token = config.get("token", "").strip()

            file_texts = []
            description_html = assignment.get("description_html", "")
            if description_html and canvas_url and canvas_token and assignment.get("canvas_id"):
                tmp_dir = tempfile.mkdtemp()
                try:
                    file_ids = _extract_canvas_file_ids(description_html)
                    for fid in file_ids[:5]:
                        try:
                            info = _download_canvas_file(canvas_url, canvas_token, fid, tmp_dir)
                            if info:
                                text = _extract_file_text(
                                    info["path"], info["filename"], info["content_type"]
                                )
                                if text.strip():
                                    file_texts.append({"filename": info["filename"], "text": text})
                        except Exception:
                            pass
                finally:
                    shutil.rmtree(tmp_dir, ignore_errors=True)

            try:
                summary = _call_anthropic(api_key, assignment, course_announcements, file_texts=file_texts)
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

        elif path == "/api/syllabi/upload":
            length  = int(self.headers.get("Content-Length", 0))
            tmp_dir = None
            try:
                body     = json.loads(self.rfile.read(length))
                course   = str(body.get("course",   "")).strip()
                filename = str(body.get("filename", "file.pdf")).strip()
                b64data  = str(body.get("pdf_base64", ""))

                if not course:
                    self._send_json({"error": "Course name is required."}, 400)
                    return
                if not b64data:
                    self._send_json({"error": "No file data received."}, 400)
                    return

                # Strip data URL prefix if present (data:application/pdf;base64,...)
                if "," in b64data:
                    b64data = b64data.split(",", 1)[1]

                config_path = os.path.join(SCRIPT_DIR, "canvas_config.json")
                try:
                    with open(config_path) as f:
                        config = json.load(f)
                except (IOError, json.JSONDecodeError):
                    self._send_json(
                        {"error": "canvas_config.json not found or invalid."}, 500)
                    return

                api_key = config.get("anthropic_key", "").strip()
                if not api_key:
                    self._send_json(
                        {"error": "No Anthropic API key configured. "
                                  "Add \"anthropic_key\": \"<your-key>\" to canvas_config.json."}, 400)
                    return

                pdf_bytes = base64.b64decode(b64data)
                tmp_dir   = tempfile.mkdtemp()
                pdf_path  = os.path.join(tmp_dir, filename)
                with open(pdf_path, "wb") as f:
                    f.write(pdf_bytes)

                pdf_text = _extract_pdf_text(pdf_path, limit=15000)
                if pdf_text.strip():
                    extracted = _call_anthropic_syllabus(api_key, course, pdf_text)
                else:
                    # Scanned/image-only PDF — render pages and use Claude vision
                    page_images = _pdf_page_images(pdf_path, max_pages=4)
                    if not page_images:
                        self._send_json(
                            {"error": "Could not extract content from this PDF. "
                                      "It may be a scanned PDF that could not be rendered."}, 400)
                        return
                    extracted = _call_anthropic_syllabus_vision(api_key, course, page_images)
                record = {
                    "course":      course,
                    "filename":    filename,
                    "uploaded_at": _dt.datetime.now().strftime("%Y-%m-%d %I:%M %p").lstrip("0"),
                    "professor":   extracted.get("professor",   {}),
                    "graded_assignments": extracted.get("assignments", []),
                    "exams":       extracted.get("exams",       []),
                    "policies":    extracted.get("policies",    {}),
                    "schedule":    extracted.get("schedule",    []),
                }
                syllabi = load_syllabi()
                syllabi[course] = record
                save_syllabi(syllabi)
                self._send_json({"ok": True, "course": course, "record": record})

            except Exception as err:
                self._send_json({"error": str(err)}, 500)
            finally:
                if tmp_dir:
                    shutil.rmtree(tmp_dir, ignore_errors=True)

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
