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
    r"we('| ha)ve received your application|thank you for your (interest|application)|"
    r"security code|verification code|verify your email|2-?step verification|security alert|"
    r"unsubscribe|newsletter|your job alert|job alerts?|new jobs?|weekly digest|password|"
    # LinkedIn/aggregator/course/promo notifications — never need a reply
    r"is hiring|new matches|impressions|visited your profile|new invitation|invitation to connect|"
    r"want to connect|posts? got|take the next step|next step in your career|new features?|"
    r"webinar|conference program|\bsurvey\b|subscription will renew|is now part of|"
    r"prepare for your upcoming visit|explore their network|cert(ificate)? with|"
    r"take the .* course|ready:|new videos?|just messaged you|sent new messages?|"
    r"latest .* jobs|career opportunities|register today|deadline|savings expire|"
    r"world cup|live demo|crash course|roundup|cheat sheet", re.I)
# Automated senders whose mail is never a personal reply. NOTE: LinkedIn 'hit-reply' /
# 'inmail' addresses ARE real recruiter messages — those are deliberately NOT matched here.
_SKIP_SENDER = re.compile(
    r"jobalerts|jobs-?noreply|jobs-?listings|job-?alerts|notifications-?noreply|"
    r"messages-?noreply|invitations?@linked|updates-?noreply@linked|invite@linked|"
    r"@udemy|coursera|glassdoor|@fidelity|teladoc|taskrabbit|meeboss|@simplify|"
    r"enotice@|ieee|@arrow|jackandjill|newsletter@|survey@|@m\.learn|"
    r"@nvidia|educative|@courses\.m|deeplearning|aaai|levels\.fyi|tickerkey|peeref|"
    r"@huntr|@expo\.dev|googlepixel|@email\.openai|@e\.link|redditmail|@reply\.snowflake|"
    r"editors-?noreply@linked|newsletters?-?n|news-?noreply|militaryonesource|"
    r"mydocbill|prudential|mailer-daemon|via LinkedIn <newsletters", re.I)
_REJECT = re.compile(
    r"not (moving|move) forward|regret to inform|unfortunately,? (we|after)|decided (to move|not to)|"
    r"pursuing other candidates|will not be (moving|proceeding)|no longer under consideration|"
    r"won't be moving forward", re.I)
_NOREPLY = re.compile(r"no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|mailer-daemon|automated@", re.I)


def _convo_key(from_addr: str, subject: str) -> str:
    """Stable conversation id: sender email + subject with Re:/Fwd: stripped. Lets us treat
    all messages in one recruiter thread as the same conversation, so dismissing/sending once
    stops the whole thread from being re-drafted."""
    import email.utils
    addr = email.utils.parseaddr(from_addr or "")[1].lower()
    subj = re.sub(r"^\s*(re|fwd|fw)\s*:\s*", "", (subject or "").strip(), flags=re.I)
    subj = re.sub(r"\s+", " ", subj).lower()
    return f"{addr}|{subj}"


def _client(cfg):
    mc, addr, pw = mail._creds(cfg)
    M = imaplib.IMAP4_SSL(mc.get("imap_host", "imap.gmail.com"))
    M.login(addr, pw)
    return M, addr


_MID = re.compile(r"<[^<>@\s]+@[^<>\s]+>")


