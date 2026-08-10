"""The skill matcher's ambiguous tokens. Each case below was a real miscount over the 2875-JD
corpus, and each one moved every affected résumé's score in the wrong direction."""
from app import resume_score
from app.resume_score import _HARD_SKILLS as H
from app.resume_score import _SOFT_SKILLS as S


def has(text, canon):
    vocab = {**H, **S}
    return canon in resume_score._present(text, vocab)


def test_c_is_the_language_and_not_its_lookalikes():
    for jd in ["Go, C, or Python", "languages: Go, Rust, C. Bonus points for Linux",
               "experience in C, C#, or Go"]:
        assert has(jd, "C"), jd
    for jd in ["Applications in Go, Python, Java, JavaScript, or C#",
               "writing Go, Swift or Objective-C",
               "win C-suite-sponsored AI opportunities",
               "The Consulting & Systems Integrator (C&SI) organization",
               "offices in New York, Washington D.C., London",
               "I.N.T.E.R.D.I.C.T. (Identify, Neutralize, Triage)"]:
        assert not has(jd, "C"), jd


def test_c_slash_cpp_counts_as_cpp_only():
    """She HAS C++. A JD writing the pair was billing her for an unmet C requirement as well —
    double-punishing a skill she has, on 259 roles."""
    for jd in ["Go, Java, C/C++ etc.", "Rust, C, or C++", "C++ or C experience", "C++/C internals"]:
        assert has(jd, "C++"), jd
        assert not has(jd, "C"), jd


def test_go_the_language_not_the_verb():
    assert has("Strong experience with Go and Kubernetes", "Go")
    assert has("Experience writing Golang services", "Go")
    assert not has("we go to market fast and let projects go", "Go")
    assert not has("our go-to-market motion", "Go")


def test_cryptography_is_not_cryptocurrency():
    """Every fintech JD in the corpus looked like it demanded cryptography she doesn't have."""
    assert has("applied cryptography and key management", "cryptography")
    assert has("cryptographic primitives", "cryptography")
    assert not has("the crypto and blockchain industry", "cryptography")
    assert not has("crypto assets and stablecoins", "cryptography")


def test_node_the_runtime_not_the_machine():
    assert has("Node.js backend services", "Node.js")
    assert not has("each node in the cluster elects a leader", "Node.js")


def test_vocabulary_the_base_resume_now_covers():
    """The 2026-07-24 wording pass, amended by the 2026-08-06 credibility pass: concept terms
    must survive in the BULLETS (that's where they're defensible); "self-directed" was a
    soft-skill keyword living only in the skills list, which the credibility rules ban — it is
    deliberately no longer covered."""
    from app import config as cfgmod, tailoring
    base_text = tailoring._base_text(tailoring.load_base(cfgmod.load_config()))
    for canon in ["ownership", "fault tolerance", "high availability",
                  "concurrency", "event-driven", "high throughput", "bulk ingestion"]:
        assert has(base_text, canon), f"base résumé no longer covers {canon!r}"


def test_surface_label_reads_like_a_resume():
    assert resume_score.surface_label("ownership") == "end-to-end ownership"
    assert resume_score.surface_label("Kafka") == "Kafka"          # pass-through when it's fine
    # relabelling must never cost coverage: the phrase still matches its own canonical
    assert has(resume_score.surface_label("fault tolerance"), "fault tolerance")
    assert has(resume_score.surface_label("self-directed"), "self-directed")


# ---------- role family & evidence shrinkage (the reporter-scored-94 fix) ----------

def test_family_classifier_poster_children():
    from app.resume_score import role_family
    non_eng = ["Reporter - This Week in Fintech", "Professional Services Consultant",
               "Deployment Strategist", "Business Development Representative - Fintech",
               "Senior Client Partner, Mid-Market (Services)", "Sales Engineer",
               "Senior Customer Engineer, Majors", "Solutions Engineering, Ads Solutions",
               "Developer Advocate", "AI Success Engineer - AMER", "Business Developer",
               "Senior Managing Consultant, Advisors & Consulting Services, Deploy"]
    for t in non_eng:
        assert role_family(t) == "non_eng", t


def test_family_classifier_protections():
    """Titles that LOOK salesy but are real engineering — each protection exists because a
    real posting needed it."""
    from app.resume_score import role_family
    eng = ["Software Application Engineer (US Federal)",          # software-prefix lookbehind
           "Sr. Production Engineer, Solutions Engineering",      # role noun beats team suffix
           "Software Engineer, Payments Partner Integrations",    # 'partner' inside an eng title
           "Senior Software Engineer - Streaming Platform Client",
           "Member of Technical Staff (Storage)", "Forward Deployed Engineer", "AI Engineer"]
    for t in eng:
        assert role_family(t) == "eng", t


