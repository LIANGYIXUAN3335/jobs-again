"""Answers the owner wrote by hand on the 8787 board.

Filler types them, Reviewer checks against them, and both models read them as confirmed facts.
Only the board's own endpoints write here; no model output ever does.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
from pathlib import Path

from . import config as cfgmod

_root = cfgmod.ROOT / "data" / "agent_lab"
_lock = threading.RLock()
_READ_LIMIT = 300   # the extension reads at most this many characters of a question
_READ_MARGIN = 20   # its copy can carry a star or a "Required" that the form's own text does not
# Only the extension's cut text is exactly that long, so this branch never lets a shorter question through.
_CUT = r"|^(?=[\s\S]{" + str(_READ_LIMIT) + r"}$)\W*"


def path(cfg: dict) -> Path:
    # Answers belong to one person, so each workspace keeps its own file.
    name = re.sub(r"\W+", "_", cfgmod.workspace_name(cfg)).strip("_").lower()
    return Path(_root) / (f"confirmed_answers.{name}.json" if name else "confirmed_answers.json")


def _words_pattern(words: list[str]) -> str:
    return r"\W*".join(words)


def question_topic(question: str, description: str = "") -> str:
    """A pattern for exactly this question, read alike by Python and the extension's JavaScript.

    It holds all that they read: the title and the statement under it (the description), and no more
    words after them. So a Yes to a bare "Acknowledgement" says nothing about the statement another form
    puts under that title, and a Yes to one statement says nothing about another. "able to travel?" never
    matches "able to travel abroad?", and "I have no relatives here" never matches "I have relatives here".
    Case and punctuation do not matter.
    """
    asked = re.sub(r"\s+", " ", f"{question} {description}").strip().lower()
    words = re.findall(r"\w+", asked)
    if not words:
        raise ValueError("问题里至少要有一个词。")
    topic = r"^\W*" + _words_pattern(words) + r"\W*(?:required\W*)?$"
    if len(asked) <= _READ_LIMIT - _READ_MARGIN:
        return topic
    # Past the limit the extension sees only the opening. Python reads all of it, see whole_text_topic.
    seen = [m[0] for m in re.finditer(r"\w+", asked) if m.end() <= _READ_LIMIT - _READ_MARGIN]
    return topic + _CUT + _words_pattern(seen) if seen else topic


def _every_word(topic: str) -> str:
    # An answer saved before the extension kept yes and no in a statement has them optional, which matches its opposite.
    return topic.replace("(?:yes)?", "yes").replace("(?:no)?", "no")


def whole_text_topic(topic: str) -> str:
    """The same pattern for a reader holding the whole text: nothing cut."""
    return _every_word(topic.split(_CUT)[0])


# A title that only names a statement written somewhere else: "GDPR Acknowledgement", "Consent Notice". The real question
# is on the page, so a Yes to the title alone says nothing about what it signs.
_STATEMENT_TITLE = re.compile(r"^\W*(?:[\w.&'’/-]+\s+){0,4}(?:acknowledge?ments?|consents?|notices?|statements?|disclosures?|declarations?|attestations?|"
                              r"certifications?|agreements?|terms|polic(?:y|ies)|confirmation)\W*$|^\W*(?:please\s+)?(?:confirm|acknowledge)\W*$", re.I)
_POINTS_ELSEWHERE = re.compile(r"\b(?:this|the|above)\s+(?:statement|notice|policy|terms|disclosure)\b", re.I)


def statement_title(question: str, options=()) -> bool:
    """Does this title only point at a statement? A question or a first-person sentence says it all by itself."""
    title = re.sub(r"\s+", " ", question or "").strip()
    if "?" in title or re.match(r"\W*i\b", title, re.I):
        return False
    return _STATEMENT_TITLE.search(title) is not None or (
        len(title.split()) <= 6 and any(_POINTS_ELSEWHERE.search(option or "") for option in options))


def _loose(text: str) -> str:
    return " ".join(word for word in re.findall(r"\w+", text.lower()) if word not in {"yes", "no"})


def reads_alike(answer: dict, asked: str) -> bool:
    """A near miss of this answer's question: the same words apart from yes and no, or the same opening where the extension's read stops.

    Filler and preflight match on all the words, so neither takes these for the same question. A form holding
    such a value got it some other way, and "I have relatives here" is not "I have no relatives here".
    A pattern written by hand has no such near miss.
    """
    question, description = answer.get("question") or "", answer.get("description") or ""
    try:
        if whole_text_topic(answer["topic"]) != whole_text_topic(question_topic(question, description)):
            return False
    except (KeyError, ValueError):
        return False
    stored, seen = _loose(f"{question} {description}"), _loose(asked)
    limit = _READ_LIMIT - _READ_MARGIN
    return stored == seen or (len(stored) > limit and len(seen) > limit and stored[:limit] == seen[:limit])


def all_answers(cfg: dict) -> list[dict]:
    try:
        data = json.loads(path(cfg).read_text(encoding="utf-8"))
        return [{**a, "topic": _every_word(a["topic"])} for a in data["answers"] if a.get("id") and a.get("topic") and a.get("answer")]
    except FileNotFoundError:
        return []
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # Without them a question stops for the owner, which is the safe direction.
        print(f"confirmed answers unreadable: {type(exc).__name__}", file=sys.stderr)
        return []


def for_company(cfg: dict, company: str | None = "") -> list[dict]:
    """Answers usable on this company's forms; a company-specific one stays with that company."""
    key = (company or "").strip().casefold()
    usable = [a for a in all_answers(cfg) if not a.get("company") or a["company"].strip().casefold() == key]
    # The first match wins, so the answer to this exact question goes before a topic-wide one.
    return sorted(usable, key=lambda a: a["topic"] != question_topic(a.get("question") or "?x", a.get("description") or ""))


