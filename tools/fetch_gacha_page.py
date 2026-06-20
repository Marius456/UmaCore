"""
Fetch and analyze the GameTora gacha page using Playwright (headless browser).
"""
import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.async_api import async_playwright
import json


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, timeout=30000)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()

        print("Loading gametora.com/umamusume/gacha...")
        await page.goto("https://gametora.com/umamusume/gacha", wait_until="networkidle", timeout=60000)
        
        # Wait a bit for JS rendering
        await asyncio.sleep(3)

        # Get full page content
        content = await page.content()
        
        # Save HTML for analysis
        with open("debug_gacha_page.html", "w", encoding="utf-8") as f:
            f.write(content)
        
        # Look for banner data in the rendered DOM
        texts = await page.locator("main").all_text_contents()
        print("Main content texts found:")
        for i, t in enumerate(texts):
            print(f"  [{i}]: {t[:200]}")
        
        # Try to get ALL text from the page
        all_text = await page.locator("body").text_content()
        print(f"\nBody text length: {len(all_text)}")
        
        # Look for banner elements - inspect the DOM structure
        banners = await page.locator("[class*='banner']").all()
        print(f"\nElements with 'banner' in class: {len(banners)}")
        for el in banners:
            html = await el.inner_html()
            print(f"  HTML: {html[:300]}")
        
        # Look for elements containing rate-up data
        rate_elements = await page.locator("text=/0\\.\\d{2}%/").all()
        print(f"\nElements with rate patterns (0.xx%): {len(rate_elements)}")
        for el in rate_elements:
            text = await el.text_content()
            print(f"  Text: {text}")
        
        # Check page title
        title = await page.title()
        print(f"\nPage title: {title}")
        
        # Look for any JSON-like script tags
        scripts = await page.locator("script[type='application/json']").all()
        print(f"\nJSON script tags: {len(scripts)}")
        for s in scripts:
            content = await s.text_content()
            print(f"  Content (first 200): {content[:200]}")
        
        await browser.close()
        print("\nDone. Debug HTML saved to debug_gacha_page.html")

if __name__ == "__main__":
    asyncio.run(main())