"""Inbox triage for the job-search mailbox: find emails that need a reply (recruiter /
scheduling / online-assessment), draft a reply in the user's voice (learned from her Sent
mail), propose real calendar slots when availability is asked, and queue them. Low-risk
scheduling replies are ARMED to auto-send after an undo window; everything else waits for the
user. Read-only on the mailbox except starring handled messages; sending goes through mail.py.
"""
from __future__ import annotations

import datetime
import imaplib
import json
import re

from . import ai as aimod
from . import config as cfgmod
from . import gcal
from . import mail
from . import store

# Header-level skips — cheap, avoid a full fetch + LLM call for obvious non-actionables.
_SKIP_SUBJECT = re.compile(
    r"application (was )?(received|submitted|reviewed)|thank you for applying|thanks for applying|"
    r"we('| ha)ve received your application|security code|verification code|verify your email|"
    r"unsubscribe|newsletter|your job alert|new jobs|weekly digest|password", re.I)
_REJECT = re.compile(
    r"not (moving|move) forward|regret to inform|unfortunately,? (we|after)|decided (to move|not to)|"
    r"pursuing other candidates|will not be (moving|proceeding)|no longer under consideration|"
    r"won't be moving forward", re.I)
_NOREPLY = re.compile(r"no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|mailer-daemon|automated@", re.I)


def _client(cfg):
    mc, addr, pw = mail._creds(cfg)
    M = imaplib.IMAP4_SSL(mc.get("imap_host", "imap.gmail.com"))
    M.login(addr, pw)
    return M, addr


def _sent_examples(cfg, limit=4) -> list[str]:
    """A few of the user's own recent sent replies, as tone few-shots for drafting."""
    try:
        M, _ = _client(cfg)
    except Exception:
        return []
    out = []
    try:
        M.select('"[Gmail]/Sent Mail"')
        typ, data = M.search(None, "ALL")
        ids = (data[0] or b"").split()
        for num in reversed(ids[-limit * 2:]):
            typ, d = M.fetch(num, "(RFC822)")
            if not d or not d[0]:
                continue
            import email as _email
            body = mail._plain_body(_email.message_from_bytes(d[0][1]))
            body = re.sub(r"\n>.*", "", body)                     # strip quoted history
            body = re.sub(r"\s+\n", "\n", body).strip()
            if 40 < len(body) < 900:
                out.append(body[:700])
            if len(out) >= limit:
                break
    except Exception:
        pass
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return out


_SYSTEM = """You triage a job-seeker's inbox and draft email replies for her. She is Casey \
Example (legal Alex Example), a backend/distributed-systems software engineer. Reply in the \
FIRST PERSON as Casey.

For the email given, decide:
- needs_reply: true only if a human is waiting on HER response (recruiter reaching out, \
scheduling/availability request, online-assessment or take-home instructions needing a reply, \
a question to her). false for pure confirmations, rejections, newsletters, receipts.
- category: "scheduling" | "assessment" | "recruiter" | "other"
- asks_availability: true if they ask when she's free / to pick a time / to schedule.
- confidence: "high" | "medium" | "low" (how sure you are of needs_reply + that the draft is safe to send).
- draft: the reply body. Warm, concise, professional, first person, no corporate filler, use \
contractions, match the tone of her past replies shown. No subject line, no signature block \
beyond "Best,\\nCasey". If they asked for availability AND slots are provided below, offer \
those specific slots naturally. If availability is asked but NO slots are provided, write the \
reply but leave a clear "[[AVAILABILITY]]" placeholder where times go.

Return ONLY a JSON object: {"needs_reply":bool,"category":str,"asks_availability":bool,\
"confidence":str,"draft":str}."""


def _classify_and_draft(cfg, msg, slots, examples):
    key = aimod.load_key(cfg)
    if not key:
        return None
    ex = "\n\n".join(f"Example of her past reply:\n{e}" for e in examples) or "(no past replies on file)"
    slot_txt = ("Available slots to offer: " + "; ".join(slots)) if slots else "No calendar slots available."
    user = (
        f"{ex}\n\n---\nIncoming email:\nFrom: {msg['from']}\nSubject: {msg['subject']}\n\n"
        f"{msg['body'][:4000]}\n\n---\n{slot_txt}\n\nTriage and draft the reply as JSON."
    )
    ai = cfg.get("ai") or {}
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=ai.get("model", "claude-opus-4-8"),
        max_tokens=1500,
        thinking={"type": "adaptive"},
        output_config={"effort": ai.get("effort", "high")},
        system=_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    try:
        return json.loads(aimod._extract_json(text))
    except Exception:
        return None