def fact_text(answer: dict) -> str:
    if answer.get("fact"):
        return answer["fact"]
    if answer.get("description"):
        return (f"Question: {answer.get('question', '')} Statement: {answer['description']} Answer: {answer['answer']} "
                "(this answer covers only this exact statement)")
    return f"Question: {answer.get('question', '')} Answer: {answer['answer']}"


def _yesno(answer: str) -> str | None:
    value = answer.strip().rstrip(".").casefold()
    return value if value in {"yes", "no"} else None


def _write(cfg: dict, answers: list[dict]) -> None:
    target = path(cfg)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "answers": answers}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(target)


def remember(cfg: dict, question: str, answer: str, *, company: str = "", topic: str = "", fact: str = "", answer_id: str = "",
             soft: bool = False, description: str = "") -> dict:
    """Add an answer, or replace the one already kept for this same question and statement."""
    question, answer, description = question.strip(), answer.strip(), description.strip()
    if not question or not answer:
        raise ValueError("问题和答案都不能为空。")
    topic = topic or question_topic(question, description)
    # An answer naming this company must not be typed into another company's form, and neither may a Yes to a
    # bare statement title with no statement beside it: another company's page can put different words behind that title.
    own = company.strip().casefold() in answer.casefold() or (statement_title(question) and not description)
    scope = company.strip() if company.strip() and own else ""
    now = time.time()
    with _lock:
        answers = all_answers(cfg)
        item = next((a for a in answers if a["topic"] == topic and (a.get("company") or "") == scope), None)
        if item is None:
            item = {"id": answer_id or "q-" + hashlib.sha256((topic + scope).encode()).hexdigest()[:12], "topic": topic, "created_at": now}
            answers.append(item)
        item.update(question=question, answer=answer, yesno=_yesno(answer), company=scope, updated_at=now)
        if description:
            item["description"] = description
        if fact:
            item["fact"] = fact
        if soft:   # A preference, not a fact: when the form cannot take it, whatever it offers is fine.
            item["soft"] = True
        _write(cfg, answers)
        return item


def change(cfg: dict, answer_id: str, answer: str) -> dict:
    answer = answer.strip()
    if not answer:
        raise ValueError("答案不能为空；不要了就删除。")
    with _lock:
        answers = all_answers(cfg)
        item = next((a for a in answers if a["id"] == answer_id), None)
        if item is None:
            raise KeyError(answer_id)
        item.update(answer=answer, yesno=_yesno(answer), updated_at=time.time())
        _write(cfg, answers)
        return item


def forget(cfg: dict, answer_id: str) -> None:
    with _lock:
        answers = all_answers(cfg)
        kept = [a for a in answers if a["id"] != answer_id]
        if len(kept) == len(answers):
            raise KeyError(answer_id)
        _write(cfg, kept)
