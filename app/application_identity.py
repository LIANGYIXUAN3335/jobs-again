"""Prepare the user's approved Casey card for one durably claimed new role."""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import yaml

from . import config as cfgmod, interview_ledger, store, tailoring
from .application_gatekeeper import _role_url
from .application_memory import validate_identity
from .resume_evidence import inspect_resume


def routed_elsewhere(cfg: dict, role: dict) -> bool:
    """The owner chose another card for this role, or for its whole company."""
    companies = (cfg.get("apply_profiles") or {}).get("companies") or {}
    company_card = companies.get((role.get("company_norm") or "").lower())
    return ((role.get("apply_profile") or "").strip() not in {"", "casey"} or
            company_card not in {None, "casey"})


def companies_under_other_cards(conn) -> set[str]:
    """Companies already holding an application sent under a non-Casey card.

    Switching names at one employer has only ever been the owner's explicit, per-company
    call (see apply_profiles.companies); the runner must not create a second candidate
    profile there on its own. Rows with no recorded card predate card tracking and are
    not evidence of another name.
    """
    rows = conn.execute(
        "SELECT DISTINCT r.company_norm FROM jobs.roles r JOIN role_state s ON s.job_id = r.job_id"
        " WHERE s.status IN ('applied', 'interviewing', 'rejected')"
        " AND COALESCE(s.apply_profile, '') NOT IN ('', 'casey')").fetchall()
    return {(row[0] or "").lower() for row in rows}


def prepare_casey_claim(cfg: dict, job: dict) -> dict:
    """Re-letter existing tailored content; publish identity and PDF together.

    No model call, bulk change, old PDF deletion, or historical application rewrite.
    Rendering happens before the database transaction, and mutable eligibility is
    checked again under the write lock before publishing the replacement.
    """
    conn = store.connect_view(cfg)
    artifact = None
    committed = False
    try:
        def current_role():
            current = store.get_role(conn, job["job_id"])
            if current:
                # get_role deliberately exposes a compact view and omits these two
                # state fields; read them explicitly for the preparation race guard.
                state = conn.execute("SELECT status_updated_at,duplicate_of FROM role_state WHERE job_id=?",
                                     (job["job_id"],)).fetchone()
                current.update(dict(state) if state else {"status_updated_at": None, "duplicate_of": None})
            return current

        role = current_role()

        def eligible(current):
            if (not current or current.get("status") != "new" or
                    routed_elsewhere(cfg, current) or
                    current.get("tailor_status") != "ready" or
                    _role_url(current.get("apply_url")) != _role_url(job.get("url"))):
                raise ValueError("岗位、名片或就绪简历已改变，未开始申请。")
            if (current.get("company_norm") or "").lower() in store.active_cooldowns(conn, cfg):
                raise ValueError("公司仍在冷却期，未开始申请。")
            if (current.get("company_norm") or "").lower() in companies_under_other_cards(conn):
                raise ValueError("这家公司已有用其他名片投出的申请，未开始申请。")
            if interview_ledger.is_blocked(current.get("company_norm") or "", interview_ledger.blocked_keys(cfg, conn)):
                raise ValueError("这家公司正在技术面，未开始申请。")

        eligible(role)
        original = Path(role.get("tailored_resume_path") or "")
        if not original.is_file():
            raise ValueError("已就绪的简历文件不存在。")
        original_hash = hashlib.sha256(original.read_bytes()).hexdigest()

        def unchanged(current):
            eligible(current)
            for key in ("apply_profile", "tailored_resume_path", "tailor_status", "status_updated_at",
                        "duplicate_of", "company", "title", "jd_hash", "apply_url"):
                if current.get(key) != role.get(key):
                    raise ValueError("岗位在准备期间被修改，未覆盖其更新。")
            if not original.is_file() or hashlib.sha256(original.read_bytes()).hexdigest() != original_hash:
                raise ValueError("原简历在准备期间改变，未覆盖其更新。")
        card = ((cfg.get("apply_profiles") or {}).get("profiles") or {}).get("casey") or {}
        profile = {**(cfg.get("profile") or {}), **card}
        if card.get("display_name") != "Casey Example" or card.get("email") != "sample-5fbfd918@example.com":
            raise ValueError("Casey 名片与已确认身份不符。")
        profile.update(first_name="Casey", last_name="Example", preferred_name="Casey")
        validate_identity(profile)
        existing = inspect_resume(original, original.name, profile)
        if role.get("apply_profile") == "casey" and existing and existing["identity_verified"]:
            conn.execute("BEGIN IMMEDIATE")
            unchanged(current_role())
            conn.commit()
            return dict(job)

        source = tailoring.resume_yaml_path(cfg, role["company"], role["job_id"])
        raw = source.read_bytes()
        tailored = yaml.safe_load(raw)
        if not isinstance(tailored, dict) or tailored.get("job_id") != role["job_id"]:
            raise ValueError("定制简历与岗位不匹配。")
        base = tailoring.load_base(cfg)
        tailoring.validate_tailored(tailored, base, target_company=role["company"])
        merged = tailoring.merge(base, tailored, card)
        score = store.resume_score_from_path(str(original))
        if score is None:
            score = 0
        parent = cfgmod.resolve_path(cfg["output_dirs"]["resumes"]) / "agent_claims"
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder = Path(tempfile.mkdtemp(prefix="claim-", dir=parent))
        artifact = folder / (tailoring.friendly_pdf_prefix(role["company"], role["title"], role["job_id"], "Casey Example") + f"{score}.pdf")
        tailoring.render_pdf(merged, artifact)
        artifact.chmod(0o600)
        evidence = inspect_resume(artifact, artifact.name, profile)
        if not evidence or not evidence["identity_verified"]:
            raise ValueError("更新后的简历姓名、邮箱或电话核对失败。")

        conn.execute("BEGIN IMMEDIATE")
        unchanged(current_role())
        if source.read_bytes() != raw:
            raise ValueError("定制简历在准备期间改变，需重新检查。")
        conn.execute("INSERT INTO role_state(job_id,status,apply_profile,tailor_status,tailored_resume_path) "
                     "VALUES(?, 'new', 'casey', 'ready', ?) ON CONFLICT(job_id) DO UPDATE SET "
                     "apply_profile='casey', tailor_status='ready', tailored_resume_path=excluded.tailored_resume_path",
                     (job["job_id"], str(artifact)))
        conn.commit()
        committed = True
        return dict(job)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        if artifact is not None and not committed:
            artifact.unlink(missing_ok=True)
            artifact.parent.rmdir()