def _star(cfg, uid):
    try:
        M, _ = _client(cfg)
        M.select("INBOX")
        M.store(str(uid).encode(), "+FLAGS", "\\Flagged")
        M.logout()
    except Exception:
        pass


def triage(cfg: dict, max_llm: int = 12) -> dict:
    """Scan recent inbox, queue emails that need a reply. Returns a summary dict."""
    inbox_cfg = cfg.get("inbox") or {}
    days = int(inbox_cfg.get("poll_days", 3))
    mode = inbox_cfg.get("autosend_mode", "gated")     # gated | all | off
    conn = store.connect_user(cfg)

    heads = mail.search_recent(cfg, days=days, limit=50)
    examples = _sent_examples(cfg)
    slots = gcal.free_slots(cfg) if gcal.available(cfg) else []

    queued, armed, llm_used = 0, 0, 0
    for h in heads:
        if _SKIP_SUBJECT.search(h["subject"] or ""):
            continue
        full = mail.get_message(cfg, h["uid"])
        mid = full.get("message_id") or f"uid:{h['uid']}"
        if store.queue_has(conn, mid):
            continue
        if _REJECT.search(full["body"][:2000]):
            continue
        if llm_used >= max_llm:
            break
        llm_used += 1
        r = _classify_and_draft(cfg, full, slots, examples)
        if not r or not r.get("needs_reply"):
            continue

        repliable = bool(full.get("reply_to")) and not _NOREPLY.search(full.get("reply_to") or "")
        low_risk = (r.get("category") == "scheduling" and r.get("confidence") == "high"
                    and repliable and slots and "[[AVAILABILITY]]" not in (r.get("draft") or ""))
        auto = (mode == "all" and repliable) or (mode == "gated" and low_risk)
        status = "armed" if auto else "pending"

        store.queue_add(conn, {
            "msg_id": mid, "uid": h["uid"], "from_addr": full["from"],
            "from_name": full["from"], "reply_to": full.get("reply_to"),
            "subject": full["subject"], "snippet": (full["body"] or "")[:280],
            "category": r.get("category"), "asks_availability": int(bool(r.get("asks_availability"))),
            "repliable": int(repliable), "draft": r.get("draft"),
            "references_hdr": full.get("references"), "in_reply_to": full.get("message_id"),
            "status": status, "found_at": store.la_now_iso(), "sent_at": None,
        })
        queued += 1
        if status == "armed":
            armed += 1
        _star(cfg, h["uid"])
    return {"scanned": len(heads), "llm_used": llm_used, "queued": queued,
            "armed": armed, "calendar": bool(slots)}


def send_due(cfg: dict) -> list[dict]:
    """Send any ARMED replies whose undo window has elapsed. Returns what was sent."""
    conn = store.connect_user(cfg)
    undo = int((cfg.get("inbox") or {}).get("undo_minutes", 30))
    now = datetime.datetime.now(store.LA)
    sent = []
    for it in store.queue_list(conn, status="armed"):
        try:
            found = datetime.datetime.fromisoformat(it["found_at"])
        except Exception:
            continue
        if (now - found).total_seconds() < undo * 60:
            continue                                          # still inside the undo window
        try:
            mail.send_reply(cfg, it["reply_to"], it["subject"], it["draft"],
                            in_reply_to=it["in_reply_to"], references=it["references_hdr"])
            store.queue_mark(conn, it["msg_id"], "sent", store.la_now_iso())
            sent.append({"to": it["reply_to"], "subject": it["subject"]})
        except Exception as e:  # noqa: BLE001
            store.queue_mark(conn, it["msg_id"], "pending")   # fall back to manual on failure
            print(f"  auto-send failed for {it['subject']}: {e}")
    return sent
