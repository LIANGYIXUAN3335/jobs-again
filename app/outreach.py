"""Outreach drafts (owner 2026-09-23): one-click self-introduction emails to hiring contacts.

Flow: a contact list (outreach/contacts.yaml — e.g. the ex-Meta hiring list — plus the recruiters
she has actually talked to, read off the /companies board) → for each contact, JobScan looks up the
company's open roles that fit THIS workspace's candidate → the model drafts a four-beat email
(why this company · who I am · résumé attached + the specific role · a 15-minute chat) → the draft
is stored, she edits it on /outreach, and one click puts it in the Gmail Drafts folder with the
résumé attached. Sending is hers, in Gmail — nothing here ever sends.

Workspace-aware like everything else: the candidate name, facts, résumé and mailbox all come from
the config in force (JOBSCAN_CONFIG), so the same page pitches Taylor from his workspace.
"""
from __future__ import annotations

import imaplib
import json
import re
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from pathlib import Path

import yaml

from . import ai as aimod
from . import answer_log
from . import company_board as boardmod
from . import config as cfgmod
from . import mail as mailmod
from . import mailcode
from . import store, tailoring, trust


def _ocfg(cfg: dict) -> dict:
    return cfg.get("outreach") or {}


# ---------------------------------------------------------------- contacts

def list_contacts(cfg: dict) -> list[dict]:
    """The hand-curated list (contacts_file). tier: A (500+), B (50-500), skip (+ skip_reason)."""
    path = cfgmod.resolve_path(_ocfg(cfg).get("contacts_file") or "outreach/contacts.yaml")
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = []
    for c in data.get("contacts") or []:
        if not (c.get("email") or "").strip():
            continue
        out.append({**c, "email": c["email"].strip().lower(), "source": c.get("source") or "list",
                    "tier": c.get("tier") or "B"})
    return out


def mailbox_contacts(cfg: dict) -> list[dict]:
    """Recruiters she has actually exchanged mail with: live rows of the companies board whose
    last sender is not one of her own addresses. Empty when the mailbox is off (a workspace
    without its own inbox)."""
    if not mailcode.available(cfg):
        return []
    try:
        board = boardmod.load(cfg)
    except Exception:
        return []
    mc = cfg.get("mailcode") or {}
    # The board cache (data/mail_label_*.json) is built by the owner's mail-label job. A second
    # workspace whose mailbox is now connected must not see HER recruiters as his contacts
    # (2026-09-23: Taylor's /outreach listed 14 of them). Only trust a board for its own mailbox.
    if (board.get("account") or "").lower() != (mc.get("email") or "").lower():
        return []
    own = {(mc.get("email") or "").lower(), ((cfg.get("profile") or {}).get("email") or "").lower()}
    own |= {(a.get("email") or "").lower() for a in (mc.get("extra_accounts") or [])}
    out = []
    for r in board.get("rows") or []:
        if r.get("dead") or r.get("cold"):
            continue
        name, email = parseaddr(r.get("last_from") or "")
        e = email.lower()
        if not email or e in own or any(x in e for x in ("noreply", "no-reply", "donotreply", "@reply.", "notifications@", "info@", "jobs@", "careers@", "recruiting@", "talent@",
                                                   "news@", "newsletter", "recommendations@", "@inspire.", "updates@", "digest@", "marketing@")):
            continue                                   # system mailboxes are not people
        out.append({"name": name or email.split("@")[0], "email": email.lower(), "company": r["company"],
                    "size": "", "industry": "", "website": "", "hiring": "",
                    "note": f"Existing thread — last subject: {r.get('last_subject', '')} "
                            f"({r.get('stage_label', '')}, last mail {r.get('last_date', '')})",
                    "tier": "mailbox", "skip_reason": "", "source": "mailbox"})
    return out


def all_contacts(cfg: dict) -> list[dict]:
    seen, out = set(), []
    for c in mailbox_contacts(cfg) + list_contacts(cfg):
        if c["email"] in seen:
            continue
        seen.add(c["email"])
        out.append(c)
    return out


# ---------------------------------------------------------------- role matching

