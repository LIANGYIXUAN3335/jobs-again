"""Read each Ashby or Greenhouse form before running it, so a form that would stop is never opened.

The form's questions come from the board's own public API. Filler's own rules, run in Node,
say which required questions already have an answer; the rest become one list for the owner, sorted
by how many applications each answer unlocks. Nothing here calls a model, so it costs no tokens.
"""
from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config as cfgmod
from . import confirmed_answers
from .agent_review import RECORDING, uncovered_question

_root = cfgmod.ROOT / "data" / "agent_lab"
_lock = threading.RLock()
_memo: dict = {}
_reader = threading.Lock()   # held for as long as a background read runs
MAX_AGE = 24 * 60 * 60
ASHBY_API = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"
QUERY = ("query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) { "
         "jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) { "
         "id title applicationForm { sections { fieldEntries { id field isRequired descriptionHtml } } } } }")
# Nobody has seen the API answer with descriptionHtml, and it refuses a whole query over one unknown field. That must
# not leave every form unread, so a refusal drops back to this query until the service restarts.
PLAIN_QUERY = QUERY.replace(" descriptionHtml", "")
_query = {"now": QUERY}
GREENHOUSE_HOSTS = {"job-boards.greenhouse.io", "boards.greenhouse.io"}
GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job}?questions=true"
# Greenhouse's controls under the names the judge reads. A hidden field is never on the page, so it is never asked.
_GREENHOUSE_KINDS = {"input_file": "File", "input_text": "String", "textarea": "LongText",
                     "multi_value_single_select": "ValueSelect", "multi_value_multi_select": "MultiValueSelect"}
# A board that must hold her data, or keep it, puts a box on the page she has to tick. The API sends only the flag.
_GREENHOUSE_CONSENTS = {"requires_consent": "GDPR consent to process my personal data",
                        "requires_processing_consent": "GDPR consent to process my personal data",
                        "requires_retention_consent": "GDPR consent to retain my personal data",
                        # With a demographic survey, a required "I consent to <company> processing my responses" box
                        # (Block, MongoDB, Chime). Nothing on file ticks it, so a run there only stops at the Reviewer.
                        "demographic_data_consent_applies": "Consent to process my demographic survey responses"}

# A short fact the resume settles, so the Reviewer can check it against what is on file.
_RESUME_FACT = re.compile(r"(most recent|current|latest)\b.{0,20}\b(company|employer|job title|title)\b|\byears of\b.{0,50}\bexperience\b", re.I)
_SALARY = re.compile(r"salary|compensation|desired pay", re.I)
_RESUME_FILE = re.compile(r"resume|\bcv\b", re.I)
# Work only the owner can do or swear to. No answer on file can stand in for it.
_HUMAN_ONLY = re.compile(r"\bexercise\b|take[- ]home|writing sample|work sample|code sample|do not use (?:an? )?\bai\b|"
                         r"without (?:the use of |using )?(?:an? )?\bai\b|personally reviewed", re.I)
# Filler's Yes click takes the first option starting with that word, and only a bare one says just that.
_BARE_YESNO = re.compile(r"\s*(?:y(?:es)?|no?)\W*", re.I)
# "How did you hear" falls back through these when the form has no "Job board".
_SOURCE_OPTION = re.compile(r"job\s?board|linkedin|online|website|other", re.I)
_TEXT_KINDS = {"String", "LongText"}
# A voluntary survey choice she can always make: config.yaml says decline (eeo:, demographics: decline).
_DECLINE_OPTION = re.compile(r"decline|don.?t wish|do not wish|prefer not|not to (?:say|answer|disclose|self)|choose not", re.I)
# A street address is a fact on file or nothing. Drafted as a short answer, a model would make one up.
_ADDRESS = re.compile(r"\baddress\b|\bstreet\b|\bzip\b|\bpostal\b|\bpost ?code\b", re.I)
_LABELS = {"ready": "可以直接投", "essay": "要模型写短答", "ask": "等你答题", "never": "投不了", "unread": "还没读到表单"}


def _name(cfg: dict) -> str:
    return re.sub(r"\W+", "_", cfgmod.workspace_name(cfg)).strip("_").lower()


def forms_path(cfg: dict) -> Path:
    return Path(_root) / f"form_preflight.{_name(cfg)}.json"


def settings_path(cfg: dict) -> Path:
    return Path(_root) / f"runner_settings.{_name(cfg)}.json"


def enabled(cfg: dict) -> bool:
    return bool((cfg.get("agent_runner") or {}).get("preflight"))


