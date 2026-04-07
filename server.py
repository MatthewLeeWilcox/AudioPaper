"""
AudioPaper API Server
FastAPI backend for the AudioPaper frontend.
"""

import asyncio
import json
import logging
import sys
import traceback
import uuid
from pathlib import Path
from typing import Optional

# Windows: SelectorEventLoop doesn't support subprocess — Playwright requires ProactorEventLoop
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from db import init_db, get_article

# ── Logging setup ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / "server.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("audiopaper")

# Quieten noisy uvicorn/httpx loggers to INFO
logging.getLogger("uvicorn.access").setLevel(logging.INFO)
logging.getLogger("uvicorn.error").setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ── Paths & DB ────────────────────────────────────────────────────────────────

AUDIO_DIR = Path(__file__).parent / "output" / "audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
log.debug("Audio dir: %s", AUDIO_DIR)

DEMO_DIR = Path(__file__).parent / "demos"
DEMO_DIR.mkdir(parents=True, exist_ok=True)
log.debug("Demo dir: %s", DEMO_DIR)

PDF_DIR = Path(__file__).parent / "pdfs"
log.debug("PDF dir: %s", PDF_DIR)

log.info("Initialising database…")
init_db()
log.info("Database ready.")

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="AudioPaper API", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# In-memory job store
jobs: dict[str, dict] = {}

VOICES = {
    "af_heart":   {"label": "Heart",    "accent": "American", "gender": "Female"},
    "af_bella":   {"label": "Bella",    "accent": "American", "gender": "Female"},
    "af_sarah":   {"label": "Sarah",    "accent": "American", "gender": "Female"},
    "af_nicole":  {"label": "Nicole",   "accent": "American", "gender": "Female"},
    "am_adam":    {"label": "Adam",     "accent": "American", "gender": "Male"},
    "am_michael": {"label": "Michael",  "accent": "American", "gender": "Male"},
    "bf_emma":    {"label": "Emma",     "accent": "British",  "gender": "Female"},
    "bf_isabella":{"label": "Isabella", "accent": "British",  "gender": "Female"},
    "bm_george":  {"label": "George",   "accent": "British",  "gender": "Male"},
    "bm_lewis":   {"label": "Lewis",    "accent": "British",  "gender": "Male"},
}


# ── Request logging middleware ────────────────────────────────────────────────

