# Personal Health Record Exporter

**A tool that lets a patient download their own medical record from their
healthcare provider's patient portal, onto their own computer.**

It connects to a hospital's standards-based patient API, downloads the record,
and turns it into a readable document — clinical notes, lab results,
medications, problems, allergies, immunizations, and visit history — plus the
underlying structured data in FHIR® format.

It runs entirely on your own machine. There is no server, no account, and no
company behind it. Your health data is never transmitted anywhere except
between your computer and your own provider.

- **Source code:** this repository — all of it, nothing hidden
- **Status:** personal-use tool, open source under the MIT license
- **Setup and technical documentation:** [SETUP.md](SETUP.md)

---

## Who this is for

People who want a durable, portable copy of their own medical record rather
than clicking through a patient portal one page at a time — and family
caretakers who manage care for someone else and need the whole picture in one
place.

If you are managing another person's care, use your provider's proxy or shared
access process so you're signing in with your own credentials against their
record. Don't use someone else's login.

---

## What data this app requests, and why

The app requests **read-only** access to the following categories. Every one of
them is part of the US Core Data for Interoperability (USCDI) — the standard set
of health data that US patients have a legal right to access electronically.

| Data requested | Why the app asks for it |
|---|---|
| Patient demographics | To label the exported record and confirm whose chart was downloaded |
| Clinical notes (`DocumentReference`, `Binary`) | The narrative your clinician wrote — the most useful part of a record for a patient or caretaker |
| Lab results (`Observation`, `DiagnosticReport`) | Test values with units and abnormal flags, so results can be tracked over time |
| Vital signs (`Observation`) | Blood pressure, weight, and similar measurements |
| Medications (`MedicationRequest`) | Current prescriptions and dosage instructions |
| Health problems (`Condition`) | Active problem list and visit diagnoses |
| Allergies (`AllergyIntolerance`) | Allergies and their reactions |
| Immunizations (`Immunization`) | Vaccination history |
| Visits (`Encounter`, `Procedure`) | When care happened, where, and with whom |
| Care plan and team (`CarePlan`, `CareTeam`, `Goal`) | Treatment plans and who is involved in care |

### What this app does *not* do

- **It cannot change anything in your medical record.** The app only ever issues
  read requests. There is no code path in it that writes, updates, or deletes.
- **It does not send your data anywhere.** Everything is written to a folder on
  your computer. There is no backend, no cloud storage, no analytics, no
  telemetry, and no third-party service of any kind.
- **It does not see your password.** You sign in on your provider's own login
  page in your own browser. The app only receives the authorization code your
  provider hands back afterward.
- **It does not request refresh tokens by default**, so its access expires
  quickly and you re-authorize each time you run it.
- **It does not access anyone else's record.** Access is limited to the patient
  who signed in.

---

## Where your data goes

Into a folder you choose on your own computer:

```
record/
  raw/          the structured FHIR data, one file per category
  notes/        your clinical notes as plain text
  record.html   a readable, printable version of the whole record
  record.md     the same in Markdown
  .auth/        your access token (file permissions 0600)
```

Treat that folder the way you'd treat a printout of your chart. The `.auth`
folder in particular holds a credential that can read your record until it
expires — the included `.gitignore` keeps all of it out of version control.

---

## Revoking access

You can disconnect this app from your record at any time, from your patient
portal — not from this app. In MyChart-based portals this is usually under
**Account Settings → Linked Apps and Devices** (sometimes "Manage Access" or
"Third-Party Apps"), where you can remove the app's authorization.

Deleting the `record/.auth` folder on your computer also destroys the local
copy of the token.

---

## Support

Please open an issue in this repository. This is a personal open-source project
maintained in spare time; there is no support team and no guaranteed response
time.

---

## Security

- Public OAuth 2.0 client using PKCE (RFC 7636) — there is no client secret,
  because a local script has nowhere safe to keep one.
- The `state` parameter is verified on the callback to prevent request forgery.
- The OAuth redirect is served over TLS on `localhost` only; it is never exposed
  to the network.
- Tokens are stored with `0600` permissions and are never logged.

If you find a security problem, please open an issue.

---

## License

MIT — see [LICENSE](LICENSE). Provided as-is, with no warranty. This tool is not
a medical device and does not provide medical advice. It reformats information
your provider already gave you; always confirm anything clinically important
with your care team, and read exported results in the context of your
clinician's interpretation rather than in isolation.

---

*Epic®, MyChart®, and related marks are trademarks of Epic Systems Corporation.
FHIR® is a registered trademark of Health Level Seven International. This
project is an independent, unaffiliated open-source tool. It is not produced,
endorsed, sponsored, or certified by Epic Systems Corporation, HL7, or any
healthcare provider organization. References to these names are solely to
describe the standards and systems this software interoperates with.*
