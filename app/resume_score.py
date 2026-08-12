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
    # "Go" is case-SENSITIVE: JDs write the language as "Go", prose writes "go to market",
    # "as we go". Without that, ~every JD looked like it asked for Go (see _find's (?-i:…) note).
    "Go": [r"(?-i:\bGo\b)", "golang"], "Java": ["java"], "Python": ["python"],
    "C++": [r"c\+\+", "cpp"], "Rust": ["rust"],
    # "C" the language, and ONLY that. The old `\bc\b(?!\+)` also fired on C#, Objective-C,
    # "C-suite", "(C&SI)" and "Washington D.C." — 509 roles (18%) grew a phantom C requirement she can never
    # meet, which is a pure, permanent skills_coverage penalty. Case-sensitive + neighbours
    # excluded on both sides, and a JD that writes the pair ("C/C++", "C, or C++", "C++ or C")
    # counts as C++ ONLY — she HAS C++, so billing that as an unmet C requirement double-
    # punished a skill she has. 509 → 130 roles; the survivors genuinely list C on its own.
    "C": [r"(?-i:(?<![\w+#&.-])(?<!C\+\+/)(?<!C\+\+, )(?<!C\+\+ or )(?<!C\+\+ and )"
          r"C(?![\s,/]*(?:or|and)?[\s,/]*C\+\+)(?![\w+#&-]))"],
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
    # NOT bare "crypto" — that matched "cryptocurrency"/"crypto assets", so every fintech JD
    # (Ripple, Circle, Coinbase…) looked like it demanded cryptography she doesn't have.
    "encryption": ["encryption", "encrypt"], "cryptography": ["cryptograph"],
    "TLS": ["tls"], "X.509": ["x.509", "x509"], "PKI": ["pki"],
    "authentication": ["authentication", "authn"], "authorization": ["authorization", "authz"],
    "security": ["security", "secure"], "compliance": ["compliance", "compliant"],
    # cloud / infra
    "AWS": ["aws", "amazon web services"], "GCP": ["gcp", "google cloud"], "Azure": ["azure"],
    "Kubernetes": ["kubernetes", "k8s"], "Docker": ["docker", "container"],
    "Terraform": ["terraform"], "CI/CD": ["ci/cd", "cicd", "continuous integration"],
    "Linux": ["linux"], "Infrastructure as Code": ["infrastructure as code", "infrastructure-as-code", "iac"],
    # frameworks / data
    # bare "node" dropped: it matched "leader node", "each node in the cluster".
    "GraphQL": ["graphql"], "Node.js": ["node.js", "nodejs"], "Spring": ["spring"],
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

# How a canonical skill should READ when the optimize loop surfaces it into a résumé's skills
# list. The canonical keys are matching tokens, not résumé prose — an "Additional Relevant
# Skills: ownership, self-directed" line looks like keyword stuffing to the human who reads it
# after the ATS does. Anything not listed here surfaces under its own name, which is already fine
# ("Kafka", "PostgreSQL"). Every phrase here still matches its canonical variants, so relabeling
# can't cost coverage.
SURFACE_AS: dict[str, str] = {
    "ownership": "end-to-end ownership",
    "self-directed": "self-directed execution",
    "fault tolerance": "fault-tolerant design",
    "high availability": "high-availability systems",
    "high throughput": "high-throughput systems",
    "low latency": "low-latency systems",
    "concurrency": "concurrency",
    "event-driven": "event-driven architecture",
    "bulk ingestion": "data ingestion",
    "message queue": "message queues",
    "database internals": "database internals",
    "API design": "API design",
    "REST APIs": "REST APIs",
    "code review": "code review",
    "testing": "automated testing",
    "debugging": "debugging & troubleshooting",
    "monitoring": "monitoring & alerting",
    "backup & restore": "backup & restore",
    "scalability": "scalability",
    "collaboration": "cross-functional collaboration",
    "communication": "technical communication",
    "problem-solving": "problem-solving",
}


def surface_label(canon: str) -> str:
    """Résumé-facing wording for a canonical skill (see SURFACE_AS)."""
    return SURFACE_AS.get(canon, canon)


# JD role phrases → the resume should echo the role family.
_TITLE_TERMS = ["software engineer", "backend", "back-end", "back end", "distributed",
                "platform", "infrastructure", "systems", "server", "member of technical staff",
                "developer", "services", "database", "reliability"]


