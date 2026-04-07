"""
Kokoro TTS — NPR-style podcast producer.
A narrator introduces each article from its summary, then a reader voice
reads the full body. All segments are stitched into a single episode WAV.
"""

import os
import wave
import array as arr
from pathlib import Path
from typing import Optional

import numpy as np
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

OUTPUT_DIR = Path(__file__).parent / "output" / "audio"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Voice config ──────────────────────────────────────────────────────────────
# Available voices:
#   American female: af_heart, af_bella, af_sarah, af_nicole
#   American male:   am_adam, am_michael
#   British female:  bf_emma, bf_isabella
#   British male:    bm_george, bm_lewis

NARRATOR_VOICE = "bm_george"   # introduces each article
READER_VOICE   = "af_heart"    # reads the article body
SPEED          = 1.0
SAMPLE_RATE    = 24000          # Kokoro outputs 24 kHz

# Silence durations (in samples at 24 kHz)
PAUSE_SHORT  = int(SAMPLE_RATE * 0.6)   # between narrator intro and article
PAUSE_LONG   = int(SAMPLE_RATE * 1.5)   # between articles


# ── Kokoro pipeline ───────────────────────────────────────────────────────────

_pipelines: dict = {}

def _pipeline(voice: str):
    lang = voice[0]   # 'a' = American, 'b' = British
    if lang not in _pipelines:
        try:
            from kokoro import KPipeline
        except ImportError:
            raise ImportError("kokoro is required: pip install kokoro")
        _pipelines[lang] = KPipeline(lang_code=lang)
    return _pipelines[lang]


# ── Audio helpers ─────────────────────────────────────────────────────────────

def _silence(samples: int) -> np.ndarray:
    return np.zeros(samples, dtype=np.float32)


def _speak(text: str, voice: str, speed: float = SPEED) -> np.ndarray:
    """Synthesize text with Kokoro, return numpy float32 array."""
    segments = []
    for _, _, audio in _pipeline(voice)(text, voice=voice, speed=speed):
        segments.append(audio.flatten())
    return np.concatenate(segments) if segments else np.array([], dtype=np.float32)


def _chunk_text(text: str, max_chars: int = 400) -> list[str]:
    """Split at sentence boundaries."""
    sentences = []
    for line in text.replace("\n\n", "\n").split("\n"):
        for s in line.replace(". ", ".\n").split("\n"):
            s = s.strip()
            if s:
                sentences.append(s)

    chunks, current = [], ""
    for s in sentences:
        if len(current) + len(s) + 1 > max_chars and current:
            chunks.append(current.strip())
            current = s
        else:
            current += " " + s
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _speak_long(text: str, voice: str, speed: float = SPEED) -> np.ndarray:
    """Synthesize long text by chunking and concatenating."""
    parts = [_speak(chunk, voice, speed) for chunk in _chunk_text(text)]
    return np.concatenate(parts) if parts else np.array([], dtype=np.float32)


def _fmt_time(seconds: float) -> str:
    """Format seconds as HH:MM:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _save_wav(audio: np.ndarray, path: Path):
    pcm = [max(-32768, min(32767, int(s * 32767))) for s in audio]
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(arr.array("h", pcm).tobytes())


# ── Claude script writer ──────────────────────────────────────────────────────

def _generate_scripts(titles: list[str]) -> tuple[str, str]:
    """Ask Claude to write a fresh intro and outro for this episode."""
    import anthropic
    from pdf_reader import get_client, get_model

    titles_str = "\n".join(f"- {t}" for t in titles)
    prompt = f"""You are the scriptwriter for AudioPaper, an NPR-style academic podcast.
Write a short, varied intro and outro for today's episode.

Today's articles:
{titles_str}

Rules:
- Always say "AudioPaper" by name in both intro and outro
- Intro: 1 sentences, welcoming user 
- Outro: 1 sentences, warm sign-off
- No markdown, no stage directions — plain spoken text only
- Make it sound different from a generic template each time

