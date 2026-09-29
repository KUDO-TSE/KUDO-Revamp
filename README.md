# KUDO Revamp Dashboard

Internal program dashboard for the KUDO platform revamp (Interface revamp and Ops backend for MS & LS): documents by category, action items, meeting summaries from transcripts, interface previews, go-to-market readiness and a plain-English status brief.

Flask + Postgres, deployed on Railway. Everyone signs in with their name and the shared team password; every change is saved in Postgres and appears for other users within about 8 seconds.

## Railway setup

1. In the Railway project, add a **Postgres** database (New, Database, PostgreSQL).
2. On the web service, set these variables:

| Variable | Value |
| --- | --- |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` (reference to the Postgres service) |
| `APP_PASSWORD` | the team password people will type to sign in |
| `SECRET_KEY` | a long random string (keeps people signed in across deploys) |
| `ANTHROPIC_API_KEY` | API key used for summaries, comment extraction and the status brief |
| `ANTHROPIC_MODEL` | optional, defaults to `claude-sonnet-5-5` |
| `ANTHROPIC_MODEL_COMPLEX` | optional, model for transcripts and the status brief |

3. Generate a public domain under Settings, Networking. Health check: `/healthz`.

## First run

On startup the app loads `seed/initial-data.json` (documents, meeting, action items, interfaces with previews, go-to-market plan). It only adds records that have never existed, so edits and deletions are never overwritten. Use **Download a backup** any time to export everything, images included.

## Local development

```bash
pip install -r requirements.txt
APP_PASSWORD=test SECRET_KEY=dev python app.py
```
Without `DATABASE_URL` the app uses a local SQLite file (`local.db`).

## Confidentiality

Contains internal KUDO project information. The seed file holds internal content: keep this repository private and share the password only with KUDO team members.
