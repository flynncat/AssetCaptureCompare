"""Single-asset selection regression. Capture data and comparison requests are browser fixtures."""
import argparse
import copy
import json
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("url")
args = parser.parse_args()
artifacts = Path(__file__).with_name("artifacts")
artifacts.mkdir(exist_ok=True)
with urllib.request.urlopen(args.url + "/api/batches") as response:
    source = next(b for b in json.load(response)["batches"] if any(a["status"] == "success" for a in b["assets"]))
item = next(a for a in source["assets"] if a["status"] == "success")
with urllib.request.urlopen(args.url + "/api/capture?id=" + item["capture_id"]) as response:
    template = json.load(response)
with urllib.request.urlopen(args.url + "/api/state") as response:
    root = Path(json.load(response)["root"])
image = root / "batches" / item["capture_id"].split("/")[0] / "assets" / item["capture_id"].split("/")[1] / "contact_sheet.png"
paths = dict(a="/Game/FolderA/Shared.Shared", b="/Game/FolderB/Shared.Shared",
             left="/Game/LeftOnly.LeftOnly", right="/Game/RightOnly.RightOnly", failed="/Game/Failed.Failed")
captures = {}


def batch(name, keys, project="project", failures=()):
    value = copy.deepcopy(source)
    value.update(batch_id="selector_" + name, label="Selector " + name, status="success", assets=[])
    for key in keys:
        capture = copy.deepcopy(template)
        capture.update(capture_id=value["batch_id"] + "/" + key, path=paths[key], identity=project + ":" + paths[key],
                       status="failed" if key in failures else "success")
        captures[capture["capture_id"]] = capture
        value["assets"].append(dict(capture_id=capture["capture_id"], path=capture["path"], identity=capture["identity"],
                                    name=capture["path"].split(".")[-1], status=capture["status"], **{"class": "StaticMesh"}))
    return value


old = batch("old", ["a", "b", "left", "failed"])
new = batch("new", ["a", "b", "right", "failed"], failures=["failed"])
missing = batch("missing", ["b"])
foreign = batch("foreign", ["a"], project="other-project")
new["assets"][0]["baseline_id"] = old["assets"][0]["capture_id"]
listing = dict(batches=[new, old, missing, foreign], history_revision="selector-fixture")


