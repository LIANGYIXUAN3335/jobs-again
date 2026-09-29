"""File the job-search mailbox by company: one Gmail label `Interviews/<Company>` per company,
applied to the WHOLE thread, so no company's history is scattered across 263 starred messages.

Why threads and not messages: a single interview process is a conversation — the recruiter's
first note, your reply, the scheduler's confirmation, the reschedule. Starring catches whichever
message you happened to be looking at; labelling the thread catches the rest.

Why COPY and not MOVE: in Gmail a label IS a folder, and IMAP COPY into a label just adds that
label. The message stays in the inbox, keeps its star, keeps every other label. This tool is
strictly additive — it never moves, deletes, unstars or archives anything.

Company resolution is three-tier and deliberately refuses to guess:
  domain      cipherdata.ai -> CipherData          (after stripping ATS/vendor senders)
  gazetteer   "Snowflake Hiring Team", "Alex <> Pinterest" matched against the 235 companies
              in the jobs DB plus a curated list of ones you interviewed with but never applied to
  unresolved  -> review queue, printed for you, never labelled

Nothing is written without --apply, and --apply only touches the `auto` tier unless you pass
--include-review.

    python -m tools.mail_label                     # dry run: what would be labelled, by company
    python -m tools.mail_label --show Google       # the actual threads behind one company
    python -m tools.mail_label --apply             # create labels + file the auto tier
"""
from __future__ import annotations

import argparse
import base64
import collections
import email
import imaplib
import json
import pathlib
import re
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import applied_scan  # noqa: E402
from app import config as cfgmod  # noqa: E402
from app import mail, store  # noqa: E402

ALL_MAIL = '"[Gmail]/All Mail"'
CACHE = pathlib.Path(__file__).resolve().parents[1] / "data" / "mail_label_scan.json"
BUCKET_STATE = pathlib.Path(__file__).resolve().parents[1] / "data" / "mail_label_buckets.json"


