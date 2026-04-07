# AudioPaper

An NPR-style academic podcast generator. Scrapes journal issues from Springer Nature, parses PDFs with Claude, and produces a single WAV episode with a narrator introducing each article and a reader reading the full text — all running locally or on AWS Bedrock.

---

## Architecture

```
podcast/
├── scraper.py       # Playwright — scrapes Nature issue pages, downloads PDFs
├── pdf_reader.py    # PyPDF + Claude — extracts and parses PDF text into structured JSON
├── tts.py           # Kokoro — produces NPR-style episode WAV from parsed articles
├── db.py            # SQLite — stores parsed articles, supports full-text search
├── server.py        # FastAPI — REST API backing the frontend
├── index.html       # React frontend — article browser, episode builder, player
├── pdfs/            # Downloaded PDFs (archived after parsing)
│   └── uploads/     # PDFs uploaded manually via the UI
├── output/
│   └── audio/       # Produced episode WAV files + chapter JSON
├── demos/           # Cached voice preview clips
├── cookies.json     # Institutional access cookies (optional)
└── articles.db      # SQLite database
```

---

## Setup

### 1. Install Python dependencies

```bash
conda activate your_env
pip install -r requirements.txt
playwright install chromium
```

### 2. Set up environment

```bash
copy .env.example .env
# Edit .env — add your keys (see Environment Variables below)
```

### 3. Initialise the database

```bash
python db.py
```

This creates `articles.db` with the required tables. The server also runs this automatically on startup, but it's worth doing once manually to confirm your environment is set up correctly.

### 4. Start the server

```bash
python server.py
# → Running at http://localhost:8000
```

### 5. Open the frontend

Open `index.html` in your browser.

---

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` or `bedrock` | `bedrock` |
| `ANTHROPIC_API_KEY` | Required if `LLM_PROVIDER=anthropic` | — |
| `AWS_ACCESS_KEY_ID` | Required if `LLM_PROVIDER=bedrock` | — |
| `AWS_SECRET_ACCESS_KEY` | Required if `LLM_PROVIDER=bedrock` | — |
| `AWS_SESSION_TOKEN` | Optional for Bedrock with session tokens | — |
| `AWS_REGION` | Bedrock region | `us-east-1` |
| `BEDROCK_MODEL_ID` | Claude model on Bedrock | `us.anthropic.claude-sonnet-4-6` |
| `ANTHROPIC_MODEL_ID` | Claude model on direct API | `claude-3-5-sonnet-20241022` |

---

## How It Works

### 1. Scraping

> **Note:** The scraper is built specifically for Springer Nature journals at `nature.com`. It will not work with other publishers.

Point the scraper at a Nature issue URL in the format `https://www.nature.com/<journal>/volumes/<vol>/issues/<issue>`. It uses Playwright to collect all article links on the issue page, then visits each article to download its PDF. Paywalled articles are skipped and recorded in the DB with their URL so they can be retried later.

```bash
python scraper.py https://www.nature.com/natmachintell/volumes/8/issues/2
```

Or use the **Ingest Journal** field in the frontend.

### 2. Institutional Access (optional)

If you have a subscription, export your browser session cookies to `cookies.json` using a browser extension like Cookie-Editor and upload it via the frontend. AudioPaper will automatically retry all previously paywalled articles with the new cookies.

### 3. PDF Parsing

Each downloaded PDF is extracted with PyPDF and sent to Claude, which returns structured JSON: title, journal, date, volume, issue, type, tags, a plain-English summary, the full body, and references. The result is stored in SQLite.

```bash
python pdf_reader.py folder ./pdfs/natmachintell-vol8-issue2
```

### 4. Producing an Episode

Select articles in the frontend and click **Produce Episode**. The TTS engine uses two Kokoro voices:

- **Narrator** — introduces each article by title and summary
- **Reader** — reads the full article body

Claude also writes a fresh intro and outro for each episode. The output is a single WAV file with a chapter timestamp file saved alongside it.

Available voices:

| ID | Accent | Gender |
|---|---|---|
| `af_heart` | American | Female |
| `af_bella` | American | Female |
| `af_sarah` | American | Female |
| `af_nicole` | American | Female |
| `am_adam` | American | Male |
| `am_michael` | American | Male |
| `bf_emma` | British | Female |
| `bf_isabella` | British | Female |
| `bm_george` | British | Male |
| `bm_lewis` | British | Male |

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Server status |
| GET | `/articles` | List articles (filter by journal, volume, issue, type) |
| GET | `/articles/search` | Full-text search |
| GET | `/articles/meta` | Distinct journals, volumes, issues, types, tags |
| GET | `/article/{id}` | Single article by ID |
| POST | `/ingest` | Scrape a Nature issue URL (returns job ID) |
| POST | `/upload-pdfs` | Upload PDFs directly for parsing (returns job ID) |
| GET | `/job/{id}` | Poll background job status |
| POST | `/cookies` | Upload institutional cookies JSON |
| GET | `/cookies` | Check cookie status |
| DELETE | `/cookies` | Remove saved cookies |
| POST | `/retry-paywalled` | Retry all paywalled articles with loaded cookies |
| GET | `/voices` | List available TTS voices |
| POST | `/voices/demo` | Generate a voice preview clip |
| POST | `/produce` | Produce a podcast episode (returns job ID) |
| GET | `/episodes` | List produced episodes |
| GET | `/audio/{filename}` | Stream a WAV file |

---

## Duplicate Detection

Articles are deduplicated at two points:

1. **Before download** — the scraper checks `article_url` against the DB and skips articles already recorded
2. **Before DB insert** — `insert_article` checks for matching `title + journal_name` and skips if found
