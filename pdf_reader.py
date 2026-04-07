"""
PDF Reader — Extract and parse academic journal PDFs using Claude.
Output JSON: title, publication_date, journal_name, body, references.
"""

import json
import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ── Config ──────────────────────────────────────────────────────────────────

PDF_DIR = Path(__file__).parent

# Supported providers: "bedrock" | "anthropic"
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "bedrock")


# ── Claude Client ────────────────────────────────────────────────────────────

def get_client():
    if LLM_PROVIDER == "bedrock":
        return anthropic.AnthropicBedrock(
            aws_access_key=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            aws_session_token=os.getenv("AWS_SESSION_TOKEN") or None,
            aws_region=os.getenv("AWS_REGION", "us-east-1"),
        )
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def get_model():
    if LLM_PROVIDER == "bedrock":
        return os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
    return os.getenv("ANTHROPIC_MODEL_ID", "claude-3-5-sonnet-20241022")


# ── PDF Text Extraction ──────────────────────────────────────────────────────

def extract_text(pdf_path: Path) -> str:
    """Extract all text from a PDF file."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ImportError("pypdf is required: pip install pypdf")

    reader = PdfReader(str(pdf_path))
    pages = [page.extract_text() or "" for page in reader.pages]
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    if not text.strip():
        raise ValueError(f"No extractable text in {pdf_path.name} — it may be a scanned/image PDF")
    return text


# ── Claude Parsing ───────────────────────────────────────────────────────────

def parse_with_claude(raw_text: str, client: anthropic.Anthropic) -> dict:
    """
    Use Claude to extract structured fields from raw journal text.
    Returns a dict with: title, publication_date, journal_name, body, references.
    """
    prompt = f"""You are parsing an academic journal article. Extract the following fields from the text below and return ONLY a JSON object with no other text:

{{
  "title": "full article title",
  "publication_date": "publication date as written (e.g. 2024, January 2024, etc.) or null if not found",
  "journal_name": "name of the journal or null if not found",
  "volume": "volume number as a string or null if not found",
  "issue": "issue number as a string or null if not found",
  "type": "Article, Editorial, Perspective, Review Article, Other",
  "tags": ["3-6 topic tags describing the subject areas, methods, and domains covered"],
  "summary": "2-3 sentence plain-English summary of the article's key findings or arguments, written for a general audience",
  "body": "the full main text of the article, excluding the references section. Preserve paragraph breaks as double newlines (\\n\\n) — this spacing is important for text-to-speech pacing and pronunciation.",
  "references": ["each reference as its own string in this array"]
}}

For "tags": use short lowercase phrases (e.g. "machine learning", "drug discovery", "graph neural networks").
For "summary": focus on what was found or argued, not just what the article is about.
For "body": keep all paragraph breaks intact using \\n\\n between paragraphs. Do not collapse or merge paragraphs.
For "references": split them as individual entries. If there are no references, use an empty array.

--- ARTICLE TEXT ---
{raw_text}
"""

    message = client.messages.create(
        model=get_model(),
        max_tokens=8096,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()
    if not raw:
        raise ValueError("Claude returned an empty response — article text may be too short or unreadable")
    return json.loads(raw)


# ── Main Functions ───────────────────────────────────────────────────────────

def process_pdf(pdf_path: Path, client: anthropic.Anthropic = None) -> dict:
    """Extract and parse a single PDF. Returns structured dict."""
    if client is None:
        client = get_client()

    print(f"[pdf] Reading {pdf_path.name} …")
    raw_text = extract_text(pdf_path)
    print(f"[pdf] Extracted {len(raw_text):,} characters — parsing with Claude …")

    result = parse_with_claude(raw_text, client)
    print(f"[pdf] Done: {result.get('title', '(no title)')}")

    from db import init_db, insert_article
    init_db()
    row_id = insert_article(result, source_file=str(pdf_path))
    print(f"[pdf] Saved to DB (id={row_id})")

    archive_dir = pdf_path.parent.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    pdf_path.rename(archive_dir / pdf_path.name)
    print(f"[pdf] Archived → {archive_dir / pdf_path.name}")

    return result


def process_folder(folder: Path = PDF_DIR, progress_cb=None) -> dict[str, dict]:
    """Process all PDFs in a folder. Returns {filename: parsed_dict}."""
    pdfs = sorted(folder.glob("*.pdf"))
    if not pdfs:
        print(f"[pdf] No PDF files found in {folder}")
        return {}

    n = len(pdfs)
    client = get_client()
    results = {}
    for i, pdf_path in enumerate(pdfs, 1):
        if progress_cb:
            progress_cb(int(i / n * 100), f"Parsing {i}/{n}: {pdf_path.stem[:50]}")
        try:
            results[pdf_path.name] = process_pdf(pdf_path, client)
        except Exception as e:
            print(f"[pdf] ERROR processing {pdf_path.name}: {e}")
    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys

    parser = argparse.ArgumentParser(description="Parse academic PDFs into structured JSON")
    sub = parser.add_subparsers(dest="cmd")

    fp = sub.add_parser("file", help="Process a single PDF")
    fp.add_argument("path", help="Path to the PDF file")

    dp = sub.add_parser("folder", help="Process all PDFs in a folder")
    dp.add_argument("path", nargs="?", default=str(PDF_DIR), help="Folder to scan (default: script directory)")

    args = parser.parse_args()

    if args.cmd == "file":
        pdf = Path(args.path)
        result = process_pdf(pdf)
        out = pdf.with_suffix(".json")
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[pdf] Saved JSON → {out}")

    elif args.cmd == "folder":
        results = process_folder(Path(args.path))
        for name, data in results.items():
            out = Path(args.path) / Path(name).with_suffix(".json").name
            out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[pdf] Saved JSON → {out}")

    else:
        parser.print_help()
        sys.exit(1)
