"""Reading a form before running it: what can go, what waits for the owner, what never can."""
import json
import threading
import time
from pathlib import Path

import pytest

from app import confirmed_answers, form_preflight as F


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "_root", tmp_path)
    monkeypatch.setattr(confirmed_answers, "_root", tmp_path)
    monkeypatch.setattr(F.cfgmod, "workspace_name", lambda _cfg: "test")
    return {"profile": {"location": "San Jose, CA", "state": "California", "work_authorized": True, "needs_sponsorship": False,
                        "first_name": "Avery", "last_name": "Chen", "email": "avery@example.test", "linkedin": "https://linkedin.test/in/avery"}}


def job(job_id, company="Acme"):
    return {"job_id": job_id, "company": company, "title": "Engineer", "url": f"https://jobs.ashbyhq.com/{company}/{job_id}/application"}


def field(label, kind="Boolean", required=True, options=()):
    return {"label": label, "type": kind, "required": required, "options": list(options)}


def load(cfg, forms):
    jobs = [job(*key) if isinstance(key, tuple) else job(key) for key in forms]
    table = {j["job_id"]: forms[key] for j, key in zip(jobs, forms)}
    F.refresh(cfg, jobs, fetch=lambda url: table[url.split("/")[4]], pause=0)
    return jobs


def verdict(cfg, *entries):
    F.forms_path(cfg).unlink(missing_ok=True)   # a form read today is not read again
    load(cfg, {"x": list(entries)})
    return F.assess(cfg)["x"]


BASE = [field("Name", "String"), field("Email", "Email"), field("Resume", "File"), field("Website", "String", required=False)]


def test_a_form_of_known_fields_and_rules_is_ready(cfg):
    assert verdict(cfg, *BASE, field("Are you legally authorized to work in the United States?"),
                   field("Where are you located?", "Location"), field("Legal First Name", "String"),
                   field("Most Recent Company", "String"), field("What is your LinkedIn URL?", "Url"),
                   field("Where did you hear about us? (e.g., LinkedIn, referral)", "String"))["verdict"] == "ready"


def test_a_written_answer_waits_behind_the_essay_setting(cfg):
    result = verdict(cfg, *BASE, field("Why are you interested in working at Acme?", "LongText"))
    assert result["verdict"] == "essay"
    assert not F.runnable(cfg, result, False) and F.runnable(cfg, result, True)


def test_an_unknown_choice_is_asked_and_an_answer_clears_it_without_reading_again(cfg):
    question = "Which Lambda office location(s) are you able to work out of?"
    jobs = load(cfg, {"x": BASE + [field(question, "ValueSelect", options=["San Jose", "Bellevue"])]})
    assert F.assess(cfg)["x"]["verdict"] == "ask"
    board = F.board(cfg, jobs)
    assert board["questions"][0]["question"] == question and board["questions"][0]["options"] == ["San Jose", "Bellevue"]
    confirmed_answers.remember(cfg, question, "San Jose")
    assert F.assess(cfg)["x"]["verdict"] == "ready"


@pytest.mark.parametrize("entry,why", [
    (field("Exercise Submission (Shared URL)", "String"), "本人"),
    (field("Why Braintrust? (DO NOT use AI; 1-2 sentences)", "LongText"), "本人"),
    (field("Please confirm: I personally reviewed this application", "Boolean"), "本人"),
    (field("Writing sample", "File"), "文件"),
    (field("Cover Letter", "File"), "文件"),
    (field("What are your salary expectations?", "String"), "薪资"),
])
def test_work_only_the_owner_can_do_never_runs(cfg, entry, why):
    result = verdict(cfg, *BASE, entry)
    assert result["verdict"] == "never" and why in result["blocks"][0]


def test_a_promise_hidden_in_an_option_also_never_runs(cfg):
    result = verdict(cfg, *BASE, field("Please confirm before submitting.", "MultiValueSelect",
                                       options=["I chose this role and personally reviewed this application."]))
    assert result["verdict"] == "never"


def test_a_statement_needs_the_owners_own_yes_even_when_a_filler_rule_would_tick_it(cfg):
    statement = "I agree to the Terms and Conditions"
    assert verdict(cfg, *BASE, field(statement))["verdict"] == "ask"
    confirmed_answers.remember(cfg, statement, "Yes")
    assert F.assess(cfg)["x"]["verdict"] == "ready"


def test_export_rules_stay_with_the_owner(cfg):
    assert verdict(cfg, *BASE, field("Cerebras is subject to the U.S. Export Administration Regulations. Are you a U.S. person?", "ValueSelect",
                                     options=["I am a U.S. person", "I am not"]))["verdict"] == "ask"


def test_optional_questions_never_hold_a_form_back(cfg):
    assert verdict(cfg, *BASE, field("Tell us a joke", "LongText", required=False), field("Any export control history?", required=False))["verdict"] == "ready"


def test_the_list_puts_the_answer_that_unlocks_the_most_first(cfg):
    jobs = load(cfg, {("a", "Acme"): BASE + [field("Rare?", "ValueSelect", options=["A"])],
                      ("b", "Beta"): BASE + [field("Common?", "ValueSelect", options=["A"])],
                      ("c", "Gamma"): BASE + [field("Common?", "ValueSelect", options=["A"])]})
    board = F.board(cfg, jobs)
    assert [(q["question"], q["forms"], q["companies"]) for q in board["questions"]] == [("Common?", 2, 2), ("Rare?", 1, 1)]
    assert board["counts"]["ask"] == 3 and board["companies"]["ask"] == 3


def test_a_form_nobody_could_read_never_counts_as_ready_and_a_gone_posting_is_not_counted(cfg):
    def fetch(url):
        if "bad" in url:
            raise OSError("network")
        return None
    jobs = [job("bad"), job("gone")]
    assert F.refresh(cfg, jobs, fetch=fetch, pause=0) == {"read": 1, "failed": 1}
    verdicts = F.assess(cfg)
    assert "bad" not in verdicts and verdicts["gone"]["verdict"] == "gone"
    assert F.board(cfg, jobs)["counts"]["unread"] == 1


def test_a_read_form_is_not_read_again_within_a_day(cfg):
    calls = []
    F.refresh(cfg, [job("x")], fetch=lambda url: calls.append(url) or BASE, pause=0, now=1000.0)
    F.refresh(cfg, [job("x")], fetch=lambda url: calls.append(url) or BASE, pause=0, now=1000.0 + 3600)
    assert len(calls) == 1
    F.refresh(cfg, [job("x")], fetch=lambda url: calls.append(url) or BASE, pause=0, now=1000.0 + 2 * F.MAX_AGE)
    assert len(calls) == 2