# ---------- role family: is this even the job this résumé is for? ----------
#
# Found the hard way (2026-07-24): a Plaid REPORTER posting scored 94/100 against her backend
# résumé — the same as real backend roles. Keyword coverage can only answer "of what the JD asks
# in our vocabulary, how much does the résumé cover"; a journalism JD asks for almost nothing in
# our vocabulary, so coverage saturates at ~100 on 2 matched terms. No amount of coverage math
# fixes that, because the question it can't ask is categorical: WRONG ROLE FAMILY.
#
# So the family check is title-driven and explicit. Empirically validated against all 2875
# titles in the DB (155 flagged, every one eyeballed). JD-side corroboration was measured and
# rejected: sales-engineer JDs name-drop 3+ languages while some real dev JDs name zero, so JD
# evidence can't tell those apart — the title can.
#
# Precedence (first match wins):
#   0. cfg resume_scoring.family_eng_allow — owner's substring whitelist, for the occasional
#      company that titles a real dev role like a field role (e.g. Adobe's
#      "Senior Application Engineer - Photoshop" is plausibly a product SWE).
#   1. ENG_CORE  — "<eng-prefix> engineer/developer" adjacency: software/backend/production/….
#      Protects "Sr. Production Engineer, Solutions Engineering" (the team suffix does not
#      outvote the role noun) and "Software Engineer, Payments Partner Integrations".
#   2. SALESY    — customer-facing engineer compounds (sales/solutions/support/success/field/
#      customer/partner/applications engineer, developer advocate/relations, business
#      developer). "(?<!software )applications? engineer" spares Workday's real
#      "Software Application Engineer".
#   3. bare engineer/developer/programmer word → eng.
#   4. NON_ENG   — consultant/strategist/reporter/analyst/sales/PM/recruiter/… families.
#   5. unknown   — no cap, but no free title points either.
_FAMILY_ENG_CORE = re.compile(
    r"\b(?:software|backend|back.?end|full.?stack|production|infra(?:structure)?|platform|"
    r"systems?|data|security|cloud|reliability|site.?reliability|machine.?learning|ml|ai|"
    r"network|storage|database|devops|embedded|firmware|distributed|forward.?deployed)"
    r"[- ]engineer|\bswe\b|\bsde\b|member of technical staff|software developer", re.I)
_FAMILY_SALESY = re.compile(
    r"(?<!software )applications?[- ]engineer|"
    # gtm/revenue added 2026-07-25: ClickHouse's "GTM Engineer, AI & Automation" (go-to-market)
    # slipped through on the bare 'engineer' rule and became a diversify wildcard.
    r"\b(?:sales|solutions?|support|success|field|customer|partner|gtm|revenue)[- ]engineer|"
    r"\bdeveloper[- ](?:advocate|relations|marketing|evangelist)|\bbusiness[- ]developer",
    re.I)
# Engineering, but not HER engineering. The taxonomy was binary (eng / non_eng), so every
# hardware, EE, mechanical, manufacturing and safety title landed in 'eng' and got the full
# coverage arithmetic — and that arithmetic INVERTS on them: the skill vocabulary only knows
# software, so a battery JD's real asks (cell chemistry, thermal, DFMEA) are invisible, the
# handful of generic words it does contain ("testing", "python") all match, and coverage
# saturates. Measured 2026-07-31: Anduril's Battery Engineer scored 93 and its Harness
# Engineer 90, while its real Senior Software Engineer, Stateful Systems scored 82. Absence
# of vocabulary was reading as "nothing was asked for that she lacks".
# Checked AFTER _FAMILY_ENG_CORE so "Senior Software Engineer, Semiconductor Systems" and
# "Platform Engineer" stay eng — a software role in a hardware domain is still a software role.
_FAMILY_HW_ENG = re.compile(
    r"\b(?:battery|harness|thermal|mechanical|electrical|chemical|civil|structural|industrial|"
    r"manufactur\w*|facilities|environmental\s+health|health\s+and\s+safety|ehs|"
    r"signal\s+integrity|power\s+integrity|rf|antenna|optical|optics|photonics|"
    r"hardware|pcb|asic|silicon|semiconductor|packaging|"
    r"functional\s+safety|vehicle\s+safety|systems\s+safety|"
    r"process\s+engineer|failure\s+analysis|materials|mechatronic\w*|powertrain|propulsion|"
    r"digital\s+design)\b", re.I)
