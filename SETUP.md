# Setup guide

*Registration, installation, and usage. For an overview of what this tool is and what data it requests, see [README.md](README.md) or the [project page](https://sw181688-sys.github.io/personal-health-record-exporter/).*

## Pulling your Stanford MyHealth record via Epic's patient FHIR API

A local tool that authenticates to an Epic MyChart system as *you*, downloads
your record through the SMART-on-FHIR patient API, and renders it as a readable
HTML/Markdown document plus raw FHIR JSON you can do anything with.

Nothing is sent anywhere. Tokens and records stay in the output directory on
your machine.

---

## What you can actually get

Verified against Stanford's live discovery document
(`https://sfd.stanfordmed.org/FHIR/api/FHIR/R4/`), which advertises
`launch-standalone` and `client-public` — exactly what a patient-facing PKCE
app needs.

| You wanted | FHIR resource | Notes |
|---|---|---|
| Doctor's notes | `DocumentReference` → `Binary` | The narrative text sits in an attachment; the script follows it and strips the HTML/RTF wrapper. Stanford exposes clinic, hospital, ED, and surgery notes. |
| Test results | `Observation` (category `laboratory`), `DiagnosticReport` | Discrete values with units, reference ranges, and abnormal flags. Radiology *reports* come through; the images themselves do not. |
| Medications | `MedicationRequest` | Includes sig/dosage instructions and status. |
| Problems | `Condition` | Split into problem list vs. encounter diagnoses. |
| Allergies | `AllergyIntolerance` | With reactions. |
| Immunizations | `Immunization` | |
| Visits | `Encounter` | Dates, type, facility. |
| Care plan / team | `CarePlan`, `CareTeam`, `Goal` | Coverage varies by org. |
| Vitals | `Observation` (category `vital-signs`) | |

### What you will *not* get — read this before you build

- **MyChart secure messages are not exposed.** This is the big one relative to
  your original list. The patient FHIR API has no messaging resource that Epic
  surfaces; message threads are a MyChart application feature, not part of the
  certified patient-access data set. Those you save manually from the portal.
- **Radiology images** (the actual DICOM) are not in the FHIR API — download
  them from MyHealth's imaging section.
- **Billing statements** are not part of patient-access FHIR.
- Coverage is governed by the 21st Century Cures Act data set, so it is broad
  and standardized, but each organization decides how much beyond that to expose.
  The script reads the server's `CapabilityStatement` and skips anything the
  org doesn't support rather than failing.

---

## Setup

### 1. Register an app on Epic's developer portal

1. Sign up at <https://fhir.epic.com> (a business or personal email both work;
   Epic asks for an organization name — your own name is fine for a personal app).
2. **Build Apps → Create**.
3. Set **Application Audience = Patients**. This is the setting that makes it a
   patient-facing app: patient-facing apps authenticate as the patient via
   MyChart login rather than through a hospital's IT department.