def test_without_the_rules_nothing_is_ready_and_the_board_says_so(cfg):
    jobs = load(cfg, {"x": BASE})

    def broken(*_):
        raise RuntimeError("node not found")
    board = F.board(cfg, jobs, rules=broken)
    assert board["error"] and board["counts"]["ready"] == 0


def test_the_form_api_response_becomes_plain_entries(monkeypatch):
    payload = {"data": {"jobPosting": {"id": "1", "title": "T", "applicationForm": {"sections": [{"fieldEntries": [
        {"id": "a", "isRequired": True, "field": {"title": "Pick  one\n", "type": "ValueSelect", "selectableValues": [{"label": "A"}, {"label": "B"}]}},
        {"id": "b", "isRequired": False, "field": {"title": "Resume", "type": "File"}}]}]}}}}

    class Response:
        def __enter__(self):
            import io
            return io.BytesIO(json.dumps(payload).encode())

        def __exit__(self, *_):
            return False
    monkeypatch.setattr(F.urllib.request, "urlopen", lambda request, timeout: Response())
    assert F.fetch_form("https://jobs.ashbyhq.com/acme/uuid/application") == [
        {"label": "Pick one", "type": "ValueSelect", "required": True, "options": ["A", "B"]},
        {"label": "Resume", "type": "File", "required": False, "options": []}]


def verdict_for(cfg, company, *entries):
    F.forms_path(cfg).unlink(missing_ok=True)
    load(cfg, {("x", company): BASE + list(entries)})
    return F.assess(cfg)["x"]


SIERRA_OFFICE = ["Yes, I am based in one of Sierra's office locations.", "While I am not based in one, I am open to relocating.", "No, I am not able to work from a Sierra office."]


def test_a_rule_yes_clicks_only_a_bare_yes(cfg):
    question = "Are you able to work from a Sierra office listed for this role?"
    result = verdict_for(cfg, "Sierra", field(question, "ValueSelect", options=SIERRA_OFFICE))
    assert result["verdict"] == "ask" and result["asks"][0]["options"] == SIERRA_OFFICE
    assert verdict_for(cfg, "Sierra", field(question, "ValueSelect", options=["Yes", "No"]))["verdict"] == "ready"
    # The sentence beside it is never clicked, and it does not matter.
    assert verdict(cfg, *BASE, field("Are you located in the Bay Area and willing to work on-site?", "ValueSelect",
                                     options=["Yes", "No, but willing to relocate"]))["verdict"] == "ready"


@pytest.mark.parametrize("label, options", [
    ("Will you now or will you in the future require employment visa sponsorship?",
     ["Yes, I will require Acme to sponsor my employment", "No, I do not require sponsorship to work in the United States"]),
    ("Will you now or in the future require immigration sponsorship?", ["Yes, now", "Yes, in the future", "No"]),
    ("Work Authorization", ["I require sponsorship to work here", "I am authorized to work for any employer in the country"]),
    # OPT and CPT are only examples of a work permit here, so naming them claims nothing.
    ("Are you legally authorized to work in the United States?",
     ["YES: If you currently hold a valid work permit (such as OPT, CPT, or an existing H-1B transfer).", "NO: If you do not have any active, legal authorization."]),
])
def test_a_legal_rule_stays_ok_on_sentences_it_can_score(cfg, label, options):
    assert verdict(cfg, *BASE, field(label, "ValueSelect", options=options))["verdict"] == "ready"


@pytest.mark.parametrize("label, options", [
    ("U.S. Work Authorization Status", ["Can work for any employer", "Can work for current employer", "Seeking work authorization"]),
    # Filler's first-word click would make this claim, so nothing here may count as ready.
    ("Are you legally authorized to work in the United States?", ["Yes - I am a U.S. citizen", "Yes - I am a permanent resident", "No"]),
])
def test_a_legal_rule_on_options_it_cannot_score_is_asked(cfg, label, options):
    assert verdict(cfg, *BASE, field(label, "ValueSelect", options=options))["verdict"] == "ask"


@pytest.mark.parametrize("entry", [
    field("If no, is your home country listed below?"),
    field("Country of residence"),                                  # a rule answers it, in words a button cannot hold
    field("Ideal start date in office", "Date"),
    field("Are you willing to work on-site on this date?", "Date"),  # a rule's Yes is no date
    field("How many years of experience do you have?", "Number"),
    field("How did you hear about Commure?", "MultiValueSelect", options=["LinkedIn", "Other Job Boards"]),
    field("How many years of relevant professional experience do you have?", "ValueSelect", options=["1-2 years", "8+"]),
    field("Where would you like to work?", "Location"),
    field("Which state do you live in?", "ValueSelect", options=["Texas", "Oregon"]),
])
def test_a_question_filler_cannot_finish_is_asked(cfg, entry):
    result = verdict(cfg, *BASE, entry)
    assert result["verdict"] == "ask" and result["asks"][0]["question"] == entry["label"]


def test_a_state_list_of_two_letter_codes_can_take_the_state(cfg):
    assert verdict(cfg, *BASE, field("State", "ValueSelect", options=["CA", "TX"]))["verdict"] == "ready"


def test_a_source_question_keeps_running_without_a_job_board_option(cfg):
    assert verdict(cfg, *BASE, field("How did you hear about us?", "ValueSelect", options=["Referral", "LinkedIn", "X"]))["verdict"] == "ready"


def test_a_confirmed_sentence_cannot_press_a_yes_no_button(cfg):
    passport = "Confirming that you have a valid passport? (necessary for international travel)"
    confirmed_answers.remember(cfg, "国际出差", "I'm not able to travel internationally right now.", topic="international travel")
    assert verdict(cfg, *BASE, field(passport))["verdict"] == "ask"
    # The same sentence does not fit these choices either, and answering this exact question clears it.
    assert verdict(cfg, *BASE, field(passport, "ValueSelect", options=["Yes", "No"]))["verdict"] == "ask"
    confirmed_answers.remember(cfg, passport, "Yes")
    assert F.assess(cfg)["x"]["verdict"] == "ready"


def test_a_preference_takes_whatever_the_form_offers(cfg):
    question = "What is your preferred coding language?"
    confirmed_answers.remember(cfg, question, "Python", soft=True)
    assert verdict(cfg, *BASE, field(question, "ValueSelect", options=["Go", "Java"]))["verdict"] == "ready"


