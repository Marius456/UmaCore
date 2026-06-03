# Implementation Plan

Replace the `aiohttp`-based HTTP client in `UmaMoeAPIScraper` with Playwright to bypass Cloudflare's `browser_proof_required` (403) protection on the uma.moe API.

Uma.moe has deployed Cloudflare anti-bot protection that returns HTTP 403 with `{"error":"browser_proof_required"}` — this is a JavaScript challenge that requires a real browser to execute. Neither `cloudscraper`, `curl_cffi`, nor raw `requests` can solve it. The solution is to use **Playwright** (a full browser automation library) to visit uma.moe first (solving the Cloudflare challenge), then fetch the API data from the same browser context. The browser instance is kept alive across scrape calls to avoid the ~5s launch overhead on every request, and cleaned up on bot shutdown.

[Types]

No new types or data structures. The existing `UmaMoeAPIScraper` class interface is unchanged — all method signatures remain identical.

[Files]

**Modified:**
1. **`requirements.txt`** — Replace `aiohttp` with `playwright>=1.60.0`
2. **`scrapers/umamoe_api_scraper.py`** — Complete rewrite of HTTP layer: use Playwright's async API to navigate a real browser, solving Cloudflare challenges automatically
3. **`main.py`** — Add import and cleanup call for the shared Playwright browser instance on shutdown

No new files created. The test script `_test_scraper.py` was temporary and deleted.

[Functions]

**`scrapers/umamoe_api_scraper.py`:**

- **Module-level `_get_browser()`** — Lazily create a shared Playwright Chromium browser instance
- **Module-level `_close_browser()`** — Close the shared browser (called from `main.py` shutdown)
- **`_fetch_json_via_page(page, url)`** — Navigate to URL, extract JSON from `<pre>` tag, return parsed dict
- **`_fetch_api_data(year, month)`** — Create a new browser context, visit uma.moe (solves Cloudflare), then navigate to API endpoint
- **`scrape()`** — Updated to call `_fetch_api_data()` instead of `_fetch_month()` — same structure, different transport
- **`_parse_api_data()`** — Unchanged

**`main.py`:**
- Added import: `from scrapers.umamoe_api_scraper import _close_browser as _close_playwright`
- Added cleanup call: `await _close_playwright()` in the `finally` block

[Classes]

No new classes. `UmaMoeAPIScraper` unchanged in name, inheritance, and public API.

[Dependencies]

- **Added**: `playwright>=1.60.0` to `requirements.txt`
- **Kept**: `aiohttp` can remain since it's harmless and potentially used elsewhere
- **Browser binary**: Chromium is automatically downloaded by `playwright install chromium` (~180MB)

[Testing]

- **Manual validation completed**: Ran `UmaMoeAPIScraper('489097586').scrape()` which returned 28 members, correct rank fields (monthly_rank=932, yesterday_rank=436), correct data date for current day (2026-06-01 representing June 1 results)
- Next step: Run `/force_check` on BonBon club to verify integration end-to-end

[Implementation Order]

1. Install playwright: `pip install playwright`
2. Install Chromium: `python -m playwright install chromium`
3. Modify `requirements.txt` — add `playwright>=1.60.0`
4. Rewrite `scrapers/umamoe_api_scraper.py` — replace aiohttp with Playwright async API
5. Update `main.py` — add Playwright browser cleanup on shutdown
6. Test: `python _test_scraper.py` — confirmed working
7. Clean up test file