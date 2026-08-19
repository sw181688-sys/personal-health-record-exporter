# CLAUDE.md

Context for Claude Code working in this repo. Read this first — it captures
decisions already made and research already done, so you don't redo either.

## What this is

A personal-use tool that pulls the owner's own medical record out of **Stanford
Health Care MyHealth** (a branded Epic MyChart instance) via the SMART-on-FHIR
patient API, and renders it as readable HTML/Markdown plus raw FHIR JSON.

The owner is a patient/caretaker, not a health-tech vendor. The goal is a
durable local copy of notes, labs, meds, problems, allergies, and visit history
— not a product.

## Current state

Code is **written and tested**; nothing has been run against real data yet.

- `epic_export.py` — the CLI. Subcommands: `find-endpoint`, `login`, `pull`,
  `render`, `all`.
- `mock_epic.py` — a local fake Epic server (SMART discovery, real PKCE
  verification, CapabilityStatement, paginated bundles, `Binary`-backed note).
- `test_flow.py` — end-to-end test against the mock. **32 checks, all passing.**
  Run with `python3 test_flow.py`. Keep it green.
- `index.html` / `README.md` / `PUBLISHING.md` — the public documentation page
  required by Epic's registration form, and how to publish it.

### What's left

1. Register the app at <https://fhir.epic.com> (see `SETUP.md` step 1).
2. Publish the docs page to GitHub Pages (see `PUBLISHING.md`), then paste that
   URL into Epic's **Public Documentation URL** field.
3. Run against Epic's sandbox to validate registration:
   test patient `fhircamila` / `epicepic1`.
4. Then run against Stanford for real.

## Hard-won findings — do not re-derive these

**Stanford's FHIR endpoint** (verified live, not guessed):
```
https://sfd.stanfordmed.org/FHIR/api/FHIR/R4/
  authorize: https://sfd.stanfordmed.org/FHIR/oauth2/authorize
  token:     https://sfd.stanfordmed.org/FHIR/oauth2/token
```
Their CapabilityStatement reports **Epic November 2025**, FHIR **4.0.1**, and
**US Core 6.1.0** profiles. Discovery advertises `launch-standalone` and
`client-public`, so a public PKCE client is the correct design.

**Automatic Client Distribution must be set to `USCDI v3`** on the Epic
registration form. This is the field that decides whether Stanford ever receives
the client ID. `None` means falling back to Epic's manual distribution process,
which requires a named contact in Stanford IT — not viable for an individual.
v3 rather than v1 because auto-sync matches organizations by their USCDI support
level, and US Core 6.1.0 *is* the USCDI v3 release. (`CMS Patient Access API` is
the payer/insurance lane — wrong for provider records.)

Auto-distribution eligibility requires the app be patient-facing, **read-only**,
use **only USCDI APIs**, and be marked production-ready. **Keep it read-only** —
the tool issues nothing but `GET`s, and adding any write would forfeit
eligibility as well as being outside the project's purpose.

**`offline_access` is deliberately omitted from the default scopes.**
Auto-distribution has two lanes: apps without refresh tokens sync to every
eligible org automatically; apps *with* refresh tokens are only queued at each
org until the developer uploads a per-organization client credential. For a
single hospital that's a few clicks, but the default stays off. The
`--offline-access` flag turns it on if unattended scheduled runs are ever
wanted. If you change this default, update `SETUP.md` step 5 to match.

**MyChart secure messages are NOT available via FHIR.** Epic does not expose
patient messaging through the patient API — it's an application feature, not
part of the Cures Act certified data set. This was an explicit ask from the
owner, so don't quietly try to add it; it can't be done this way. Same for
radiology DICOM images and billing statements.

**Epic puts an `OperationOutcome` in every search bundle.** It rides along as
an entry with `search.mode: "outcome"`, so naive "collect every entry" code
counts it as a clinical record — every resource count comes out one too high and
it renders as a meaningless `?` row. `fetch_all` separates them out. Do not just
drop them, though: two of the four Epic returns say *"Results of this sub-type
will not be returned"*, which is the server telling you the export is
**incomplete**. They're deduped into `manifest.json` as `server_notices` and
rendered as an "About this export" section. The mock now emits them too — it
didn't, which is exactly why this reached a live server undetected.

**`Binary` must be in the app's registered API list.** Clinical note narrative
lives in `DocumentReference` attachments that resolve through `Binary`. Without
it, notes come back as empty references.

## Conventions

- Python 3.11+, stdlib-first. Only `requests` and `cryptography` as deps.
- No new dependencies without a good reason; this needs to stay easy to audit,
  because the whole security argument is "read the code, it's short."
- The tool must never write to the medical record and must never transmit data
  anywhere except between the user's machine and their provider. No telemetry,
  no analytics, no third-party calls.
- Tokens: `0600`, stored under `<out>/.auth/`, never logged.
- `.gitignore` excludes all record output and credentials. **Verify
  `git status --short` before any commit** — a public commit containing real
  chart data is very hard to undo.

## Testing

```bash
python3 test_flow.py
```

Runs the whole flow against `mock_epic.py`: PKCE round trip (including
rejection of a bad verifier), token file permissions, capability filtering,
bundle pagination, `Binary` note retrieval and HTML stripping, token refresh,
the no-refresh-token expiry path, and rendering.

No network access is needed — the mock is local. Real Epic endpoints were
reachable for research but the sandbox was never exercised end to end, so
**step 3 above is genuinely unverified**; expect to debug the first real run.

## Notes on the owner's situation

If this is ever used for a family member's record rather than the owner's own,
the right path is Stanford's **Share Access / proxy** process so the caretaker
authenticates with their own credentials — not sharing a login. Adult proxies
are invited by the account holder; a child under 18 needs a Child Share Access
Request form submitted at a clinic (about a week to process).