def test_an_answer_on_file_clears_a_date_and_a_count(cfg):
    start, years = "Ideal start date in office", "How many years of experience do you have?"
    confirmed_answers.remember(cfg, start, "2027-01-04")
    confirmed_answers.remember(cfg, years, "7")
    assert verdict(cfg, *BASE, field(start, "Date"), field(years, "Number"))["verdict"] == "ready"


def test_a_multi_select_cannot_be_ticked_even_with_an_answer_on_file(cfg):
    question = "Please select your preferred working location."
    entry = field(question, "MultiValueSelect", options=["Mountain View, CA", "Los Angeles, CA"])
    assert verdict(cfg, *BASE, entry)["verdict"] == "ask"
    confirmed_answers.remember(cfg, question, "Mountain View, CA")
    result = F.assess(cfg)["x"]
    assert result["verdict"] == "never" and question in result["blocks"][0]


def test_a_salary_number_still_never_runs(cfg):
    assert verdict(cfg, *BASE, field("What are your salary expectations?", "Number"))["verdict"] == "never"


@pytest.mark.parametrize("statement", [
    "I certify that all information in this application is accurate.",
    "Do you agree to our Code of Conduct?",
    "Do you accept the Applicant Privacy Notice?",
    "I confirm that I am a U.S. citizen or permanent resident.",
    "I confirm that I have not used AI tools to write this application.",
    "I have read and accept the privacy policy.",
])
def test_a_statement_to_sign_is_asked_though_a_rule_would_say_yes(cfg, statement):
    assert verdict(cfg, *BASE, field(statement))["verdict"] == "ask"


def test_a_statement_inside_an_option_is_asked(cfg):
    assert verdict(cfg, *BASE, field("Are you able to come to the office?", "ValueSelect",
                                     options=["Yes", "I agree to binding arbitration"]))["verdict"] == "ask"


@pytest.mark.parametrize("label, kind, options", [
    ("Please confirm you are authorized to work in the United States.", "Boolean", []),
    ("Will you now or in the future require sponsorship?", "Boolean", []),
    ("Please confirm you can work from our San Francisco office 3 days a week.", "Boolean", []),
    # Opting out signs nothing, so the consent beside it does not hold the form back.
    ("Interview Recording Consent", "ValueSelect", ["Yes, I consent to be recorded", "Opt out of recording"]),
])
def test_questions_with_an_answer_on_file_are_not_mistaken_for_statements(cfg, label, kind, options):
    assert verdict(cfg, *BASE, field(label, kind, options=options))["verdict"] == "ready"


def test_rules_run_as_an_unattended_fill_of_this_companys_form(cfg):
    assert verdict(cfg, *BASE, field("Are you able to lift 50 pounds?"))["verdict"] == "ask"
    question = "Are you a former employee of Aurora?"
    assert verdict_for(cfg, "Aurora", field(question))["verdict"] == "ready"
    assert verdict_for(cfg, "Acme", field(question))["verdict"] == "ask"


def test_what_a_question_says_under_its_title_is_read_too(cfg):
    assert verdict(cfg, *BASE, {**field("Question one"), "description": "Are you legally authorized to work in the United States?"})["verdict"] == "ready"
    assert verdict(cfg, *BASE, {**field("Are you authorized to work in the United States?"),
                                "description": "I agree to binding arbitration."})["verdict"] == "ask"


def test_a_form_older_than_a_day_counts_as_unread_even_with_no_file_changing(cfg):
    jobs = [job("x")]
    F.refresh(cfg, jobs, fetch=lambda url: BASE, pause=0, now=1000.0)
    assert F.assess(cfg, now=1000.0 + 3600)["x"]["verdict"] == "ready"
    assert "x" not in F.assess(cfg, now=1000.0 + 2 * F.MAX_AGE)
    board = F.board(cfg, jobs)
    assert board["counts"]["unread"] == 1 and board["counts"]["ready"] == 0


def test_a_failed_re_read_does_not_keep_the_old_verdict(cfg):
    jobs, later = [job("x")], 1000.0 + 2 * F.MAX_AGE

    def down(url):
        raise OSError("network")
    F.refresh(cfg, jobs, fetch=lambda url: BASE, pause=0, now=1000.0)
    assert F.refresh(cfg, jobs, fetch=down, pause=0, now=later) == {"read": 0, "failed": 1}
    assert "x" not in F.assess(cfg, now=later)


def test_a_form_with_no_questions_counts_as_unread_and_is_read_again(cfg):
    calls = []
    jobs = [job("x")]
    F.refresh(cfg, jobs, fetch=lambda url: calls.append(url) or [], pause=0)
    assert "x" not in F.assess(cfg) and F.board(cfg, jobs)["counts"]["unread"] == 1
    F.refresh(cfg, jobs, fetch=lambda url: calls.append(url) or BASE, pause=0)
    assert len(calls) == 2 and F.assess(cfg)["x"]["verdict"] == "ready"


def api_reply(monkeypatch, payload):
    class Response:
        def __enter__(self):
            import io
            return io.BytesIO(json.dumps(payload).encode())

        def __exit__(self, *_):
            return False
    monkeypatch.setattr(F.urllib.request, "urlopen", lambda request, timeout: Response())


URL = "https://jobs.ashbyhq.com/acme/uuid/application"


@pytest.mark.parametrize("payload", [{"data": None, "errors": [{"message": "boom"}]}, {"data": None},
                                     {"data": {"jobPosting": None}, "errors": [{"message": "boom"}]}])
def test_a_failed_query_is_unread_not_gone(cfg, monkeypatch, payload):
    api_reply(monkeypatch, payload)
    with pytest.raises(ValueError):
        F.fetch_form(URL)
    jobs = [job("x")]
    assert F.refresh(cfg, jobs, pause=0) == {"read": 0, "failed": 1}
    assert "x" not in F.assess(cfg) and F.board(cfg, jobs)["counts"]["unread"] == 1


def test_a_posting_the_api_says_does_not_exist_is_gone(monkeypatch):
    api_reply(monkeypatch, {"data": {"jobPosting": None}})
    assert F.fetch_form(URL) is None


def test_a_description_the_api_sends_is_kept_as_plain_text(monkeypatch):
    api_reply(monkeypatch, {"data": {"jobPosting": {"applicationForm": {"sections": [{"fieldEntries": [
        {"isRequired": True, "field": {"title": "Acknowledgement", "type": "Boolean", "descriptionHtml": "<p>I certify  this.</p>"}}]}]}}}})
    assert F.fetch_form(URL)[0]["description"] == "I certify this."