def link(asset="a", left=old, right=new):
    return args.url + "/?" + urlencode(dict(asset="project:" + paths[asset], left=left["batch_id"], right=right["batch_id"]))


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors, requests, comparisons = [], [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("Object.defineProperty(navigator, 'clipboard', {value: {writeText: async value => {window.copiedLink = value;}}});")
    waiting = []
    page.route("**/api/batches", lambda route: waiting.append(route))
    page.route("**/api/progress?summary=1", lambda route: route.fulfill(json=dict(running=False, history_revision=listing["history_revision"])))
    page.route("**/api/calibration", lambda route: route.fulfill(json=dict(running=False)))
    page.route("**/files/**", lambda route: route.fulfill(path=str(image)))

    def capture(route):
        capture_id = parse_qs(urlparse(route.request.url).query)["id"][0]
        requests.append(capture_id)
        route.fulfill(json=captures[capture_id])

    def compare(route):
        data = route.request.post_data_json
        comparisons.append(data)
        assert captures[data["left"]]["identity"] == captures[data["right"]]["identity"]
        route.fulfill(json=dict(status="success", category="small", score=1, mean=1, worst_view="front", views=[],
                                parameters=dict(library="fixture", version="1", sigma=1.5, K1=.01, K2=.03, data_range=1), thresholds=[.99, .97]))

    page.route("**/api/capture?**", capture)
    page.route("**/api/compare", compare)
    page.goto(link())
    page.locator('[data-tab="compare"]').click()
    page.get_by_role("searchbox", name="查找资产", exact=True).fill("Shared")
    assert waiting
    for route in waiting:
        route.fulfill(json=listing)
    page.unroute("**/api/batches")
    page.route("**/api/batches", lambda route: route.fulfill(json=listing))
    expect(page.locator("#right-image")).to_be_visible()
    assert page.locator("#left-asset, #right-asset").count() == 0
    expect(page.locator("#compare-asset")).to_have_value("project:" + paths["a"])
    page.locator("#compare-asset-search").fill("")
    expect(page.locator("#compare-asset-count")).to_have_text("6 / 6 个资产")
    # Search matches names/paths without replacing the current pair.
    requested = len(requests)
    page.locator("#compare-asset-search").fill("FOLDERB/SHARED")
    expect(page.locator("#compare-asset-count")).to_have_text("1 / 6 个资产")
    assert len(requests) == requested
    page.locator("#compare-asset").select_option("project:" + paths["b"])
    expect(page.locator("#left-caption")).to_contain_text(paths["b"])
    expect(page.locator("#right-caption")).to_contain_text(paths["b"])
    page.locator("#run-compare").click()
    expect(page.locator("#compare-info")).to_contain_text("最低 SSIM")
    assert comparisons[-1]["left"] == "selector_old/b" and comparisons[-1]["right"] == "selector_new/b"
    page.locator("#compare-asset-search").fill("not-found")
    expect(page.locator("#compare-asset-count")).to_have_text("0 / 6 个资产")
    expect(page.locator("#compare-asset")).to_have_value("project:" + paths["b"])
    page.locator("#compare-asset-search").fill("")
    # A one-sided or failed record must not select another asset as a substitute.
    page.locator("#compare-asset").select_option("project:" + paths["right"])
    expect(page.locator("#left-caption")).to_contain_text("无此资产记录")
    expect(page.locator("#left-image")).to_be_hidden()
    expect(page.locator("#right-caption")).to_contain_text(paths["right"])
    expect(page.locator("#run-compare")).to_be_disabled()
    page.locator("#compare-asset").select_option("project:" + paths["failed"])
    expect(page.locator("#right-caption")).to_contain_text("失败")
    expect(page.locator("#run-compare")).to_be_disabled()
    page.locator("#compare-asset").select_option("project:" + paths["a"])
    page.locator("#right-batch").select_option(foreign["batch_id"])
    expect(page.locator("#right-caption")).to_contain_text("无此资产记录")
    expect(page.locator("#run-compare")).to_be_disabled()
    # The asset survives batch changes, refreshes, and a missing-side link round trip.
    page.locator("#right-batch").select_option(missing["batch_id"])
    page.locator("#refresh").click()
    expect(page.locator("#compare-asset")).to_have_value("project:" + paths["a"])
    page.locator("#copy-link").click()
    expect(page.locator("#notice")).to_contain_text("已复制")
    copied = page.evaluate("window.copiedLink")
    assert parse_qs(urlparse(copied).query)["right"] == [missing["batch_id"]]
    page.goto(copied)
    expect(page.locator("#left-image")).to_be_visible()
    expect(page.locator("#right-caption")).to_contain_text("无此资产记录")
    page.locator("#right-batch").select_option(new["batch_id"])
    page.locator("#left-batch").select_option(missing["batch_id"])
    page.locator("#use-baseline").check()
    expect(page.locator("#left-batch")).to_have_value(old["batch_id"])
    expect(page.locator("#left-caption")).to_contain_text(paths["a"])
    page.locator("#compare-asset-search").fill("Shared")
    page.screenshot(path=str(artifacts / "single-asset-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(artifacts / "single-asset-mobile.png"), full_page=True)
    # Legacy links prefer the right-hand asset and match that identity in the left batch.
    page.goto(args.url + "/?" + urlencode(dict(left="selector_old/a", right="selector_new/b")))
    expect(page.locator("#compare-asset")).to_have_value("project:" + paths["b"])
    expect(page.locator("#left-caption")).to_contain_text(paths["b"])
    expect(page.locator("#right-caption")).to_contain_text(paths["b"])
    page.goto(args.url + "/?" + urlencode(dict(asset="deleted", left=old["batch_id"], right=new["batch_id"])))
    expect(page.locator("#compare-asset")).to_have_value("")
    expect(page.locator("#run-compare")).to_be_disabled()
    assert not errors, errors
    browser.close()
print("Single-asset search, identity matching, missing/failed records, baseline and links passed.")