def _incremental_with_retry(cfg: dict, *, days: int, root: str, dry: bool,
                            attempts: int = 3) -> dict:
    """Gmail IMAP occasionally closes the socket during batched header reads."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return incremental(cfg, days=days, root=root, dry=dry)
        except (imaplib.IMAP4.abort, OSError) as e:
            last = e
            if i == attempts - 1:
                break
            wait = 5 * (i + 1)
            print(f"IMAP transport error; retrying in {wait}s ({i + 1}/{attempts}): {e}",
                  file=sys.stderr, flush=True)
            time.sleep(wait)
    raise last or RuntimeError("mail label incremental failed")

# Senders that are plumbing, not the employer. The company must come from the display name or
# the subject for these — "sample-bbdb2b94@example.com" is Ashby's mail server, not a company you
# interviewed with, and labelling by domain here would create an `Interviews/Ashbyhq` junk drawer.
_VENDOR_DOMAINS = {
    "ashbyhq.com", "greenhouse.io", "us.greenhouse-mail.io", "greenhouse-mail.io", "lever.co",
    "hire.lever.co", "myworkday.com", "myworkdayjobs.com", "icims.com", "talent.icims.com",
    "successfactors.com", "taleo.net", "smartrecruiters.com", "jobvite.com", "applytojob.com",
    "breezy.hr", "breezy-mail.com", "matroid.breezy-mail.com", "workable.com", "ashby.hq",
    "comeet.co", "comeet-notifications.com", "comeet.com",
    "goodtime.io", "modernloop.io", "interviews.modernloop.io", "calendly.com", "hire.google.com",
    "codesignal.com", "hackerrank.com", "hackerrankforwork.com", "karat.io", "coderpad.io",
    "codility.com", "woven.teams", "linkedin.com", "hit-reply.linkedin.com",
    "inmail-hit-reply.linkedin.com", "ziprecruiter.com", "indeed.com", "indeedemail.com",
    "glassdoor.com", "wellfound.com", "angel.co", "dice.com", "monster.com", "visage-mail.com",
    "rexpandjob.com", "weekdaymail.com", "weekdayhire.com", "weekdayemail.com", "gmail.com",
    "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com", "proton.me", "docusign.net",
    "goodtime.com", "paraform.com", "aweber.com", "mailchimp.com", "sendgrid.net",
    "notifications.google.com", "drive-shares-dm.google.com", "docs.google.com",
    "calendar-notification.google.com", "calendar-server.bounces.google.com",
}

# Mail that is starred but is not a job thread. Kept narrow and evidence-based: these are the
# domains that showed up in the actual starred set and are unambiguously something else.
_NOISE_DOMAINS = {
    "coursera.org", "m.learn.coursera.org", "ieee.org", "em3009.ieee.org", "deliver.ieee.org",
    "peeref.com", "simplepractice.com", "notifications.lyrahealth.com", "memberclicks-mail.net",
    "mailer.predictiveindex.com", "edgecity.live", "wethehobby.com", "dhs.gov", "uscis.gov",
    "manus.im", "news.manus.im", "educative.io", "formation.dev", "deeplearning.ai", "udemy.com",
    "students.udemy.com", "aaai.org", "wiley.com", "email2.wiley.com", "teladoc.com",
    "teladochealth.com", "tickerkey.com", "notion.so", "simplify.jobs", "monster.com",
    "notifications.monster.com", "wellfound.com", "hi.wellfound.com",
}

# Subdomain noise to peel off before reading a domain as a company name.
_DOMAIN_PREFIXES = re.compile(
    r"^(?:mail|email|e|em\d*|mailer|notify|notifications|no-?reply|noreply|reply|talent|careers|"
    r"career|jobs|hire|hiring|recruiting|recruit|apply|info|team|news|updates?|smtp|bounce[sd]?|"
    r"link|click|marketing|inbound|support|help|xwf|my)\.")

# Two-part public suffixes that must not be mistaken for the registrable name.
_MULTI_TLD = {"co.uk", "com.au", "co.jp", "com.br", "co.in", "com.cn", "co.nz", "com.sg",
              "gov.sg", "gov.uk", "org.uk", "ac.uk", "com.hk", "co.il", "com.tw", "co.kr"}

# The company you wrote TO is in the subject, not the domain: your own outreach threads
# ("Interested in Engineering Opportunities at Embedding VC") and intro threads
# ("Alex <> Zinc Collective") both name it in a fixed position.
_SUBJECT_COMPANY = [
    re.compile(r"opportunit(?:y|ies)\s+(?:at|with)\s+(.{2,40}?)\s*$", re.I),
    re.compile(r"<>\s*(.{2,40}?)\s*$"),
    re.compile(r"\bapplying to\s+(.{2,40}?)\s*$", re.I),
    re.compile(r"\bapplication (?:to|at|with)\s+(.{2,40}?)\s*$", re.I),
    re.compile(r"(?:role|position|interview|engineer\w*)\s+(?:at|with)\s+(.{2,40}?)\s*$", re.I),
]
# Trailing junk that clings to a company name pulled out of a subject line.
_TRIM = re.compile(r"^[\s\-–—:,]+|[\s\-–—:,.!?]+$|\s*\((?:vc|inc|llc)[^)]*\)\s*$", re.I)

# Companies interviewed with that never entered the jobs DB (recruiter-sourced, referral, or
# pre-JobScan). Domain resolution catches most; these are the ones whose mail arrives from a
# vendor domain only, so a gazetteer entry is the only way to see them.
_EXTRA_COMPANIES = [
    "CipherData", "Anytime AI", "Supermicro", "Super Micro Computer", "Snorkel AI", "Snorkel",
    "Upstart", "PathAI", "Hadrian", "Hadrian Automation", "Lucid Motors", "Lucid", "OpenArt",
    "OpenArt AI", "Prudential", "Airwallex", "Kognitos", "Altruist", "Dagster Labs", "Matroid",
    "EvolutionaryScale", "Ropes AI", "OCBridge", "Shared Context", "Zinc Collective",
    "FactMachine", "Nuvo", "Skymavis", "Sky Mavis", "Arrow Electronics", "Atticus", "Airbnb",
    "Freshworks", "Docusign", "Temu", "Cohere", "Anthropic", "Pinterest", "Snowflake", "Amazon",
    "Google", "Microsoft", "Apple", "Meta", "Ford", "Nova Ltd",
]

# Alias -> canonical, so `careers.ford.com`, "Ford Motor Company" and "Exciting Opportunity at
# Ford" all land in one label instead of three.
_CANON = {
    "amazon web services (aws)": "Amazon", "aws": "Amazon", "amazon jobs": "Amazon",
    "mail.amazon.jobs": "Amazon", "amazon.jobs": "Amazon",
    "ford motor company": "Ford", "super micro computer": "Supermicro",
    "snorkel ai": "Snorkel", "openart ai": "OpenArt", "hadrian automation": "Hadrian",
    "lucid motors": "Lucid", "sky mavis": "Skymavis", "anytime-ai": "Anytime AI",
    "cipherdata": "CipherData", "pathai": "PathAI", "ocbridge": "OCBridge",
    "dagsterlabs": "Dagster Labs", "evolutionaryscale": "EvolutionaryScale",
    "google llc": "Google", "alphabet": "Google", "meta platforms": "Meta", "facebook": "Meta",
    "hugrecruitment": "Hug Recruitment", "erginc": "ERG Inc", "adp": "ADP", "ibm": "IBM",
    "sap": "SAP", "tsmc": "TSMC", "amd": "AMD", "nvidia": "Nvidia", "hpe": "HPE",
}

# A job thread says one of these somewhere. Used only to sort resolved-but-doubtful mail into
# the review tier — never to label on its own.
_JOBBISH = re.compile(
    r"interview|recruit|hiring|your application|applying|candidate|phone screen|onsite|"
    r"on-site|offer|role|position|opportunit|assessment|coding|take[- ]home|schedul|"
    r"availabilit|next steps?|referral|job (?:id|req)|req\b|resume|résumé|cv\b|"
    r"talent|screen\b|loop\b|debrief|compensation|\bHR\b|hiring manager|"
    r"engineer|developer|technical staff|\bSWE\b|scientist|"
    r"(?:talk|chat|call|meet(?:ing)?|conversation) with", re.I)


# Mass mail. Every one of these names companies and reads as hiring-related, so keyword tiering
# alone lets them through — they have to be recognised for what they are: broadcasts, not a
# process you are in. (`_JOBBISH` says "this is about jobs"; this says "nobody wrote it to you".)
_BULK = re.compile(
    r"job alert|jobs? for you|new jobs?\b|new matches|recommended for you|apply now to|"
    r"hiring trends|we'?re hiring\b|roundup|spotlight|newsletter|digest\b|webinar|"
    r"accepted your invitation|explore their network|viewed your profile|"
    r"people you may know|top (?:companies|picks)|early bird|save your (?:spot|workflows)|"
    r"unsubscribe|weekly|daily digest|just dropped|free trial|credits?\b|"
    r"course|enroll|certificat|conference|call for papers|product news", re.I)


def _dec(s) -> str:
    return applied_scan._decode(s)


def _norm(s: str) -> str:
    return applied_scan._norm(s)


def _display(sender: str) -> str:
    """`"Snowflake Hiring Team" <sample-bbdb2b94@example.com>` -> `Snowflake Hiring Team`."""
    s = re.sub(r"<[^>]*>", " ", sender or "")
    s = re.sub(r"[\w.+-]+@[\w.-]+", " ", s)                   # bare addresses too
    return re.sub(r"\s+", " ", s).strip().strip('"').strip()


def _domain(sender: str) -> str:
    m = re.search(r"@([\w.-]+)", sender or "")
    return (m.group(1) if m else "").lower().strip(">").strip()


def _registrable(dom: str) -> str:
    """`careers.ford.com` -> `ford`; `talent.icims.com` -> `icims`. Empty for junk."""
    d = _DOMAIN_PREFIXES.sub("", dom or "")
    while _DOMAIN_PREFIXES.match(d):
        d = _DOMAIN_PREFIXES.sub("", d)
    parts = d.split(".")
    if len(parts) < 2:
        return ""
    tail2 = ".".join(parts[-2:])
    name = parts[-3] if (tail2 in _MULTI_TLD and len(parts) >= 3) else parts[-2]
    return name if len(name) >= 2 else ""


def _is_vendor(dom: str) -> bool:
    if not dom:
        return True
    if dom in _VENDOR_DOMAINS:
        return True
    return any(dom.endswith("." + v) or dom == v for v in _VENDOR_DOMAINS)


def _is_noise(dom: str) -> bool:
    return bool(dom) and any(dom == n or dom.endswith("." + n) for n in _NOISE_DOMAINS)


def _canon(name: str) -> str:
    key = re.sub(r"\s+", " ", (name or "").strip()).lower()
    key = re.sub(r"[,.]?\s*(inc|llc|ltd|corp|corporation|co)\.?$", "", key).strip()
    if key in _CANON:
        return _CANON[key]
    # "Alterion.ai" and "Alterion" are one company and must not become two labels. A domain
    # suffix glued to a name is never part of the name.
    stripped = re.sub(r"\.(ai|io|com|co|dev|app|so|inc)$", "", name.strip(), flags=re.I)
    return _CANON.get(stripped.lower(), stripped)


def _gazetteer(cfg) -> list[tuple[re.Pattern, str]]:
    """(word-boundary pattern, canonical name), longest name first so `Amazon Web Services`
    is tried before `Amazon`."""
    names: set[str] = set(_EXTRA_COMPANIES)
    try:
        conn = store.connect_view(cfg)
        try:
            for (c,) in conn.execute("SELECT DISTINCT company FROM jobs.roles"):
                if c and len(c) >= 3:
                    names.add(c.strip())
        finally:
            conn.close()
    except Exception as e:                                    # DB is a bonus, not a dependency
        print(f"  (jobs DB unavailable, using built-in list only: {e})", file=sys.stderr)
    out = []
    for n in sorted(names, key=len, reverse=True):
        if len(n) < 3 or n.lower() in {"the", "ai", "inc", "llc"}:
            continue
        out.append((re.compile(r"(?<![\w-])" + re.escape(n) + r"(?![\w-])", re.I), _canon(n)))
    return out


def _case_ok(matched: str, gname: str) -> bool:
    """全小写的普通英文词不当公司名（2026-08-03 "Loop 公司"事故）：营销邮件标题里的
    "couldn't pass the loop" / "interview loop" 曾造出一家叫 Loop 的公司。真公司提及
    带大写（"Loop | SWE"）；官方名字本身就是全小写的（gname 全小写）不受此限。"""
    return matched != matched.lower() or gname == gname.lower()


def _label_name(company: str) -> str:
    """Gmail label component: no `/` (would nest), no quotes, single-spaced. Canonicalised HERE
    as well as at scan time, so a naming fix takes effect on the existing cache without a
    2-minute re-read of the mailbox."""
    s = re.sub(r"[\/\\\"]", " ", _canon(company or ""))
    return re.sub(r"\s+", " ", s).strip()


# The bucket is a directory, not a field: `Interviews/1-进行中/Google`. Numbered so Gmail's
# alphabetical sidebar puts them in funnel order rather than by Chinese collation.
BUCKETS = {"live": "1-进行中", "cold": "2-已冷", "dead": "3-已挂"}


def _bucket_of(row: dict) -> str:
    return "dead" if row.get("dead") else ("cold" if row.get("cold") else "live")


def _utf7(name: str) -> str:
    """Encode a mailbox name as modified UTF-7 (RFC 3501 §5.1.3).

    IMAP mailbox names are not UTF-8 and imaplib encodes its arguments as ASCII, so a label
    like `Interviews/1-进行中/Google` raises UnicodeEncodeError before it ever reaches Gmail.
    Modified UTF-7 is base64 of UTF-16BE, with `,` for `/` and no padding, wrapped in `&`…`-`.
    """
    out, buf = [], []

    def flush():
        if buf:
            b = base64.b64encode("".join(buf).encode("utf-16-be")).decode("ascii")
            out.append("&" + b.rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in name:
        if "\x20" <= ch <= "\x7e":
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def _un_utf7(name: str) -> str:
    """Inverse of _utf7 — LIST hands mailbox names back in the encoded form."""
    out, i = [], 0
    while i < len(name):
        if name[i] != "&":
            out.append(name[i])
            i += 1
            continue
        j = name.find("-", i)
        if j < 0:
            out.append(name[i:])
            break
        if j == i + 1:
            out.append("&")
        else:
            b = name[i + 1:j].replace(",", "/")
            try:
                out.append(base64.b64decode(b + "=" * (-len(b) % 4)).decode("utf-16-be"))
            except Exception:
                out.append(name[i:j + 1])
        i = j + 1
    return "".join(out)


def _q(name: str) -> str:
    """A mailbox name ready to hand to imaplib: encoded, then quoted."""
    return '"' + _utf7(name) + '"'


def _guard(label: str, root: str) -> str:
    """The one thing that must never happen: \\Deleted + EXPUNGE against `[Gmail]/All Mail`
    removes the label in a label folder but TRASHES the message in All Mail. Every destructive
    call goes through here, so pointing one at the wrong mailbox is a crash, not a data loss."""
    name = label.strip('"')
    if not name.startswith(root + "/") or name.startswith("[Gmail]"):
        raise ValueError(f"refusing to modify {name!r}: not under {root}/")
    return _q(name)


def _delete_label(M, label: str, root: str) -> bool:
    """Unfile a company from a bucket by deleting that bucket's label outright.

    Every `<root>/<bucket>/<Company>` label holds exactly one company's mail, so dropping the
    label is the whole operation — verified against the live mailbox: the messages keep their
    stars, their inbox place and every other label. This is deliberately NOT the \\Deleted +
    EXPUNGE route: that one removes a label inside a label folder but TRASHES the message in
    All Mail, and it needs per-folder UIDs (Gmail numbers every label separately), so a stale
    uid list silently no-ops. Deleting the label needs neither."""
    typ, _ = M.delete(_guard(label, root))
    return typ == "OK"


def _labels(M) -> set[str]:
    typ, boxes = M.list()
    out = set()
    for b in boxes or []:
        try:
            out.add(_un_utf7(b.decode().split(' "/" ', 1)[-1].strip().strip('"')))
        except Exception:
            pass
    return out


def _from_subject(subjects: list[str]) -> str:
    """A company name lifted out of a subject line, or ''. Rejects anything that reads like a
    sentence fragment rather than a name — a bad name here becomes a bad Gmail label."""
    for s in subjects:
        for pat in _SUBJECT_COMPANY:
            m = pat.search(s or "")
            if not m:
                continue
            name = _TRIM.sub("", m.group(1)).strip()
            name = re.sub(r"\s+", " ", name)
            if not (2 <= len(name) <= 40) or len(name.split()) > 4:
                continue
            # Must read as a proper noun. "a growing EV firm" is a description, not a label.
            if re.search(r"[<>@|/\\]|\d{4}", name) or not re.match(r"[A-Z]", name):
                continue
            if name.lower() in {"the company", "your team", "us", "you", "me", "this role"}:
                continue
            return name
    return ""


def _resolve(text: str, domains: list[str], gaz, subjects: list[str] | None = None) -> tuple[str, str, str]:
    """(company, tier, why). tier: auto | review | skip.

    Naming a company is NOT on its own enough to file a thread. Every sender has a domain, so
    domain-only confidence would file your bank and your newsletters under `Interviews/`. A
    thread reaches `auto` only when the company is corroborated — it's in the gazetteer (a place
    you actually applied) or the thread reads like hiring correspondence. This is what makes the
    incremental run over ALL new mail safe rather than just the hand-starred backfill.
    """
    real = [d for d in domains if d and not _is_vendor(d) and not _is_noise(d)]
    jobbish = bool(_JOBBISH.search(text))
    hits = [(m.start(), gname) for pat, gname in gaz for m in [pat.search(text)]
            if m and _case_ok(m.group(0), gname)]
    named = {g for _, g in hits}
    # Compared on letters-and-digits only: a domain can never carry the space in
    # "Cockroach Labs", so `cockroachlabs.com` has to be able to find it.
    gaz_norm = {_norm(g): g for _, g in gaz}

    # 1. The sender's own domain is a company you know. Strongest signal there is, and it runs
    #    before the bulk filter so a real "apply now to finish your application" from the
    #    employer isn't mistaken for a job blast.
    for d in real:
        name = _registrable(d)
        gname = gaz_norm.get(_norm(_canon(name))) or gaz_norm.get(_norm(name)) if name else None
        if gname:
            return gname, "auto", f"domain {d} + known company"

    # 2. Mass mail. Job boards, newsletters and network noise all name companies and all read as
    #    hiring-related, which is exactly why keyword tiering alone can't hold them back.
    if _BULK.search(text):
        return "", "skip", "job alert / newsletter / network noise"
    if not real and not named and any(_is_noise(d) for d in domains if d):
        return "", "skip", "non-job sender"

    # 3. A real domain that isn't a company you know. It's usually a messenger — a staffing
    #    agency (erginc.com for Lucid), outsourced HR (adp.com for Ford) — or a domain too
    #    squashed to name anything (`open.gov.sg` -> "Gov"). Whatever the thread spells out wins.
    for d in real:
        name = _registrable(d)
        if not name:
            continue
        if len(named) == 1:
            client = next(iter(named))
            return client, ("auto" if jobbish else "review"), f"{client} named in subject (via {d})"
        spelled = _from_subject(subjects or [])
        if spelled:
            return _canon(spelled), ("auto" if jobbish else "review"), f"named in subject (via {d})"
        pretty = _canon(name.capitalize())
        if jobbish:
            return pretty, "auto", f"domain {d}"
        return pretty, "skip", f"domain {d}, nothing hiring-related in it"

    # 4. Vendor-only senders (Ashby, GoodTime, a recruiter's gmail): the text is all there is.
    if hits:
        if len(named) > 1:
            return min(hits)[1], "review", f"ambiguous ({', '.join(sorted(named)[:4])})"
        if jobbish:
            return hits[0][1], "auto", "named in subject/sender"
        return hits[0][1], "review", "company named, but nothing hiring-related in it"
    spelled = _from_subject(subjects or [])
    if spelled and jobbish:
        return _canon(spelled), "review", "company read off the subject only"
    if not jobbish:
        return "", "skip", "no company, not job-related"
    return "", "review", "company not identified"


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _thrids(M, uids: list[bytes]) -> list[str]:
    """Thread id per message, batched. One FETCH per 200 messages instead of one per message —
    on 263 starred messages that is the difference between ~4 minutes and ~5 seconds."""
    out = []
    for batch in _chunks(uids, 200):
        typ, d = M.uid("FETCH", b",".join(batch), "(X-GM-THRID)")
        if typ != "OK":
            continue
        for item in d or []:
            raw = item[0] if isinstance(item, tuple) else item
            m = re.search(rb"X-GM-THRID (\d+)", raw or b"")
            if m:
                out.append(m.group(1).decode())
    return out


def _headers(M, uids: list[bytes]) -> list[tuple[str, email.message.Message]]:
    """(uid, parsed headers) for a set of messages, in one round trip per 100."""
    out = []
    for batch in _chunks(uids, 100):
        typ, d = M.uid("FETCH", b",".join(batch),
                       "(UID BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE)])")
        if typ != "OK":
            continue
        for item in d or []:
            if not isinstance(item, tuple) or len(item) < 2:
                continue                                    # the lone b')' terminator lines
            m = re.search(rb"UID (\d+)", item[0] or b"")
            out.append(((m.group(1).decode() if m else ""),
                        email.message_from_bytes(item[1])))
    return out


def _say(on: bool, msg: str) -> None:
    if on:
        print(msg, flush=True)                                 # flush: this runs under launchd


def scan(cfg: dict, query: str = "is:starred", days: int = 0, limit: int = 0,
         progress: bool = False) -> dict:
    """Group mail into threads, resolve a company per thread. Read-only."""
    mc, addr, pw = mail._creds(cfg)
    gaz = _gazetteer(cfg)
    _say(progress, f"gazetteer: {len(gaz)} company names")
    M = imaplib.IMAP4_SSL(mc.get("imap_host", "imap.gmail.com"), timeout=30)
    M.login(addr, pw)
    try:
        M.select(ALL_MAIL, readonly=True)
        q = query
        if days:
            since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y/%m/%d")
            q = f"{q} after:{since}"
        typ, data = M.uid("SEARCH", None, "X-GM-RAW", f'"{q}"')
        seeds = (data[0] or b"").split()
        if limit:
            seeds = seeds[-limit:]

        thrids: list[str] = list(dict.fromkeys(_thrids(M, list(reversed(seeds)))))  # newest first
        _say(progress, f"  {len(seeds)} seeds -> {len(thrids)} threads; reading headers…")

        threads = []
        for i, thrid in enumerate(thrids, 1):
            if progress and i % 25 == 0:
                _say(progress, f"    {i}/{len(thrids)} threads")
            typ, d = M.uid("SEARCH", None, "X-GM-THRID", thrid)
            uids = (d[0] or b"").split()
            if not uids:
                continue
            msgs, domains = [], []
            for uid, hm in _headers(M, uids):
                subj, frm = _dec(hm.get("Subject")), _dec(hm.get("From"))
                domains.append(_domain(frm))
                # A thread you started reaches the company through To:, never From:.
                domains.append(_domain(_dec(hm.get("To"))))
                msgs.append({"uid": uid, "subject": subj, "from": frm,
                             "date": _iso(hm.get("Date")), "_sort": _sortable(hm.get("Date")),
                             # who spoke last decides whose turn it is on the board
                             "from_me": addr.lower() in (frm or "").lower()})
            if not msgs:
                continue
            msgs.sort(key=lambda m: m["_sort"])
            for m in msgs:
                m.pop("_sort", None)
            own = _domain(addr)
            doms = [d for d in domains if d and d != own]
            subjects = [m["subject"] for m in msgs]
            # Display names + subjects only. Raw addresses are excluded on purpose: matching the
            # gazetteer against "sample-a955c521@example.com" filed a Lucid thread under LinkedIn.
            text = " || ".join(dict.fromkeys(subjects + [_display(m["from"]) for m in msgs]))
            company, tier, why = _resolve(text, doms, gaz, subjects=subjects)
            threads.append({
                "thrid": thrid, "uids": [u.decode() for u in uids], "n": len(uids),
                "company": company, "tier": tier, "why": why,
                "subject": subjects[0][:120], "from": msgs[0]["from"][:70],
                "domains": sorted(set(doms))[:4],
                "last_date": msgs[-1]["date"],
                "last_from_me": msgs[-1]["from_me"],
                "subjects": [s[:120] for s in subjects[:8]],
                "messages": [{k: (v[:160] if isinstance(v, str) else v) for k, v in m.items()}
                             for m in msgs],
            })
        return {"account": addr, "seeds": len(seeds), "threads": threads}
    finally:
        try:
            M.logout()
        except Exception:
            pass


def _sortable(raw):
    try:
        return email.utils.parsedate_to_datetime(raw).astimezone(timezone.utc)
    except Exception:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _iso(raw) -> str:
    try:
        return email.utils.parsedate_to_datetime(raw).astimezone(store.LA).date().isoformat()
    except Exception:
        return ""


def plan(cfg: dict, threads: list[dict], tiers=("auto",)) -> dict:
    """Where each company belongs: `{company: (bucket, [threads])}`, plus the board rows the
    bucket came from. Pure computation — no IMAP, no writes."""
    from app import company_board                               # lazy: it imports this module

    todo = [t for t in threads if t["tier"] in tiers and t["company"]]
    by_co: dict[str, list[dict]] = collections.defaultdict(list)
    for t in todo:
        by_co[_label_name(t["company"])].append(t)
    try:
        conn = store.connect_user(cfg)
        try:
            ov = store.company_overrides(conn)
        finally:
            conn.close()
    except Exception:
        ov = {}
    # 传人工拍板进去：手动结案的公司，Gmail 标签也要进 3-已挂 —— 一行一标签同一事实。
    rows = {_label_name(r["company"]): r for r in company_board.build(threads, ov)}
    # 没有看板行的公司分两种：被"真人回音门槛"滤掉的（纯投递回执，ABB/ABTB 那批）和被
    # 手动隐藏的——都不配占 1-进行中，收进 2-已冷（2026-08-03 user：没推进到真人回复的
    # 不算"进行中"）。
    return {co: (_bucket_of(rows[co]) if co in rows else "cold", ts) for co, ts in by_co.items()}


def reconcile(cfg: dict, threads: list[dict], root: str = "Interviews", tiers=("auto",),
              dry: bool = True, prune_flat: bool = False, force: bool = False,
              touched: set[str] | None = None, progress: bool = False) -> dict:
    """File every company into `<root>/<bucket>/<Company>` and unfile it from the others.

    Adding is a COPY (additive, idempotent). Moving between buckets is a label DELETE of the
    stale bucket — safe precisely because each bucket label holds one company and nothing else.
    The flat `<root>/<Company>` labels from the first pass are dropped only with --prune-flat,
    and only after the nested label for that company is confirmed in place.
    """
    mc, addr, pw = mail._creds(cfg)
    want = plan(cfg, threads, tiers=tiers)
    prev = _load_buckets()
    # A scheduled run should cost what changed, not what exists: a company is worth touching
    # only if new mail arrived for it, or the board just moved it to a different bucket.
    hot = want.keys() if touched is None else touched
    acting = {co for co, (b, _ts) in want.items()
              if force or prev.get(co) != b or co in hot}

    if dry:
        moves = {co: (prev.get(co), b) for co, (b, _t) in want.items() if prev.get(co) not in (None, b)}
        return {"companies": len(want), "acting": len(acting), "moves": moves,
                "buckets": collections.Counter(b for b, _t in want.values()),
                "messages": sum(t["n"] for _b, ts in want.values() for t in ts), "written": False}

    M = imaplib.IMAP4_SSL(mc.get("imap_host", "imap.gmail.com"), timeout=30)
    M.login(addr, pw)
    made, copied, unfiled, pruned, failed = [], 0, [], [], []
    try:
        existing = _labels(M)
        # Create the parents explicitly. Gmail will invent them as \Noselect containers when a
        # child appears, but a real label is what the web UI renders as a collapsible folder.
        for parent in [root] + [f"{root}/{d}" for d in BUCKETS.values()]:
            M.create(_q(parent))
            try:
                M.subscribe(_q(parent))
            except Exception:
                pass
        M.select(ALL_MAIL)                                      # writable: COPY needs it
        for i, co in enumerate(sorted(acting), 1):
            if progress and i % 20 == 0:
                _say(True, f"    {i}/{len(acting)} companies")
            bucket, ts = want[co]
            target = f"{root}/{BUCKETS[bucket]}/{co}"
            typ, _ = M.create(_q(target))
            if typ == "OK":
                made.append(target)
            try:
                M.subscribe(_q(target))                         # show it in the Gmail sidebar
            except Exception:
                pass
            ok = True
            for t in ts:
                typ, resp = M.uid("COPY", ",".join(t["uids"]), _q(target))
                if typ == "OK":
                    copied += t["n"]
                else:
                    ok = False
                    failed.append((co, t["thrid"], (resp[0] or b"").decode()[:80]))
            if not ok:                                          # never unfile what didn't refile
                continue
            for b, dirname in BUCKETS.items():
                stale = f"{root}/{dirname}/{co}"
                if b != bucket and stale in existing and _delete_label(M, stale, root):
                    unfiled.append(stale)
        if prune_flat:
            # Everything directly under the root that isn't a bucket is left over from the flat
            # scheme (including names a later canonicalisation renamed, e.g. `Alterion.ai`).
            buckets = set(BUCKETS.values())
            for name in sorted(existing):
                rest = name[len(root) + 1:] if name.startswith(root + "/") else None
                if rest and "/" not in rest and rest not in buckets and _delete_label(M, name, root):
                    pruned.append(name)
        _save_buckets({co: b for co, (b, _t) in want.items()})
        return {"companies": len(want), "acting": len(acting), "created": made,
                "messages": copied, "unfiled": unfiled, "pruned": pruned,
                "failed": failed, "written": True,
                "buckets": collections.Counter(b for b, _t in want.values())}
    finally:
        try:
            M.logout()
        except Exception:
            pass


def _load_buckets() -> dict[str, str]:
    """Which bucket each company was in last time, so a run only touches what changed."""
    if BUCKET_STATE.exists():
        try:
            return json.loads(BUCKET_STATE.read_text())
        except Exception:
            return {}
    return {}


def _save_buckets(d: dict[str, str]) -> None:
    BUCKET_STATE.parent.mkdir(parents=True, exist_ok=True)
    BUCKET_STATE.write_text(json.dumps(d, indent=1, ensure_ascii=False))


def incremental(cfg: dict, days: int = 3, root: str = "Interviews",
                dry: bool = False) -> dict:
    """The standing job: file mail that has arrived since the last run.

    Seeds on ALL recent mail, not just starred — the point is that a new company's first email
    gets filed the moment it lands, before you have thought to star it. Safety comes from the
    tiering, not from the seed: only `auto` (an employer domain you know, or an unambiguous
    company name in a hiring-shaped thread) is filed. Everything else waits on /companies.

    Idempotent by construction: re-labelling a message Gmail already labelled is a no-op, so
    overlapping windows cost nothing and a missed run self-heals on the next one.
    """
    res = scan(cfg, query=f"newer_than:{days}d", progress=False)
    threads = res["threads"]
    # Bucketing needs the whole history — three days of mail can't tell you that Upstart has
    # been silent since November. Reconcile against the merged view, act on what moved.
    merged = _merge_cache(res)
    fresh = {_label_name(t["company"]) for t in threads if t["tier"] == "auto" and t["company"]}
    # No pruning on the schedule: --prune-flat deletes anything sitting directly under the root,
    # which is right for the one-off migration and wrong for a job that runs unattended every
    # two hours next to labels you may have made by hand.
    out = reconcile(cfg, merged["threads"], root=root, tiers=("auto",), dry=dry, touched=fresh)
    out["scanned_threads"] = len(threads)
    out["review"] = [{"date": t["last_date"], "why": t["why"], "subject": t["subject"]}
                     for t in threads if t["tier"] == "review"]
    # Leave the cache warm so /companies opens instantly on the freshest mail.
    if not dry:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(merged, indent=1, ensure_ascii=False))
    return out


def _merge_cache(fresh: dict) -> dict:
    """Fold a short incremental scan into the full backfill, keyed by thread id, so the board
    keeps its history instead of shrinking to the last three days."""
    old = {}
    if CACHE.exists():
        try:
            prev = json.loads(CACHE.read_text())
            old = {t["thrid"]: t for t in prev.get("threads", [])}
        except Exception:
            old = {}
    for t in fresh.get("threads", []):
        old[t["thrid"]] = t                                    # fresh view of a thread wins
    return {"account": fresh.get("account", ""), "seeds": fresh.get("seeds", 0),
            "threads": sorted(old.values(), key=lambda t: t.get("last_date") or "", reverse=True)}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--incremental", action="store_true",
                    help="scheduled mode: file the last --days of new mail, print a one-liner")
    ap.add_argument("--query", default="is:starred", help="Gmail search picking the seed messages")
    ap.add_argument("--days", type=int, default=0, help="limit seeds to the last N days")
    ap.add_argument("--limit", type=int, default=0, help="cap seed messages (for a quick look)")
    ap.add_argument("--root", default="Interviews", help="parent label")
    ap.add_argument("--apply", action="store_true", help="actually create labels and file mail")
    ap.add_argument("--include-review", action="store_true",
                    help="with --apply, also file the review tier")
    ap.add_argument("--show", metavar="COMPANY", help="print every thread for one company")
    ap.add_argument("--cached", action="store_true", help="reuse the last scan instead of IMAP")
    ap.add_argument("--prune-flat", action="store_true",
                    help="delete the old flat <root>/<Company> labels once nested ones are in place")
    ap.add_argument("--force", action="store_true",
                    help="re-file every company, not just the ones that moved")
    args = ap.parse_args(argv)

    cfg = cfgmod.load_config()
    if args.incremental:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        r = _incremental_with_retry(cfg, days=args.days or 3, root=args.root,
                                    dry=not args.apply)
        verb = "would file" if not args.apply else "filed"
        moved = len(r.get("unfiled", [])) if args.apply else len(r.get("moves", {}))
        print(f"[{stamp}] mail_label: {r['scanned_threads']} threads seen, {verb} "
              f"{r['messages']} messages across {r['acting']} companies, {moved} changed bucket"
              + (f", {len(r['review'])} need review" if r["review"] else ""), flush=True)
        for co, thrid, err in r.get("failed", []):
            print(f"  FAILED {co} thread {thrid}: {err}", flush=True)
        return 0
    if args.cached and CACHE.exists():
        res = json.loads(CACHE.read_text())
        print(f"(cached scan of {res['account']})")
    else:
        res = scan(cfg, query=args.query, days=args.days, limit=args.limit, progress=True)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(res, indent=1, ensure_ascii=False))
    threads = res["threads"]

    if args.show:
        want = args.show.lower()
        for t in threads:
            if want in (t["company"] or "").lower():
                print(f"\n{t['company']}  [{t['tier']}] {t['last_date']}  ({t['n']} msgs) {t['why']}")
                print(f"    from {t['from']}")
                for s in t["subjects"]:
                    print(f"      · {s}")
        return 0

    tiers = collections.Counter(t["tier"] for t in threads)
    print(f"\n{res['account']}: {res['seeds']} starred messages -> {len(threads)} threads")
    print(f"  auto {tiers['auto']}   review {tiers['review']}   skip {tiers['skip']}\n")

    use_tiers = ("auto", "review") if args.include_review else ("auto",)
    want = plan(cfg, threads, tiers=use_tiers)
    for bucket, dirname in BUCKETS.items():
        cos = {co: ts for co, (b, ts) in want.items() if b == bucket}
        print(f"--- {args.root}/{dirname}  ({len(cos)} 家) ---")
        for co, ts in sorted(cos.items(), key=lambda kv: max(
                (t["last_date"] for t in kv[1]), default=""), reverse=True):
            last = max((t["last_date"] for t in ts), default="")
            print(f"  {co:28} {sum(t['n'] for t in ts):3} msgs  "
                  f"{len(ts):2} threads  last {last}")
        print()

    rev = [t for t in threads if t["tier"] == "review"]
    if rev:
        print(f"\n--- {len(rev)} THREADS NEED YOUR EYE (not labelled) ---")
        for t in sorted(rev, key=lambda t: t["last_date"], reverse=True):
            print(f"  {t['last_date']}  {(t['company'] or '?'):16} {t['why'][:34]:34} "
                  f"{t['subject'][:56]}")
    skipped = [t for t in threads if t["tier"] == "skip"]
    if skipped:
        print(f"\n--- {len(skipped)} SKIPPED (read as non-job) ---")
        for t in sorted(skipped, key=lambda t: t["last_date"], reverse=True)[:20]:
            print(f"  {t['last_date']}  {t['from'][:38]:38} {t['subject'][:52]}")

    r = reconcile(cfg, threads, root=args.root, tiers=use_tiers, dry=not args.apply,
                  prune_flat=args.prune_flat, force=args.force, progress=True)
    if not args.apply:
        print(f"Nothing written. `--apply` would file {r['messages']} messages across "
              f"{r['companies']} companies ({r['acting']} of them need a write). "
              f"Stars, inbox and unrelated labels are untouched.")
        if r["moves"]:
            print("  bucket changes: " + ", ".join(
                f"{co} {a}→{b}" for co, (a, b) in list(r["moves"].items())[:12]))
        return 0
    print(f"\nfiled {r['messages']} messages across {r['acting']} companies "
          f"({len(r['created'])} labels created, {len(r['unfiled'])} stale buckets removed, "
          f"{len(r['pruned'])} flat labels pruned)")
    for co, thrid, err in r["failed"]:
        print(f"  FAILED {co} thread {thrid}: {err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
