"""Take screenshots of the Capital Markets Grafana dashboard for review."""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

OUT = Path("C:/Jeeves/redshift-bot/tmp/cap_markets_dashboard")
OUT.mkdir(parents=True, exist_ok=True)

URL = "http://localhost:3001/d/jeeves-cap-markets/capital-markets?orgId=1&refresh=1m&kiosk"

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1600, "height": 900}, device_scale_factor=1)
        page = await ctx.new_page()
        await page.goto(URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(5000)

        # Find the actual scrolling element (Grafana wraps content in scroll-canvas)
        scroller = await page.evaluate("""() => {
          const el = document.querySelector('.scrollbar-view') ||
                     document.querySelector('[class*="scroll"]') ||
                     document.scrollingElement;
          return el ? el.scrollHeight : document.body.scrollHeight;
        }""")
        print(f"Detected scroller height: {scroller}px")

        # Scroll to bottom in steps to force lazy panels to render
        steps = max(1, scroller // 600)
        for i in range(steps + 1):
            await page.evaluate(f"""() => {{
              const el = document.querySelector('.scrollbar-view') || document.scrollingElement;
              if (el) el.scrollTop = {i * 600};
            }}""")
            await page.wait_for_timeout(700)

        # Re-measure after scrolling triggered renders
        scroller2 = await page.evaluate("""() => {
          const el = document.querySelector('.scrollbar-view') || document.scrollingElement;
          return el ? el.scrollHeight : document.body.scrollHeight;
        }""")
        print(f"Post-scroll height: {scroller2}px")

        # Scroll back to top and take slice screenshots
        await page.evaluate("""() => {
          const el = document.querySelector('.scrollbar-view') || document.scrollingElement;
          if (el) el.scrollTop = 0;
        }""")
        await page.wait_for_timeout(2000)

        slice_h = 900
        for i in range(0, scroller2, slice_h):
            await page.evaluate(f"""() => {{
              const el = document.querySelector('.scrollbar-view') || document.scrollingElement;
              if (el) el.scrollTop = {i};
            }}""")
            await page.wait_for_timeout(900)
            idx = i // slice_h
            await page.screenshot(path=str(OUT / f"slice_{idx:02d}.png"), full_page=False)

        await browser.close()
        print(f"Saved {scroller2 // slice_h + 1} slices to {OUT}")

asyncio.run(main())