def _replied_mids(cfg, days: int = 45) -> set[str]:
    """Message-IDs the user has ALREADY replied to — collected from her Sent Mail's
    In-Reply-To/References headers over the last `days`. A thread whose newest inbound
    message is in this set is done; we must not re-draft a reply for it. Cheap: one
    header-only pass over Sent Mail, no LLM."""
    try:
        M, _ = _client(cfg)
    except Exception:
        return set()
    out: set[str] = set()
    try:
        M.select('"[Gmail]/Sent Mail"')
        since = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = M.search(None, f"(SINCE {since})")
        ids = (data[0] or b"").split()[-500:]         # cap the scan to the recent window
        if ids:
            # ONE batched fetch (not 500 round-trips) — headers only, fast.
            typ, d = M.fetch(b",".join(ids), "(BODY.PEEK[HEADER.FIELDS (IN-REPLY-TO REFERENCES)])")
            for part in (d or []):
                blob = part[1] if isinstance(part, tuple) else part
                if not blob:
                    continue
                hdr = blob.decode("utf-8", "ignore") if isinstance(blob, (bytes, bytearray)) else str(blob)
                out.update(m.group(0) for m in _MID.finditer(hdr))
    except Exception:
        pass
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return out


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
    slot_txt = (("Her real availability windows to offer (phrase them naturally, don't dump "
                 "the whole list — pick the best 2–3 and invite them to pick a time):\n- "
                 + "\n- ".join(slots)) if slots else "No calendar availability available.")
    user = (
        f"{ex}\n\n---\nIncoming email:\nFrom: {msg['from']}\nSubject: {msg['subject']}\n\n"
        f"{msg['body'][:4000]}\n\n---\n{slot_txt}\n\nTriage and draft the reply as JSON."
    )
    ai = cfg.get("ai") or {}
    # Triage runs every 30 min on the API key — use a cheap model (inbox.model, default Haiku).
    # Résumé tailoring keeps Opus (it runs on the subscription, separately). Email classify +
    # short draft doesn't need Opus. Haiku 4.5 does NOT support adaptive thinking, so only add
    # thinking/effort for the Opus/Sonnet family.
    model = (cfg.get("inbox") or {}).get("model") or ai.get("model", "claude-haiku-4-5")
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    kwargs = {"model": model, "max_tokens": 1500, "system": _SYSTEM,
              "messages": [{"role": "user", "content": user}]}
    if "haiku" not in model:
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": ai.get("effort", "high")}
    resp = client.messages.create(**kwargs)
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


def triage(cfg: dict, max_llm: int = 12, days: int | None = None, limit: int = 60) -> dict:
    """Scan recent inbox, queue emails that need a reply. Returns a summary dict.
    `days` overrides the config poll window (owner wants a 28-day first sweep)."""
    inbox_cfg = cfg.get("inbox") or {}
    if days is None:
        days = int(inbox_cfg.get("poll_days", 3))
    mode = inbox_cfg.get("autosend_mode", "gated")     # gated | all | off
    conn = store.connect_user(cfg)

    heads = mail.search_recent(cfg, days=days, limit=limit)
    examples = _sent_examples(cfg)
    replied = _replied_mids(cfg, days=max(days, 45))    # threads she's already answered
    # Conversations she already dismissed or sent a reply for — don't re-draft their follow-ups.
    closed = {_convo_key(r["from_addr"], r["subject"]) for r in store.queue_closed_rows(conn)}
    # Prefer broad availability windows (whole day / morning / afternoon) — easier for a
    # recruiter to arrange around than three narrow 45-min slots. Fall back to slots, then
    # to the owner's stated availability.
    slots = []
    if gcal.available(cfg):
        offer = (cfg.get("calendar") or {}).get("offer", "windows")
        slots = gcal.free_windows(cfg) if offer == "windows" else gcal.free_slots(cfg)
    # No calendar connected → offer the owner's stated availability so scheduling replies still
    # propose real times instead of leaving a [[AVAILABILITY]] placeholder.
    if not slots and inbox_cfg.get("availability"):
        slots = [inbox_cfg["availability"]]

    queued, armed, skipped_replied, llm_used = 0, 0, 0, 0
    for h in heads:
        if _SKIP_SUBJECT.search(h["subject"] or "") or _SKIP_SENDER.search(h["from"] or ""):
            continue
        full = mail.get_message(cfg, h["uid"])
        mid = full.get("message_id") or f"uid:{h['uid']}"
        if store.queue_has(conn, mid):
            continue
        # Same conversation already dismissed/sent → skip the whole thread (her explicit
        # "I'm done with this one" — don't keep re-drafting its follow-up messages).
        if _convo_key(full["from"], full["subject"]) in closed:
            skipped_replied += 1
            continue
        # Already answered? If her Sent Mail replied to THIS message, it's handled — skip it
        # (a later inbound message in the same thread has its own Message-ID and still surfaces).
        if full.get("message_id") and full["message_id"].strip() in replied:
            skipped_replied += 1
            continue
        # No-reply "type": nothing to answer if we can't actually reply to the sender. Skip
        # before spending an LLM call.
        repliable = bool(full.get("reply_to")) and not _NOREPLY.search(full.get("reply_to") or "")
        if not repliable:
            continue
        if _REJECT.search(full["body"][:2000]):
            continue
        if llm_used >= max_llm:
            break
        llm_used += 1
        r = _classify_and_draft(cfg, full, slots, examples)
        if not r or not r.get("needs_reply"):
            continue

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
            "armed": armed, "skipped_replied": skipped_replied, "calendar": bool(slots)}


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
