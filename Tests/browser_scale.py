"""Measure viewer pagination using 1000 transport fixtures and a real capture image."""
import argparse
import copy
import json
import time
import urllib.request
from pathlib import Path
from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("url")
args = parser.parse_args()
with urllib.request.urlopen(args.url + "/api/batches") as response:
    batches = json.load(response)["batches"]
source = next(batch for batch in batches if any(a["status"] == "success" for a in batch["assets"]))
asset = next(a for a in source["assets"] if a["status"] == "success")
parts = asset["capture_id"].split("/")
with urllib.request.urlopen(args.url + "/files/batches/{}/assets/{}/contact_sheet.png".format(*parts)) as response:
    png = response.read()
batch = copy.deepcopy(source)
batch.update(label="1000 record viewer fixture", status="success", assets=[])
for index in range(1000):
    item = copy.deepcopy(asset)
    item.update(name="Probe_{}".format(index), path="/Acceptance/Probe_{0}.Probe_{0}".format(index),
                capture_id=batch["batch_id"] + "/probe_{}".format(index), asset_id="probe_{}".format(index))
    batch["assets"].append(item)
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/batches", lambda route: route.fulfill(json={"batches": [batch]}))
    page.route("**/api/progress", lambda route: route.fulfill(json={"running": False}))
    page.route("**/api/calibration", lambda route: route.fulfill(json={"running": False}))
    page.route("**/files/**", lambda route: route.fulfill(body=png, content_type="image/png"))
    started = time.monotonic()
    page.goto(args.url, wait_until="domcontentloaded")
    page.locator('[data-tab="batches"]').click()
    expect(page.locator(".batch")).to_have_count(1)
    elapsed = time.monotonic() - started
    assert elapsed <= 3, elapsed
    assert page.locator(".thumbnail img").count() <= 50
    page.locator(".batch details summary").click()
    expect(page.locator(".history-row")).to_have_count(50)
    assert not errors, errors
    artifacts = Path(__file__).with_name("artifacts")
    artifacts.mkdir(exist_ok=True)
    page.screenshot(path=str(artifacts / "viewer-1000.png"), full_page=True)
    report = {"records": 1000, "first_usable_seconds": elapsed, "visible_record_rows": 50,
              "thumbnail_count": page.locator(".thumbnail img").count(), "fixture": "browser transport"}
    (artifacts / "viewer-scale.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    browser.close()
