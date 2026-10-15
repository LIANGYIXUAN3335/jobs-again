"""Real-browser check of the Chrome runner page; unit tests use fake DOMs and cannot catch browser behavior.

Loads the real extension in a throwaway headless Chromium, pairs it with the running 8787 service, and
optionally opens one public application page read-only. Never clicks Start or Submit, never claims a job,
never prints tokens. The service shows the runner as online for 90 seconds afterwards.

    .venv/bin/python tools/runner_smoke.py [application-page-url]
"""
import hashlib
import json
import re
import sys
import tempfile
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8787"
EXTENSION = str(Path(__file__).resolve().parent.parent / "extension")
EXTENSION_ID = "".join(chr(ord("a") + int(c, 16)) for c in hashlib.sha256(EXTENSION.encode()).hexdigest()[:32])
OPEN_APPLICATION = """async url => {
  const tab = await chrome.tabs.create({url, active: true});
  const deadline = Date.now() + 25000;
  let last = 'no reply';
  while (Date.now() < deadline) {
    try {
      const reply = await chrome.tabs.sendMessage(tab.id, {type: 'jobscanRunnerReady'}, {frameId: 0});
      last = JSON.stringify(reply).slice(0, 300);
      if (reply && (reply.ready === true || reply.status === 'ready' || reply.gate)) return last;
    } catch (error) { last = 'error: ' + error.message; }
    await new Promise(done => setTimeout(done, 700));
  }
  return 'timeout, last = ' + last;
}"""


def board_call(path, token, body=None):
    request = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-JobScan-Review": token, "Origin": BASE, "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(request, timeout=10))


def main():
    html = urllib.request.urlopen(BASE + "/agent-lab/runner", timeout=10).read().decode()
    token = re.search(r'name="review-token" content="([^"]+)"', html).group(1)
    state = board_call("/api/agent-lab/runner/status", token)
    if state["enabled"] or state["active"]:
        sys.exit("A batch is live; not loading a second runner next to it.")
    with sync_playwright() as p, tempfile.TemporaryDirectory() as profile:
        context = p.chromium.launch_persistent_context(profile, channel="chromium", headless=True, args=[
            f"--disable-extensions-except={EXTENSION}", f"--load-extension={EXTENSION}"])
        try:
            page = context.new_page()
            page.goto(f"chrome-extension://{EXTENSION_ID}/runner.html")
            page.wait_for_timeout(800)
            page.evaluate("chrome.storage.local.set({jobscanPort: 8787, jobscanIdentity: ''})")
            page.fill("#pair-code", board_call("/api/agent-lab/pair-code", token, {})["code"])
            page.click("#pair")
            page.wait_for_timeout(3500)
            print("connection:", page.inner_text("#connection"), "|", page.inner_text("#pair-state"))
            if page.inner_text("#notice"):
                print("notice:", page.inner_text("#notice"))
            print("board sees:", board_call("/api/agent-lab/runner/status", token)["worker"])
            if len(sys.argv) > 1:
                print("application page:", page.evaluate(OPEN_APPLICATION, sys.argv[1]))
        finally:
            context.close()


if __name__ == "__main__":
    main()
