"""
Nature.com Issue Scraper
Scrapes all articles/editorials from a Nature journal issue page and downloads their PDFs.

Usage:
  python scraper.py https://www.nature.com/natmachintell/volumes/8/issues/2
  python scraper.py https://www.nature.com/natmachintell/volumes/8/issues/2 --out pdfs/nmi-8-2
"""

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright

COOKIES_FILE = Path(__file__).parent / "cookies.json"

# ── Config ──────────────────────────────────────────────────────────────────

DEFAULT_OUT = Path(__file__).parent / "pdfs"

# Article types to include (matched against the label on the issue page)
INCLUDE_TYPES = {
    "article",
    "editorial",
    "review article",
    "perspective",
    "correspondence",
    "comment",
    "research briefing",
    "news & views",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def safe_filename(title: str, max_len: int = 80) -> str:
    """Turn an article title into a safe filename."""
    slug = re.sub(r"[^\w\s-]", "", title.lower())
    slug = re.sub(r"[\s_]+", "_", slug).strip("-_")
    return slug[:max_len]


async def _is_paywalled(page) -> bool:
    """Return True if the current page is behind a paywall."""
    return await page.evaluate("""() => {
        // Explicit paywall / purchase UI
        if (document.querySelector('a[data-track-action="purchase article"]')) return true;
        if (document.querySelector('.c-purchasing-option')) return true;
        if (document.querySelector('[data-test="paywall"], .c-article-body--gated')) return true;

        // "Access options" section (the whole block shown when content is gated)
        if (document.querySelector('.c-article-access-options, [data-test="access-options"]')) return true;

        // "Access through your institution" button
        if (document.querySelector('a[data-track-action="access through institution"], a[href*="access-options"]')) return true;
        const allLinks = [...document.querySelectorAll('a')];
        if (allLinks.some(a => /access through your institution/i.test(a.textContent))) return true;

        // SpringerLink purchase link (shown in "Buy this article" box)
        if (document.querySelector('a[href*="link.springer.com"][href*="purchase"], a[href*="/purchase"]')) return true;

        // dc.rights meta tag
        const rights = document.querySelector('meta[name="dc.rights"]');
        if (rights && (rights.content || '').toLowerCase().includes('restricted')) return true;

        // No PDF download button but institution/login gate present
        const hasPDF = !!document.querySelector('a[data-track-action="download pdf"]');
        const hasGate = !!document.querySelector('a[href*="login"], [data-test="institution-access"]');
        if (!hasPDF && hasGate) return true;

        return false;
    }""")


# ── Scraper ──────────────────────────────────────────────────────────────────

async def scrape_issue(issue_url: str, out_dir: Path, progress_cb=None):
    out_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        # Load institutional cookies if available
        if COOKIES_FILE.exists():
            try:
                cookies = json.loads(COOKIES_FILE.read_text())
                await context.add_cookies(cookies)
                print(f"[scraper] Loaded {len(cookies)} cookies from {COOKIES_FILE.name}")
            except Exception as e:
                print(f"[scraper] Warning: could not load cookies: {e}")
        page = await context.new_page()

        # ── Step 1: Load the issue page and collect article links ────────────
        print(f"[scraper] Loading issue page: {issue_url}")
        await page.goto(issue_url, wait_until="domcontentloaded", timeout=60_000)

        # Nature issue pages list articles inside <article> elements or <li> items
        # Each has a type label and a link to the article page
        articles = await page.evaluate("""() => {
            const results = [];
            // Article cards on nature issue pages
            const cards = document.querySelectorAll('article[data-test="article"], li[data-test="article"]');
            cards.forEach(card => {
                const typeEl = card.querySelector('[data-test="article-type"], .c-meta__type, .c-card__label');
                const linkEl = card.querySelector('a[data-track-action="view article"], h3 a, h2 a');
                const titleEl = card.querySelector('h3, h2');
                if (linkEl) {
                    results.push({
                        type: typeEl ? typeEl.textContent.trim().toLowerCase() : "unknown",
                        title: titleEl ? titleEl.textContent.trim() : linkEl.textContent.trim(),
                        href: linkEl.href,
                    });
                }
            });
            return results;
        }""")

        if not articles:
            print("[scraper] No articles found with primary selector — trying fallback …")
            articles = await page.evaluate("""() => {
                const results = [];
                document.querySelectorAll('a[href*="/articles/"]').forEach(a => {
                    const text = a.textContent.trim();
                    if (text.length > 10) {
                        results.push({ type: "article", title: text, href: a.href });
                    }
                });
                // Deduplicate by href
                const seen = new Set();
                return results.filter(r => {
                    if (seen.has(r.href)) return false;
                    seen.add(r.href);
                    return true;
                });
            }""")

        # Filter to desired article types
        to_fetch = [a for a in articles if a["type"] in INCLUDE_TYPES or a["type"] == "unknown"]
        n = len(to_fetch)
        print(f"[scraper] Found {n} articles to download")
        if progress_cb:
            progress_cb(0, f"Found {n} articles — downloading PDFs…")

        # ── Step 2: Visit each article page and download the PDF ─────────────
        base = f"{urlparse(issue_url).scheme}://{urlparse(issue_url).netloc}"

        for i, article in enumerate(to_fetch, 1):
            title = article["title"]
            article_url = article["href"]
            filename = safe_filename(title) + ".pdf"
            out_path = out_dir / filename

            if progress_cb:
                progress_cb(int(i / n * 100), f"Downloading {i}/{n}: {title[:50]}")

            if out_path.exists():
                print(f"[{i}/{n}] Skipping (already exists): {filename}")
                continue

            from db import article_url_exists
            if article_url_exists(article_url):
                print(f"[{i}/{n}] Skipping (already in DB): {title[:60]}")
                continue

            print(f"[{i}/{n}] {title[:60]}")

            try:
                await page.goto(article_url, wait_until="domcontentloaded", timeout=60_000)

                # Check for paywall before attempting download
                if await _is_paywalled(page):
                    print(f"  [PAYWALL] {title[:60]}")
                    # Extract whatever metadata is visible on the paywalled page
                    meta = await page.evaluate("""() => ({
                        publication_date: (document.querySelector('meta[name="citation_date"]') || {}).content || null,
                        journal_name:     (document.querySelector('meta[name="citation_journal_title"]') || {}).content || null,
                        volume:           (document.querySelector('meta[name="citation_volume"]') || {}).content || null,
                        issue:            (document.querySelector('meta[name="citation_issue"]') || {}).content || null,
                        type:             (document.querySelector('meta[name="dc.type"]') || {}).content || null,
                    })""")
                    from db import insert_article
                    insert_article({
                        "title":            title,
                        "publication_date": meta.get("publication_date"),
                        "journal_name":     meta.get("journal_name"),
                        "volume":           meta.get("volume"),
                        "issue":            meta.get("issue"),
                        "type":             meta.get("type") or article.get("type"),
                        "paywalled":        True,
                        "article_url":      article_url,
                    })
                    if progress_cb:
                        progress_cb(int(i / n * 100), f"Paywalled {i}/{n}: {title[:50]}")
                    continue

                # Find the primary article PDF — Nature's download button has a
                # specific data attribute. Ignore supplementary/external PDFs.
                pdf_url = await page.evaluate("""(base) => {
                    // 1. Prefer the explicit "Download PDF" button (most reliable)
                    const btn = document.querySelector('a[data-track-action="download pdf"]');
                    if (btn) return btn.href;

                    // 2. Any same-domain /articles/*.pdf link (not springer static content)
                    const links = [...document.querySelectorAll('a[href$=".pdf"]')];
                    const primary = links.find(a =>
                        a.href.startsWith(base) && /\/articles\/s\d/.test(a.href)
                    );
                    return primary ? primary.href : null;
                }""", base)

                if not pdf_url:
                    # Construct the standard Nature PDF URL from the article slug
                    match = re.search(r"/articles/(s[\w-]+)", article_url)
                    if match:
                        pdf_url = f"{base}/articles/{match.group(1)}.pdf"

                if not pdf_url:
                    print(f"  [!] Could not find PDF link — skipping")
                    continue

                # Click the download button on the article page and intercept the download.
                # Navigating directly to the PDF URL fails because the browser treats it
                # as a file download before Playwright can read the response.
                try:
                    async with page.expect_download(timeout=90_000) as dl_info:
                        # Click the download button — this triggers the download event
                        await page.evaluate("""() => {
                            const btn = document.querySelector('a[data-track-action="download pdf"]');
                            if (btn) btn.click();
                        }""")
                    download = await dl_info.value
                    await download.save_as(out_path)
                    size_kb = out_path.stat().st_size // 1024
                    print(f"  → Saved {size_kb} KB: {filename}")
                except Exception as e:
                    print(f"  [!] Failed: {e}")

            except Exception as e:
                print(f"  [!] Error: {e}")

        await browser.close()

    print(f"\n[scraper] Done. PDFs saved to: {out_dir}")


# ── Retry paywalled ──────────────────────────────────────────────────────────

async def retry_paywalled(out_dir: Path, progress_cb=None):
    """Re-attempt download for all paywalled articles recorded in the DB."""
    from db import get_paywalled_articles, update_article_content
    from pdf_reader import process_pdf

    articles = get_paywalled_articles()
    if not articles:
        print("[scraper] No paywalled articles to retry")
        if progress_cb:
            progress_cb(100, "No paywalled articles to retry")
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    n = len(articles)
    print(f"[scraper] Retrying {n} paywalled article(s)")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        if COOKIES_FILE.exists():
            try:
                cookies = json.loads(COOKIES_FILE.read_text())
                await context.add_cookies(cookies)
                print(f"[scraper] Loaded {len(cookies)} cookies")
            except Exception as e:
                print(f"[scraper] Warning: could not load cookies: {e}")

        page = await context.new_page()
        base = None

        for i, article in enumerate(articles, 1):
            title = article["title"]
            article_url = article["article_url"]
            article_id = article["id"]

            if progress_cb:
                progress_cb(int(i / n * 100), f"Retrying {i}/{n}: {title[:50]}")

            print(f"[{i}/{n}] Retrying: {title[:60]}")

            try:
                await page.goto(article_url, wait_until="domcontentloaded", timeout=60_000)

                if await _is_paywalled(page):
                    print(f"  [STILL PAYWALLED] {title[:60]}")
                    continue

                if base is None:
                    parsed = urlparse(article_url)
                    base = f"{parsed.scheme}://{parsed.netloc}"

                pdf_url = await page.evaluate("""(base) => {
                    const btn = document.querySelector('a[data-track-action="download pdf"]');
                    if (btn) return btn.href;
                    const links = [...document.querySelectorAll('a[href$=".pdf"]')];
                    const primary = links.find(a =>
                        a.href.startsWith(base) && /\/articles\/s\d/.test(a.href)
                    );
                    return primary ? primary.href : null;
                }""", base)

                if not pdf_url:
                    match = re.search(r"/articles/(s[\w-]+)", article_url)
                    if match:
                        pdf_url = f"{base}/articles/{match.group(1)}.pdf"

                if not pdf_url:
                    print(f"  [!] Could not find PDF link — skipping")
                    continue

                filename = safe_filename(title) + ".pdf"
                out_path = out_dir / filename

                try:
                    async with page.expect_download(timeout=90_000) as dl_info:
                        await page.evaluate("""() => {
                            const btn = document.querySelector('a[data-track-action="download pdf"]');
                            if (btn) btn.click();
                        }""")
                    download = await dl_info.value
                    await download.save_as(out_path)
                    print(f"  → Downloaded: {filename}")
                except Exception as e:
                    print(f"  [!] Download failed: {e}")
                    continue

                # Parse the PDF and update the DB record
                try:
                    parsed_data = process_pdf(out_path)
                    update_article_content(article_id, parsed_data)
                    print(f"  → Updated DB record id={article_id}")
                except Exception as e:
                    print(f"  [!] Parse/update failed: {e}")

            except Exception as e:
                print(f"  [!] Error: {e}")

        await browser.close()

    print("[scraper] Retry complete")
    if progress_cb:
        progress_cb(100, "Retry complete")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Download PDFs from a Nature journal issue")
    parser.add_argument("url", help="Nature issue URL (e.g. https://www.nature.com/natmachintell/volumes/8/issues/2)")
    parser.add_argument("--out", default=None, help="Output folder (default: ./pdfs/<journal>-vol-issue)")
    args = parser.parse_args()

    # Auto-generate output folder name from URL if not specified
    if args.out:
        out_dir = Path(args.out)
    else:
        parts = args.url.rstrip("/").split("/")
        # Extract journal/volume/issue from URL path
        try:
            journal = parts[3]
            vol = parts[5]
            issue = parts[7]
            out_dir = DEFAULT_OUT / f"{journal}-vol{vol}-issue{issue}"
        except IndexError:
            out_dir = DEFAULT_OUT / "download"

    asyncio.run(scrape_issue(args.url, out_dir))