def allow_essays(cfg: dict) -> bool:
    try:
        return json.loads(settings_path(cfg).read_text(encoding="utf-8")).get("allow_unread_essays") is True
    except (OSError, ValueError):
        return False


def set_allow_essays(cfg: dict, allowed: bool) -> None:
    _write(settings_path(cfg), {"allow_unread_essays": bool(allowed)})


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def read_forms(cfg: dict) -> dict:
    try:
        return json.loads(forms_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _description(*sources: dict) -> str:
    """The statement under a bare title, if the API sent one. `field` is plain JSON, so its keys come back as they are."""
    for source in sources:
        for key in ("descriptionPlain", "description", "descriptionHtml"):
            if isinstance(source.get(key), str) and source[key].strip():
                return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", source[key]))).strip()[:3000]
    return ""


def _ask_api(parts: list[str], query: str) -> dict:
    body = json.dumps({"operationName": "ApiJobPosting", "query": query,
                       "variables": {"organizationHostedJobsPageName": parts[0], "jobPostingId": parts[1]}}).encode()
    request = urllib.request.Request(ASHBY_API, data=body, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # GraphQL answers a query it cannot run with a 400. Any other failure says nothing about the query.
        if exc.code != 400 or query is PLAIN_QUERY:
            raise
        return {"errors": [{"message": "the API refused this query"}]}


def fetch_form(url: str) -> list[dict] | None:
    """The form's fields, or None when the posting no longer exists. A failed query raises: it says nothing about the form."""
    if urlparse(url).hostname in GREENHOUSE_HOSTS:
        return fetch_greenhouse_form(url)
    parts = urlparse(url).path.strip("/").split("/")
    reply = _ask_api(parts, _query["now"])
    if reply.get("errors") and _query["now"] is not PLAIN_QUERY:
        reply = _ask_api(parts, PLAIN_QUERY)
        if not reply.get("errors"):
            _query["now"] = PLAIN_QUERY
    if not isinstance(reply.get("data"), dict) or reply.get("errors"):
        raise ValueError("the form API answered with no data")
    posting = reply["data"].get("jobPosting")
    if not posting:
        return None
    entries = []
    for section in (posting.get("applicationForm") or {}).get("sections") or []:
        for entry in section.get("fieldEntries") or []:
            field = entry.get("field") or {}
            entries.append({"label": re.sub(r"\s+", " ", field.get("title") or "").strip(), "type": field.get("type") or "",
                            "required": bool(entry.get("isRequired")),
                            "options": [v.get("label") or "" for v in field.get("selectableValues") or []]})
            description = _description(field, entry)
            if description:
                entries[-1]["description"] = description
    return entries


def _greenhouse_posting(url: str) -> tuple[str, str]:
    """The board and job id, from /<board>/jobs/<id> or the embed form's ?for=<board>&token=<id>."""
    parsed = urlparse(url)
    parts, query = parsed.path.strip("/").split("/"), parse_qs(parsed.query)
    if parts[0] == "embed":
        board, job = (query.get("for") or [""])[0], (query.get("token") or [""])[0]
    elif len(parts) >= 3 and parts[1] == "jobs":
        board, job = parts[0], parts[2]
    else:
        board = job = ""
    # An application-request link names no posting the API knows, and that is a failed read, not a gone posting.
    if not re.fullmatch(r"[A-Za-z0-9_-]+", board) or not re.fullmatch(r"[0-9]+", job):
        raise ValueError("no Greenhouse board and job id in this link")
    return board, job


def _greenhouse_entry(question: dict, kind: str | None = None) -> dict | None:
    fields = [f for f in question.get("fields") or [] if f.get("type") != "input_hidden"]
    if not fields:
        return None
    # Resume and cover letter come as a file with a paste box beside it; the file is what the page asks for first.
    field = next((f for f in fields if f.get("type") == "input_file"), fields[0])
    entry = {"label": re.sub(r"\s+", " ", question.get("label") or "").strip(),
             "type": kind or _GREENHOUSE_KINDS.get(field.get("type"), field.get("type") or ""),
             "required": bool(question.get("required")), "options": [v.get("label") or "" for v in field.get("values") or []]}
    description = _description(question)
    if description:
        entry["description"] = description
    return entry


def fetch_greenhouse_form(url: str) -> list[dict] | None:
    """A Greenhouse form in the same entries as an Ashby one. A 404 is a posting the board no longer lists; any other failure raises."""
    board, job = _greenhouse_posting(url)
    request = urllib.request.Request(GREENHOUSE_API.format(board=board, job=job), headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            reply = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if not isinstance(reply, dict) or not isinstance(reply.get("questions"), list):
        raise ValueError("the form API answered with no questions")
    entries = [_greenhouse_entry(q) for q in reply["questions"]]
    # Location is a typeahead on the page; latitude and longitude beside it are hidden.
    entries += [_greenhouse_entry(q, "Location") for q in reply.get("location_questions") or []]
    # The voluntary surveys join only where the board makes them required: an optional one never holds a form back.
    for section in reply.get("compliance") or []:
        # EEOC titles come as keys ("VeteranStatus"); the page shows them as words.
        entries += [{**_greenhouse_entry({**q, "label": re.sub(r"(?<=[a-z])(?=[A-Z])", " ", q.get("label") or "")}), "survey": True}
                    for q in section.get("questions") or [] if q.get("required") is True and _greenhouse_entry(q)]
    survey = reply.get("demographic_questions") or {}
    entries += [{**_greenhouse_entry({"label": q.get("label"), "required": True,
                                      "fields": [{"type": q.get("type"), "values": q.get("answer_options") or []}]}), "survey": True}
                for q in survey.get("questions") or [] if q.get("required") is True]
    consents = []
    for rule in reply.get("data_compliance") or []:
        for flag, label in _GREENHOUSE_CONSENTS.items():
            # One box per consent: two of the same title on one form would read as a section heading.
            if rule.get(flag) is True and label not in consents:
                consents.append(label)
    entries += [{"label": label, "type": "Boolean", "required": True, "options": []} for label in consents]
    return [entry for entry in entries if entry]


def _usable(form: dict, now: float) -> bool:
    """Read within a day, and with questions in it. Anything else counts as never read, never as ready."""
    entries = form.get("entries")
    return now - form.get("at", 0) < MAX_AGE and (entries is None or any(e.get("required") for e in entries))


def refresh(cfg: dict, jobs: list[dict], fetch=fetch_form, pause: float = 0.25, now: float | None = None) -> dict:
    """Read the forms that are unread or older than a day. One bad read leaves that job unread, never ready."""
    now = now or time.time()
    forms = read_forms(cfg)
    read = failed = 0
    for job in jobs:
        cached = forms.get(job["job_id"])
        if cached and _usable(cached, now):
            continue
        try:
            entries = fetch(job["url"])
        except Exception:  # noqa: BLE001 — a network failure says nothing about the form
            failed += 1
            continue
        forms[job["job_id"]] = {"at": now, "company": job["company"], "title": job["title"], "url": job["url"], "entries": entries}
        read += 1
        if read % 20 == 0:
            with _lock:
                _write(forms_path(cfg), forms)
        if pause:
            time.sleep(pause)
    with _lock:
        _write(forms_path(cfg), forms)
    return {"read": read, "failed": failed}


def refresh_in_background(cfg: dict, load_jobs) -> bool:
    """Only one reader runs at a time; a second request while it runs does nothing."""
    if not _reader.acquire(blocking=False):
        return False

    def work():
        try:
            refresh(cfg, load_jobs())
        finally:
            _reader.release()
    try:
        threading.Thread(target=work, daemon=True, name="form-preflight").start()
    except BaseException:
        # No thread will ever release it, and a retry would wait on this reader for good.
        _reader.release()
        raise
    return True


def reading() -> bool:
    return _reader.locked()


def run_rules(cfg: dict, groups: list[dict]) -> list[list[str]]:
    """Ask Filler's own rules, in Node, what they would do with each question. One group per form."""
    node = shutil.which("node") or next((p for p in ("/opt/homebrew/bin/node", "/usr/local/bin/node") if Path(p).exists()), None)
    if not node:
        raise RuntimeError("node not found")
    script = cfgmod.ROOT / "tools" / "form_rules.cjs"
    payload = json.dumps({"profile": cfg.get("profile") or {}, "groups": groups})
    done = subprocess.run([node, str(script)], input=payload, capture_output=True, text=True, timeout=60, check=True)
    return json.loads(done.stdout)


def _choice_takes(entry: dict, said: dict) -> bool:
    """Would Filler land on a right option of this single choice?"""
    picked = said.get("picked") or ""
    # The scorer reads sentence options. With no score, all that is left is a blind click or a blank.
    if said.get("legal"):
        return said.get("legal_pick", -1) >= 0
    # "Yes, I am based in one of our offices" claims more than the Yes this rule stands behind.
    if said.get("yesno") and not said.get("choice") and not said.get("text"):
        return _BARE_YESNO.fullmatch(picked) is not None
    if said.get("choice") == "Job board":
        return _SOURCE_OPTION.search(" ".join(entry["options"])) is not None
    return bool(picked)


def _on_page(entry: dict) -> bool:
    """A title naming a statement the API did not send: the words she would be answering are only on the application page."""
    return not entry.get("description") and not RECORDING.search(entry["label"]) and confirmed_answers.statement_title(entry["label"], entry["options"])


def _judge(entry: dict, said: dict) -> tuple[str, str]:
    """Whether Filler can finish this required question, judged on what the control can hold."""
    label, kind, options = entry["label"], entry["type"], entry["options"]
    asked = f"{label} {entry.get('description', '')}".strip()
    if kind == "File":
        return ("ok", "") if _RESUME_FILE.search(label) else ("never", f"要另外传文件：{label}")
    # Filler answers a single-choice survey question from her eeo settings, or with the decline it offers.
    if entry.get("survey") and kind == "ValueSelect" and any(_DECLINE_OPTION.search(option) for option in options):
        return "ok", ""
    # The promise can sit in an option: "Please confirm" with one choice saying "I personally reviewed this".
    if _HUMAN_ONLY.search(" ".join([asked, *options])):
        return "never", f"要你本人完成：{label}"
    if said.get("how") == "confirmed":
        # Nothing on the page can tick these, so an answer on file does not get the form through.
        if kind == "MultiValueSelect":
            return "never", f"这道多选题程序勾不了，要你本人勾：{label}"
        # A sentence cannot press a Yes/No button. A preference takes whatever the form offers; a fact has to fit.
        fits = bool(said.get("yesno")) if kind == "Boolean" else not options or bool(said.get("picked") or said.get("soft"))
        return ("ok", "") if fits else ("ask", label)
    if _on_page(entry):
        return "ask", label
    if _SALARY.search(label):
        return "never", f"要报薪资，程序不替你报：{label}"
    # Filler may have a rule for these, but the Reviewer only accepts your own answer.
    if uncovered_question(asked, options):
        return "ask", label
    if kind == "Boolean":
        return ("ok", "") if said.get("yesno") else ("ask", label)
    if kind == "ValueSelect":
        return ("ok", "") if _choice_takes(entry, said) else ("ask", label)
    # Nothing ticks a multi-select, and no fact on file gives a date. A count goes into a box Filler does not type in.
    if kind in {"MultiValueSelect", "Date", "Number"}:
        return "ask", label
    # Filler's own field reader, run on this label. "City of birth" starts like "City", and only it tells them apart.
    # With nothing on file for that field it types nothing, and the question is still open.
    claimed = said.get("claimed") and said.get("value")
    if kind == "Location":
        # A typeahead takes her city only under a label asking where she is.
        return ("ok", "") if said.get("text") or said.get("claimed") in {"location", "country"} or said.get("place") else ("ask", label)
    # "Remote work preference" takes no Yes, so a rule that could only say Yes or No leaves the box blank.
    if kind in _TEXT_KINDS and said.get("how") == "rule" and said.get("typed") is False and not claimed:
        return "ask", label
    if said.get("how") or claimed or _RESUME_FACT.search(label):
        return "ok", ""
    if kind in _TEXT_KINDS:
        return ("ask", label) if _ADDRESS.search(label) else ("essay", label)
    return "ask", label


def _for_the_rules(cfg: dict, company: str | None) -> list[dict]:
    """Her answers, each with a pattern for its title alone: the API can send a title with no statement, and the page then decides."""
    answers = confirmed_answers.for_company(cfg, company)
    return [{**a, "title_topic": confirmed_answers.question_topic(a.get("question") or "?x")} for a in answers]


def _near_miss(answers: list[dict], entry: dict) -> bool:
    return any(confirmed_answers.reads_alike(item, f"{entry['label']} {entry.get('description', '')}") for item in answers)


def _stamp(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def assess(cfg: dict, rules=run_rules, now: float | None = None) -> dict:
    """Each usable form's verdict: ready, essay, ask, never or gone. Answering a question needs no new read."""
    now = now or time.time()
    forms = {job_id: form for job_id, form in read_forms(cfg).items() if _usable(form, now)}
    # The forms themselves are in the key: one ages out with no file changing.
    key = (_stamp(forms_path(cfg)), _stamp(confirmed_answers.path(cfg)), _stamp(cfgmod.ROOT / "extension" / "content.js"), tuple(forms))
    if _memo.get("key") == key and rules is run_rules:
        return _memo["value"]
    jobs = list(forms.items())
    live = [(job_id, form) for job_id, form in jobs if form.get("entries") is not None]
    groups = [{"company": form.get("company") or "", "confirmed": _for_the_rules(cfg, form.get("company")),
               "entries": [e for e in form["entries"] if e["required"]]} for _, form in live]
    answers = rules(cfg, groups) if groups else []
    result = {job_id: {"verdict": "gone", "company": form.get("company"), "title": form.get("title"), "url": form.get("url"),
                       "blocks": [], "asks": [], "essays": []}
              for job_id, form in jobs if form.get("entries") is None}
    for (job_id, form), said, group in zip(live, answers, groups):
        required = [e for e in form["entries"] if e["required"]]
        # Over the whole form: one required row among four optional ones is still a section title.
        seen = [(e["label"], e.get("description", "")) for e in form["entries"]]
        blocks, asks, essays = [], [], []
        for entry, answer in zip(required, said):
            asked = (entry["label"], entry.get("description", ""))
            verdict, detail = _judge(entry, answer)
            # The Reviewer stops on these whatever fills them, so the form would only stop on the page.
            if verdict == "ok" and answer.get("how") != "confirmed" and _near_miss(group["confirmed"], entry):
                verdict, detail = "ask", entry["label"]
            # A title repeated on one form heads a section. One answer would go into every row, and into every later form.
            if verdict == "ask" and seen.count(asked) > 1:
                verdict, detail = "never", f"这个标题在表上出现了 {seen.count(asked)} 次，要你本人填：{entry['label']}"
            if verdict == "never":
                if detail not in blocks:
                    blocks.append(detail)
            elif verdict == "ask":
                asks.append({"question": entry["label"], "description": entry.get("description", ""), "kind": entry["type"],
                             "options": ["Yes", "No"] if entry["type"] == "Boolean" else entry["options"], "on_page": _on_page(entry)})
            elif verdict == "essay":
                essays.append(detail)
        result[job_id] = {"verdict": "never" if blocks else "ask" if asks else "essay" if essays else "ready",
                          "company": form.get("company"), "title": form.get("title"), "url": form.get("url"),
                          "blocks": blocks, "asks": asks, "essays": essays}
    if rules is run_rules:
        _memo.update(key=key, value=result)
    return result


def runnable(cfg: dict, verdict: dict | None, essays_allowed: bool) -> bool:
    return bool(verdict) and (verdict["verdict"] == "ready" or (verdict["verdict"] == "essay" and essays_allowed))


def board(cfg: dict, pool: list[dict], rules=run_rules) -> dict:
    """The owner's one list: what can run, what waits for which answer, what will never run."""
    try:
        verdicts = assess(cfg, rules)
        error = ""
    except Exception as exc:  # noqa: BLE001 — without the rules nothing counts as ready, and the board says why
        verdicts, error = {}, f"读不了填表规则（{type(exc).__name__}），现在不会领取任何岗位。"
    counts = dict.fromkeys(_LABELS, 0)
    companies: dict[str, set] = {k: set() for k in _LABELS}
    # By title and statement: two statements under one title are two questions, and one answer covers only one.
    questions: dict[tuple, dict] = {}
    parked = []
    for job in pool:
        verdict = verdicts.get(job["job_id"])
        kind = verdict["verdict"] if verdict else "unread"
        if kind == "gone":
            continue
        counts[kind] += 1
        companies[kind].add(job["company"])
        if kind == "never":
            parked.append({"company": job["company"], "title": job["title"], "url": job["url"], "why": verdict["blocks"][:2]})
        for ask in (verdict or {}).get("asks", []):
            item = questions.setdefault((ask["question"], ask.get("description", "")),
                                        {**ask, "forms": 0, "companies": set(), "company": job["company"], "job_id": job["job_id"], "url": job["url"]})
            item["forms"] += 1
            item["companies"].add(job["company"])
    listed = sorted(questions.values(), key=lambda q: (-len(q["companies"]), -q["forms"], q["question"]))
    return {"counts": counts, "companies": {k: len(v) for k, v in companies.items()},
            "questions": [{**q, "companies": len(q["companies"])} for q in listed[:40]], "more_questions": max(0, len(listed) - 40),
            "never": parked[:30], "allow_unread_essays": allow_essays(cfg), "reading": reading(), "error": error}
