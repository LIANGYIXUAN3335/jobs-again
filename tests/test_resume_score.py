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
    """The 2026-07-24 wording pass. These are the terms the JDs use for work she had already
    done; if a future edit drops them from resumes/resume_base.yaml, this fails."""
    from app import config as cfgmod, tailoring
    base_text = tailoring._base_text(tailoring.load_base(cfgmod.load_config()))
    for canon in ["ownership", "self-directed", "fault tolerance", "high availability",
                  "concurrency", "event-driven", "high throughput", "bulk ingestion"]:
        assert has(base_text, canon), f"base résumé no longer covers {canon!r}"


def test_surface_label_reads_like_a_resume():
    assert resume_score.surface_label("ownership") == "end-to-end ownership"
    assert resume_score.surface_label("Kafka") == "Kafka"          # pass-through when it's fine
    # relabelling must never cost coverage: the phrase still matches its own canonical
    assert has(resume_score.surface_label("fault tolerance"), "fault tolerance")
    assert has(resume_score.surface_label("self-directed"), "self-directed")