@app.middleware("http")
async def log_requests(request: Request, call_next):
    log.info("→ %s %s", request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception as exc:
        log.error("Unhandled exception on %s %s\n%s", request.method, request.url.path, traceback.format_exc())
        raise
    log.info("← %s %s %d", request.method, request.url.path, response.status_code)
    return response


# ── Models ────────────────────────────────────────────────────────────────────

class IngestRequest(BaseModel):
    url: str

class EpisodeRequest(BaseModel):
    article_ids: list[int]
    narrator: str = "bm_george"
    reader: str = "af_heart"
    name: str = ""

class DemoRequest(BaseModel):
    voice: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def new_job(meta: dict) -> str:
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status": "running", "progress": "", **meta}
    log.info("[job:%s] Created — %s", job_id, meta)
    return job_id

def job_progress(job_id: str, msg: str):
    jobs[job_id]["progress"] = msg
    log.info("[job:%s] %s", job_id, msg)

def job_done(job_id: str, **extra):
    jobs[job_id]["status"] = "done"
    jobs[job_id].update(extra)
    log.info("[job:%s] Done — %s", job_id, extra)

def job_error(job_id: str, exc: Exception):
    jobs[job_id]["status"] = "error"
    jobs[job_id]["error"] = str(exc)
    log.error("[job:%s] Error — %s\n%s", job_id, exc, "".join(traceback.format_exception(exc)))


# ── Routes ────────────────────────────────────────────────────────────────────

COOKIES_FILE = Path(__file__).parent / "cookies.json"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/cookies")
async def upload_cookies(file: UploadFile = File(...), background_tasks: BackgroundTasks = None):
    """Accept a Netscape/JSON cookie file for institutional access."""
    data = await file.read()
    try:
        cookies = json.loads(data)
        if not isinstance(cookies, list):
            raise ValueError("Expected a JSON array of cookies")
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(400, f"Invalid cookie file: {e}")
    COOKIES_FILE.write_bytes(data)
    log.info("Cookies saved: %d cookies", len(cookies))

    # Auto-trigger a paywalled retry job if there are locked articles
    from db import get_paywalled_articles
    paywalled = get_paywalled_articles()
    job_id = None
    if paywalled and background_tasks is not None:
        job_id = new_job({"type": "retry_paywalled", "total": len(paywalled), "progress_pct": 0})

        def _run():
            try:
                from scraper import retry_paywalled

                def cb(pct, msg):
                    jobs[job_id]["progress"] = msg
                    jobs[job_id]["progress_pct"] = pct

                asyncio.run(retry_paywalled(PDF_DIR, progress_cb=cb))
                job_done(job_id, progress="Retry complete")
            except Exception as e:
                job_error(job_id, e)

        background_tasks.add_task(_run)
        log.info("[job:%s] Auto-started retry for %d paywalled articles", job_id, len(paywalled))

    return {"saved": len(cookies), "paywalled_count": len(paywalled), "retry_job_id": job_id}


@app.post("/retry-paywalled")
async def retry_paywalled_endpoint(background_tasks: BackgroundTasks):
    """Manually trigger a retry of all paywalled articles."""
    if not COOKIES_FILE.exists():
        raise HTTPException(400, "No cookies loaded — upload cookies.json first")

    from db import get_paywalled_articles
    paywalled = get_paywalled_articles()
    if not paywalled:
        return {"message": "No paywalled articles to retry", "job_id": None}

    job_id = new_job({"type": "retry_paywalled", "total": len(paywalled), "progress_pct": 0})

    def _run():
        try:
            from scraper import retry_paywalled

            def cb(pct, msg):
                jobs[job_id]["progress"] = msg
                jobs[job_id]["progress_pct"] = pct

            asyncio.run(retry_paywalled(PDF_DIR, progress_cb=cb))
            job_done(job_id, progress="Retry complete")
        except Exception as e:
            job_error(job_id, e)

    background_tasks.add_task(_run)
    log.info("[job:%s] Manual retry for %d paywalled articles", job_id, len(paywalled))
    return {"job_id": job_id, "total": len(paywalled)}


@app.delete("/cookies")
def delete_cookies():
    """Remove saved institutional cookies."""
    if COOKIES_FILE.exists():
        COOKIES_FILE.unlink()
        log.info("Cookies deleted")
    return {"deleted": True}


@app.get("/cookies")
def cookies_status():
    """Check whether a cookies file is loaded."""
    if not COOKIES_FILE.exists():
        return {"loaded": False, "count": 0}
    try:
        cookies = json.loads(COOKIES_FILE.read_text())
        return {"loaded": True, "count": len(cookies)}
    except Exception:
        return {"loaded": False, "count": 0}


@app.get("/voices")
def get_voices():
    log.debug("Returning %d voices", len(VOICES))
    return {"voices": [{"id": k, **v} for k, v in VOICES.items()]}


@app.post("/voices/demo")
async def voice_demo(req: DemoRequest, background_tasks: BackgroundTasks):
    """Generate (or return cached) a demo clip for a voice."""
    voice = req.voice
    log.info("Voice demo requested: %s", voice)

    if voice not in VOICES:
        log.warning("Unknown voice requested: %s", voice)
        raise HTTPException(400, f"Unknown voice: {voice}")

    demo_path = DEMO_DIR / f"demo_{voice}.wav"
    if demo_path.exists():
        log.debug("Demo cache hit: %s", demo_path.name)
        return {"audio_file": demo_path.name}

    job_id = new_job({"voice": voice, "type": "demo"})

    def _run():
        log.debug("[job:%s] Synthesising demo for voice %s", job_id, voice)
        try:
            from tts import synthesize
            label = VOICES[voice]["label"]
            text = (
                f"Hello, my name is {label}. "
                f"I'm an AudioPaper narrator. "
                f"Would you like me to be the Narrator or Reader?"
            )
            log.debug("[job:%s] Calling synthesize(), output: %s", job_id, demo_path)
            synthesize(text, voice=voice, output_path=demo_path)
            job_done(job_id, audio_file=demo_path.name)
        except Exception as e:
            job_error(job_id, e)

    background_tasks.add_task(_run)
    return {"job_id": job_id}


@app.post("/upload-pdfs")
async def upload_pdfs(background_tasks: BackgroundTasks, files: list[UploadFile] = File(...)):
    """Accept one or more PDF uploads, save them, parse with Claude, and store in DB."""
    if not files:
        raise HTTPException(400, "No files provided")

    saved = []
    upload_dir = PDF_DIR / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    for f in files:
        if not f.filename.lower().endswith(".pdf"):
            continue
        dest = upload_dir / f.filename
        dest.write_bytes(await f.read())
        saved.append(dest)

    if not saved:
        raise HTTPException(400, "No valid PDF files found")

    job_id = new_job({"type": "upload_pdfs", "total": len(saved), "progress_pct": 0})

    def _run():
        try:
            from pdf_reader import process_pdf, get_client
            client = get_client()
            n = len(saved)
            for i, pdf_path in enumerate(saved, 1):
                jobs[job_id]["progress"] = f"Parsing {i}/{n}: {pdf_path.stem[:50]}"
                jobs[job_id]["progress_pct"] = int(i / n * 100)
                try:
                    process_pdf(pdf_path, client)
                except Exception as e:
                    log.error("[job:%s] Failed to parse %s: %s", job_id, pdf_path.name, e)
            job_done(job_id, progress="Done")
        except Exception as e:
            job_error(job_id, e)

    background_tasks.add_task(_run)
    log.info("[job:%s] Uploading %d PDFs", job_id, len(saved))
    return {"job_id": job_id, "count": len(saved)}


@app.post("/ingest")
async def ingest(req: IngestRequest, background_tasks: BackgroundTasks):
    """Scrape a Nature issue URL, download PDFs, parse and store in DB."""
    log.info("Ingest requested: %s", req.url)
    job_id = new_job({"url": req.url, "type": "ingest", "pdfs_done": 0, "parsed_done": 0, "progress_pct": 0})

    def _run():
        try:
            from scraper import scrape_issue
            from pdf_reader import process_folder

            # Derive output folder from URL
            parts = req.url.rstrip("/").split("/")
            try:
                journal = parts[3]
                vol     = parts[5]
                issue   = parts[7]
                folder  = PDF_DIR / f"{journal}-vol{vol}-issue{issue}"
            except IndexError:
                log.warning("[job:%s] Could not parse URL segments, using default folder", job_id)
                folder = PDF_DIR / "download"

            log.info("[job:%s] Output folder: %s", job_id, folder)

            def scrape_cb(pct: int, msg: str):
                jobs[job_id]["progress"]     = msg
                jobs[job_id]["progress_pct"] = pct // 2   # 0-50%
                log.info("[job:%s] scrape %d%%  %s", job_id, pct, msg)

            job_progress(job_id, "Downloading PDFs…")
            log.debug("[job:%s] Calling scrape_issue(%s, %s)", job_id, req.url, folder)
            # Run Playwright in its own event loop so it gets a fresh ProactorEventLoop
            # on Windows (FastAPI's event loop doesn't support subprocesses).
            asyncio.run(scrape_issue(req.url, folder, progress_cb=scrape_cb))

            pdfs = list(folder.glob("*.pdf"))
            jobs[job_id]["pdfs_done"] = len(pdfs)
            log.info("[job:%s] Scrape complete — %d PDFs downloaded", job_id, len(pdfs))

            def parse_cb(pct: int, msg: str):
                jobs[job_id]["progress"]     = msg
                jobs[job_id]["progress_pct"] = 50 + pct // 2   # 50-100%
                log.info("[job:%s] parse %d%%  %s", job_id, pct, msg)

            log.debug("[job:%s] Calling process_folder(%s)", job_id, folder)
            process_folder(folder, progress_cb=parse_cb)

            job_done(job_id, progress="Complete")
        except Exception as e:
            job_error(job_id, e)

    background_tasks.add_task(_run)
    return {"job_id": job_id}


@app.get("/job/{job_id}")
def get_job(job_id: str):
    if job_id not in jobs:
        log.warning("Job not found: %s", job_id)
        raise HTTPException(404, "Job not found")
    log.debug("[job:%s] Status poll — %s", job_id, jobs[job_id].get("status"))
    return jobs[job_id]


@app.get("/articles")
def get_articles(
    journal: Optional[str] = None,
    volume:  Optional[str] = None,
    issue:   Optional[str] = None,
    type:    Optional[str] = None,
    search:  Optional[str] = None,
):
    log.debug("Articles query — journal=%s volume=%s issue=%s type=%s search=%s",
              journal, volume, issue, type, search)
    from db import list_articles, search as db_search
    if search:
        articles = db_search(search)
    else:
        articles = list_articles(journal=journal, volume=volume, issue=issue, article_type=type)

    slim = [{k: v for k, v in a.items() if k != "body"} for a in articles]
    log.debug("Articles query returned %d results", len(slim))
    return {"articles": slim, "total": len(slim)}


@app.get("/articles/{article_id}")
def get_article_full(article_id: int):
    log.debug("Article detail requested: id=%d", article_id)
    a = get_article(article_id)
    if not a:
        log.warning("Article not found: id=%d", article_id)
        raise HTTPException(404, "Article not found")
    return a


@app.get("/filters")
def get_filters():
    """Return distinct journal/volume/issue/type values for filter dropdowns."""
    log.debug("Filters requested")
    import sqlite3
    from db import DB_PATH
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    def distinct(col):
        rows = conn.execute(
            f'SELECT DISTINCT "{col}" FROM articles WHERE "{col}" IS NOT NULL ORDER BY "{col}"'
        ).fetchall()
        return [r[0] for r in rows if r[0]]

    result = {
        "journals": distinct("journal_name"),
        "volumes":  distinct("volume"),
        "issues":   distinct("issue"),
        "types":    distinct("type"),
    }
    conn.close()
    log.debug("Filters: journals=%d volumes=%d issues=%d types=%d",
              len(result["journals"]), len(result["volumes"]),
              len(result["issues"]),   len(result["types"]))
    return result


@app.post("/episode")
async def produce_episode(req: EpisodeRequest, background_tasks: BackgroundTasks):
    """Produce a podcast episode from selected article IDs."""
    log.info("Episode requested — %d articles, narrator=%s reader=%s",
             len(req.article_ids), req.narrator, req.reader)

    if not req.article_ids:
        log.warning("Episode request with no articles")
        raise HTTPException(400, "No articles selected")

    job_id = new_job({"type": "episode", "total": len(req.article_ids), "progress_pct": 0})

    def _run():
        try:
            from db import get_article
            from tts import produce_episode as tts_produce

            articles = [get_article(i) for i in req.article_ids]
            missing  = [i for i, a in zip(req.article_ids, articles) if not a]
            articles = [a for a in articles if a]
            if missing:
                log.warning("[job:%s] Article IDs not found in DB: %s", job_id, missing)
            log.info("[job:%s] Loaded %d articles", job_id, len(articles))

            def progress_cb(pct: int, msg: str):
                jobs[job_id]["progress"]     = msg
                jobs[job_id]["progress_pct"] = pct
                log.info("[job:%s] %d%%  %s", job_id, pct, msg)

            log.debug("[job:%s] Calling tts_produce(), narrator=%s reader=%s",
                      job_id, req.narrator, req.reader)
            path = tts_produce(
                articles,
                narrator_voice=req.narrator,
                reader_voice=req.reader,
                progress_cb=progress_cb,
            )
            log.info("[job:%s] Audio produced: %s", job_id, path)

            chapters_path = path.with_suffix(".chapters.json")

            import json as _json
            meta_path = path.with_suffix(".meta.json")
            meta_path.write_text(_json.dumps({"name": req.name or path.stem}))

            job_done(
                job_id,
                audio_file=path.name,
                chapters_file=chapters_path.name if chapters_path.exists() else None,
            )
        except Exception as e:
            job_error(job_id, e)

    background_tasks.add_task(_run)
    return {"job_id": job_id}


@app.get("/episodes")
def list_episodes():
    """List all produced episodes with their metadata and chapters."""
    import json as _json
    eps = []
    for wav in sorted(AUDIO_DIR.glob("episode_*.wav"), key=lambda p: p.stat().st_mtime, reverse=True):
        stem = wav.stem
        meta_path  = AUDIO_DIR / f"{stem}.meta.json"
        chap_path  = AUDIO_DIR / f"{stem}.chapters.json"

        name = stem
        if meta_path.exists():
            try:
                name = _json.loads(meta_path.read_text()).get("name") or stem
            except Exception:
                pass

        chapters = []
        if chap_path.exists():
            try:
                chapters = _json.loads(chap_path.read_text())
            except Exception:
                pass

        eps.append({
            "audio_file": wav.name,
            "name": name,
            "chapters": chapters,
        })
    log.debug("Episodes list: %d found", len(eps))
    return {"episodes": eps}


@app.get("/demos/{filename}")
def serve_demo(filename: str):
    path = DEMO_DIR / filename
    log.debug("Demo requested: %s (exists=%s)", filename, path.exists())
    if not path.exists():
        raise HTTPException(404, "Demo file not found")
    return FileResponse(str(path), media_type="audio/wav")


@app.get("/audio/{filename}")
def serve_audio(filename: str):
    path = AUDIO_DIR / filename
    log.debug("Audio requested: %s (exists=%s)", filename, path.exists())
    if not path.exists():
        log.warning("Audio file not found: %s", path)
        raise HTTPException(404, "Audio file not found")
    return FileResponse(str(path), media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn
    log.info("Starting AudioPaper API on http://0.0.0.0:8000")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