# ---- found by the independent check of the first round ----
@pytest.mark.parametrize("label, options", [
    ("Are you authorized to work in the United States?", ["Yes - I have a green card", "No"]),
    ("Are you authorized to work in the United States?", ["Yes - GC", "No"]),
    ("Are you authorized to work in the United States?", ["Yes - H1B", "No"]),
    ("Are you authorized to work in the United States?", ["Yes - H1-B", "No"]),
    ("Are you authorized to work in the United States?", ["Yes - U.S. national", "No"]),
    ("Are you authorized to work in the United States?", ["Yes - naturalized", "No"]),
    ("Are you authorized to work in the United States?", ["Yes (TS/SCI cleared)", "No"]),
    ("Are you authorized to work in the United States?", ["Yes, I hold a passport from this country", "No"]),
    ("Are you authorized to work in the United States?", ["Yes, permanently", "Yes, temporarily", "No"]),
    ("Will you now or in the future require sponsorship?", ["Yes", "No - I have a green card"]),
    ("Do you hold work authorization?", ["Yes - Green Card", "No"]),
])
def test_an_option_claiming_a_status_is_asked(cfg, label, options):
    assert verdict(cfg, *BASE, field(label, "ValueSelect", options=options))["verdict"] == "ask"


def test_preflight_sees_what_filler_reads_on_a_plain_box(cfg):
    assert verdict(cfg, *BASE, field("Legal First & Last Name", "String"))["verdict"] == "ready"
    assert verdict(cfg, *BASE, field("Preferred First & Last Name", "String"))["verdict"] == "ready"
    # A rule answers this one, because Filler's reader no longer takes it for a location.
    assert verdict(cfg, *BASE, field("Do you require Visa sponsorship to work in your selected location? If so, which one? And when does your Visa expire?", "String"))["verdict"] == "ready"
    for label in ("City of birth", "Country of citizenship", "Native country"):
        assert verdict(cfg, *BASE, field(label, "String"))["verdict"] == "essay", label
        assert verdict(cfg, *BASE, field(label, "ValueSelect", options=["China", "United States"]))["verdict"] == "ask", label
    assert verdict(cfg, *BASE, field("Is this location acceptable?", "Location"))["verdict"] == "ask"
    for label in ("Current Location", "Your Location", "Current City & State"):
        assert verdict(cfg, *BASE, field(label, "Location"))["verdict"] == "ready", label


def test_a_rule_that_can_only_say_yes_or_no_is_asked_on_a_box_that_asks_no_such_thing(cfg):
    assert verdict(cfg, *BASE, field("Marketing opt-in", "String"))["verdict"] == "ask"
    assert verdict(cfg, *BASE, field("Are you at least 18 years of age?", "String"))["verdict"] == "ready"
    for label in ("Preferred days in office", "Commute distance (miles)", "Remote work preference", "State your reason for applying", "State of licensure"):
        assert verdict(cfg, *BASE, field(label, "String"))["verdict"] == "essay", label


@pytest.mark.parametrize("statement", [
    "By submitting, you agree to our hybrid work policy and our code of conduct.",
    "You acknowledge that this remote position is at-will employment.",
    "Applicants must accept the on-call policy and the Employee Handbook.",
    "Please confirm you agree to our code of conduct and will work on-site.",
    "Please confirm you are authorized to work in the US and that you agree to the code of conduct.",
    "Please confirm you are authorized to work in the U.S. and that you have never been convicted of a felony.",
    "Will you require an export license to access export-controlled technology?",
    "Are you subject to export-control restrictions?",
    "Are you permitted to work with export-controlled technology?",
    "Are you willing to travel to a foreign country?",
    "Are you willing to travel across borders for this role?",
    "Are you able to travel to any of our global offices?",
])
def test_these_wait_for_the_owner_in_a_batch(cfg, statement):
    assert verdict(cfg, *BASE, field(statement))["verdict"] == "ask"


def test_the_export_country_question_keeps_its_standing_answer(cfg):
    assert verdict(cfg, *BASE, field("Are you a citizen or resident of an export-controlled country?"))["verdict"] == "ready"


def test_labels_found_on_real_forms_keep_their_verdict(cfg):
    for label in ("Are you legally authorized to work in the United States?", "Are you able to come into the office four days per week?",
                  "Are you commutable to San Francisco 3x per week?", "Are you willing to travel across the US?",
                  "I am comfortable with the fact that this position requires being in one of our offices 3-4 days per week and is not a remote position."):
        assert verdict(cfg, *BASE, field(label))["verdict"] == "ready", label


def test_a_field_with_nothing_on_file_is_still_an_open_question(cfg):
    assert verdict(cfg, *BASE, field("GitHub profile", "String"))["verdict"] == "essay"
    cfg["profile"]["github"] = "https://github.test/avery"
    assert verdict(cfg, *BASE, field("GitHub profile", "String"))["verdict"] == "ready"


# ---- a statement under a bare title ----
GDPR = "GDPR Acknowledgement"
NOTICE = "I acknowledge the GDPR notice."
ARBITRATION = "I acknowledge and agree to binding arbitration."


def statement(description, label=GDPR, kind="Boolean", options=()):
    entry = field(label, kind, options=options)
    return {**entry, "description": description} if description else entry


def three_forms(cfg, label=GDPR):
    return load(cfg, {("j1", "Acme"): BASE + [statement(NOTICE, label)], ("j2", "Beta"): BASE + [statement(ARBITRATION, label)],
                      ("j3", "Gamma"): BASE + [statement("", label)]})


def verdicts(cfg):
    return {job_id: item["verdict"] for job_id, item in F.assess(cfg).items()}


def test_two_statements_under_one_title_are_two_questions_and_the_card_shows_each(cfg):
    jobs = three_forms(cfg)
    shown = F.board(cfg, jobs)["questions"]
    assert {(q["question"], q["description"], q["forms"]) for q in shown} == {(GDPR, NOTICE, 1), (GDPR, ARBITRATION, 1), (GDPR, "", 1)}


def test_the_same_statement_on_two_forms_is_one_question(cfg):
    jobs = load(cfg, {("a", "Acme"): BASE + [statement(NOTICE)], ("b", "Beta"): BASE + [statement(NOTICE)]})
    shown = F.board(cfg, jobs)["questions"]
    assert [(q["description"], q["forms"], q["companies"]) for q in shown] == [(NOTICE, 2, 2)]


