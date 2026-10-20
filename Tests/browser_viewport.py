"""Exercise hover wipe, cursor-anchored wheel zoom and canvas sizing with real images."""
import argparse
import json
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
pair = min((a for b in batches for a in b["assets"] if a.get("baseline_id") in available
            and a.get("comparison", {}).get("status") == "success"), key=lambda a: a["comparison"]["score"])
url = args.url + "/?" + urlencode({"left": pair["baseline_id"], "right": pair["capture_id"]})
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url)
    expect(page.locator("#right-image")).to_be_visible()
    page.locator('[data-mode="slider"]').click()
    expect(page.locator("#split-divider")).to_be_visible()
    assert page.locator('input[type="range"]').count() == 0
    for image in ("#slider-left", "#slider-right"):
        page.locator(image).evaluate("image => image.decode()")
    stage = page.locator(".slider-stage")
    stage.scroll_into_view_if_needed()
    rect = stage.bounding_box()
    outer = page.locator("#image-stage").bounding_box()
    assert abs(rect["width"] - outer["width"]) < 4, "Single-image canvas must use the available width"
    for position in (.2, .8):
        page.mouse.move(rect["x"] + rect["width"] * position, rect["y"] + min(100, rect["height"] / 2))
        value = float(page.locator("#split-divider").get_attribute("aria-valuenow"))
        assert abs(value - position * 100) <= 1, value
        clip = page.locator("#slider-clip").evaluate("element => element.style.clipPath")
        assert clip.startswith("inset(0px 0px 0px "), clip
        assert abs(float(clip.rsplit(" ", 1)[1].removesuffix("%)")) - value) <= 1, clip
    scroll = page.evaluate("scrollY")
    page.mouse.wheel(0, -500)
    expect(page.locator("#zoom-label")).not_to_have_text("100%")
    assert page.evaluate("scrollY") == scroll, "Wheel over canvas must zoom instead of scrolling the page"
    transforms = page.locator("#slider-left, #slider-right").evaluate_all("images => images.map(i => i.style.transform)")
    assert transforms[0] == transforms[1] and "scale(1)" not in transforms[0], transforms
    # The image point under the cursor stays fixed when wheel zoom changes scale.
    before = page.locator("#slider-left").evaluate("image => Array.from(new DOMMatrix(getComputedStyle(image).transform).toFloat64Array())")
    page.mouse.wheel(0, 200)
    page.wait_for_timeout(100)
    after = page.locator("#slider-left").evaluate("image => Array.from(new DOMMatrix(getComputedStyle(image).transform).toFloat64Array())")
    point = [rect["width"] * .3, min(100, rect["height"] / 2) - rect["height"] / 2]
    for axis in (0, 1):
        original = (point[axis] - before[12 + axis]) / before[0]
        updated = (point[axis] - after[12 + axis]) / after[0]
        assert abs(original - updated) < .1, (original, updated)
    page.locator("#split-divider").focus()
    previous = int(page.locator("#split-divider").get_attribute("aria-valuenow"))
    page.keyboard.press("ArrowLeft")
    assert int(page.locator("#split-divider").get_attribute("aria-valuenow")) == previous - 2
    page.locator("#zoom-reset").click()
    expect(page.locator("#zoom-label")).to_have_text("100%")
    page.screenshot(path=str(artifacts / "hover-wipe-desktop.png"), full_page=True)
    page.locator("#view").select_option("front")
    stage.scroll_into_view_if_needed()
    single = stage.bounding_box()
    page.mouse.move(single["x"] + single["width"] * .6, single["y"] + single["height"] / 2)
    assert abs(int(page.locator("#split-divider").get_attribute("aria-valuenow")) - 60) <= 1
    page.mouse.wheel(0, -300)
    expect(page.locator("#zoom-label")).not_to_have_text("100%")
    assert page.locator("#slider-left").evaluate("image => image.style.transform") == page.locator("#slider-right").evaluate("image => image.style.transform")
    page.locator("#view").select_option("sheet")
    stage.scroll_into_view_if_needed()
    # Regular page scrolling still works outside the image canvas.
    page.mouse.move(5, 100)
    scroll = page.evaluate("scrollY")
    page.mouse.wheel(0, -300)
    page.wait_for_timeout(100)
    assert page.evaluate("scrollY") < scroll
    page.set_viewport_size({"width": 390, "height": 844})
    stage.scroll_into_view_if_needed()
    rect = stage.bounding_box()
    page.mouse.move(rect["x"] + rect["width"] * .4, rect["y"] + rect["height"] / 2)
    assert abs(int(page.locator("#split-divider").get_attribute("aria-valuenow")) - 40) <= 1
    page.mouse.wheel(0, -200)
    expect(page.locator("#zoom-label")).not_to_have_text("100%")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(artifacts / "hover-wipe-mobile.png"), full_page=True)
    assert not errors, errors
    browser.close()
print("Hover wipe, wheel zoom, cursor anchoring, page scrolling and responsive canvas passed.")
