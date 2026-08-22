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

Code is **in production use**: a full export has been run against Stanford
against the owner's real chart, producing 3,527 resources and 138 clinical
notes. Sandbox and CI both stay green.

- `epic_export.py` — the CLI. Subcommands: `find-endpoint`, `login`, `pull`,
  `render`, `all`.
- `mock_epic.py` — a local fake Epic server (SMART discovery, real PKCE
  verification, CapabilityStatement, paginated bundles, `Binary`-backed note).
- `test_flow.py` — end-to-end test against the mock. **76 checks, all passing.**
  Run with `python test_flow.py`. Keep it green.
- `index.html` / `README.md` / `PUBLISHING.md` — the public documentation page
  required by Epic's registration form, and how to publish it.

### Done

1. ~~Register the app at <https://fhir.epic.com>~~ — registered, **Save & Ready
   for Sandbox**. Client ids live in the Epic portal, not in this repo: they
   aren't secrets for a public PKCE client, but this repo is public and there
   is no reason to hand out an app identity.
2. ~~Publish the docs page~~ — live at
   <https://sw181688-sys.github.io/personal-health-record-exporter/>, terms at
   `/terms`. Both pasted into the Epic form.
3. ~~Validate against Epic's sandbox~~ — full `all` run against test patient
   `fhircamila`, pulling 17 resource types, 4 notes, and 254 vitals.
4. ~~Mark the app Ready for Production~~ — done; auto-distribution worked and
   Stanford accepted the production client id with no manual step.
5. ~~Run against Stanford~~ — 3,527 resources, 138 notes, 20 rendered sections.

### What's left

Nothing required. The one open option is registering the APIs that currently
return 403 (see below) — which needs a **second app registration**, because
Epic locks an app once it is marked ready for production.

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

**A category search cannot reach every Observation.** The tool searches
`laboratory`, `vital-signs` and `social-history`. Radiology findings are in
none of those, so 114 Observations referenced by the owner's `DiagnosticReport`
records were invisible to every search — but read fine by id. Likewise
`Practitioner`, `Location`, `Organization` and `PractitionerRole` are not
patient-searchable at all. `resolve_references()` walks every `reference` in
the export and fetches what is missing, in up to `RESOLVE_ROUNDS` passes
because each wave surfaces the next (a recovered `PractitionerRole` points at
a `Practitioner`). This recovered **637 resources, 2,890 → 3,527**, on the
live chart. Do not remove it in the belief that searching covers the record.

**Epic leaves HTML entities in note text.** Stripping `<tags>` is not enough:
the owner's notes held 9,511 entities across 131 of 138 files — `&nbsp;`,
`&#8226;` bullets, CJK as numeric escapes, and 133 `&lt;`/`&gt;` where a note
saying "temp &lt; 100" had lost its comparison operator. Decode **after**
stripping tags, never before: the other order turns `&lt;div&gt;` into a real
tag the stripper eats.

**Stanford refuses some of what its own data references.** 403 on 158
`Specimen`, 30 `Encounter`, 19 `ServiceRequest` and 4 `Observation` ids that
appear as references in the export. That is the concrete form of Epic's
boilerplate "may not contain the entire record" warning. These are recorded as
`server_notices` and rendered — never silently dropped.

**These APIs are not on the current registration** (403 on every run):
`Coverage`, `FamilyMemberHistory`, `ImagingStudy`, `ImmunizationRecommendation`,
`Appointment`, `QuestionnaireResponse`, `Specimen`. They are listed in `WANTED`
anyway — an unregistered type is logged and skipped, and what the client asks
for does **not** affect auto-distribution eligibility. Only the registration
does. Adding them requires a second app registration, and the USCDI-only rule
still applies to whatever is chosen.

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
- Tokens are stored under `<out>/.auth/`, restricted to the current user, and
  never logged. Use `lock_down()`, not `os.chmod` — chmod on Windows only
  toggles the read-only bit and does **not** restrict who can read the file.
- The access token must never leave the provider's origin. Server-supplied
  URLs (pagination `next`, `Binary` attachments) go through `same_origin()`
  first; the token is a session header and would ride along.
- `.gitignore` excludes all record output and credentials. **Verify
  `git status --short` before any commit** — a public commit containing real
  chart data is very hard to undo.

## Testing

```bash
python test_flow.py
```

On Windows use `python`, not `python3` — the latter is the Microsoft Store
stub and reports "Python was not found" even with Python installed.

Runs the whole flow against `mock_epic.py`: PKCE round trip (including
rejection of a bad verifier), token file permissions, capability filtering,
bundle pagination, `Binary` note retrieval and HTML stripping, token refresh,
the no-refresh-token expiry path, rendering, cross-origin token containment,
and HTML escaping.

No network access is needed — the mock is local.

**Keep the mock at least as messy as production.** Every bug that reached a
live server got there because `mock_epic.py` was tidier than Epic: clean
bundles hid the `OperationOutcome` entries, distinct short ids hid the note
filename collision, a `valueString` blood pressure hid component parsing.
When real Epic surprises you, put the surprise in the mock.

### CI

`.github/workflows/test.yml` runs the suite on **ubuntu + windows × Python
3.11/3.12/3.13**. Both platforms are required, not decorative: `lock_down()`
and the test's permission check each have a Windows branch and a POSIX
branch, and any one machine only ever exercises one of them.

The workflow must never touch a real health system — no client id, no token,
no live pull. Actions logs on a public repo are public.

## Notes on the owner's situation

If this is ever used for a family member's record rather than the owner's own,
the right path is Stanford's **Share Access / proxy** process so the caretaker
authenticates with their own credentials — not sharing a login. Adult proxies
are invited by the account holder; a child under 18 needs a Child Share Access
Request form submitted at a clinic (about a week to process).
