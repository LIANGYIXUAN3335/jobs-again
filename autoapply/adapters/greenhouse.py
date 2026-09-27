"""Greenhouse (job-boards.greenhouse.io) adapter — the verified primary target.

Modern Greenhouse form shape (probed live 2026-08-20):
- Fixed ids: #first_name #last_name #email #phone(tel) #country(combobox)
  #candidate-location(combobox autocomplete)
- File inputs: #resume #cover_letter
- Custom questions: #question_<id>, role=combobox for dropdowns else plain text
- EEO: numeric-id comboboxes (#1101 gender, #1102 transgender, #1103 orientation,
  #1104 ethnicity, #1126 veteran, #1127 disability) + US self-ID (#gender #hispanic_ethnicity
  #veteran_status #disability_status) + a required #gdpr_* consent checkbox
- Submit button text: "Submit application"
- Verification is score-based (g-recaptcha-response is page-filled) — no user step normally.
"""
from __future__ import annotations

from playwright.sync_api import Page

from .. import engine
from ..models import Answer, Question, QType, Quarantine, Source

CONFIRM_TEXTS = ["your application has been submitted", "thank you for applying",
                 "application submitted", "we received your application",
                 "thanks for applying", "successfully submitted"]


def matches(url: str) -> bool:
    return "greenhouse.io" in (url or "")


def _combobox_options(page: Page, loc) -> list[str]:
    """Open a Greenhouse combobox, read its option texts, close it."""
    try:
        loc.scroll_into_view_if_needed()
        loc.click()
        page.wait_for_timeout(300)
        opts = page.locator("[role='option']:visible")
        texts = [opts.nth(i).inner_text().strip() for i in range(min(opts.count(), 80))]
        page.keyboard.press("Escape")
        page.wait_for_timeout(120)
        return [t for t in texts if t]
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return []


def read_questions(page: Page) -> list[Question]:
    """Enumerate every fillable control with its label, type, required flag, options."""
    raw = page.evaluate(r"""() => {
      const out=[];
      const q='input:not([type=hidden]),select,textarea';
      document.querySelectorAll(q).forEach(el=>{
        if(el.type==='hidden' || el.type==='search') return;
        if(el.id && el.id.startsWith('iti-')) return;         // phone country internal search
        let lab=(el.labels&&el.labels[0]&&el.labels[0].innerText)||el.getAttribute('aria-label')||el.getAttribute('placeholder')||'';
        lab=(lab||'').replace(/\s+/g,' ').replace(/\*$/,'').trim();
        const req=el.required||el.getAttribute('aria-required')==='true';
        out.push({id:el.id||'', name:el.name||'', tag:el.tagName.toLowerCase(),
                  type:el.type||'', role:el.getAttribute('role')||'', label:lab, required:!!req});
      });
      return out;
    }""")
    questions: list[Question] = []
    for f in raw:
        if not f["id"] and not f["label"]:
            continue
        label = f["label"]
        sel = f"#{_css(f['id'])}" if f["id"] else ""
        if f["type"] == "file":
            qt = QType.FILE
        elif f["type"] == "checkbox":
            qt = QType.CHECKBOX
        elif f["role"] == "combobox" or f["tag"] == "select":
            qt = QType.SELECT
        elif f["type"] == "tel":
            qt = QType.TEXT
        else:
            qt = QType.TEXT
        q = Question(label=label or f["id"], qtype=qt, required=f["required"],
                     selector=sel, meta={"gh_id": f["id"]})
        if qt == QType.SELECT and sel and f["id"] not in ("country",):
            q.options = _combobox_options(page, page.locator(sel))
        questions.append(q)
    return questions


def _css(el_id: str) -> str:
    # numeric ids (#1101) and ids with special chars need escaping for querySelector-style use;
    # Playwright's #id works for alnum/underscore. Fall back to attribute selector otherwise.
    return el_id


def _loc(page: Page, q: Question):
    gid = q.meta.get("gh_id", "")
    if gid and gid[0].isdigit():
        return page.locator(f"[id='{gid}']")
    return page.locator(q.selector) if q.selector else None


def fill(page: Page, answers: list[Answer], resume_path: str) -> None:
    # résumé first (its own file input); cover letter left empty on purpose
    try:
        rf = page.locator("#resume")
        if rf.count():
            engine.upload_file(page, rf, resume_path)
    except Exception as e:
        raise Quarantine(f"résumé upload failed: {e}")

    for a in answers:
        q = a.question
        if a.source == Source.SKIP or a.value in ("", None):
            continue
        if q.qtype == QType.FILE:
            continue
        loc = _loc(page, q)
        if loc is None or not loc.count():
            if q.required:
                raise Quarantine("required field not locatable at fill time", q.label)
            continue
        try:
            if q.qtype == QType.CHECKBOX:
                engine.set_checkbox(page, loc, bool(a.value))
            elif q.qtype == QType.SELECT:
                gid = q.meta.get("gh_id", "")
                if gid == "country" or "country" in q.label.lower():
                    engine.pick_combobox(page, loc, str(a.value), type_first=True)
                elif gid == "candidate-location" or "location" in q.label.lower():
                    engine.pick_combobox(page, loc, str(a.value), type_first=True)
                else:
                    ok = engine.pick_combobox(page, loc, str(a.value))
                    if not ok and q.required:
                        raise Quarantine(f"could not select {a.value!r}", q.label)
            else:
                engine.fill_text(page, loc, str(a.value))
        except Quarantine:
            raise
        except Exception as e:
            if q.required:
                raise Quarantine(f"fill error on required field: {e}", q.label)


def location_answer_hint() -> str:
    return "San Jose"


def submit(page: Page, *, dry_run: bool) -> str | None:
    """Click Submit; return the confirmation text seen, or None if not confirmed.
    dry_run stops right before the click (fills validated, nothing sent)."""
    btn = page.get_by_role("button", name="Submit application")
    if not btn.count():
        btn = page.locator("button:has-text('Submit application'), input[type=submit]")
    if not btn.count():
        raise Quarantine("submit button not found")
    if dry_run:
        return None
    btn.first.scroll_into_view_if_needed()
    btn.first.click()
    return _await_confirmation(page)


def _await_confirmation(page: Page) -> str | None:
    import time
    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            body = (page.evaluate("document.body ? document.body.innerText : ''") or "").lower()
        except Exception:
            body = ""
        for t in CONFIRM_TEXTS:
            if t in body:
                return t
        page.wait_for_timeout(700)
    return None