Return ONLY a JSON object:
{{"intro": "...", "outro": "..."}}"""

    client = get_client()
    msg = client.messages.create(
        model=get_model(),
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1].lstrip("json").strip()
    import json
    data = json.loads(raw)
    return data["intro"], data["outro"]


# ── Public API ────────────────────────────────────────────────────────────────

def produce_episode(
    articles: list[dict],
    output_path: Optional[Path] = None,
    narrator_voice: str = NARRATOR_VOICE,
    reader_voice: str = READER_VOICE,
    speed: float = SPEED,
    progress_cb=None,   # callable(pct: int, msg: str) or None
) -> Path:
    """
    Produce a full podcast episode from a list of article dicts.

    Structure:
      [intro] → for each article: [narrator intro] [pause] [article body] [pause] → [outro]

    Returns the path to the saved episode WAV.
    """
    def _cb(pct: int, msg: str):
        print(f"[tts] {pct:3d}%  {msg}")
        if progress_cb:
            progress_cb(pct, msg)

    titles = [a.get("title", "Untitled") for a in articles if a.get("body")]
    _cb(0, "Generating intro & outro script…")
    intro_text, outro_text = _generate_scripts(titles)

    if output_path is None:
        from datetime import datetime
        output_path = OUTPUT_DIR / f"episode_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"

    valid = [a for a in articles if a.get("body")]
    n = len(valid)
    # Budget: 0-5 script, 5-10 intro, 10-85 articles (75 pts), 85-90 outro, 90-95 save
    segments: list[np.ndarray] = []
    chapters: list[dict] = []

    def _current_seconds() -> float:
        return sum(len(s) for s in segments) / SAMPLE_RATE

    # ── Intro ────────────────────────────────────────────────────────────────
    _cb(5, "Synthesising intro…")
    segments.append(_speak(intro_text, narrator_voice, speed))
    segments.append(_silence(PAUSE_LONG))

    # ── Articles ─────────────────────────────────────────────────────────────
    for i, article in enumerate(valid, 1):
        title       = article.get("title", "Untitled")
        summary     = article.get("summary", "")
        body        = article.get("body", "")
        source_file = article.get("source_file", "")

        pct = 10 + int((i - 1) / n * 75)
        _cb(pct, f"Article {i}/{n}: {title[:50]}")

        timestamp_sec = _current_seconds()
        chapters.append({
            "index":         i,
            "title":         title,
            "source_file":   source_file,
            "timestamp_sec": round(timestamp_sec, 2),
            "timestamp":     _fmt_time(timestamp_sec),
        })

        intro = f"Article {i}: {title}. {summary}"
        segments.append(_speak(intro, narrator_voice, speed))
        segments.append(_silence(PAUSE_SHORT))
        segments.append(_speak_long(body, reader_voice, speed))
        segments.append(_silence(PAUSE_LONG))

    # ── Outro ────────────────────────────────────────────────────────────────
    _cb(85, "Synthesising outro…")
    segments.append(_speak(outro_text, narrator_voice, speed))

    # ── Save ─────────────────────────────────────────────────────────────────
    _cb(90, "Saving episode…")
    import json
    episode = np.concatenate([s for s in segments if len(s) > 0])
    _save_wav(episode, output_path)
    duration = len(episode) / SAMPLE_RATE
    print(f"[tts] Episode saved → {output_path}  ({duration/60:.1f} min)")

    # Save chapter timestamps alongside the WAV
    chapters_path = output_path.with_suffix(".chapters.json")
    chapters_path.write_text(json.dumps(chapters, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[tts] Chapters saved → {chapters_path}")
    for ch in chapters:
        print(f"       {ch['timestamp']}  {ch['title'][:60]}")

    return output_path


def synthesize(
    text: str,
    voice: str = READER_VOICE,
    speed: float = SPEED,
    output_path: Optional[Path] = None,
) -> Path:
    """Synthesize a single piece of text to a WAV file."""
    if output_path is None:
        import time
        output_path = OUTPUT_DIR / f"tts_{int(time.time())}.wav"
    audio = _speak_long(text, voice, speed)
    _save_wav(audio, output_path)
    print(f"[tts] Saved → {output_path}")
    return output_path


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, json

    parser = argparse.ArgumentParser(description="Kokoro podcast TTS")
    sub = parser.add_subparsers(dest="cmd")

    # Produce a full episode from the DB
    ep = sub.add_parser("episode", help="Produce a podcast episode from the DB")
    ep.add_argument("--journal", default=None, help="Filter by journal name")
    ep.add_argument("--volume", default=None)
    ep.add_argument("--issue", default=None)
    ep.add_argument("--narrator", default=NARRATOR_VOICE)
    ep.add_argument("--reader", default=READER_VOICE)
    ep.add_argument("--speed", type=float, default=SPEED)
    ep.add_argument("--out", default=None, help="Output WAV path")

    # Produce episode from a JSON file
    jp = sub.add_parser("episode-json", help="Produce episode from a JSON file of articles")
    jp.add_argument("path", help="Path to JSON file (single article or array)")
    jp.add_argument("--narrator", default=NARRATOR_VOICE)
    jp.add_argument("--reader", default=READER_VOICE)
    jp.add_argument("--speed", type=float, default=SPEED)
    jp.add_argument("--out", default=None)

    # Speak raw text
    sp = sub.add_parser("speak", help="Speak text directly")
    sp.add_argument("text")
    sp.add_argument("--voice", default=READER_VOICE)
    sp.add_argument("--speed", type=float, default=SPEED)

    # List voices
    sub.add_parser("voices", help="List available voices")

    args = parser.parse_args()

    if args.cmd == "episode":
        from db import init_db, list_articles
        init_db()
        articles = list_articles(journal=args.journal, volume=args.volume, issue=args.issue)
        if not articles:
            print("[tts] No articles found in DB matching filters")
        else:
            produce_episode(
                articles,
                output_path=Path(args.out) if args.out else None,
                narrator_voice=args.narrator,
                reader_voice=args.reader,
                speed=args.speed,
            )

    elif args.cmd == "episode-json":
        data = json.loads(Path(args.path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = [data]
        produce_episode(
            data,
            output_path=Path(args.out) if args.out else None,
            narrator_voice=args.narrator,
            reader_voice=args.reader,
            speed=args.speed,
        )

    elif args.cmd == "speak":
        synthesize(args.text, voice=args.voice, speed=args.speed)

    elif args.cmd == "voices":
        voices = {
            "af_heart": "American Female", "af_bella": "American Female",
            "af_sarah": "American Female", "af_nicole": "American Female",
            "am_adam":  "American Male",   "am_michael": "American Male",
            "bf_emma":  "British Female",  "bf_isabella": "British Female",
            "bm_george": "British Male",   "bm_lewis": "British Male",
        }
        for v, label in voices.items():
            tag = " ← narrator" if v == NARRATOR_VOICE else (" ← reader" if v == READER_VOICE else "")
            print(f"  {v:<15} {label}{tag}")

    else:
        parser.print_help()