def test_family_allowlist_override():
    from app.resume_score import role_family
    cfg = {"resume_scoring": {"family_eng_allow": ["application engineer - photoshop"]}}
    assert role_family("Senior Application Engineer - Photoshop") == "non_eng"
    assert role_family("Senior Application Engineer - Photoshop", cfg) == "eng"


def _score(jd, title):
    from app import config as cfgmod, tailoring, prescreen, resume_score
    cfg = cfgmod.load_config()
    base = tailoring.load_base(cfg)
    return resume_score.score_resume(jd, prescreen.base_as_merged(base), cfg,
                                     tailoring._base_text(base), title=title)


def test_wrong_family_is_capped_regardless_of_coverage():
    """A journalism JD that happens to name two matchable terms must not outscore backend
    roles. This exact failure shipped: a Plaid Reporter posting scored 94/100."""
    sc = _score("We want a reporter covering fintech. Familiarity with SQL and Python a plus.",
                "Reporter - This Week in Fintech")
    assert sc.family == "non_eng"
    assert sc.overall <= 40
    assert sc.verdict == "drop"
    assert sc.title_alignment == 0


def test_thin_jd_no_longer_scores_a_free_100():
    """Near-zero evidence used to read as certainty: coverage = 2/2 matched = 100%. Shrinkage
    makes it converge on the skeptical prior instead."""
    sc = _score("Great communicator wanted. Python is a plus.", "Data Platform Associate Lead")
    assert sc.skills_coverage < 80          # was 100 before shrinkage
    assert sc.overall < 80                  # was ~94


def test_rich_backend_jd_still_scores_strong():
    jd = ("Senior backend engineer: Go, Java, Python, SQL, Kafka, PostgreSQL, Redis, Docker, "
          "Kubernetes, AWS, distributed systems, microservices, gRPC, high availability, "
          "low latency, monitoring, CI/CD, code review, mentorship, cross-functional collaboration.")
    sc = _score(jd, "Senior Backend Engineer")
    assert sc.family == "eng"
    # 2026-08-06 credibility pass: the base no longer lists code review / mentorship (soft
    # skills are banned as keywords) or gRPC / low latency (no bullet evidence), so this JD
    # tops out at APPLY, not STRONG — that lost coverage is deliberate honesty, not a bug.
    assert sc.overall >= 80, sc.summary_line()
    assert sc.verdict in ("apply", "strong"), sc.summary_line()


def test_empty_jd_scores_the_prior_not_perfection():
    sc = _score("", "Software Engineer")
    assert sc.skills_coverage == 60         # (0 + k·p0) / (0 + k) = p0
    assert sc.overall < 80                  # an empty JD is unknown, not a perfect match


def test_non_software_engineering_titles_are_wrong_family():
    from app.resume_score import role_family

    """Engineering, but not HER engineering. The taxonomy was binary, so hardware/EE/safety
    titles landed in 'eng' and got the full coverage arithmetic — which INVERTS on them: the
    skill vocabulary only knows software, so the JD's real asks are invisible, the few generic
    words it does contain all match, and coverage saturates. Measured before the fix: Anduril's
    Battery Engineer 93 and Harness Engineer 90 vs its real Senior SWE, Stateful Systems 82."""
    for t in ["Senior Battery Engineer (Drones)", "Senior Harness Engineer, Omen",
              "Environmental Health and Safety Senior Engineer", "Senior Facilities Engineer",
              "Senior PCB Engineer", "Senior Thermal Engineer", "Senior Signal Integrity Engineer",
              "Vehicle Systems Safety", "Senior Co-Packaged Optics Process Engineer",
              "Senior Digital Design Engineer", "Senior IC Packaging Development Engineer"]:
        assert role_family(t) == "non_eng", t


def test_a_software_role_in_a_hardware_domain_stays_engineering():
    from app.resume_score import role_family

    """The guard that stops the rule over-firing: the domain word is a modifier, the job is
    still software. Each of these was a real false positive while calibrating."""
    for t in ["Senior Software Engineer, Stateful Systems",
              "Harness and Platform Engineer, AI Safety and Security",
              "Software Linux Engineer - Installation and Packaging",
              "Senior Systems Software Engineer, Semiconductor Systems",
              "Senior Software Engineer, Meta Factory Agent Harness",
              "Backend Engineer", "Site Reliability Engineer", "Data Engineer",
              "Security Engineer", "Machine Learning Engineer", "Platform Engineer"]:
        assert role_family(t) == "eng", t


def test_data_center_and_manufacturing_are_not_rescued_by_stray_software_words():
    from app.resume_score import role_family

    """Two false RESCUES found while calibrating: 'Data Center' contains 'data', and 'Mass
    Production Infrastructure Manufacture Engineer' contains 'infrastructure'. In both the
    software-ish word names a building or a pipeline, not the job."""
    assert role_family("Data Center Engineering and Operations EHS Engineer") == "non_eng"
    assert role_family("Senior Mass Production Infrastructure Manufacture Engineer") == "non_eng"
