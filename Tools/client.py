"""Local API client used for repeatable capture and acceptance runs."""
import argparse
import json
import re
import time
import urllib.request


class Client:
    def __init__(self, url):
        self.url = url.rstrip("/")
        with urllib.request.urlopen(self.url) as response:
            self.token = re.search(r'name="acc-token" content="([^"]+)"', response.read().decode("utf-8")).group(1)

    def get(self, path):
        with urllib.request.urlopen(self.url + path, timeout=150) as response:
            return json.load(response)

    def post(self, path, data):
        request = urllib.request.Request(self.url + path, json.dumps(data).encode("utf-8"),
            {"Content-Type": "application/json", "X-ACC-Token": self.token})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(error.read().decode("utf-8")) from error


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--scan")
    parser.add_argument("--paths", nargs="+")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--label", default="G0 capture")
    args = parser.parse_args()
    client = Client(args.url)
    if args.scan:
        print(json.dumps(client.post("/api/editor", {"action": "scan", "path": args.scan}), ensure_ascii=False))
    if args.paths:
        print(json.dumps(client.post("/api/capture", {"paths": args.paths, "concurrency": args.count, "label": args.label})))
