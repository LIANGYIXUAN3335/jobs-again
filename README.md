# JobScan

JobScan normalizes job boards into a local review queue. A Python scanner fetches ATS listings, applies configurable filters, and ranks the roles. A FastAPI dashboard tracks decisions, follow-ups, and résumé drafts. The browser extension can prepare application fields for review.

The demo uses fictional postings and a résumé template with placeholder details.

## Run the offline demo

Requires Python 3.11 or newer.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export JOBSCAN_CONFIG=config.demo.yaml
python tools/demo_scan.py
python -m app.web
```

Open [the local dashboard](http://127.0.0.1:8787/?status=all). Search for **Demo Systems**, open **Backend Platform Engineer**, and set it to **Skip**. Reload the dashboard to see the decision persisted in the local user database. The demo replaces the ATS response with an in-process fixture and uses the normal filtering, scoring, and persistence pipeline.

## Architecture

| Layer | Implementation |
| --- | --- |
| ATS ingestion | Adapters normalize heterogeneous responses into a common posting model. The scanner namespaces source IDs and fetches expensive descriptions after filtering. |
| Ranking | Location and scope filters feed a configurable score. A description hash lets unchanged postings reuse prior scores. |
| Persistence | The scanner writes the jobs database. The review UI stores application decisions in a separate database, so a rescan preserves user state. |
| Review | FastAPI renders the dashboard and serves a loopback API. The extension checks role and form state during assisted filling. |

## Use your own configuration

Copy `config.example.yaml` to `config.yaml` and the example résumé to `resumes/resume_base.yaml`. Configure the boards and ranking preferences you want to use, then run `python -m app.scan` for live ingestion. Leave the review UI bound to localhost. Adding a mailbox or tailoring provider is optional.

Run the core checks with `JOBSCAN_CONFIG=config.example.yaml python -m pytest tests/test_sources.py tests/test_scoring.py tests/test_filters.py tests/test_store.py`.
