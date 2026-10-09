# JobScan Apply Helper (browser extension)

Pre-fills **Greenhouse / Lever / Ashby** application forms with low-risk profile fields +
the tailored résumé PDF from your local JobScan, then **stops before submit** — you review
and submit yourself. After you submit, one click marks the role `applied` (closing the
dedup loop).

Optional AI drafting can fill editable open-ended prose questions when enabled in the local
server. Sensitive/legal/user-choice fields stay manual.

## Install (Chrome, unpacked)

1. Start the local server: `python -m app.web` (the extension talks to `127.0.0.1:8787`).
2. Open `chrome://extensions`, turn on **Developer mode** (top-right).
3. **Load unpacked** → select this `extension/` folder.
4. Pin the puzzle-piece icon if you like. Click it to confirm it says "connected".

## Use

1. Find a role in the JobScan UI, apply from its link (or any Greenhouse/Lever/Ashby form).
2. On the application page a floating **JobScan** panel appears (bottom-right) if the page
   matches a role in your DB. It shows the role and whether a tailored PDF is ready.
3. Click **Fill this application** — it fills name/email/phone/LinkedIn/GitHub/location and
   attaches the tailored PDF to the résumé field. It logs what it filled and what needs your
   confirmation.
4. **Review everything, fix the rest, and submit yourself.**
5. Click **✓ I submitted this** → the role is marked `applied` in JobScan with the résumé
   variant used, so it never resurfaces in your review list.

## What it deliberately does NOT do

- **Never submits.** ToS, anti-bot, and accuracy all need a human — you click submit.
- **Never guesses a fact that isn't recorded.** Everything it fills comes from `config.yaml`
  (profile fields, the `eeo` self-ID answers, standing legal/eligibility answers). A question
  with no recorded answer is left for you, never invented.
- **Leaves blank on purpose**: salary/compensation and referral/"how did you hear" —
  owner answers those per-company.

(2026-09-09 owner rescope, restored 2026-09-15: EEO/self-ID — gender, ethnicity, veteran,
disability — auto-fill from your configured `eeo` answers; work-auth and other legal
eligibility questions fill from recorded facts; consent/acknowledgement boxes are ticked.
You review the whole form before submitting. Do not re-add a category-level block here —
see memory `jobscan-autofill-policy`.)
  Those are yours to answer.
- **Only fills empty fields** — it won't overwrite anything you already typed.
- Custom / multi-step forms may only partly fill. It fills what it can and lists the rest.
  Positioning: "~90% filled, you finish + submit," not auto-apply.

## Scope

Greenhouse is the primary, verified target (fields mapped against the real hosted form,
including the embedded iframe used on company career sites). Lever and Ashby use the same
generic label-based filler and will fill the standard fields; report any that miss and the
mapping can be tightened. It also fires inside the ATS iframe when a company embeds the
form on its own careers page (`all_frames`).

## Applying in several tabs at once

Supported — the content script is injected per tab and runs independently, and the service
worker is a stateless API proxy. Two things make it hold up:

- **The pin is claimed per tab.** The server's `/api/pin` is a single global slot, so the
  next ↗ click used to steal an older tab's role and silently attach the wrong tailored
  résumé. Now the frame that actually renders a panel off the pin copies it into that tab's
  `sessionStorage` (keyed by origin) and `DELETE`s the server slot, so the next ↗ owns the
  slot outright. `GET /api/pinned` stays idempotent on purpose — with `all_frames` on, a
  probing iframe must not consume a pin it never uses.
  Remaining race: firing two ↗ clicks back-to-back *before* the first tab's script boots.
- **Backgrounded tabs re-arm themselves.** Chrome throttles a hidden tab's timers to ~1/min,
  so the boot poll's window is wall-clock rather than a tick count, and `visibilitychange`
  starts a fresh window whenever a tab becomes visible with no panel up.

## Troubleshooting

- Panel doesn't appear → the URL didn't match a stored role. Confirm the role is in the DB
  (it must have been fetched by a scan) and that the server is running (extension popup
  shows connection status).
- "server not running" in the popup → `python -m app.web`.
- Résumé not attached → that role has no tailored PDF yet; tailor it first (Phase 2).
