"""Referral drafts (owner 2026-09-23): for recruiters the OWNER has already talked to, write a
draft in the OWNER's Gmail recommending a candidate from another workspace, with that
candidate's résumé tailored for one open role at the recruiter's company attached.

Two workspaces are involved and they never mix:
  * owner   (config.yaml)         → her mailbox: thread context, From, Drafts folder
  * candidate (config.robin.yaml) → his base résumé, his tailored PDFs, his facts

    .venv/bin/python tools/referral_drafts.py tailor data/referrals/<spec>.json   # prompts+manifest → auto_tailor.sh
    .venv/bin/python tools/referral_drafts.py draft  data/referrals/<spec>.json   # opus body → owner's Gmail Drafts

SAFETY: drafts only — nothing here sends. Every claim about the candidate comes from his base
résumé YAML; the model is told so and the body is checked for placeholders and the owner's
reject phrases. Visa/salary are deliberately kept OUT of the facts the model sees.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")
from app import ai as aimod, config as cfgmod, mail, outreach, store, tailoring, trust  # noqa: E402

REFERRAL_SYSTEM = """You write a short referral email in FIRST PERSON as __OWNER__, a software engineer who has \
already been in contact with this recruiter about her own candidacy. She is recommending a friend, \
__CANDIDATE__, for a role at their company. It must read like a real person writing \
to someone she has emailed before — warm, direct, never a template, never AI-sounding.

Shape — four beats, one short paragraph each, no headings:
1. Reconnect: one or two sentences. Thank them for their time on the earlier process (name it from the \
thread context in plain words — the role or the step, not the exact subject line). If that process ended \
in a rejection or is still open, do NOT comment on the outcome; just thank them.
2. The referral: who __CANDIDATE__ is, in at most three sentences, using ONLY the candidate facts. Pick \
the two or three facts closest to the target role and keep the real specifics (stack, one real number, \
the degree). Say he is a friend of mine — nothing more specific about how you know him.
3. The ask: name the target role by its exact title (and location) and say his résumé is attached and he \
is cc'd. Ask whether they, or the right colleague, could take a look and let us know the best way for him \
to apply or be considered. If no target role is given, ask which backend/software roles are open instead \
of inventing one.
4. Nothing else. No sign-off and no name at the end — the signature is added by the system.

