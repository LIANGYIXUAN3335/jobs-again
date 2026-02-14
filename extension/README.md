# JobScan Apply Helper (browser extension)

Pre-fills **Greenhouse / Lever / Ashby** application forms with your profile + the tailored
résumé PDF from your local JobScan, then **stops before submit** — you review and submit
yourself. After you submit, one click marks the role `applied` (closing the dedup loop).

No LLM, no API key, no model — plain local JS talking to your local JobScan API.

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
   attaches the tailored PDF to the résumé field. It logs what it filled and what it left.
4. **Review everything, fix the rest, and submit yourself.**
5. Click **✓ I submitted this** → the role is marked `applied` in JobScan with the résumé
   variant used, so it never resurfaces in your review list.

## What it deliberately does NOT do

- **Never submits.** ToS, anti-bot, and accuracy all need a human — you click submit.
- **Never fills sensitive/voluntary fields**: gender, ethnicity, veteran, disability,
  work-authorization/visa, salary, "how did you hear". Those are yours to answer.
- **Only fills empty fields** — it won't overwrite anything you already typed.
- Custom / multi-step forms may only partly fill. It fills what it can and lists the rest.
  Positioning: "~90% filled, you finish + submit," not auto-apply.

## Scope

Greenhouse is the primary, verified target (fields mapped against the real hosted form,
including the embedded iframe used on company career sites). Lever and Ashby use the same
generic label-based filler and will fill the standard fields; report any that miss and the
mapping can be tightened. It also fires inside the ATS iframe when a company embeds the
form on its own careers page (`all_frames`).

## Troubleshooting

- Panel doesn't appear → the URL didn't match a stored role. Confirm the role is in the DB
  (it must have been fetched by a scan) and that the server is running (extension popup
  shows connection status).
- "server not running" in the popup → `python -m app.web`.
- Résumé not attached → that role has no tailored PDF yet; tailor it first (Phase 2).