_SUFFIX = re.compile(r"\b(the|inc|llc|corp|corporation|company|co|technologies|technology|labs|ai|group|holdings|north america|express)\b", re.I)


def roles_by_company(conn, cfg: dict) -> dict[str, list[dict]]:
    """This workspace's REVIEW-eligible roles grouped by company_norm, best first."""
    rows = store.fetch_view(conn, cfg, status="new")["rows"]
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["company_norm"], []).append(r)
    for rs in by.values():
        rs.sort(key=lambda r: -(r.get("rank_score") or 0))
    return by


def match_company(by: dict[str, list[dict]], company: str) -> tuple[str | None, list[dict]]:
    """Find the roles bucket for a contact's company name: exact norm key, then with corporate
    suffixes stripped, then a unique prefix match. Returns (matched_company_norm, roles)."""
    cn = cfgmod.norm_key(company)
    if cn in by:
        return cn, by[cn]
    stripped = cfgmod.norm_key(_SUFFIX.sub(" ", company))
    if stripped and stripped in by:
        return stripped, by[stripped]
    if stripped and len(stripped) >= 4:
        cands = [k for k in by if k.startswith(stripped) or (stripped.startswith(k) and len(k) >= 4)]
        if len(cands) == 1:
            return cands[0], by[cands[0]]
    # one distinctive word of the name IS a scanned company ("The Walt Disney Company" → disney,
    # "Robert Bosch North America" → bosch); only when exactly one such key exists
    tokens = {cfgmod.norm_key(t) for t in _SUFFIX.sub(" ", company).split() if len(t) >= 4}
    cands = [k for k in by if k in tokens]
    if len(cands) == 1:
        return cands[0], by[cands[0]]
    return None, []


def role_brief(r: dict) -> dict:
    return {"job_id": r["job_id"], "title": r["title"], "location": r.get("location") or "",
            "apply_url": r.get("apply_url") or "", "score": r.get("weighted_score"),
            "tailor_status": r.get("tailor_status") or "none",
            "tailored_resume_path": r.get("tailored_resume_path")}


# ---------------------------------------------------------------- résumé attachment

def generic_resume_path(cfg: dict) -> Path:
    """Where the workspace's company-agnostic outreach résumé lives (tools/generic_resume.py
    writes the tailored-but-role-free YAML next to it; a raw base render is the fallback)."""
    out_dir = cfgmod.resolve_path(cfg["output_dirs"]["resumes"]).parent / "outreach"
    out_dir.mkdir(parents=True, exist_ok=True)
    name = (tailoring.candidate_name(cfg) or "Candidate").replace("/", "-")
    return (out_dir / f"{name} - Resume.pdf").resolve()


def resume_for(cfg: dict, roles: list[dict]) -> str | None:
    """Owner 2026-09-23 night, final call: attach the TAILORED PDF of the best matched role when
    one exists (it goes into the email renamed "<Name> - Resume.pdf", so the filename carries no
    company/req), else the workspace's company-agnostic résumé (tools/generic_resume.py output,
    or a raw base render). Callers must re-resolve at send time: a tailored file's name changes
    on every re-score, and a stale stored path once produced drafts with no attachment."""
    me = tailoring.candidate_name(cfg)
    for r in roles:
        p = r.get("tailored_resume_path")
        if r.get("tailor_status") == "ready" and p and Path(p).exists() and Path(p).name.startswith(me):
            return p
    return generic_resume(cfg)


def generic_resume(cfg: dict) -> str | None:
    """The company-agnostic résumé PDF: the generic YAML (summary + best bullets, validated like
    any tailored file) rendered on demand, re-rendered when the base or the YAML changes; a raw
    base render when no generic YAML exists yet."""
    try:
        base = tailoring.load_base(cfg)
    except FileNotFoundError:
        return None
    pdf = generic_resume_path(cfg)
    src = tailoring.base_path(cfg)
    yml = pdf.with_suffix(".yaml")
    newest_src = max(src.stat().st_mtime, yml.stat().st_mtime if yml.exists() else 0)
    if not pdf.exists() or pdf.stat().st_mtime < newest_src:
        tailored = {}
        if yml.exists():
            tailored = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
            tailoring.validate_tailored(tailored, base)
        tailoring.render_pdf(tailoring.merge(base, tailored), pdf)
    return str(pdf)