@pytest.mark.parametrize("label", [GDPR, GDPR + ":", "Please confirm:", "Acknowledgement."])
def test_an_answer_for_one_form_clears_only_that_form(cfg, label):
    three_forms(cfg, label)
    assert verdicts(cfg) == {"j1": "ask", "j2": "ask", "j3": "ask"}
    # What the bare form's card sends: no statement, so it says nothing about the two that have one.
    confirmed_answers.remember(cfg, label, "Yes", company="Gamma")
    assert verdicts(cfg) == {"j1": "ask", "j2": "ask", "j3": "ready"}
    confirmed_answers.remember(cfg, label, "Yes", description=NOTICE, company="Acme")
    assert verdicts(cfg) == {"j1": "ready", "j2": "ask", "j3": "ready"}


def test_with_no_statement_from_the_api_an_answer_to_that_title_lets_the_page_decide(cfg):
    # j3's form says something the API did not send. Filler holds her answer against those words and stays blank if they differ.
    three_forms(cfg)
    confirmed_answers.remember(cfg, GDPR, "Yes", description=ARBITRATION)
    assert verdicts(cfg) == {"j1": "ask", "j2": "ready", "j3": "ready"}


def test_another_title_or_a_bare_answer_does_not_stand_in_for_the_page(cfg):
    three_forms(cfg)
    confirmed_answers.remember(cfg, "Another title", "Yes", description=ARBITRATION)
    assert verdicts(cfg) == {"j1": "ask", "j2": "ask", "j3": "ask"}


ILLUMIO = field("GDPR Acknowledgement", "ValueSelect", options=["I Acknowledge/Accept this Statement"])


def test_the_statement_only_the_page_shows_is_asked_with_no_answer_offered_on_the_card(cfg):
    jobs = load(cfg, {("j", "Illumio"): BASE + [ILLUMIO]})
    result = F.assess(cfg)["j"]
    assert result["verdict"] == "ask" and result["asks"][0]["on_page"] is True
    shown = F.board(cfg, jobs)["questions"][0]
    assert shown["on_page"] is True and shown["url"] == jobs[0]["url"]


def test_a_bare_titled_yes_or_no_is_the_same_and_a_statement_from_the_api_can_be_answered(cfg):
    assert verdict(cfg, *BASE, field("Consent Notice"))["asks"][0]["on_page"] is True
    with_words = verdict(cfg, *BASE, {**field("Consent Notice"), "description": "I agree to binding arbitration."})
    assert with_words["verdict"] == "ask" and with_words["asks"][0]["on_page"] is False
    assert verdict(cfg, *BASE, field("Are you able to lift 50 pounds?"))["asks"][0]["on_page"] is False


def test_an_answer_she_gave_on_the_page_lets_that_form_run_under_either_control(cfg):
    jobs = load(cfg, {("j", "Illumio"): BASE + [ILLUMIO]})
    assert F.assess(cfg)["j"]["verdict"] == "ask"
    confirmed_answers.remember(cfg, ILLUMIO["label"], "I Acknowledge/Accept this Statement", description="I acknowledge the notice.", company="Illumio")
    assert F.assess(cfg)["j"]["verdict"] == "ready"
    assert jobs


def test_a_text_that_only_reads_like_her_answer_is_asked_though_a_rule_would_fill_it(cfg):
    label = "Are you authorized to work in the United States?"
    entry = {**field(label), "description": "Answer if so."}
    assert verdict(cfg, *BASE, entry)["verdict"] == "ready"
    confirmed_answers.remember(cfg, label, "Yes", description="Answer yes if so.")
    assert verdict(cfg, *BASE, entry)["verdict"] == "ask"


def test_the_rules_run_once_while_nothing_has_changed(cfg, monkeypatch):
    # Every claim asks for the verdicts, so a memo that never hits starts Node on each poll.
    load(cfg, {"x": BASE + [field("Are you able to lift 50 pounds?")]})
    real, runs = F.subprocess.run, []
    monkeypatch.setattr(F.subprocess, "run", lambda *a, **k: (runs.append(1), real(*a, **k))[1])
    monkeypatch.setattr(F, "_memo", {})
    first = F.assess(cfg)
    assert F.assess(cfg) == first and len(runs) == 1


def test_a_title_repeated_on_the_form_counts_even_when_only_one_row_is_required(cfg):
    row = field("Education History", "ValueSelect", options=["January", "February"])
    optional = {**row, "required": False}
    result = verdict(cfg, *BASE, row, optional, optional, optional)
    assert result["verdict"] == "never" and result["asks"] == []
    assert "4 次" in result["blocks"][0]


def _replies(monkeypatch, *replies):
    """The API answers each request with the next reply, and the queries it saw are kept."""
    seen = []

    def urlopen(request, timeout=None):
        seen.append(json.loads(request.data.decode())["query"])
        reply = replies[len(seen) - 1]
        if isinstance(reply, Exception):
            raise reply

        class Response:
            def __enter__(self):
                import io
                return io.BytesIO(json.dumps(reply).encode())

            def __exit__(self, *_):
                return False
        return Response()
    monkeypatch.setattr(F.urllib.request, "urlopen", urlopen)
    monkeypatch.setitem(F._query, "now", F.QUERY)
    return seen


FORM_REPLY = {"data": {"jobPosting": {"applicationForm": {"sections": [{"fieldEntries": [
    {"isRequired": True, "descriptionHtml": "<p>I don&#39;t  object &amp; agree.</p>", "field": {"title": "Consent Notice", "type": "Boolean"}}]}]}}}}


def test_the_statement_comes_from_the_form_entry_when_the_api_sends_it(monkeypatch):
    seen = _replies(monkeypatch, FORM_REPLY)
    assert F.fetch_form(URL)[0]["description"] == "I don't object & agree."
    assert "descriptionHtml" in seen[0]


@pytest.mark.parametrize("refusal", [{"errors": [{"message": 'Cannot query field "descriptionHtml" on type "FormFieldEntry".'}]},
                                     F.urllib.error.HTTPError(URL, 400, "Bad Request", {}, None)])
def test_an_api_that_will_not_take_the_statement_field_leaves_no_form_unread(monkeypatch, refusal):
    plain = {"data": {"jobPosting": {"applicationForm": {"sections": [{"fieldEntries": [{"isRequired": True, "field": {"title": "Consent Notice", "type": "Boolean"}}]}]}}}}
    seen = _replies(monkeypatch, refusal, plain, plain)
    assert F.fetch_form(URL)[0]["label"] == "Consent Notice"
    assert "descriptionHtml" in seen[0] and "descriptionHtml" not in seen[1]
    F.fetch_form(URL)
    assert len(seen) == 3, "the next form does not ask twice"


