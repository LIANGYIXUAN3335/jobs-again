"""generic_resume.py — the workspace's company-agnostic résumé for outreach emails (owner 2026-09-23
night: 「草稿要带 resume，但 resume 上不能带具体岗位信息」).

One headless Claude Code session (same worker, same honesty rules and validator as the nightly
tailoring) writes output/<ws>/outreach/<Name> - Resume.yaml: a summary plus the strongest bullets
and 4 skill groups, aimed at the candidate's positioning (config tailoring.positioning) instead
of one JD. app/outreach.resume_for renders it to the PDF every draft attaches.

    JOBSCAN_CONFIG=config.robin.yaml .venv/bin/python tools/generic_resume.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, ".")
from app import config as cfgmod, outreach, tailoring  # noqa: E402

CLAUDE = os.path.expanduser("~/.local/bin/claude")


def main() -> None:
    cfg = cfgmod.load_config()
    base = tailoring.load_base(cfg)
    at = cfg.get("auto_tailor") or {}
    model = at.get("model", "claude-opus-5-5")
    pdf = outreach.generic_resume_path(cfg)
    out_yaml = pdf.with_suffix(".yaml")
    angle = str((cfg.get("tailoring") or {}).get("generic_angle") or "backend")
    positioning = str((cfg.get("tailoring") or {}).get("positioning") or "").strip()
    prompt = "\n".join([
        f"# Generic résumé — {base['name']} (no specific role)",
        "",
        tailoring._instructions_for(cfg, base, angle),
        "## Target",
        "This résumé is attached to cold-outreach emails to recruiters at many different companies, so it",
        "must NOT be aimed at one job: no company name, no job title, no req number anywhere. Aim it at",
        "the candidate's positioning below — a backend / distributed-systems software engineer role at a",
        "mid-to-large US tech company. Pick the strongest bullets (the recent job 3-4, older 2-3), 4 skill",
        "groups, and a 2-3 sentence summary that states evidence (systems, scale, measured deltas) and never",
        "self-assessment. The JD-keyword coverage guide does not apply here — there is no JD.",
        "",
        f"Positioning: {positioning or '(none on file — use the base résumé as is)'}",
        "",
        "## Base résumé — the ONLY source of truth for facts (reword, don't invent)",
        "```yaml", yaml.safe_dump(base, sort_keys=False, allow_unicode=True).strip(), "```",
        "",
        tailoring._SCHEMA.format(resume_out=out_yaml, job_id="generic", angle=angle)
        .replace("2-3 sentence professional summary tailored to THIS role", "2-3 sentence professional summary (no role or company named)"),
    ])
    prompt_path = pdf.parent / "generic_prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    worker = (
        f"Read {prompt_path} and write the résumé YAML it asks for to {out_yaml}, following its schema and "
        f"rules exactly (facts only from the base résumé; no company, job title or req number anywhere; "
        f"max 4 skill groups; no soft skills as skill items). Then run "
        f".venv/bin/python tools/score_yaml.py \"{out_yaml}\" and fix any validator error it prints. "
        f"Work non-interactively and stop when the file passes."
    )
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    r = subprocess.run([CLAUDE, "-p", worker, "--model", model,
                        "--allowedTools", "Read", "Write", "Edit", "Bash(.venv/bin/python:*)"],
                       cwd=str(cfgmod.ROOT), env=env, capture_output=True, text=True, timeout=900)
    print(r.stdout[-1500:], r.stderr[-500:])
    if not out_yaml.exists():
        sys.exit("worker did not write the YAML")
    t = yaml.safe_load(out_yaml.read_text(encoding="utf-8")) or {}
    tailoring.validate_tailored(t, base)
    if pdf.exists():
        pdf.unlink()
    print("generic résumé →", outreach.resume_for(cfg, []))


if __name__ == "__main__":
    main()
