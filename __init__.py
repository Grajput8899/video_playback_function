import logging
import os
import azure.functions as func
from playwright.sync_api import sync_playwright
from datetime import datetime
import time
from azure.storage.blob import BlobServiceClient, BlobClient

# ---------- CONFIG ----------
VIDEO_SELECTOR = os.getenv("VIDEO_SELECTOR", "video")  # CSS selector
MAX_WAIT_SECONDS = int(os.getenv("MAX_WAIT_SECONDS", 600))  # Max seconds to wait for video
BLOB_CONN_STR = os.getenv("BLOB_CONN_STR")  # Azure Blob Storage connection string
BLOB_CONTAINER = os.getenv("BLOB_CONTAINER", "video-playback-logs")
# -----------------------------

def wait_for_video_end(page, selector=VIDEO_SELECTOR, max_wait_seconds=MAX_WAIT_SECONDS):
    """Play video and wait for it to end or timeout."""
    try:
        exists = page.query_selector(selector)
        if not exists:
            return "no_video", None, f"No element matching selector '{selector}' found."

        duration = page.evaluate(f"""() => {{
            const v = document.querySelector("{selector}");
            return v ? (isFinite(v.duration) ? v.duration : -1) : -1;
        }}""")

        started = page.evaluate(f"""() => {{
            const v = document.querySelector("{selector}");
            if (!v) return false;
            try {{
                const p = v.play();
                if (p && typeof p.then === 'function') {{
                    p.catch(e => console.log('play rejected:', e));
                }}
                return true;
            }} catch(e) {{
                return false;
            }}
        }}""")

        if not started:
            return "play_failed", None, "video element exists but play() could not start (autoplay blocked?)."

        # Poll for ended
        elapsed = 0
        poll = 1
        max_wait = int(max_wait_seconds if max_wait_seconds > 0 else 600)
        while elapsed < max_wait:
            ended = page.evaluate(f"""() => {{
                const v = document.querySelector("{selector}");
                return v ? !!v.ended : true;
            }}""")
            if ended:
                played = page.evaluate(f"""() => {{
                    const v = document.querySelector("{selector}");
                    if (!v) return null;
                    return v.currentTime || v.duration || null;
                }}""")
                return "played", float(played) if played is not None else None, "Video ended normally."
            time.sleep(poll)
            elapsed += poll

        return "timeout", None, f"Timed out after {max_wait} seconds waiting for video to end."
    except Exception as e:
        return "error", None, f"Exception while waiting for video end: {e}"

def log_to_blob(log_text, filename=None):
    """Optional: write logs to Azure Blob Storage."""
    if not BLOB_CONN_STR:
        return
    try:
        blob_service_client = BlobServiceClient.from_connection_string(BLOB_CONN_STR)
        container_client = blob_service_client.get_container_client(BLOB_CONTAINER)
        if not container_client.exists():
            container_client.create_container()
        if not filename:
            filename = f"video_log_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
        blob_client = container_client.get_blob_client(filename)
        blob_client.upload_blob(log_text, overwrite=True)
    except Exception as e:
        logging.warning(f"Failed to upload log to blob: {e}")

def main(req: func.HttpRequest) -> func.HttpResponse:
    """Azure Function entry point."""
    url = req.params.get('url')
    runs = int(req.params.get('runs', 5))

    if not url:
        return func.HttpResponse("No URL provided", status_code=400)

    results = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded")
            for i in range(runs):
                start_time = datetime.utcnow()
                status, duration, note = wait_for_video_end(page)
                end_time = datetime.utcnow()
                results.append({
                    "run": i+1,
                    "start_time": start_time.isoformat(),
                    "end_time": end_time.isoformat(),
                    "duration_sec": round(duration, 2) if duration else None,
                    "status": status,
                    "note": note
                })
            browser.close()
    except Exception as e:
        return func.HttpResponse(f"Error running Playwright: {e}", status_code=500)

    # Optional: log to Blob Storage
    log_to_blob(str(results))

    return func.HttpResponse(str(results), status_code=200)
