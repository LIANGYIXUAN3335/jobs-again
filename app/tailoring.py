"""Phase 2 — resume tailoring via file-system handoff to Claude Code. NO LLM API call.

  queue:  write output/prompts/{stem}.md   (JD + company + LOCKED base resume + schema)
  you:    open it in VS Code, run Claude Code by hand, save the tailored *structured*
          answer to output/resumes/{stem}.yaml
  ingest: validate → merge with the locked base → render a deterministic PDF via Typst

The base (identity, education, job headers/dates) is never regenerated, so no tailored
resume can alter a fact. Tailoring only supplies summary + reworded bullets + skills order.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

from . import config as cfgmod
from . import store

BASE_PATH = "resumes/resume_base.yaml"


# ---------- paths ----------

def _stem(company: str, job_id: str) -> str:
    return f"{cfgmod.norm_key(company)}_{job_id.replace(':', '_')}"


def prompt_path(cfg: dict, company: str, job_id: str) -> Path:
    d = cfgmod.resolve_path(cfg["output_dirs"]["prompts"])
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_stem(company, job_id)}.md"


def resume_yaml_path(cfg: dict, company: str, job_id: str) -> Path:
    d = cfgmod.resolve_path(cfg["output_dirs"]["resumes"])
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_stem(company, job_id)}.yaml"


def resume_pdf_path(cfg: dict, company: str, job_id: str) -> Path:
    return resume_yaml_path(cfg, company, job_id).with_suffix(".pdf")


def load_base(cfg: dict) -> dict:
    return yaml.safe_load(cfgmod.resolve_path(BASE_PATH).read_text(encoding="utf-8"))


# ---------- queue: write the prompt file ----------

_INSTRUCTIONS = """\
You are tailoring Casey Example's resume for ONE specific role. Produce a truthful,
JD-aligned resume as a STRUCTURED YAML file (schema at the bottom) — not prose.

Rules (important):
- Work ONLY from the real experience in the base resume below. Do NOT invent employers,
  titles, dates, metrics, or skills. Reword and re-emphasize; never fabricate.
- Use the job description's own terminology where it truthfully matches Casey's work.
- Casey is moving from frontend/product/ads work toward backend/distributed systems.
  Reframe her real work in terms of the services, data models, APIs, reliability, and
  scale behind it (e.g. state-transition logic, validation/guardrail systems, Go/internal
  API integration, Prometheus monitoring, data consistency across pipelines) — without
  claiming backend ownership she didn't have.
- Keep every real job present. You may reorder/trim bullets per job and rewrite them, but
  keep them accurate. Aim for a tight 1-2 pages.
- List any JD requirements she does NOT clearly meet in `gaps` (honest, for her eyes only
  — this does NOT go into the PDF). Do not paper over them in the bullets.

Angle for this role: {angle} — {angle_hint}
"""

_SCHEMA = """\
# ===== SAVE YOUR ANSWER AS THIS FILE (exact path): =====
# {resume_out}
#
# ===== OUTPUT SCHEMA (YAML) — fill in, keep job_id exactly: =====
job_id: "{job_id}"          # keep verbatim — links the resume back to the role
angle: "{angle}"
summary: >
  2-3 sentence professional summary tailored to THIS role, truthful, backend-leaning.
skills:                     # pick & order from the base skill pool; JD-relevant first
  - label: "Programming Languages"
    items: ["Go/Golang", "Java", "Python", "SQL", "TypeScript"]
  - label: "Databases & Middleware"
    items: ["PostgreSQL/Postgres", "MySQL", "Cassandra", "Redis", "Kafka"]
  # ...more groups as useful
experience:                 # one block per real job; keep the company names verbatim
  - company: "TikTok"
    bullets:
      - "Reworded bullet emphasizing services/APIs/data/scale, truthful."
      - "..."
  - company: "Meta"
    bullets: ["...", "..."]
  - company: "Gemini Express Corp"
    bullets: ["..."]
  - company: "Shanghai Haoxiong Education Technology Co., Ltd"
    bullets: ["..."]
gaps:                       # JD asks for X, she has Y — NOT rendered into the PDF
  - "JD wants deep Kubernetes/operator experience; she has container/Docker basics."