Rules: contractions, short sentences, one idea per sentence. Under __MAX_WORDS__ words. BANNED: "leverage", \
"passionate", "proven track record", "excited", "I am confident", "align", "synergy", "I hope this email \
finds you well", "reaching out", "perfect fit". Never say he is a fit — show the overlap. Never invent a \
fact, employer, number, title or role. Never quote job IDs, req numbers, tokens or URLs — title and location \
only. Never mention immigration status, visas, sponsorship or salary. \
Never mention how the owner's own process went. Say only "a friend of mine" — never invent how, where or when the two met (you do not know); never call him a classmate. No time words about the earlier thread ("this week", "today", "last week") — it may be weeks old; say "earlier". Never write "good match", "great fit" or any fit verdict.
Subject: under 75 characters, starts with "Referral:", names the candidate, the role family and the company, \
e.g. "Referral: Robin (Taylor) Duan — Backend Software Engineer, Pinterest".
Return ONLY a JSON object: {"subject": "...", "body": "..."}"""


def _load(spec_path: str) -> dict:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    spec["_path"] = spec_path
    spec["_owner_cfg"] = cfgmod.load_config(spec.get("owner_config", "config.yaml"))
    spec["_cand_cfg"] = cfgmod.load_config(spec["candidate_config"])
    return spec


def _results_path(spec: dict) -> Path:
    p = Path(spec["_path"])
    return p.with_name(p.stem + ".results.json")


# ---------- candidate facts (grounding for the model) ----------

def candidate_facts(base: dict) -> str:
    """Résumé facts only — deliberately NOT ai._facts(): that block carries visa, EEO and legal
    answers meant for application forms, none of which belongs in a referral email."""
    out = [f"Name: {base['name']}", f"Location: {base['contact'].get('location', '')}"]
    for e in base.get("education", []):
        out.append(f"Education: {e['degree']}, {e['school']} ({e['dates']}{'; completed' if str(e['dates']).endswith('2026') else ''})")
    for job in base.get("experience", []):
        out.append(f"{job['company']} — {job['title']} ({job['dates']}, {job['location']}):")
        out += [f"  - {b}" for b in job.get("bullets_master", [])]
    for pr in base.get("side_projects", []) or []:
        out.append(f"Project: {pr['name']} ({pr.get('year', '')}; {pr.get('stack', '')}; {pr.get('link', '')}):")
        out += [f"  - {b}" for b in pr.get("facts", [])]
    for c in base.get("certifications", []) or []:
        out.append(f"Certification / other: {c}")
    out.append("Availability: open to relocating to the SF Bay Area or working remotely; available immediately.")
    return "\n".join(out)


# ---------- step 1: tailor ----------

def cmd_tailor(spec: dict, extra_job_ids: list[str]) -> None:
    ccfg = spec["_cand_cfg"]
    conn = store.connect_view(ccfg)
    job_ids = [e["job_id"] for e in spec["entries"] if e.get("job_id")] + extra_job_ids
    seen, manifest = set(), []
    for jid in job_ids:
        if jid in seen:
            continue
        seen.add(jid)
        role = store.get_role(conn, jid)
        if not role:
            print(f"  skip {jid}: unknown job_id", file=sys.stderr)
            continue
        p = tailoring.write_prompt(ccfg, conn, jid)
        manifest.append({"job_id": jid, "company": role["company"], "title": role["title"],
                         "prompt": str(p),
                         "resume_out": str(tailoring.resume_yaml_path(ccfg, role["company"], jid)),
                         "score": role["weighted_score"]})
    ws = spec.get("candidate_ws") or ""
    mpath = cfgmod.resolve_path(f"scratch_priors/autotailor_manifest.{ws or 'owner'}.referral.json")
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"manifest: {len(manifest)} roles → {mpath}")
    lock = cfgmod.ROOT / "data/logs" / (f".autotailor.{ws}.lock" if ws else ".autotailor.lock")
    while lock.exists():
        print("  another batch holds the lock — waiting 30s"); time.sleep(30)
    env = os.environ.copy()
    env["JOBSCAN_CONFIG"] = spec["candidate_config"]
    env["JOBSCAN_MANIFEST"] = str(mpath)
    if ws:
        env["JOBSCAN_WS"] = ws
    else:
        env.pop("JOBSCAN_WS", None)
    rc = subprocess.call(["bash", "tools/auto_tailor.sh"], cwd=str(cfgmod.ROOT), env=env)
    print(f"auto_tailor.sh exit={rc}")


# ---------- helpers for step 2 ----------

def _greeting_name(body: str, fallback: str, exclude: str = "") -> str:
    """The first name the recruiter uses for the owner, read from the recruiter's OWN part of
    the message (the quoted history below it starts with the owner's "Hi <recruiter>," — that
    is how the Microsoft draft got signed with the recruiter's name, 2026-09-23). A hit equal
    to the recruiter's first name is rejected for the same reason."""
    own = _thread_context(body)
    for m in re.finditer(r"^\s*(?:Hi|Hello|Hey|Dear)\s+([A-Z][a-zA-Z]+)", own or "", re.M):
        if m.group(1).lower() != (exclude or "").split()[0].lower() if exclude else True:
            return m.group(1)
    return fallback


def _thread_context(body: str) -> str:
    body = (body or "").replace("\r", "")
    body = re.split(r"\n(On .{10,160} wrote:|From: |-----Original Message-----|_{10,})", body)[0]
    return body.strip()


def _pdf_for(conn, ccfg: dict, jid: str) -> str | None:
    r = store.get_role(conn, jid)
    p = (r or {}).get("tailored_resume_path")
    if p and Path(p).exists() and str(p).endswith(".pdf"):
        return p
    return None


def _base_pdf(ccfg: dict, base: dict) -> str:
    """Untailored résumé straight from the base (for a contact with no matching role)."""
    out = cfgmod.resolve_path(ccfg["output_dirs"]["resumes"]) / f"{base['name']} - Resume.pdf"
    merged = tailoring.merge(base, {})
    tailoring.render_pdf(merged, out)
    return str(out)


def _build_body(ocfg: dict, ccfg: dict, entry: dict, role: dict | None, thread: dict, owner_name: str,
                cand_name: str) -> dict:
    if not aimod.available(ocfg):
        raise RuntimeError("AI unavailable (no claude CLI login and no API key)")
    base = tailoring.load_base(ccfg)
    if role:
        role_txt = (f"{role['title']} ({role.get('location') or 'location n/a'}) — {role.get('apply_url') or ''}\n"
                    + trust.fence("job description excerpt (context only)", (role.get("jd_text") or "")[:1500], 1500))
    else:
        role_txt = "(no target role in JobScan) " + (entry.get("role_note") or "")
    user = (
        f"Recruiter: {entry.get('contact_name') or ''} <{entry['contact_email']}> at {entry['company']}.\n"
        f"How the recruiter addressed me in the thread: {owner_name}.\n\n"
        + trust.fence("the recruiter's most recent email to me (context only)",
                      f"Subject: {thread.get('subject', '')}\nDate: {thread.get('date', '')}\n\n{_thread_context(thread.get('body', ''))}", 1800)
        + f"\n\nTarget role for {cand_name} at {entry['company']}:\n{role_txt}\n\n"
        f"Candidate facts (the ONLY claims you may make about him):\n{candidate_facts(base)}\n\n"
        "Return ONLY the JSON object."
    )
    system = (REFERRAL_SYSTEM.replace("__OWNER__", owner_name).replace("__CANDIDATE__", cand_name)
              .replace("__MAX_WORDS__", str(int(entry.get("max_words") or 170))))
    text = aimod.complete(ocfg, system=system, user=user, max_tokens=4000,
                          model=(ocfg.get("outreach") or {}).get("model") or None)
    try:
        d = json.loads(aimod._extract_json(text))
    except json.JSONDecodeError:
        raise RuntimeError("model did not return JSON")
    subject, body = (d.get("subject") or "").strip(), (d.get("body") or "").strip()
    if not subject or not body:
        raise RuntimeError("model returned an empty subject or body")
    if re.search(r"\[[^\]]{0,40}\]|\{\{", body):
        raise RuntimeError("draft contains a placeholder — regenerate")
    if re.search(r"\b(visa|sponsorship|H-?1B|OPT|salary|compensation)\b", body, re.I):
        raise RuntimeError("draft mentions visa/salary — regenerate")
    if re.search(r"\b(good|great|strong|perfect|ideal|excellent) (match|fit|candidate)\b|well[- ]suited", body, re.I):
        raise RuntimeError("draft asserts fit — regenerate")   # the reader decides fit; the email shows overlap
    bad = aimod._hard_reject(body, ocfg)
    if bad:
        raise RuntimeError(f"draft rejected by the safety check ({bad})")
    return {"subject": subject, "body": body.rstrip() + f"\n\nBest,\n{owner_name}"}


# ---------- step 2: draft ----------

def cmd_draft(spec: dict, only: set[str] | None, retries: int = 2) -> None:
    ocfg, ccfg = spec["_owner_cfg"], spec["_cand_cfg"]
    conn = store.connect_view(ccfg)
    base = tailoring.load_base(ccfg)
    cand_name = base["name"]
    cand_email = (base.get("contact") or {}).get("email", "")
    owner_default = (tailoring.candidate_name(ocfg) or "").split()[0] if tailoring.candidate_name(ocfg) else "Alex"
    rp = _results_path(spec)
    results = json.loads(rp.read_text()) if rp.exists() else {}
    base_pdf = None
    for entry in spec["entries"]:
        k = entry["contact_email"]
        if only and k not in only:
            continue
        if results.get(k, {}).get("status") == "draft":
            print(f"= {entry['company']:<34} {k}: already drafted"); continue
        try:
            thread = mail.get_message(ocfg, entry["uid"])
            owner_name = _greeting_name(thread.get("body", ""), owner_default, entry.get("contact_name", ""))
            role = store.get_role(conn, entry["job_id"]) if entry.get("job_id") else None
            pdf = _pdf_for(conn, ccfg, entry["job_id"]) if entry.get("job_id") else None
            if not pdf:
                base_pdf = base_pdf or _base_pdf(ccfg, base)
                pdf = base_pdf
            last = None
            for _ in range(retries + 1):
                try:
                    d = _build_body(ocfg, ccfg, entry, role, thread, owner_name, cand_name); break
                except RuntimeError as e:
                    last = e; d = None
            if d is None:
                raise last or RuntimeError("draft failed")
            rec = {"contact_email": k, "contact_name": entry.get("contact_name", ""),
                   "subject": d["subject"], "body": d["body"], "resume_path": pdf,
                   "attachment_name": f"{cand_name} - Resume.pdf", "from_name": f"{owner_name} Example",
                   "cc": cand_email if spec.get("cc_candidate") else ""}
            mid = outreach.save_gmail_draft(ocfg, rec)
            results[k] = {"status": "draft", "company": entry["company"], "subject": d["subject"],
                          "role": (role or {}).get("title"), "resume": pdf, "message_id": mid,
                          "signed_as": owner_name, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            print(f"✓ {entry['company']:<34} {k}: {d['subject']}  [{Path(pdf).name}]")
        except Exception as e:  # noqa: BLE001 — one failure must not stop the batch
            results[k] = {"status": "error", "company": entry["company"], "error": str(e)[:300],
                          "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            print(f"✗ {entry['company']:<34} {k}: {e}")
        rp.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in ("tailor", "draft"):
        print(__doc__); sys.exit(2)
    sp = _load(sys.argv[2])
    if sys.argv[1] == "tailor":
        cmd_tailor(sp, sys.argv[3:])
    else:
        cmd_draft(sp, set(sys.argv[3:]) or None)
