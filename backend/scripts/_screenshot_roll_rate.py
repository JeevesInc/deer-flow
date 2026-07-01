"""Zoom in on the roll-rate matrix panels for a clear screenshot."""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

OUT = Path("C:/Jeeves/redshift-bot/tmp/cap_markets_dashboard")
URL = "http://localhost:3001/d-solo/jeeves-cap-markets/capital-markets?orgId=1&panelId=701&__feature.dashboardSceneSolo=true"

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1400, "height": 700}, device_scale_factor=1.5)
        page = await ctx.new_page()
        # panelId 701 — count matrix
        await page.goto(URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(4000)
        await page.screenshot(path=str(OUT / "rollrate_count.png"), full_page=False)
        # panelId 702 — pct matrix
        url2 = URL.replace("panelId=701", "panelId=702")
        await page.goto(url2, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(4000)
        await page.screenshot(path=str(OUT / "rollrate_pct.png"), full_page=False)
        await browser.close()
        print("Saved rollrate_count.png and rollrate_pct.png")

asyncio.run(main())
