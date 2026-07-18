"""Resume ⇄ JD match scoring — local heuristics, NO LLM / no API key (project rule).

Mirrors what Jobscan.co's "Match Rate" measures, but honestly:
  - skills_coverage  : of the skills the JD asks for, how many the resume surfaces
                       (variant-aware: "Go" == "Golang", "Postgres" == "PostgreSQL")
  - title_alignment  : does the resume's role language match the JD's role
  - achievements     : share of experience bullets carrying a concrete metric
  - parseability     : are the standard sections present & machine-readable
      overall 0-100 = weighted blend; gate (default 80) → apply / hold

Truth-safety: the feedback loop only ever suggests keywords that are ALREADY TRUE for
the candidate (present somewhere in the locked base résumé). Keywords the JD wants but
the base does not support are reported as honest GAPS, never as "add these".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------- skill vocabulary (canonical -> surface variants) ----------
# Domain-tuned for the target set (backend / distributed systems / databases / fintech).
# Each canonical skill matches if ANY variant appears as a whole word/phrase.
_HARD_SKILLS: dict[str, list[str]] = {
    # languages
    "Go": ["go", "golang"], "Java": ["java"], "Python": ["python"],
    "C++": [r"c\+\+", "cpp"], "Rust": ["rust"], "C": [r"\bc\b(?!\+)"],
    "SQL": ["sql"], "TypeScript": ["typescript"], "JavaScript": ["javascript"],
    "Scala": ["scala"], "Kotlin": ["kotlin"], "Ruby": ["ruby"],
    # databases / storage / streaming
    "PostgreSQL": ["postgres", "postgresql"], "MySQL": ["mysql"],
    "MongoDB": ["mongodb", "mongo"], "Cassandra": ["cassandra"], "Redis": ["redis"],
    "DynamoDB": ["dynamodb"], "Elasticsearch": ["elasticsearch", "elastic search"],
    "Kafka": ["kafka"], "Spanner": ["spanner"], "CockroachDB": ["cockroachdb", "cockroach"],
    # distributed-systems concepts
    "distributed systems": ["distributed system", "distributed systems"],
    "consensus": ["consensus"], "Raft": ["raft"], "Paxos": ["paxos"],
    "replication": ["replication", "replicat"], "sharding": ["sharding", "shard"],
    "consistency": ["consistency", "consistent"], "concurrency": ["concurrency", "concurrent"],
    "idempotency": ["idempoten"], "exactly-once": ["exactly-once", "exactly once"],
    "storage engine": ["storage engine"], "database internals": ["database internals", "db internals"],
    "query engine": ["query engine", "query processing", "sql processing"],
    "microservices": ["microservice"], "gRPC": ["grpc"],
    "REST APIs": ["rest api", "restful", "rest apis"], "API design": ["api design", "apis"],
    "high throughput": ["high throughput", "high-throughput"],
    "low latency": ["low latency", "low-latency"],
    "high availability": ["high availability", "high-availability"],
    "fault tolerance": ["fault tolerance", "fault-tolerant", "resilien", "survivab"],
    "scalability": ["scalab", "scale", "large-scale", "large scale"],
    "observability": ["observability", "observable"], "monitoring": ["monitoring", "monitor"],
    "Prometheus": ["prometheus"], "reliability": ["reliability", "reliable"],
    "event-driven": ["event-driven", "event driven"],
    "stream processing": ["stream processing", "streaming"],
    "message queue": ["message queue", "queueing", "queuing"],
    "backup & restore": ["backup", "restore"], "disaster recovery": ["disaster recovery"],
    "bulk ingestion": ["bulk ingestion", "bulk load", "ingestion"],
    # security
    "encryption": ["encryption", "encrypt"], "cryptography": ["cryptograph", "crypto"],
    "TLS": ["tls"], "X.509": ["x.509", "x509"], "PKI": ["pki"],
    "authentication": ["authentication", "authn"], "authorization": ["authorization", "authz"],
    "security": ["security", "secure"], "compliance": ["compliance", "compliant"],
    # cloud / infra
    "AWS": ["aws", "amazon web services"], "GCP": ["gcp", "google cloud"], "Azure": ["azure"],
    "Kubernetes": ["kubernetes", "k8s"], "Docker": ["docker", "container"],
    "Terraform": ["terraform"], "CI/CD": ["ci/cd", "cicd", "continuous integration"],
    "Linux": ["linux"], "Infrastructure as Code": ["infrastructure as code", "infrastructure-as-code", "iac"],
    # frameworks / data
    "GraphQL": ["graphql"], "Node.js": ["node.js", "nodejs", "node"], "Spring": ["spring"],
    "Pandas": ["pandas"], "PyTorch": ["pytorch"],
}

_SOFT_SKILLS: dict[str, list[str]] = {
    "code review": ["code review"], "mentorship": ["mentor", "mentorship"],
    "collaboration": ["collaborat", "cross-functional", "cross functional"],
    "communication": ["communicat"], "ownership": ["ownership", "end-to-end accountab", "accountab"],
    "self-directed": ["self-directed", "self-starter", "self starter", "autonomous"],
    "testing": ["unit test", "test coverage", "regression test", "testing"],
    "debugging": ["debugging", "troubleshoot", "diagnostic"],
    "problem-solving": ["problem-solving", "problem solving"],
}

# JD role phrases → the resume should echo the role family.
_TITLE_TERMS = ["software engineer", "backend", "back-end", "back end", "distributed",
                "platform", "infrastructure", "systems", "server", "member of technical staff",
                "developer", "services", "database", "reliability"]


def _find(text: str, variants: list[str]) -> bool:
    for v in variants:
        if re.search(r"[\\()\[\].+]", v):        # regex-y variants used raw
            pat = v
        elif len(v) >= 6:                        # long stems match as a prefix:
            pat = r"\b" + re.escape(v)           # "scalab" → scalability/scalable, "monitor" → monitoring
        else:                                    # short tokens (go, c, sql, tls) stay whole-word
            pat = r"\b" + re.escape(v) + r"\b"   # so "go" doesn't match "google"
        if re.search(pat, text, re.IGNORECASE):
            return True
    return False


def _present(text: str, vocab: dict[str, list[str]]) -> set[str]:
    return {canon for canon, vs in vocab.items() if _find(text, vs)}


def _resume_text(merged: dict) -> str:
    parts = [merged.get("summary", "")]
    for g in merged.get("skills", []):
        parts.append(g.get("label", "")); parts.extend(g.get("items", []))
    for j in merged.get("experience", []):
        parts.append(j.get("title", "")); parts.extend(j.get("bullets", []))
    return " \n ".join(parts)


def jd_keyword_plan(jd_text: str, base_text: str) -> tuple[list[str], list[str]]:
    """For prompt-time feedback. Returns (coverable, true_gaps):
      coverable = JD-relevant skills that ARE true for her (in base) → surface these.
      true_gaps = JD-relevant skills NOT in base → honest gaps, never fabricate.
    """
    vocab = {**_HARD_SKILLS, **_SOFT_SKILLS}
    jd_skills = _present(jd_text or "", vocab)
    base_skills = _present(base_text or "", vocab)
    return sorted(jd_skills & base_skills), sorted(jd_skills - base_skills)


@dataclass
class ResumeScore:
    overall: int
    verdict: str                       # apply | hold
    skills_coverage: int
    title_alignment: int
    achievements: int
    parseability: int
    matched_hard: list[str] = field(default_factory=list)
    missing_hard: list[str] = field(default_factory=list)
    matched_soft: list[str] = field(default_factory=list)
    missing_soft: list[str] = field(default_factory=list)
    coverable: list[str] = field(default_factory=list)   # missing but TRUE (in base) → safe to add
    true_gaps: list[str] = field(default_factory=list)   # missing and NOT in base → honest gap

    def summary_line(self) -> str:
        return (f"match {self.overall}/100 ({self.verdict.upper()}) · "
                f"skills {self.skills_coverage} · title {self.title_alignment} · "
                f"metrics {self.achievements} · parse {self.parseability}")


def score_resume(jd_text: str, merged: dict, cfg: dict, base_text: str | None = None) -> ResumeScore:
    rs = (cfg or {}).get("resume_scoring") or {}
    floor = float(rs.get("floor", 65))
    target = float(rs.get("target", rs.get("gate", 80)))   # `gate` kept as a fallback for old configs
    strong = float(rs.get("strong", 85))
    jd = jd_text or ""
    rtext = _resume_text(merged)

    jd_hard = _present(jd, _HARD_SKILLS)
    jd_soft = _present(jd, _SOFT_SKILLS)
    r_hard = _present(rtext, _HARD_SKILLS)
    r_soft = _present(rtext, _SOFT_SKILLS)

    matched_hard = sorted(jd_hard & r_hard)
    missing_hard = sorted(jd_hard - r_hard)
    matched_soft = sorted(jd_soft & r_soft)
    missing_soft = sorted(jd_soft - r_soft)

    # skills coverage: hard skills weigh 2×, soft 1× (ATS match is mostly hard skills)
    got = 2 * len(matched_hard) + len(matched_soft)
    need = 2 * len(jd_hard) + len(jd_soft)
    skills_coverage = round(100 * got / need) if need else 100

    # title alignment: does the resume echo the JD's role family?
    jd_titles = {t for t in _TITLE_TERMS if t in jd.lower()}
    r_titles = {t for t in _TITLE_TERMS if t in rtext.lower()}
    title_alignment = round(100 * len(jd_titles & r_titles) / len(jd_titles)) if jd_titles else 100

    # achievements: share of experience bullets carrying a concrete number/metric
    bullets = [b for j in merged.get("experience", []) for b in j.get("bullets", [])]
    metric = re.compile(r"\d|%|x\b")
    with_metric = sum(1 for b in bullets if metric.search(b))
    achievements = round(100 * with_metric / len(bullets)) if bullets else 0
    achievements = min(100, achievements + 20)   # a well-formed resume needn't quantify every line

    # parseability: standard sections present & non-empty (our template guarantees this)
    have = sum(bool(merged.get(k)) for k in ("summary", "skills", "experience", "education"))
    parseability = round(100 * have / 4)

    overall = round(0.60 * skills_coverage + 0.15 * title_alignment
                    + 0.15 * achievements + 0.10 * parseability)

    # truth-safe feedback: split what's missing into "coverable" (true, in base) vs real gaps.
    base_present = _present(base_text or "", {**_HARD_SKILLS, **_SOFT_SKILLS}) if base_text else set()
    all_missing = missing_hard + missing_soft
    coverable = sorted(m for m in all_missing if m in base_present)
    true_gaps = sorted(m for m in all_missing if m not in base_present)

    verdict = ("strong" if overall >= strong else "apply" if overall >= target
               else "improve" if overall >= floor else "drop")
    return ResumeScore(
        overall=overall, verdict=verdict,
        skills_coverage=skills_coverage, title_alignment=title_alignment,
        achievements=achievements, parseability=parseability,
        matched_hard=matched_hard, missing_hard=missing_hard,
        matched_soft=matched_soft, missing_soft=missing_soft,
        coverable=coverable, true_gaps=true_gaps,
    )