"""

_ANGLE_HINTS = {
    "backend-distributed": "lead with distributed systems, services, data consistency, "
                           "reliability, Go/Java backend, Kafka/queues, observability.",
    "payments": "lead with correctness/validation/guardrails, idempotency, ledgers, "
                "money-movement reliability, Go/internal APIs, monitoring.",
}


def suggest_angle(cfg: dict, role: dict) -> str:
    text = f"{role.get('title', '')} {role.get('jd_text', '')} {role.get('company', '')}".lower()
    pay_terms = ("payment", "fintech", "ledger", "billing", "money", "transaction", "card")
    if any(t in text for t in pay_terms):
        return "payments"
    return "backend-distributed"


def write_prompt(cfg: dict, conn, job_id: str, angle: str | None = None) -> Path:
    role = store.get_role(conn, job_id)
    if not role:
        raise ValueError(f"unknown job_id {job_id!r}")
    angle = angle or suggest_angle(cfg, role)
    base = load_base(cfg)
    notes = cfgmod.load_company_notes(cfg).get(cfgmod.norm_key(role["company"]), {})
    out_path = resume_yaml_path(cfg, role["company"], job_id)

    parts = [
        f"# Tailor résumé — {role['company']}: {role['title']}",
        "",
        _INSTRUCTIONS.format(angle=angle, angle_hint=_ANGLE_HINTS.get(angle, "")),
        "## Company context",
        f"- Company: {role['company']}",
        f"- Notes: {json.dumps(notes) if notes else '(none on file)'}",
        f"- Location: {role.get('location') or 'n/a'} · Apply: {role.get('apply_url') or 'n/a'}",
        "",
        "## Job description",
        "```",
        (role.get("jd_text") or "(no JD text captured)").strip(),
        "```",
        "",
        "## Base résumé — the ONLY source of truth for facts (reword, don't invent)",
        "```yaml",
        yaml.safe_dump(base, sort_keys=False, allow_unicode=True).strip(),
        "```",
        "",
        _SCHEMA.format(resume_out=out_path, job_id=job_id, angle=angle),
    ]
    p = prompt_path(cfg, role["company"], job_id)
    p.write_text("\n".join(parts), encoding="utf-8")
    store.set_tailor(conn, job_id, tailor_status="queued", prompt_path=str(p))
    return p


# ---------- ingest: validate → merge → render ----------

class TailorError(ValueError):
    pass


def validate_tailored(t: dict) -> None:
    if not isinstance(t, dict):
        raise TailorError("tailored file is not a YAML mapping")
    if not t.get("job_id"):
        raise TailorError("missing `job_id` (keep the one from the prompt)")
    exp = t.get("experience")
    if not isinstance(exp, list) or not exp:
        raise TailorError("`experience` must be a non-empty list")
    for i, job in enumerate(exp):
        if not isinstance(job, dict) or not job.get("company"):
            raise TailorError(f"experience[{i}] needs a `company`")
        bl = job.get("bullets")
        if not isinstance(bl, list) or not all(isinstance(b, str) and b.strip() for b in bl):
            raise TailorError(f"experience[{i}] ({job.get('company')}) needs non-empty string bullets")


def merge(base: dict, tailored: dict) -> dict:
    """Facts from base; summary/bullets/skills order from tailored. Base wins on identity."""
    by_company = {j["company"]: j for j in tailored.get("experience", [])}
    experience = []
    for job in base["experience"]:
        t = by_company.get(job["company"])
        bullets = (t.get("bullets") if t else None) or job["bullets_master"]
        experience.append({
            "company": job["company"], "title": job["title"],
            "location": job["location"], "dates": job["dates"], "bullets": bullets,
        })
    if tailored.get("skills"):
        skills = [{"label": g["label"], "items": g["items"]} for g in tailored["skills"]]
    else:
        skills = [{"label": k, "items": v} for k, v in base["skills_master"].items()]
    return {
        "name": base["name"], "contact": base["contact"],
        "summary": tailored.get("summary", ""),
        "education": base["education"], "skills": skills, "experience": experience,
    }


def _typst_bin() -> str:
    for cand in ("typst", "/opt/homebrew/bin/typst", "/usr/local/bin/typst"):
        if shutil.which(cand) or Path(cand).exists():
            return cand
    raise TailorError("typst not found — install it: brew install typst")


def render_pdf(merged: dict, out_pdf: Path) -> Path:
    template = cfgmod.ROOT / "templates" / "resume.typ"
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "data.json").write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
        shutil.copy(template, tmp / "resume.typ")
        proc = subprocess.run(
            [_typst_bin(), "compile", "resume.typ", str(out_pdf)],
            cwd=tmp, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise TailorError(f"typst failed:\n{proc.stderr.strip()}")
    return out_pdf


def ingest_file(cfg: dict, conn, yaml_file: Path) -> dict:
    """Validate one tailored yaml, render its PDF, mark the role ready. Returns a summary."""
    tailored = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
    validate_tailored(tailored)
    job_id = tailored["job_id"]
    role = store.get_role(conn, job_id)
    if not role:
        raise TailorError(f"{yaml_file.name}: job_id {job_id!r} not in DB")
    merged = merge(load_base(cfg), tailored)
    pdf = render_pdf(merged, resume_pdf_path(cfg, role["company"], job_id))
    store.set_tailor(conn, job_id, tailor_status="ready", tailored_resume_path=str(pdf))
    return {"job_id": job_id, "company": role["company"], "pdf": str(pdf),
            "gaps": tailored.get("gaps") or []}
