# Safe Multi-Worker Apply Pipeline

## Owner Operating Memory

- 2026-08-18: Stop applying for the rest of today. Do not submit the currently prepared
  Runpod application today.
- For future standard job applications, the owner has explicitly authorized the agent to
  click final Submit after filling and reviewing the form, when all answers come from the
  approved profile/resume/application policy and no facts are invented.
- This submit authorization covers ordinary application data transmission: contact info,
  resume upload, LinkedIn/GitHub/profile fields, standard truthful attestations, privacy /
  data-processing consent, AI hiring policy acknowledgement, background-check willingness,
  right-to-work documentation acknowledgement, salary policy, work authorization, sponsorship,
  and voluntary EEO choices according to the approved policy.
- Still stop for captchas, login or email/SMS verification that cannot be completed from the
  connected account, assessments, take-homes, coding/API tasks, unknown legal facts, non-compete
  or post-employment restrictions, arbitration or unusual legal commitments, exact relocation /
  onsite commitments outside policy, or any task the candidate must actually perform.
- After a successful submission, mark the exact JobScan position `applied` with the submitted
  resume variant. If the posting is delisted or no longer online, mark it `applied` with a
  delisted/applied-equivalent note per owner instruction.

This project should optimize the boring parts of applying while staying truthful and within
the owner's approved application policy. Workers may prepare tabs, attach the correct tailored
resume, draft/edit prose, review the completed form, and submit standard applications under
the Owner Operating Memory above.

## Current Flow

1. `app.scan` imports jobs from ATS APIs and stores facts in `data/jobscan.db`.
2. The review UI filters to `status:new`, applies budget rules, and shows roles worth reviewing.
3. Resume tailoring writes prompts under `output/prompts/`; Claude Code can turn those into
   YAML under `output/resumes/`, then `app.ingest` renders deterministic PDFs.
4. `/apply` lists roles with `tailor_status=ready` and an application URL.
5. The Chrome extension matches the application page to the JobScan role, fills approved
   profile/application fields, attaches the tailored PDF, and optionally drafts open-ended prose.
6. The worker reviews the form. If no stop condition appears, it submits the application and
   records the role `applied`; otherwise it saves progress and logs an action-required note.

## Credential Model

- Scanning, filtering, scoring: no LLM key.
- Resume tailoring: Claude Code subscription via `tools/auto_tailor.sh`; the script unsets
  `ANTHROPIC_API_KEY` so it does not use pay-per-token API credentials.
- Application prose drafting: optional Anthropic API key in gitignored `secrets.yaml` or
  `ANTHROPIC_API_KEY`; used by `app/ai.py` only for editable open-ended answers and cover notes.
- Email verification code helper: optional Gmail app password in gitignored `secrets.yaml`.
- There is an `openai_api_key` placeholder in `secrets.example.yaml`, but the inspected apply
  flow does not currently call OpenAI.

## Worker Design

Use browser-tab workers, not server-side submission workers.

Each worker owns one application tab and one role:

1. Claim a role from `/apply` or from a future queue endpoint.
2. Open that role's `apply_url`.
3. Let the extension bind the tab to the role via URL lookup or a per-tab pin.
4. Fill approved fields:
   - legal/preferred name as configured
   - email, phone, location, country
   - LinkedIn, GitHub, website
   - education and employment history when the form asks for structured history
   - tailored resume PDF for that exact role
   - standard work authorization / sponsorship answers from policy
   - salary from the JD range low end, or the configured fallback when no range exists
   - voluntary EEO choices from policy, preferring decline / prefer-not-to-say where available
   - standard truthful attestations and privacy / data-processing / AI hiring acknowledgements
   - editable open-ended prose, only when supported by the resume/profile and the role context
5. Stop and show warnings for blocked fields or tasks listed in Owner Operating Memory.
6. When no stop condition appears, click final submit and record `applied` via the existing
   button/API.

Concurrency should stay modest: 3-5 visible tabs is enough. More than that makes human review
hard and increases ATS anti-automation risk.

## Manual Confirmation Fields

Workers must leave these blank or untouched and log a warning unless the answer is explicitly
covered by the approved profile/application policy:

- unknown legal facts
- criminal history, felony, conviction, non-compete, post-employment restrictions
- export-control, U.S.-person, citizenship, permanent-resident, clearance, or government-role
  attestations that conflict with immutable facts
- exact relocation / onsite commitments outside approved location policy
- referral/source when no source or referrer is known
- electronic signature / typed legal name if the certification text contains a new or unusual
  legal commitment
- captcha, assessment, take-home, coding/API task, or live task

## Inputs Needed From The Candidate

- Confirm `config.yaml` profile fields are current: legal name, preferred name, email, phone,
  location, LinkedIn, GitHub, website, education.
- Confirm the current work-authorization wording to use manually on forms.
- Confirm whether sponsorship is needed now or in the future.
- Decide salary expectations or per-role salary strategy.
- Decide voluntary self-ID answers, or decline choices, per form.
- Provide referral names/emails only when a specific referral exists.
- Provide Anthropic API key only if open-question drafting should run from API tokens.
- Provide Gmail app password only if email-code autofill should be used.
- Keep Chrome extension installed and JobScan server running at `127.0.0.1:8787`.

## Next Implementation Step

The current `/apply` page already supports opening many application pages. A safer next upgrade
would be a "worker queue" page that opens only N tabs at a time, tracks each tab as
`opened -> filled -> needs_user -> submitted/skip`, and refuses to advance a role past
`needs_user` without a human click.