4. Set **Automatic Client Distribution = USCDI v3**. ← *This is the field that
   determines whether Stanford ever sees your app at all.*

   Epic's automatic client distribution pushes your client ID to every eligible
   organization without their IT staff doing anything. Leaving it on **None**
   means falling back to Epic's manual client distribution process, which
   requires a named contact at Stanford to download your client record — not a
   realistic path for one patient exporting their own chart.

   To qualify, an app must be **patient-facing**, **read-only**, use **only
   USCDI APIs**, and be **marked production-ready**. This tool satisfies the
   first three by construction (it issues nothing but `GET`s).

   Choose **v3, not v1**: auto-sync matches organizations to their USCDI support
   level, so the version you pick decides which organizations you reach.
   Stanford's server reports Epic **November 2025** and declares **US Core
   6.1.0** profiles — US Core 6.1.0 *is* the USCDI v3 release — so v3 is the
   match. (`CMS Patient Access API` is for payer/insurer data, not provider
   records; it isn't what you want here.)

5. **Do not request `offline_access`** unless you're prepared for extra work.
   Auto-distribution has two lanes. Apps without refresh tokens sync to every
   eligible organization with zero per-org action. Apps *with* refresh tokens
   are only *queued* at each organization until the developer uploads a client
   credential for that specific organization. This tool omits `offline_access`
   by default for exactly that reason; you re-authenticate in the browser each
   run instead, which for a periodic snapshot is a fine trade. (`--offline-access`
   turns it back on if you want it.)

6. Add the APIs you want — **and only USCDI ones**, or you forfeit
   auto-distribution eligibility. At minimum:
   `Patient.Read (R4)`, `Patient.Search (R4)`, `Observation.Read (Labs) (R4)`,
   `Observation.Search (R4)`, `DocumentReference.Read/Search (R4)`,
   `MedicationRequest`, `Condition`, `AllergyIntolerance`, `Immunization`,
   `Encounter`, `DiagnosticReport`, plus `Binary` (clinical note bodies are
   fetched through it).

7. **Public Documentation URL:**

   ```
   https://sw181688-sys.github.io/personal-health-record-exporter/
   ```

   Select **https://** in the dropdown, then paste the rest without the scheme.
   This repo ships the page that lives at that URL (`index.html`); see
   [PUBLISHING.md](PUBLISHING.md) for the five commands that put it online.

   Epic requires a public documentation page for distributed apps, and patients
   see it on the consent screen when they authorize. Don't point it at a
   placeholder or a page that doesn't exist yet — publish first, then paste the
   URL here.

8. **Redirect URI:** `https://localhost:8765/callback`
   Epic requires an HTTPS redirect URI before an app can be marked production-ready.
   The script serves that callback locally over TLS with a self-signed cert it
   generates itself — your browser will warn once on the redirect; that page is
   your own machine.
   For sandbox testing, `http://localhost:8765/callback` also works.
9. Do **not** request a client secret. This is a public client using PKCE —
   there is nowhere safe to keep a secret in a local script.
10. Save, and copy the **Non-Production Client ID** (for sandbox) and the
   **Production Client ID** (for Stanford). Production client IDs take roughly
   an hour to propagate after you mark the app ready.

### 2. Install

```bash
pip install requests cryptography
```

### 3. Try it against Epic's sandbox first

Do this before pointing at real data — it confirms your registration and
redirect URI are right without involving your actual record.

```bash
python3 epic_export.py --out ./sandbox all \
  --fhir-base https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4/ \
  --client-id YOUR_NON_PRODUCTION_CLIENT_ID
```

Log in with Epic's test patient: **`fhircamila` / `epicepic1`**.

### 4. Point it at Stanford

```bash
python3 epic_export.py --out ./record all \
  --fhir-base https://sfd.stanfordmed.org/FHIR/api/FHIR/R4/ \
  --client-id YOUR_PRODUCTION_CLIENT_ID
```

Your browser opens Stanford MyHealth. You log in normally — the script never
sees your username or password; it only receives the authorization code that
comes back. Then it pulls everything and writes:

```
record/
  raw/*.json          one file per resource type — full fidelity FHIR
  notes/*.txt         clinical note text, one file per note
  notes_index.json
  manifest.json       what was pulled, when, counts
  record.md           readable summary
  record.html         same, styled, prints cleanly
  .auth/tokens.json   your tokens (mode 600) — treat as a password
```

### Finding a different health system

```bash
python3 epic_export.py find-endpoint "stanford"
```

Searches Epic's published directory of ~1,250 production R4 endpoints.

---

## Commands

| Command | What it does |
|---|---|
| `find-endpoint NAME` | Look up an organization's FHIR base URL |
| `login` | SMART/PKCE browser login, caches tokens |
| `pull` | Download everything for the authenticated patient |
| `render` | Rebuild `record.md` / `record.html` from what's on disk |
| `all` | All three |

`pull` and `render` reuse cached tokens, and `login` requests `offline_access`,
so `all` is the normal way to run it: log in, pull, render in one pass. Because
refresh tokens are off by default (see registration step 5), a scheduled run
would need you present to authorize it — for an unattended monthly snapshot,
add `--offline-access` and do the per-organization credential upload once.

---

## Security notes

- `.auth/tokens.json` grants read access to the full record. It's written 0600;
  keep the output directory off shared drives and out of git.
- Public client + PKCE means there is no secret to leak, and the authorization
  code is useless without the verifier held in memory for that one run.
- The script only ever issues `GET` requests. It cannot modify your record.
- If you're doing this as a **caretaker for someone else**, use Stanford's Share
  Access / proxy so you have your own credentials against their record, rather
  than using their login. Adult proxies are invited by the account holder;
  a child under 18 needs a Child Share Access Request form at a clinic
  (about a week to process).

---

## Testing

`test_flow.py` runs the whole thing end to end against `mock_epic.py`, a local
fake Epic that implements SMART discovery, a real PKCE check, a
CapabilityStatement, paginated searches, and a `Binary`-backed note.

```bash
python3 test_flow.py
```

24 checks covering the PKCE round trip (including rejection of a bad verifier),
token file permissions, pagination, capability filtering, note retrieval and
HTML stripping, token refresh, the no-refresh-token expiry path, and rendering.