# …unless the title itself says software. Two false rescues found while calibrating and
# deliberately excluded from this guard: 'data' would rescue "Data Center … EHS Engineer" via
# the building, and 'infrastructure' would rescue "Mass Production Infrastructure Manufacture
# Engineer" — in both the software-ish word is a modifier, not the job.
_FAMILY_SW_MARKER = re.compile(
    r"\b(?:software|swe|sde|backend|back-end|frontend|front-end|full[- ]?stack|devops|sre|"
    r"site\s+reliability|machine\s+learning|\bml\b|cloud|api|web|mobile|android|ios|"
    r"application\s+developer|developer\s+experience|data(?!\s*cent))\b", re.I)
_FAMILY_ENG_WORD = re.compile(
    r"\bengineer(?:ing)?\b|\bdeveloper\b|\bprogrammer\b|\btechnologist\b", re.I)
_FAMILY_NON_ENG = re.compile(
    r"\breporter\b|\bjournalist\b|\beditor\b|\bwriter\b|\bcontent\b|\bmarketing\b|\bbrand\b|"
    r"\bcommunications?\b|public relations|\bsales\b|account (?:executive|manager|director)|"
    r"business development|\brepresentative\b|\bpartner\b|\bpartnerships?\b|\bconsultant\b|"
    r"\bconsulting\b|\badvisors?\b|\badvisory\b|\bstrategist\b|\bstrategy\b|\bevangelist\b|"
    r"\badvocate\b|\banalyst\b|\brecruit(?:er|ing)?\b|\bsourcer\b|\btalent\b|\bpeople\b|"
    r"human resources|\bhr\b|\blegal\b|\bcounsel\b|\battorney\b|\bparalegal\b|\bfinance\b|"
    r"\bfinancial\b|\baccountant\b|\baccounting\b|\bpayroll\b|\btreasury\b|\btax\b|"
    r"\bprocurement\b|\bbuyer\b|\bdesigner\b|\bux\b|\bui\b|product manager|product management|"
    r"program manager|project manager|product marketing|product owner|chief of staff|"
    r"executive assistant|\badministrative\b|office (?:manager|coordinator)|\bworkplace\b|"
    r"\bcommunity\b|customer (?:success|experience|support)|support specialist|success manager|"
    r"\btrainer\b|\binstructor\b|\bcoach\b|\btechnician\b|\bfellows?\b|\bfellowship\b|"
    r"\bresearcher\b|\bscientist\b|\binvestigator\b|\bsupervisor\b|\bassociate\b|\bcto\b",
    re.I)


def role_family(title: str | None, cfg: dict | None = None) -> str:
    """'eng' | 'non_eng' | 'unknown' — from the role TITLE (the JD can't be trusted for this;
    see the block comment above)."""
    t = (title or "").strip()
    if not t:
        return "unknown"
    allow = ((cfg or {}).get("resume_scoring") or {}).get("family_eng_allow") or []
    tl = t.lower()
    if any(str(a).lower() in tl for a in allow):
        return "eng"
    if _FAMILY_ENG_CORE.search(t):
        return "eng"
    if _FAMILY_SALESY.search(t):
        return "non_eng"
    # Non-software engineering disciplines. Reported as non_eng on purpose: every consumer of
    # this function (the 40-point cap, verdict=drop, scoring's drop_non_eng_titles, optimize,
    # gap_report) already does the right thing with that value, and a third family would have
    # to be taught to each of them separately.
    if _FAMILY_HW_ENG.search(t) and not _FAMILY_SW_MARKER.search(t):
        return "non_eng"
    if _FAMILY_ENG_WORD.search(t):
        return "eng"
    if _FAMILY_NON_ENG.search(t):
        return "non_eng"
    return "unknown"


def _find(text: str, variants: list[str]) -> bool:
    """A variant containing regex metacharacters is used as a raw pattern — that's the escape
    hatch for the ambiguous tokens. Matching is case-insensitive overall, so a variant that
    must be case-SENSITIVE (the "Go"/"go" and "C"/"c" problem) wraps itself in `(?-i:…)`."""
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