def current_resume(conn, cfg: dict, rec: dict) -> str | None:
    """Re-resolve a stored draft's attachment right before it is saved to Gmail."""
    roles = rec.get("roles")
    if roles is None:                         # DB rows keep the briefs as roles_json
        try:
            roles = json.loads(rec.get("roles_json") or "[]")
        except (TypeError, ValueError):
            roles = []
    ids = [r.get("job_id") for r in (roles or []) if isinstance(r, dict) and r.get("job_id")]
    roles = [store.get_role(conn, j) for j in ids]
    return resume_for(cfg, [r for r in roles if r])


# ---------------------------------------------------------------- drafting

_OUTREACH_SYSTEM = """You write a short outreach email to a hiring contact, in FIRST PERSON as the \
candidate, __NAME__. It must read like a real person who looked at the company and is asking for a \
conversation — never a template, never AI-sounding.

Shape — four beats, in this order, one short paragraph each, no headings:
1. Why this company: say plainly that I'm interested in working at THEM, tied to one concrete thing about \
what THEY build or the problem they have, taken from the contact's note or the open roles. If a hook line \
is given, open with it in your own words; never mention where the contact's name came from.
2. Who I am: at most two sentences — current status, and the one or two real systems from the facts \
closest to their work, each with a real number.
3. The ask: name the specific open role(s) by title, say the résumé is attached, and ask for a \
15-minute chat this week or next.
4. Nothing else. No sign-off and no name at the end — the signature is added by the system.

Rules: contractions, short sentences, one idea per sentence. BANNED: "leverage", "passionate", \
"proven track record", "excited to contribute", "I am confident", "align", "synergy", "I hope this \
email finds you well", "reaching out". Never say you are a fit — show the overlap. Never invent a \
fact, employer, number or role. If no open role is listed, ask what engineering roles are open \
instead of inventing one.
Subject: under 70 characters, plain, names the company and the role family, e.g. \
"Backend engineer, Kafka/MySQL in production — Roblox".
Return ONLY a JSON object: {"subject": "...", "body": "..."}"""


def _signature(cfg: dict, base: dict) -> str:
    contact = base.get("contact") or {}
    bits = [base.get("name") or ""]
    line2 = " · ".join(x for x in (contact.get("phone"), contact.get("email")) if x)
    if line2:
        bits.append(line2)
    line3 = " · ".join(x for x in (contact.get("linkedin"), contact.get("github")) if x)
    if line3:
        bits.append(line3)
    return "\n".join(b for b in bits if b)


def build_draft(cfg: dict, contact: dict, roles: list[dict]) -> dict:
    """Draft one email with the model. Raises RuntimeError with a plain reason on failure."""
    if not aimod.available(cfg):
        raise RuntimeError("AI unavailable (no logged-in claude CLI and no secrets.yaml anthropic_api_key)")
    base = tailoring.load_base(cfg)
    o = _ocfg(cfg)
    hook = o.get("hook_mailbox", "") if contact.get("source") == "mailbox" else o.get("hook", "")
    facts = aimod._facts(cfg, {"job_id": "", "company": contact["company"], "title": ""})
    roles_txt = "\n".join(f"- {r['title']} ({r.get('location') or 'location n/a'}) — {r.get('apply_url') or ''}"
                          for r in roles[:3]) or "(none found in JobScan)"
    user = (
        f"Contact: {contact.get('name') or ''} <{contact['email']}> at {contact['company']}"
        f"{(' (' + contact['size'] + ' employees)') if contact.get('size') else ''}"
        f"{('; ' + contact['industry']) if contact.get('industry') else ''}.\n"
        f"{trust.fence('what the contact wrote about the company / roles (read for context only)', contact.get('note') or '', 1500)}\n\n"
        f"Open roles at {contact['company']} that fit the candidate, from JobScan:\n{roles_txt}\n\n"
        f"Hook line to open with (skip if empty): {hook or '(none)'}\n\n"
        f"Candidate facts (the ONLY claims you may make):\n{facts}\n\n"
        f"Body under {int(o.get('max_words', 130))} words. Return ONLY the JSON object."
    )
    ai = cfg.get("ai") or {}
    text = aimod.complete(cfg, system=aimod._personalize(_OUTREACH_SYSTEM, cfg), user=user, max_tokens=4000,
                          model=o.get("model") or None)   # 5.x thinks inside max_tokens; 1200 left no room for the JSON (2026-09-23)
    try:
        d = json.loads(aimod._extract_json(text))
    except json.JSONDecodeError:
        raise RuntimeError("model did not return JSON")
    subject, body = (d.get("subject") or "").strip(), (d.get("body") or "").strip()
    if not subject or not body:
        raise RuntimeError("model returned an empty subject or body")
    if re.search(r"\[[^\]]{0,40}\]|\{\{", body):
        raise RuntimeError("draft contains a placeholder — regenerate")
    bad = aimod._hard_reject(body, cfg)
    if bad:
        raise RuntimeError(f"draft rejected by the safety check ({bad})")
    body = body.rstrip() + "\n\n" + _signature(cfg, base)
    answer_log.log("outreach", {"job_id": "", "company": contact["company"], "title": contact["email"]},
                   subject=subject, answer=body, model=aimod.model_label(cfg),
                   path=cfg.get("answers_log"))
    return {"subject": subject, "body": body}