def test_any_other_failure_says_nothing_about_the_query(monkeypatch):
    seen = _replies(monkeypatch, F.urllib.error.HTTPError(URL, 503, "Busy", {}, None))
    with pytest.raises(F.urllib.error.HTTPError):
        F.fetch_form(URL)
    assert len(seen) == 1 and F._query["now"] is F.QUERY
    # Refused both ways: nothing shows it was the field, so the next form asks for it again.
    seen = _replies(monkeypatch, {"errors": [{"message": "rate limited"}]}, {"errors": [{"message": "rate limited"}]})
    with pytest.raises(ValueError):
        F.fetch_form(URL)
    assert len(seen) == 2 and F._query["now"] is F.QUERY


def test_no_test_lifts_the_shared_isolation():
    # Undoing the whole monkeypatch also undoes conftest's data paths, and whatever runs next writes to the real folder.
    for path in Path(__file__).parent.glob("test_*.py"):
        assert "monkeypatch." + "undo()" not in path.read_text(encoding="utf-8"), path.name


def test_a_control_other_than_yes_no_with_a_statement_is_judged_on_all_of_it(cfg):
    # On the page Filler reads this control by its title, so a title's answer must not stand for what the API says under it.
    entry = statement(ARBITRATION, "Acknowledgement", "ValueSelect", ["Yes", "No"])
    confirmed_answers.remember(cfg, "Acknowledgement", "Yes")
    assert verdict(cfg, *BASE, entry)["verdict"] == "ask"
    confirmed_answers.remember(cfg, "Acknowledgement", "Yes", description=ARBITRATION)
    assert F.assess(cfg)["x"]["verdict"] == "ready"


def test_the_two_kinds_of_answer_on_file_keep_working(cfg):
    travel = "Are you able to travel internationally?"
    confirmed_answers.remember(cfg, "国际出差", "I'm not able to travel internationally right now.", topic="travel\\w*\\s+internationally|international(?:ly)? travel")
    assert verdict(cfg, *BASE, field(travel, "LongText"))["verdict"] == "ready"
    assert verdict(cfg, *BASE, field(travel))["verdict"] == "ask", "a sentence cannot press a Yes/No button"
    assert verdict(cfg, *BASE, {**field("Offsite", "LongText"), "description": "Is international travel a problem?"})["verdict"] == "ready"
    language = "What is your preferred coding language?"
    confirmed_answers.remember(cfg, language, "Python", soft=True, topic="preferred coding language")
    assert verdict(cfg, *BASE, field(language, "ValueSelect", options=["Go", "Java"]))["verdict"] == "ready"


def test_a_title_repeated_on_one_form_is_never_offered_an_answer(cfg):
    row = field("Education History", "ValueSelect", options=["January", "February"])
    result = verdict(cfg, *BASE, row, row, row, field("Rare?", "ValueSelect", options=["A"]))
    assert result["verdict"] == "never" and result["blocks"] == ["这个标题在表上出现了 3 次，要你本人填：Education History"]
    assert [ask["question"] for ask in result["asks"]] == ["Rare?"]


def test_two_statements_under_one_title_on_one_form_are_still_separate_questions(cfg):
    result = verdict(cfg, *BASE, statement(NOTICE), statement(ARBITRATION))
    assert result["verdict"] == "ask" and [ask["description"] for ask in result["asks"]] == [NOTICE, ARBITRATION]


# ---- one reader at a time ----
def reader_finished():
    for _ in range(500):
        if not F.reading():
            return True
        time.sleep(0.01)
    return False


def test_a_reader_that_cannot_start_leaves_nothing_held(cfg, monkeypatch):
    def broken(self):
        raise RuntimeError("can't start new thread")
    # A context of its own: undoing the whole monkeypatch would also lift conftest's isolation, and the reader would then write to the real folder.
    with monkeypatch.context() as patched:
        patched.setattr(F.threading.Thread, "start", broken)
        with pytest.raises(RuntimeError):
            F.refresh_in_background(cfg, lambda: [])
    assert F.reading() is False
    assert F.refresh_in_background(cfg, lambda: []) is True and reader_finished()


def test_only_one_reader_runs_and_a_later_one_may_start_when_it_ends(cfg):
    gate = threading.Event()

    def load_jobs():
        gate.wait(5)
        return []
    try:
        assert F.refresh_in_background(cfg, load_jobs) is True
        assert F.reading() is True and F.board(cfg, [])["reading"] is True
        assert F.refresh_in_background(cfg, lambda: []) is False
    finally:
        gate.set()
    assert reader_finished() and F.refresh_in_background(cfg, lambda: []) is True and reader_finished()


def test_a_reader_that_fails_midway_still_lets_the_next_one_start(cfg, monkeypatch):
    def broken():
        raise OSError("database is locked")
    monkeypatch.setattr(threading, "excepthook", lambda _args: None)
    assert F.refresh_in_background(cfg, broken) is True
    assert reader_finished()