# ---------- summary quality: self-praise vs. operational evidence ----------
# Found the hard way (2026-08-11): 30 tailored résumés shipped a summary ending in
# "— a strong fit for <the JD's exact title>. Self-directed, ramps fast on new codebases."
# 18 of them went out as PDFs (Apple ×5, Google ×5, Microsoft ×4, LinkedIn ×2, Mastercard ×2).
#
# Why the existing scorer never caught it: `achievements` only inspects experience BULLETS, and
# `parseability` only asks whether summary is non-empty. So the one paragraph guaranteed to be
# read by a human was the single unscored field in the document.
#
# Two things are wrong with that sentence, and they are opposite failures:
#   1. It states the CONCLUSION the reader is supposed to reach. "A strong fit for X" is the
#      hiring manager's judgment to make; asserting it yourself gets discounted on sight, and
#      pasting the JD's literal title into the prose advertises that this was mail-merged.
#   2. It spends the most expensive six seconds on the page saying nothing checkable.
#
# So this is scored as a subtraction (puffery) plus an addition (evidence you operated
# something real): scale, failure modes, reliability verbs, and numbers. The intent is not
# "ban adjectives" — it is that every clause in the summary should be something a reference
# check could confirm.
_PUFF = [
    r"\ba (?:strong|great|excellent|perfect|natural|ideal) fit\b",
    r"\bwell[- ]suited\b", r"\bwell[- ]positioned\b", r"\bideal candidate\b",
    r"\bproven track record\b", r"\bpassionate about\b", r"\bself[- ]directed\b",
    r"\bramps? fast\b", r"\bfast learner\b", r"\bquick learner\b",
    r"\bteam player\b", r"\bgo[- ]getter\b", r"\bresults[- ]driven\b",
    r"\bhighly (?:motivated|skilled|experienced)\b", r"\bexceptional\b",
    r"\bworld[- ]class\b", r"\btop[- ]tier\b", r"\brock ?star\b", r"\bguru\b", r"\bninja\b",
    r"\bthrives? in\b", r"\bdeep passion\b", r"\bstrong desire\b",
]
# Evidence that the writer has actually RUN something in production. Deliberately concrete:
# these are the nouns you only use if you were on the hook when it broke.
_EVIDENCE = [
    r"\d",                                                   # any number at all
    r"\b(?:p50|p90|p95|p99|percentile|tail latency)\b",
    r"\b(?:qps|rps|tps|throughput|concurrent|scale|scaling)\b",
    r"\b(?:on[- ]call|incident|outage|postmortem|live[- ]site|sev\d?)\b",
    r"\b(?:failure|fault|degradation|regression|rollback|retry|timeout|circuit break)\w*\b",
    r"\b(?:uptime|availability|sla|slo|error budget)\b",
    r"\b(?:migrat|deprecat|decommission|cutover)\w*\b",
    r"\b(?:reduc|cut|drop|elimina|prevent|contain)\w*\b",     # verbs that imply a measured delta
]
_PUFF_RE = [re.compile(p, re.I) for p in _PUFF]
_EVIDENCE_RE = [re.compile(p, re.I) for p in _EVIDENCE]


