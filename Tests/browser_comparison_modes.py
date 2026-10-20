"""Exercise mode buttons from the default sheet without a prior Compare click."""
import argparse
import json
import re
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import expect, sync_playwright


parser = argparse.ArgumentParser()
parser.add_argument("url")
args = parser.parse_args()
artifacts = Path(__file__).with_name("artifacts")
artifacts.mkdir(exist_ok=True)
with urllib.request.urlopen(args.url + "/api/batches") as response:
    batches = json.load(response)["batches"]
available = {a["capture_id"] for b in batches for a in b["assets"] if a["status"] == "success"}
candidates = [a for b in batches for a in b["assets"]
              if a.get("baseline_id") in available and a.get("comparison", {}).get("status") == "success"]
pair = min(candidates, key=lambda asset: asset["comparison"]["score"])
url = args.url + "/?" + urlencode({"left": pair["baseline_id"], "right": pair["capture_id"]})
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    for viewport in ({"width": 1440, "height": 1000}, {"width": 390, "height": 844}):
        page.set_viewport_size(viewport)
        for first_mode in ("diff", "heat"):
            page.goto(url)
            expect(page.locator("#right-image")).to_be_visible()
            page.locator(f'[data-mode="{first_mode}"]').click()
            for view in ("sheet", "front", "sheet"):
                page.locator("#view").select_option(view)
                for mode in (first_mode, "heat" if first_mode == "diff" else "diff"):
                    page.locator(f'[data-mode="{mode}"]').click()
                    image = page.locator("#slider-left")
                    expect(image).to_be_visible()
                    expect(image).to_have_attribute("src", re.compile(f"/files/comparisons/.+/{view}_{mode}\\.png"))
                    image.evaluate("image => image.decode()")
                    assert image.evaluate("image => image.naturalWidth > 0 && image.getBoundingClientRect().width > 0")
                    if view == "sheet" and pair["comparison"]["score"] < 1:
                        assert image.evaluate("""image => {
                            const canvas = document.createElement('canvas');
                            canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
                            const context = canvas.getContext('2d'); context.drawImage(image, 0, 0);
                            return context.getImageData(0, 0, canvas.width, canvas.height).data.some((v, i) => i % 4 !== 3 && v > 0);
                        }"""), "Changed images must produce visible difference pixels"
                    expect(page.locator("#slider-clip")).to_be_hidden()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(artifacts / f"modes-{first_mode}-{viewport['width']}.png"), full_page=True)
    page.goto(url)
    expect(page.locator("#right-image")).to_be_visible()
    delayed = []
    page.route("**/api/compare", lambda route: delayed.append((route, route.fetch())))
    page.locator('[data-mode="heat"]').click()
    page.wait_for_timeout(200)
    assert delayed, "Expected a comparison request"
    page.locator("#left-asset").select_option("")
    expect(page.locator("#run-compare")).to_be_disabled()
    for route, response in delayed:
        route.fulfill(response=response)
    page.unroute("**/api/compare")
    expect(page.locator("#compare-info")).not_to_contain_text("最低 SSIM")
    expect(page.locator("#slider-left")).to_be_hidden()
    assert not errors, errors
    browser.close()
print("Comparison mode buttons passed from the default sheet on desktop and mobile.")
