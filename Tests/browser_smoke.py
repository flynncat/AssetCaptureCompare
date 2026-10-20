import argparse
import json
import sys
import urllib.request
from urllib.parse import urlencode
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

parser = argparse.ArgumentParser()
parser.add_argument("url")
args = parser.parse_args()
artifacts = Path(__file__).resolve().parent / "artifacts"
artifacts.mkdir(exist_ok=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Content/Python"))
from acc.common import DEFAULT_PROFILE
from acc.storage import Store
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(args.url)
    page.locator("#connection").filter(has_text="UE 已连接").wait_for()
    expect(page.locator("#warmup_frames")).to_have_attribute("min", "0")
    expect(page.locator("label").filter(has=page.locator("#concurrency"))).to_contain_text("采集并发上限")
    assert page.locator("#concurrency").get_attribute("max") is None
    page.locator("#concurrency").fill("32")
    assert page.locator("#concurrency").evaluate("input => input.checkValidity()")
    page.get_by_role("button", name="扫描目录", exact=True).click()
    page.locator(".asset-row").first.wait_for()
    assert page.locator(".asset-row").count() <= 50
    page.screenshot(path=str(artifacts / "capture-desktop.png"), full_page=True)
    page.get_by_role("button", name="历史批次", exact=True).count()
    page.locator('[data-tab="batches"]').click()
    page.screenshot(path=str(artifacts / "batches-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator('[data-tab="capture"]').click()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(artifacts / "capture-mobile.png"), full_page=True)
    with urllib.request.urlopen(args.url + "/api/batches") as response:
        batches = json.load(response)["batches"]
    pair = next((a for b in batches for a in b["assets"] if a.get("comparison", {}).get("status") == "success"
                 and a.get("baseline_id")), None)
    if pair:
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.goto(args.url + "/?" + urlencode({"left": pair["baseline_id"], "right": pair["capture_id"]}))
        expect(page.locator("#right-image")).to_have_js_property("naturalWidth", 1920)
        page.locator("#run-compare").click()
        expect(page.locator("#compare-info")).to_contain_text("最低 SSIM")
        page.locator("#threshold-small").fill("0.98")
        page.locator("#threshold-review").fill("0.95")
        with page.expect_response("**/api/compare") as response:
            page.locator("#run-compare").click()
        assert response.value.json()["thresholds"] == [0.98, 0.95]
        page.screenshot(path=str(artifacts / "compare-desktop.png"), full_page=True)
        page.locator("#view").select_option("front")
        page.locator('[data-mode="slider"]').click()
        expect(page.locator("#slider-right")).to_have_js_property("naturalWidth", 480)
        page.locator("#split").focus()
        page.keyboard.press("ArrowRight")
        page.locator('[data-mode="heat"]').click()
        expect(page.locator("#slider-left")).to_have_js_property("naturalWidth", 480)
        page.screenshot(path=str(artifacts / "heatmap-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator('[data-mode="side"]').click()
        for selector in ("#left-image", "#right-image"):
            expect(page.locator(selector)).to_have_js_property("naturalWidth", 480)
            page.locator(selector).evaluate("image => image.decode()")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(artifacts / "compare-mobile.png"), full_page=True)
    thin = next((a for b in batches for a in b["assets"] if a.get("baseline_id") and
                 a["status"] == "success" and a.get("comparison", {}).get("status") == "incomplete"), None)
    if thin:
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.goto(args.url + "/?" + urlencode({"left": thin["baseline_id"], "right": thin["capture_id"]}))
        page.locator("#run-compare").click()
        expect(page.locator("#compare-info")).to_contain_text("无完整主分数")
        expect(page.locator("#scores")).to_contain_text("ROI 过小")
        page.screenshot(path=str(artifacts / "thin-roi-desktop.png"), full_page=True)
    with urllib.request.urlopen(args.url + "/api/state") as response:
        output = json.load(response)["root"]
    store = Store(output)
    fixture = store.new_batch([{"path": "/Acceptance/Deletion.Deletion", "name": "Deletion", "class": "StaticMesh"}], DEFAULT_PROFILE,
        {"requested_concurrency": 1}, label="UI deletion acceptance")
    asset = fixture["assets"][0]
    store.finish_asset(fixture, asset, {"status": "failed", "views": [], "error": "Disposable UI acceptance fixture"})
    fixture["status"] = "failed"
    store.save_batch(fixture)
    try:
        page.locator('[data-tab="batches"]').click()
        page.locator("#refresh").click()
        page.locator("#batch-search").fill("UI deletion acceptance")
        row = page.locator(".batch").filter(has=page.get_by_role("heading", name="UI deletion acceptance", exact=True))
        row.get_by_role("button", name="删除", exact=True).click()
        expect(page.locator("#delete-description")).to_contain_text("MiB")
        page.locator("#delete-dialog").get_by_role("button", name="取消", exact=True).click()
        expect(row).to_be_visible()
        row.get_by_role("button", name="删除", exact=True).click()
        page.locator("#delete-dialog").get_by_role("button", name="删除批次", exact=True).click()
        expect(row).to_have_count(0)
    finally:
        if (store.root / "batches" / fixture["batch_id"]).exists():
            store.delete_batch(fixture["batch_id"])
    assert not errors, errors
    browser.close()
print("Browser smoke passed; desktop/mobile screenshots saved.")