def summary_quality(summary: str | None, *, title: str | None = None) -> tuple[int, list[str]]:
    """Score a résumé summary 0-100 on 'checkable claims, not self-assessment'.

    Returns (score, flags). Flags are human-readable and are meant to be handed straight to the
    auto-tailor loop as fix instructions — a number alone can't tell the model WHICH clause to cut.
    """
    text = (summary or "").strip()
    if not text:
        # Neutral, NOT zero. This scorer runs on two different things: the tailored output (which
        # always has a summary) and the base résumé via prescreen (which deliberately has none —
        # the base is a fact store, summaries are written per-application). Scoring absence as 0
        # punished the base for a field it is not supposed to carry. Nothing is lost by staying
        # neutral here: `parseability` already counts summary as one of its four required sections,
        # so a tailored résumé that ships without one is still penalized — just not twice.
        return 100, []

    flags: list[str] = []
    hits = [r.pattern for r in _PUFF_RE if r.search(text)]
    # Each distinct puff phrase costs 18; three of them zeroes the puff budget outright.
    puff_penalty = min(60, 18 * len(hits))
    if hits:
        found = ", ".join(f'"{r.search(text).group(0)}"' for r in _PUFF_RE if r.search(text))
        flags.append(f"self-praise — cut {found}; state what you built/operated instead")

    # Pasting the JD's literal title into the prose is the mail-merge tell, and it is worth
    # flagging separately from generic puffery because the fix is different (delete, don't rewrite).
    if title:
        # Both sides must be normalized identically — punctuation becomes a space AND runs of
        # whitespace collapse. Normalizing only the title silently disabled this check, because
        # "Sr. Software Engineer" leaves a double space in the résumé text where the "." was.
        def _norm(s: str) -> str:
            return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s.lower())).strip()
        t = _norm(title)
        if len(t) > 12 and t in _norm(text):
            puff_penalty = min(75, puff_penalty + 15)
            flags.append(f'echoes the JD title verbatim ("{title}") — reads as mail-merged; delete it')

    ev = sum(1 for r in _EVIDENCE_RE if r.search(text))
    evidence_credit = min(30, 10 * ev)          # 3+ distinct kinds of evidence = full credit

    # The evidence prompt is deliberately NOT raised on every thin summary. Measured against the
    # real corpus (2026-08-11), an `ev < 2` trigger fired on 1722 of 3397 — half the library,
    # including perfectly serviceable summaries — which is alarm fatigue, and a check that cries
    # wolf on half the corpus gets ignored exactly when it's right. It now fires only when the
    # summary offers NOTHING checkable, or when puffery was cut and something must fill the hole
    # (there the advice is actionable rather than a vague "be more concrete").
    if ev == 0 or (hits and ev < 2):
        flags.append("no operational evidence — name a system you ran, its scale, "
                     "and a failure mode you reduced")

    return max(0, min(100, 70 - puff_penalty + evidence_credit)), flags


@dataclass
class ResumeScore:
    overall: int
    verdict: str                       # strong | apply | improve | drop
    skills_coverage: int
    title_alignment: int
    achievements: int
    parseability: int
    summary_quality: int = 100         # see summary_quality(); 100 = nothing to fix
    summary_flags: list[str] = field(default_factory=list)
    matched_hard: list[str] = field(default_factory=list)
    missing_hard: list[str] = field(default_factory=list)
    matched_soft: list[str] = field(default_factory=list)
    missing_soft: list[str] = field(default_factory=list)
    coverable: list[str] = field(default_factory=list)   # missing but TRUE (in base) → safe to add
    true_gaps: list[str] = field(default_factory=list)   # missing and NOT in base → honest gap
    family: str = "unknown"            # eng | non_eng | unknown (see role_family)

    def summary_line(self) -> str:
        fam = "  ⛔ WRONG ROLE FAMILY" if self.family == "non_eng" else ""
        # Surfaced even though its weight is small: a puffy summary is a cheap, high-value fix,
        # and it is the part of the résumé a human actually reads.
        puff = f"  ⚠️ SUMMARY {self.summary_quality}: {self.summary_flags[0]}" if self.summary_flags else ""
        return (f"match {self.overall}/100 ({self.verdict.upper()}) · "
                f"skills {self.skills_coverage} · title {self.title_alignment} · "
                f"metrics {self.achievements} · parse {self.parseability} · "
                f"summary {self.summary_quality}{fam}{puff}")


