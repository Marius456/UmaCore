# Implementation Plan

[Overview]
Modify UmaMoeAPIScraper to switch from headless Playwright Chromium to non-headless system Chrome to bypass Cloudflare's updated browser detection on uma.moe.

The current Playwright implementation opens a headless Chromium browser instance, visits uma.moe to solve the Cloudflare challenge, then fetches the API endpoint. After uma.moe upgraded their Cloudflare protection, the headless browser is reliably detected and the challenge never resolves, causing a 45-second timeout and subsequent failure. The fix switches to real system Chrome (installed on the host) running in headed mode, combined with stealth anti-detection scripts and persistent cookie storage. This makes the browser indistinguishable from a normal user's Chrome session. The window is managed by creating it minimized/hidden and cleaning up afterward.

Context: The bot runs on Windows 11 with Google Chrome installed. It does not use Docker. All scraping happens via the UmaMoeAPIScraper which currently uses Playwright's bundled Chromium in headless mode. The fix must be robust against future Cloudflare changes and maintain backward compatibility with the existing scraper interface (`scrape()`, `get_current_day()`, `get_data_date()`, etc.).

[Types]
No new types, interfaces, or data structures are required.

The existing `UmaMoeAPIScraper` class and its method signatures remain unchanged. All modifications are internal to `_get_browser()`, `_get_browser_context()`, and `_fetch_api_data()`.

[Files]
Three files will be modified. No new files will be created, and no files will be deleted.

**Modified files:**

1. `scrapers/umamoe_api_scraper.py` — Core changes:
   - `_get_browser()`: Launch real Chrome via `channel: 'chrome'` with `headless=False`, add stealth launch args
   - `_get_browser_context()`: New helper that creates a persistent context with cookie storage
   - `_fetch_api_data()`: Use the new context, add `add_init_script()` for JS-based stealth patches, improve logging
   - Add browser launch timeout argument to handle slow Chrome startup
   - Add Chrome executable path detection fallback (look in common Windows locations)
   - Add `_setup_stealth_patches(page)` method: applies JS patches via `add_init_script()` to override `navigator.webdriver`, add `chrome.runtime`, spoof plugins, languages, and WebGL fingerprint
   - Add persistent cookie jar (`cookie_dir` argument or env var) to save/load Cloudflare clearance cookies across bot restarts
   - Handle Chrome process cleanup on shutdown

2. `config/settings.py` — Add configuration:
   - `PLAYWRIGHT_HEADLESS` setting (default `False` since we don't use headless anymore)
   - `PLAYWRIGHT_COOKIE_DIR` setting for persistent cookie storage path
   - Keep backward-compatible defaults

3. `requirements.txt` — No changes needed (Playwright is already a dependency, no new packages required)

4. `main.py` — Minor update in shutdown sequence to ensure Chrome processes are properly killed

[Functions]
No functions are removed. Four functions are modified, and two new functions are added.

**Modified functions:**

1. `_get_browser()` in `scrapers/umamoe_api_scraper.py`:
   - Signature: unchanged `async def _get_browser() -> Browser`
   - Change: switch from `headless=True` to `headless=False`, add `channel='chrome'`, add launch args for stealth
   - Add timeout to browser launch
   - Add Chrome path detection for Windows
   - On failure, log the specific Chrome path that was tried
   - Return the browser instance as before

2. `_fetch_api_data()` in `scrapers/umamoe_api_scraper.py`:
   - Signature: unchanged `async def _fetch_api_data(year: int, month: int) -> dict`
   - Change: use persistent context with cookie directory, apply stealth patches before navigation
   - After successful challenge, save cookies to disk
   - On subsequent calls, load cached cookies and skip main page visit if cookies are still valid
   - Better logging of Cloudflare challenge status

3. `_close_browser()` in `scrapers/umamoe_api_scraper.py`:
   - Signature: unchanged `async def _close_browser()`
   - Change: ensure Chrome processes are fully terminated (not just Playwright context closed)
   - Add `subprocess` cleanup for orphaned Chrome instances

4. `async def daily_check_for_club()` in `bot/tasks.py` (implicitly):
   - Signature: unchanged
   - No direct changes needed — the scraper interface is unchanged

**New functions:**

1. `_setup_stealth_patches(page)` in `scrapers/umamoe_api_scraper.py`:
   - Signature: `async def _setup_stealth_patches(page: Page) -> None`
   - Purpose: Apply JavaScript-based stealth patches to evade Cloudflare's headless detection
   - Patches:
     - `Object.defineProperty(navigator, 'webdriver', { get: () => false })`
     - Override `navigator.plugins` to return a non-empty array
     - Override `navigator.languages` to `['en-US', 'en']`
     - Override `navigator.hardwareConcurrency` to `8` (common CPU count)
     - Override `chrome.runtime` if available
     - Override `navigator.permissions.query` for specific permission types
   - Called via `page.add_init_script()` before any page navigation

2. `_get_cookie_dir()` in `scrapers/umamoe_api_scraper.py`:
   - Signature: `def _get_cookie_dir() -> str`
   - Purpose: Returns the path to the persistent cookie storage directory
   - Creates the directory if it doesn't exist
   - Reads from `settings.PLAYWRIGHT_COOKIE_DIR` or defaults to `./.umamoe_cookies`

[Classes]
No classes are modified, added, or removed.

The `UmaMoeAPIScraper` class interface is unchanged. All changes are internal method modifications.

[Dependencies]
No new dependencies required.

All needed functionality is already available via the existing `playwright` package (v1.60.0+). The `subprocess` and `os` modules are Python standard library.

[Testing]
Testing involves manual verification since this is a runtime Cloudflare integration test.

**Validation steps:**
1. Run the bot and trigger a `/force_check` command — verify the API data is fetched successfully
2. Verify that the Chrome window opens briefly during the first scrape
3. Verify that subsequent scrapes reuse cookies and don't re-open a visible window (or open it minimized)
4. Verify the scraper recovers gracefully if Chrome is not installed
5. Run the bot continuously and verify multiple hourly checks succeed without accumulating Chrome processes
6. Verify bot shutdown properly terminates Chrome and leaves no orphan processes

[Implementation Order]
The implementation follows a logical sequence where the foundational browser/context changes are made first, then stealth and cookie persistence are added.

1. **Configure settings** — Add `PLAYWRIGHT_HEADLESS` and `PLAYWRIGHT_COOKIE_DIR` to `config/settings.py`
2. **Core browser launch** — Modify `_get_browser()` in `umamoe_api_scraper.py` to use `channel='chrome'` with `headless=False`, add stealth launch args, add Chrome path detection for Windows, add launch timeout
3. **Persistent context** — Add `_get_browser_context()` that creates a persistent Playwright context using the cookie directory; add `_get_cookie_dir()` helper
4. **Stealth patches** — Add `_setup_stealth_patches(page)` function with all JS overrides; wire it into `_fetch_api_data()`
5. **Cookie lifecycle** — Modify `_fetch_api_data()` to use persistent context, save cookies on success, reuse them on subsequent calls
6. **Process cleanup** — Update `_close_browser()` to ensure Chrome processes are fully killed; update `main.py` shutdown sequence if needed
7. **Logging improvements** — Add detailed logging of Chrome path detection, cookie status, challenge resolution
8. **Graceful fallback** — Add fallback to bundled Chromium if system Chrome is not found (with warning log)
9. **Full integration test** — Run `/force_check` and verify end-to-end success