def test_many_requests_at_once_start_one_reader(cfg):
    release, started, barrier = threading.Event(), [], threading.Barrier(8)

    def request():
        barrier.wait(5)
        started.append(F.refresh_in_background(cfg, lambda: release.wait(5) and []))
    threads = [threading.Thread(target=request) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    release.set()
    assert started.count(True) == 1 and started.count(False) == 7 and reader_finished()


# ---- Greenhouse ----
# Trimmed from real boards-api replies (Verkada, Fivetran, Anthropic, Block, Waymo, Grafana, Braze), read 2026-11-08.
GREENHOUSE_REPLY = {
    "id": 5278670007, "title": "Software Engineer",
    "questions": [
        {"description": None, "label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text", "values": []}]},
        {"description": None, "label": "Resume/CV", "required": True, "fields": [
            {"name": "resume", "type": "input_file", "values": []}, {"name": "resume_text", "type": "textarea", "values": []}]},
        {"description": None, "label": "Cover Letter", "required": False, "fields": [
            {"name": "cover_letter", "type": "input_file", "values": []}, {"name": "cover_letter_text", "type": "textarea", "values": []}]},
        {"description": None, "label": "Do you have the right to work in the country for which you are applying?\t\n", "required": True,
         "fields": [{"name": "question_33024939003", "type": "multi_value_single_select", "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
        {"description": "<p>Please note this includes scenarios where candidates would require employer visa sponsorship following CPT or OPT.</p>",
         "label": "Will you now or at any point in the future require employer visa sponsorship (e.g., H-1B, TN, O-1)?", "required": True,
         "fields": [{"name": "question_13340013007", "type": "multi_value_single_select",
                     "values": [{"label": "Yes", "value": 36218748007}, {"label": "No", "value": 36218750007}]}]},
        {"description": "<p>Why do you want to work at Anthropic? (We value this response highly - great answers are often 200-400 words.)</p>",
         "label": "Why Anthropic?", "required": True, "fields": [{"name": "question_18848370008", "type": "textarea", "values": []}]},
        {"description": None, "label": "By clicking “accept,” you agree that any disputes related to your application shall be submitted to binding arbitration.",
         "required": True, "fields": [{"name": "question_12812187008[]", "type": "multi_value_multi_select", "values": [{"label": "Accept", "value": 20829556008}]}]},
        {"description": "<p>Please ensure to provide <em>either</em> your LinkedIn profile or Resume, we require at least one of the two.&nbsp;</p>",
         "label": "LinkedIn Profile", "required": False, "fields": [{"name": "question_1", "type": "input_text", "values": []}]},
    ],
    "location_questions": [
        {"description": None, "label": "Longitude", "required": True, "fields": [{"name": "longitude", "type": "input_hidden", "values": []}]},
        {"description": None, "label": "Latitude", "required": True, "fields": [{"name": "latitude", "type": "input_hidden", "values": []}]},
        {"description": None, "label": "Location", "required": True, "fields": [{"name": "location", "type": "input_text", "values": []}]}],
    "compliance": [{"type": "eeoc", "description": "&lt;p&gt;\n  If you believe you belong to any of the categories of protected veterans listed below&lt;/p&gt;", "questions": [
        {"required": True, "label": "VeteranStatus", "fields": [{"name": "veteran_status", "type": "multi_value_single_select", "values": [
            {"label": "I don't wish to answer", "value": "3"}, {"label": "I am not a protected veteran", "value": "1"}]}]},
        {"required": False, "label": "Gender", "fields": [{"name": "gender", "type": "multi_value_single_select", "values": [
            {"label": "Decline To Self Identify", "value": "3"}, {"label": "Female", "value": "2"}, {"label": "Male", "value": "1"}]}]}]}],
    "demographic_questions": {"header": "Candidate Demographic Survey", "description": "<p>Completion of this self-identification form is voluntary.</p>", "questions": [
        {"id": 75902, "label": "Please indicate your gender", "required": True, "type": "multi_value_single_select", "answer_options": [
            {"id": 439382, "label": "Male", "free_form": False, "decline_to_answer": False},
            {"id": 439385, "label": "I don't wish to answer", "free_form": False, "decline_to_answer": True}]},
        {"id": 4021036004, "label": "How would you describe your gender identity? (mark all that apply)", "required": False,
         "type": "multi_value_multi_select", "answer_options": [{"id": 4125627004, "label": "Man", "free_form": False, "decline_to_answer": False}]},
        {"id": 1, "label": "Race", "required": None, "type": "multi_value_single_select", "answer_options": []}]},
    "data_compliance": [{"type": "gdpr", "requires_consent": True, "requires_processing_consent": False, "requires_retention_consent": True,
                         "retention_period": 1093, "demographic_data_consent_applies": False}],
}

GREENHOUSE_ENTRIES = [
    {"label": "First Name", "type": "String", "required": True, "options": []},
    {"label": "Resume/CV", "type": "File", "required": True, "options": []},
    {"label": "Cover Letter", "type": "File", "required": False, "options": []},
    {"label": "Do you have the right to work in the country for which you are applying?", "type": "ValueSelect", "required": True, "options": ["Yes", "No"]},
    {"label": "Will you now or at any point in the future require employer visa sponsorship (e.g., H-1B, TN, O-1)?", "type": "ValueSelect",
     "required": True, "options": ["Yes", "No"],
     "description": "Please note this includes scenarios where candidates would require employer visa sponsorship following CPT or OPT."},
    {"label": "Why Anthropic?", "type": "LongText", "required": True, "options": [],
     "description": "Why do you want to work at Anthropic? (We value this response highly - great answers are often 200-400 words.)"},
    {"label": "By clicking “accept,” you agree that any disputes related to your application shall be submitted to binding arbitration.",
     "type": "MultiValueSelect", "required": True, "options": ["Accept"]},
    {"label": "LinkedIn Profile", "type": "String", "required": False, "options": [],
     "description": "Please ensure to provide either your LinkedIn profile or Resume, we require at least one of the two."},
    {"label": "Location", "type": "Location", "required": True, "options": []},
    {"label": "Veteran Status", "type": "ValueSelect", "required": True, "options": ["I don't wish to answer", "I am not a protected veteran"],
     "survey": True},
    {"label": "Please indicate your gender", "type": "ValueSelect", "required": True, "options": ["Male", "I don't wish to answer"],
     "survey": True},
    {"label": "GDPR consent to process my personal data", "type": "Boolean", "required": True, "options": []},
    {"label": "GDPR consent to retain my personal data", "type": "Boolean", "required": True, "options": []},
]


def greenhouse_api(monkeypatch, reply):
    """The board API answers every request with this reply, or raises it; the links it was asked for are kept."""
    asked = []

    def urlopen(request, timeout=None):
        asked.append(request.full_url)
        if isinstance(reply, Exception):
            raise reply

        class Response:
            def __enter__(self):
                import io
                return io.BytesIO(json.dumps(reply).encode())

            def __exit__(self, *_):
                return False
        return Response()
    monkeypatch.setattr(F.urllib.request, "urlopen", urlopen)
    return asked


GH_URL = "https://job-boards.greenhouse.io/verkada/jobs/5278670007"


def test_a_greenhouse_reply_becomes_the_same_plain_entries(monkeypatch):
    asked = greenhouse_api(monkeypatch, GREENHOUSE_REPLY)
    assert F.fetch_form(GH_URL) == GREENHOUSE_ENTRIES
    assert asked == ["https://boards-api.greenhouse.io/v1/boards/verkada/jobs/5278670007?questions=true"]


@pytest.mark.parametrize("url, board, job_id", [
    ("https://job-boards.greenhouse.io/verkada/jobs/5278670007", "verkada", "5278670007"),
    ("https://boards.greenhouse.io/waymo/jobs/8310693?gh_jid=8310693", "waymo", "8310693"),
    ("https://job-boards.greenhouse.io/embed/job_app?for=affirm&token=7981723003", "affirm", "7981723003"),
    ("https://boards.greenhouse.io/embed/job_app?token=8314235&for=stripe&gh_src=x", "stripe", "8314235"),
])
def test_every_greenhouse_link_shape_reads_the_same_posting(monkeypatch, url, board, job_id):
    asked = greenhouse_api(monkeypatch, {"questions": []})
    assert F.fetch_form(url) == []
    assert asked == [f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?questions=true"]


def test_a_greenhouse_link_naming_no_posting_is_unread_and_asks_nothing(cfg, monkeypatch):
    asked = greenhouse_api(monkeypatch, {"questions": []})
    url = "https://job-boards.greenhouse.io/airbnb/job_application_requests/d8lfovcc0xn2onpngvqatrl1xiwu81us"
    with pytest.raises(ValueError):
        F.fetch_form(url)
    assert asked == []
    jobs = [{**job("x"), "url": url}]
    assert F.refresh(cfg, jobs, pause=0) == {"read": 0, "failed": 1}
    assert F.board(cfg, jobs)["counts"]["unread"] == 1


def test_a_greenhouse_posting_the_board_no_longer_lists_is_gone(cfg, monkeypatch):
    greenhouse_api(monkeypatch, F.urllib.error.HTTPError(GH_URL, 404, "Not Found", {}, None))
    assert F.fetch_form(GH_URL) is None
    jobs = [{**job("x"), "url": GH_URL}]
    assert F.refresh(cfg, jobs, pause=0) == {"read": 1, "failed": 0}
    assert F.assess(cfg)["x"]["verdict"] == "gone"


@pytest.mark.parametrize("failure", [F.urllib.error.HTTPError(GH_URL, 503, "Busy", {}, None), OSError("network"),
                                     {"status": 500, "error": "boom"}, ["not", "a", "posting"]])
def test_a_greenhouse_api_failure_is_unread_never_ready(cfg, monkeypatch, failure):
    greenhouse_api(monkeypatch, failure)
    with pytest.raises((OSError, ValueError)):
        F.fetch_form(GH_URL)
    jobs = [{**job("x"), "url": GH_URL}]
    assert F.refresh(cfg, jobs, pause=0) == {"read": 0, "failed": 1}
    assert "x" not in F.assess(cfg) and F.board(cfg, jobs)["counts"]["unread"] == 1


GH_PLAIN = {"questions": GREENHOUSE_REPLY["questions"][:2] + GREENHOUSE_REPLY["questions"][3:4] + [
    {"description": None, "label": "Email", "required": True, "fields": [{"name": "email", "type": "input_text", "values": []}]}],
    "location_questions": GREENHOUSE_REPLY["location_questions"]}


def test_a_plain_greenhouse_form_is_ready_and_a_board_that_keeps_her_data_waits_for_her_consent(cfg, monkeypatch):
    jobs = [{**job("x"), "url": GH_URL}]
    greenhouse_api(monkeypatch, GH_PLAIN)
    F.refresh(cfg, jobs, pause=0)
    assert F.assess(cfg)["x"]["verdict"] == "ready"
    F.forms_path(cfg).unlink()
    greenhouse_api(monkeypatch, {**GH_PLAIN, "data_compliance": GREENHOUSE_REPLY["data_compliance"]})
    F.refresh(cfg, jobs, pause=0)
    result = F.assess(cfg)["x"]
    assert result["verdict"] == "ask" and [ask["question"] for ask in result["asks"]] == [
        "GDPR consent to process my personal data", "GDPR consent to retain my personal data"]


def test_an_optional_survey_never_reaches_the_form(monkeypatch):
    greenhouse_api(monkeypatch, {"questions": [], "compliance": GREENHOUSE_REPLY["compliance"],
                                 "demographic_questions": GREENHOUSE_REPLY["demographic_questions"]})
    assert [e["label"] for e in F.fetch_form(GH_URL)] == ["Veteran Status", "Please indicate your gender"]


def test_a_required_survey_choice_with_a_decline_is_answered_and_one_without_still_asks(cfg, monkeypatch):
    jobs = [{**job("x"), "url": GH_URL}]
    survey = {"questions": [{**q, "answer_options": [{"label": "Male"}, {"label": "Female"}]}
                            if q["label"] == "Please indicate your gender" else q
                            for q in GREENHOUSE_REPLY["demographic_questions"]["questions"]]}
    greenhouse_api(monkeypatch, {**GH_PLAIN, "compliance": GREENHOUSE_REPLY["compliance"]})
    F.refresh(cfg, jobs, pause=0)
    assert F.assess(cfg)["x"]["verdict"] == "ready"
    F.forms_path(cfg).unlink()
    greenhouse_api(monkeypatch, {**GH_PLAIN, "demographic_questions": {**GREENHOUSE_REPLY["demographic_questions"], **survey}})
    F.refresh(cfg, jobs, pause=0)
    result = F.assess(cfg)["x"]
    assert result["verdict"] == "ask" and [ask["question"] for ask in result["asks"]] == ["Please indicate your gender"]


def test_an_address_is_asked_never_drafted(cfg, monkeypatch):
    jobs = [{**job("x"), "url": GH_URL}]
    greenhouse_api(monkeypatch, {**GH_PLAIN, "questions": GH_PLAIN["questions"] + [
        {"description": None, "label": "Legal Address", "required": True, "fields": [{"name": "q1", "type": "input_text", "values": []}]}]})
    F.refresh(cfg, jobs, pause=0)
    result = F.assess(cfg)["x"]
    assert result["verdict"] == "ask" and [ask["question"] for ask in result["asks"]] == ["Legal Address"]


def test_a_board_asking_consent_for_its_survey_answers_waits_for_her(cfg, monkeypatch):
    jobs = [{**job("x"), "url": GH_URL}]
    rule = {**GREENHOUSE_REPLY["data_compliance"][0], "requires_consent": False, "requires_retention_consent": False,
            "demographic_data_consent_applies": True}
    greenhouse_api(monkeypatch, {**GH_PLAIN, "data_compliance": [rule]})
    F.refresh(cfg, jobs, pause=0)
    result = F.assess(cfg)["x"]
    assert result["verdict"] == "ask" and [ask["question"] for ask in result["asks"]] == ["Consent to process my demographic survey responses"]