def score_resume(jd_text: str, merged: dict, cfg: dict, base_text: str | None = None,
                 *, title: str | None = None) -> ResumeScore:
    rs = (cfg or {}).get("resume_scoring") or {}
    floor = float(rs.get("floor", 65))
    target = float(rs.get("target", rs.get("gate", 80)))   # `gate` kept as a fallback for old configs
    strong = float(rs.get("strong", 85))
    jd = jd_text or ""
    rtext = _resume_text(merged)
    fam = role_family(title, cfg) if title else "unknown"

    jd_hard = _present(jd, _HARD_SKILLS)
    jd_soft = _present(jd, _SOFT_SKILLS)
    r_hard = _present(rtext, _HARD_SKILLS)
    r_soft = _present(rtext, _SOFT_SKILLS)

    matched_hard = sorted(jd_hard & r_hard)
    missing_hard = sorted(jd_hard - r_hard)
    matched_soft = sorted(jd_soft & r_soft)
    missing_soft = sorted(jd_soft - r_soft)

    # skills coverage: hard skills weigh 2×, soft 1× (ATS match is mostly hard skills),
    # SHRUNK toward a skeptical prior by k pseudo-units of evidence. Raw got/need let any JD
    # that asks for ≤2 things in our vocabulary hand out a perfect 100 — that is how a
    # journalism posting outscored real backend roles: near-zero evidence read as certainty.
    # (got + k·p0) / (need + k) makes low-evidence coverage converge on p0 (a deliberate
    # "probably mediocre") and rich-JD coverage stay data-driven; an empty JD now scores p0,
    # not 100. k is in weighted units (6 ≈ three hard skills of evidence).
    K = float(rs.get("shrink_k", 6))
    P0 = float(rs.get("shrink_prior", 0.60))
    got = 2 * len(matched_hard) + len(matched_soft)
    need = 2 * len(jd_hard) + len(jd_soft)
    skills_coverage = round(100 * (got + K * P0) / (need + K))

    # title alignment: does the resume echo the JD's role family? The old "no title terms in
    # the JD → free 100" default REWARDED non-engineering JDs (a reporter posting contains no
    # engineering title terms, so it got full marks exactly where it deserved zero). Absent
    # evidence is now neutral-at-best, and a non-eng title is 0 outright.
    jd_blob = f"{title or ''}\n{jd}".lower()
    jd_titles = {t for t in _TITLE_TERMS if t in jd_blob}
    r_titles = {t for t in _TITLE_TERMS if t in rtext.lower()}
    if fam == "non_eng":
        title_alignment = 0
    elif jd_titles:
        title_alignment = round(100 * len(jd_titles & r_titles) / len(jd_titles))
    elif fam == "eng":
        title_alignment = 70           # title says engineer; the JD text just never echoed terms
    else:
        title_alignment = 30           # nothing anywhere says this is her role family

    # achievements: share of experience bullets carrying a concrete number/metric
    bullets = [b for j in merged.get("experience", []) for b in j.get("bullets", [])]
    metric = re.compile(r"\d|%|x\b")
    with_metric = sum(1 for b in bullets if metric.search(b))
    achievements = round(100 * with_metric / len(bullets)) if bullets else 0
    achievements = min(100, achievements + 20)   # a well-formed resume needn't quantify every line

    # parseability: standard sections present & non-empty (our template guarantees this)
    have = sum(bool(merged.get(k)) for k in ("summary", "skills", "experience", "education"))
    parseability = round(100 * have / 4)

    # summary quality: the one field a human is guaranteed to read was the one field nothing
    # scored. Its 0.05 weight comes out of parseability, which is a constant 100 for our own
    # template and therefore carries no signal — so a clean summary nets ZERO change vs. the old
    # formula, and only a puffed one loses points (≤5). Bounded on purpose: this is meant to
    # steer the tailor loop via `summary_flags`, not to silently reclassify the existing DB.
    sq, sq_flags = summary_quality(merged.get("summary"), title=title)

    overall = round(0.60 * skills_coverage + 0.15 * title_alignment
                    + 0.15 * achievements + 0.05 * parseability + 0.05 * sq)
    # Wrong role family is categorical, not gradual: no coverage arithmetic may rescue a
    # journalism/sales/consulting posting into the apply queue. Capped below tailor_min (50)
    # so these also stop being auto-tailored, and verdict-forced to drop.
    if fam == "non_eng":
        overall = min(overall, int(rs.get("family_cap", 40)))

    # truth-safe feedback: split what's missing into "coverable" (true, in base) vs real gaps.
    base_present = _present(base_text or "", {**_HARD_SKILLS, **_SOFT_SKILLS}) if base_text else set()
    all_missing = missing_hard + missing_soft
    coverable = sorted(m for m in all_missing if m in base_present)
    true_gaps = sorted(m for m in all_missing if m not in base_present)

    verdict = ("drop" if fam == "non_eng"
               else "strong" if overall >= strong else "apply" if overall >= target
               else "improve" if overall >= floor else "drop")
    return ResumeScore(
        overall=overall, verdict=verdict,
        skills_coverage=skills_coverage, title_alignment=title_alignment,
        achievements=achievements, parseability=parseability,
        summary_quality=sq, summary_flags=sq_flags,
        matched_hard=matched_hard, missing_hard=missing_hard,
        matched_soft=matched_soft, missing_soft=missing_soft,
        coverable=coverable, true_gaps=true_gaps, family=fam,
    )
