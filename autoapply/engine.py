"""Playwright driver + form-field primitives shared by all ATS adapters.

Presentation: an ordinary Chromium executing each page's own scripts, no identity
overrides of any kind. Headed by default (most honest, best for debugging); headless is
opt-in and keeps Chromium's normal headless identification.

Success is proven positively: after submit an adapter must observe the site's own
confirmation state. If it doesn't (whatever the reason — an extra verification step, a
site that declines automated traffic, a slow page), the caller records that and leaves
the job for the human queue. We never analyze or engage with verification steps.

Field-handling lessons carried over from the extension:
- Check is_dropdownish() before typing into any text input (a react-select disguised as a
  text box must be opened and clicked, not typed into — the Samsara incident).
- Confirm the company's name is on the page before filling (Temporal→Precisely incident).
"""
from __future__ import annotations

import pathlib
import random
import re
import time

from playwright.sync_api import Page, TimeoutError as PWTimeout  # noqa: F401

VIEWPORT = {"width": 1440, "height": 1000}


def human_pause() -> None:
    time.sleep(random.uniform(0.18, 0.32))


def between_jobs_pause(cfg: dict) -> None:
    lo, hi = ((cfg.get("autoapply") or {}).get("pace_seconds") or [8, 20])[:2]
    time.sleep(random.uniform(float(lo), float(hi)))


def launch(pw, headed: bool = True):
    browser = pw.chromium.launch(headless=not headed)
    ctx = browser.new_context(viewport=VIEWPORT, locale="en-US",
                              timezone_id="America/Los_Angeles")
    ctx.set_default_timeout(15_000)
    return browser, ctx


def goto(page: Page, url: str, settle_ms: int = 1500) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    page.wait_for_timeout(settle_ms)


def norm_alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def company_on_page(page: Page, company: str) -> bool:
    """Mismatch guard: normalized company name must appear in URL, title, or body head."""
    want = norm_alnum(company)
    if not want:
        return False
    hay = norm_alnum(page.url) + norm_alnum(page.title())
    try:
        hay += norm_alnum(page.evaluate("document.body ? document.body.innerText.slice(0, 6000) : ''"))
    except Exception:
        pass
    return want in hay


def is_dropdownish(handle) -> bool:
    """True when a text-looking input actually drives a picker (react-select & friends)."""
    try:
        role = (handle.get_attribute("role") or "").lower()
        haspopup = (handle.get_attribute("aria-haspopup") or "").lower()
        expanded = handle.get_attribute("aria-expanded")
        autocomplete = (handle.get_attribute("aria-autocomplete") or "").lower()
        cls = (handle.get_attribute("class") or "").lower()
        readonly = handle.get_attribute("readonly")
        return (role == "combobox" or haspopup in ("listbox", "menu", "true")
                or expanded is not None or autocomplete == "list"
                or "select" in cls or readonly is not None)
    except Exception:
        return False


def fill_text(page: Page, locator, value: str) -> None:
    locator.scroll_into_view_if_needed()
    locator.click()
    locator.fill(str(value))
    human_pause()


def select_native(page: Page, locator, option_label: str) -> bool:
    """Native <select>: exact label, then case-insensitive contains."""
    try:
        locator.select_option(label=option_label)
        human_pause()
        return True
    except Exception:
        pass
    try:
        options = locator.locator("option").all_text_contents()
        want = option_label.strip().lower()
        for o in options:
            if want == o.strip().lower() or want in o.strip().lower() or o.strip().lower() in want:
                locator.select_option(label=o)
                human_pause()
                return True
    except Exception:
        pass
    return False


def pick_combobox(page: Page, locator, option_label: str, *, type_first: bool = False) -> bool:
    """ARIA combobox / react-select: open, (optionally) type to filter, click the option."""
    locator.scroll_into_view_if_needed()
    locator.click()
    page.wait_for_timeout(250)
    if type_first:
        try:
            locator.fill("")
            locator.type(option_label[:30], delay=40)
            page.wait_for_timeout(600)
        except Exception:
            pass
    want = option_label.strip().lower()
    deadline = time.time() + 5
    while time.time() < deadline:
        opts = page.locator("[role='option']:visible")
        n = opts.count()
        if n:
            texts = [opts.nth(i).inner_text().strip() for i in range(min(n, 60))]
            for i, t in enumerate(texts):
                tl = t.lower()
                if want == tl or want in tl or tl in want:
                    opts.nth(i).click()
                    human_pause()
                    return True
            if type_first:                # filtered list without exact text — take the first
                opts.first.click()
                human_pause()
                return True
        page.wait_for_timeout(200)
    page.keyboard.press("Escape")         # never leave a menu open (Workday lesson)
    return False


def set_checkbox(page: Page, locator, value: bool) -> None:
    locator.scroll_into_view_if_needed()
    if locator.is_checked() != bool(value):
        locator.click()
    human_pause()


def choose_radio_by_label(page: Page, container, option_label: str) -> bool:
    """Click the radio/button whose visible label best matches option_label."""
    want = option_label.strip().lower()
    labels = container.locator("label, [role='radio'], button")
    n = labels.count()
    for i in range(min(n, 40)):
        el = labels.nth(i)
        try:
            t = el.inner_text().strip().lower()
        except Exception:
            continue
        if t and (t == want or want in t or t in want):
            el.click()
            human_pause()
            return True
    return False


def upload_file(page: Page, locator, path: str) -> None:
    locator.set_input_files(path)
    page.wait_for_timeout(1200)           # give the widget time to show the attached state


def shot(page: Page, dest: pathlib.Path, name: str) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    p = dest / f"{name}.png"
    try:
        page.screenshot(path=str(p), full_page=True)
    except Exception:
        page.screenshot(path=str(p))      # some pages break full_page capture
    return str(p)