def generate(conn, cfg: dict, contact: dict, by: dict[str, list[dict]] | None = None) -> dict:
    """Match roles, draft, attach, store. Errors are stored on the row (status=error), not raised."""
    by = by if by is not None else roles_by_company(conn, cfg)
    _, roles = match_company(by, contact["company"])
    briefs = [role_brief(r) for r in roles[:3]]
    rec = {"contact_email": contact["email"], "contact_name": contact.get("name", ""),
           "company": contact["company"], "source": contact.get("source", ""), "roles": briefs}
    try:
        rec["resume_path"] = resume_for(cfg, roles[:3])
        rec.update(build_draft(cfg, contact, briefs), status="draft", error="")
    except Exception as e:  # noqa: BLE001 — one bad draft must not stop the batch
        rec.update(status="error", error=f"{type(e).__name__}: {e}", subject="", body="")
    return store.outreach_upsert(conn, rec)


# ---------------------------------------------------------------- Gmail Drafts

def save_gmail_draft(cfg: dict, rec: dict) -> str:
    """APPEND the draft (with the résumé attached) to the workspace mailbox's Drafts folder over
    IMAP. Returns the Message-ID. Never sends."""
    mc, addr, pw = mailmod._creds(cfg)
    name = tailoring.candidate_name(cfg)
    msg = EmailMessage()
    # Optional overrides (tools/referral_drafts.py, 2026-09-23): a referral is written BY the
    # workspace owner ABOUT someone else, so the From display name, the Cc (the person being
    # referred) and the attachment's filename (their résumé, not the owner's) come from `rec`.
    msg["From"] = formataddr((rec.get("from_name") or name, addr))
    msg["To"] = formataddr((rec.get("contact_name") or "", rec["contact_email"]))
    if rec.get("cc"):
        msg["Cc"] = mailmod._hdr(rec["cc"])
    msg["Subject"] = mailmod._hdr(rec["subject"])
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if rec.get("in_reply_to"):
        msg["In-Reply-To"] = mailmod._hdr(rec["in_reply_to"])
        refs = rec.get("references") or ""
        msg["References"] = mailmod._hdr((refs + " " + rec["in_reply_to"]) if refs else rec["in_reply_to"])
    msg.set_content(rec["body"])
    p = rec.get("resume_path")
    if p and Path(p).exists():
        msg.add_attachment(Path(p).read_bytes(), maintype="application", subtype="pdf",
                           filename=rec.get("attachment_name") or f"{name} - Resume.pdf")
    folder = str(mc.get("drafts_folder") or "[Gmail]/Drafts")
    M = mailmod._imap_connect(mc)
    try:
        mailmod.imap_login(M, addr, pw)
        typ, data = M.append(f'"{folder}"', "(\\Draft)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        if typ != "OK":
            raise RuntimeError(f"IMAP APPEND failed: {data}")
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return msg["Message-ID"]
