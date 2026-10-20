"""History freshness regression; all capture responses are browser-local fixtures."""
import argparse
import copy
import json
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("url")
args = parser.parse_args()
artifacts = Path(__file__).with_name("artifacts")
artifacts.mkdir(exist_ok=True)
with urllib.request.urlopen(args.url + "/api/batches") as response:
    source = json.load(response)["batches"][0]


def fixture(number, label):
    batch = copy.deepcopy(source)
    batch.update(batch_id="history_test_" + str(number), label=label, status="failed")
    batch["assets"] = [dict(path="/Game/History.History", name="History", status="failed",
                            capture_id=batch["batch_id"] + "/asset", error="Browser fixture")]
    return batch


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    old = fixture(1, "Earlier capture")
    current = fixture(2, "framcrazy")
    listing = {"batches": [old], "history_revision": "one"}
    progress = {"running": False, "history_revision": "one"}
    requests = []

    def batches(route):
        requests.append(1)
        route.fulfill(json=listing)

    page.route("**/api/batches", batches)
    page.route("**/api/progress?summary=1", lambda route: route.fulfill(json=progress))
    page.route("**/api/editor", lambda route: route.fulfill(json={"assets": [dict(
        path="/Game/History.History", name="History", **{"class": "StaticMesh"})]}))
    page.goto(args.url)
    expect(page.locator("#right-batch")).to_have_value(old["batch_id"])
    initial_requests = len(requests)
    # Another tab completes a task entirely between polls (idle -> idle).
    listing.update(batches=[current, old], history_revision="two")
    progress["history_revision"] = "two"
    expect(page.locator("#latest-capture-summary")).to_contain_text("framcrazy", timeout=10000)
    expect(page.locator("#right-batch option")).to_have_count(2)
    expect(page.locator("#right-batch")).to_have_value(old["batch_id"])
    assert len(requests) == initial_requests + 1
    page.wait_for_timeout(3200)
    assert len(requests) == initial_requests + 1, "Unchanged history must not download all assets every poll"
    progress.update(running=True, batch_id=current["batch_id"], completed=0, total=1,
                    actual_concurrency=1, concurrency_limit=4, workers=[])
    expect(page.locator("#progress-summary")).to_contain_text("并发 1 / 上限 4", timeout=10000)
    progress["history_revision"] = "checkpoint"
    page.wait_for_timeout(3200)
    assert len(requests) == initial_requests + 1, "Asset checkpoints must not download all history"
    progress["running"] = False
    expect(page.locator("#progress-summary")).to_have_text("任务结束", timeout=10000)
    listing["history_revision"] = progress["history_revision"] = "two"
    page.locator('[data-tab="batches"]').click()
    page.locator("#batch-search").fill("nonmatching filter")
    page.locator("#batch-status").select_option("success")
    page.locator("#result-filter").select_option("small")
    page.locator("#latest-batch").click()
    expect(page.locator(".batch")).to_have_count(1)
    expect(page.locator(".batch h3")).to_have_text("framcrazy")
    expect(page.locator(".batch")).to_contain_text("完成")
    # Navigation also reloads history if revision polling is unavailable.
    progress.pop("history_revision")
    third = fixture(3, "Navigation refresh")
    listing.update(batches=[third, current, old], history_revision="three")
    page.locator('[data-tab="compare"]').click()
    expect(page.locator("#right-batch option")).to_have_count(3)
    # A successful submission selects the exact returned batch and sends the label.
    created = fixture(4, "Submitted label")

    def capture(route):
        assert route.request.post_data_json["label"] == "Submitted label"
        assert route.request.post_data_json["profile"]["movable"] is False
        listing.update(batches=[created, third, current, old], history_revision="four")
        route.fulfill(json={"batch_id": created["batch_id"]})

    page.route("**/api/capture", capture)
    page.locator('[data-tab="capture"]').click()
    page.locator("#selection").click()
    expect(page.locator("#asset-count")).to_contain_text("1 已选")
    page.locator("#label").fill("Submitted label")
    expect(page.locator("#movable")).to_be_checked()
    page.locator("#movable").uncheck()
    page.locator("#capture-start").click()
    expect(page.locator("#latest-capture-summary")).to_contain_text("Submitted label")
    expect(page.locator("#right-batch")).to_have_value(created["batch_id"])
    page.locator("#latest-batch").click()
    expect(page.locator(".batch h3")).to_have_text("Submitted label")
    # Empty/cancelled batches must remain locatable too.
    empty = fixture(5, "Empty interrupted batch")
    empty.update(status="interrupted", assets=[])
    listing.update(batches=[empty, created, third, current, old], history_revision="five")
    page.locator("#latest-batch").click()
    expect(page.locator(".batch h3")).to_have_text("Empty interrupted batch")
    # Preparation failure must not be replaced by a generic task-ended notice.
    pending = []
    page.unroute("**/api/capture")
    page.route("**/api/capture", lambda route: pending.append(route))
    page.locator('[data-tab="capture"]').click()
    page.locator("#capture-start").click()
    expect(page.locator("#notice")).to_contain_text("正在创建")
    progress.update(preparation={"phase": "dependencies", "completed": 0, "total": 1, "elapsed_seconds": 0})
    expect(page.locator("#progress-summary")).to_contain_text("检查资产依赖", timeout=10000)
    assert pending
    pending[0].fulfill(status=400, json={"error": "Preparation failed for labelled request"})
    expect(page.locator("#notice")).to_contain_text("Preparation failed")
    progress.pop("preparation")
    expect(page.locator("#progress-summary")).to_have_text("任务结束", timeout=10000)
    expect(page.locator("#notice")).to_contain_text("Preparation failed")
    page.locator("#latest-batch").click()
    page.screenshot(path=str(artifacts / "history-refresh-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(artifacts / "history-refresh-mobile.png"), full_page=True)
    # Verify the real persisted latest batch without modifying any capture data.
    page.unroute_all()
    page.goto(args.url)
    page.locator("#latest-batch").click()
    expect(page.locator(".batch")).to_have_count(1)
    expect(page.locator(".batch h3")).to_have_text(source["label"])
    expect(page.locator(".batch")).to_contain_text(source["batch_id"])
    page.screenshot(path=str(artifacts / "history-live-mobile.png"), full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.screenshot(path=str(artifacts / "history-live-desktop.png"), full_page=True)
    assert not errors, errors
    browser.close()
print("History refresh, exact batch navigation, label submission and failure notice passed.")
