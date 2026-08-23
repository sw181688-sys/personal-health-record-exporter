#!/usr/bin/env python3
"""A tiny fake Epic FHIR server used to exercise epic_export.py end to end.

It implements just enough to be honest about the real thing: SMART discovery,
an authorize endpoint that redirects back with a code, a token endpoint that
actually verifies the PKCE challenge, a CapabilityStatement, paginated
searches, and a DocumentReference whose body lives behind a Binary fetch.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 9099
BASE = f"http://127.0.0.1:{PORT}/api/FHIR/R4"
PATIENT_ID = "eTestPatient123"

# A redirect is the one way the server can walk the access token off-origin
# without the client ever choosing to go there: the same-origin check has
# already passed by the time the Location header arrives. The mock served no
# redirect from any API path, so that whole branch went untested — the same
# shape of gap that let the OperationOutcome entries and the note filename
# collision reach a live server. A different port is a different origin.
REDIRECT_TARGET_PORT = PORT + 1
OFF_ORIGIN_REDIRECT = f"http://127.0.0.1:{REDIRECT_TARGET_PORT}/steal"

STATE: dict = {}  # holds the pending auth request

#         date          name              value  unit       flag  ref range
LABS = [
    ("2026-07-14", "Hemoglobin A1c", 7.4, "%", "H", (4.0, 5.6)),
    ("2026-07-14", "Creatinine", 0.9, "mg/dL", "", (0.6, 1.1)),
    ("2026-01-09", "Hemoglobin A1c", 8.1, "%", "H", (4.0, 5.6)),
    ("2025-06-02", "Potassium", 3.2, "mmol/L", "L", (3.5, 5.1)),
    # Ordered but never resulted. Dropping this row entirely made it look as
    # though the test was never done.
    ("2025-06-02", "Vitamin D, 25-Hydroxy", None, "", "", None),
]


def obs(i, date, name, val, unit, flag, rng=None):
    r = {
        "resourceType": "Observation", "id": f"obs{i}",
        "status": "final",
        "category": [{"coding": [{"code": "laboratory"}]}],
        # No `text` here on purpose: Epic frequently sends only coding.display,
        # and reading `.text` alone renders a bare "?".
        "code": {"coding": [{"system": "http://loinc.org", "display": name}]},
        # 02:00 UTC is the previous calendar day across the Americas.
        "effectiveDateTime": f"{date}T02:00:00Z",
    }
    if val is None:
        r["dataAbsentReason"] = {"coding": [{"display": "Not performed"}]}
    else:
        r["valueQuantity"] = {"value": val, "unit": unit}
    if rng:
        r["referenceRange"] = [{"low": {"value": rng[0], "unit": unit},
                                "high": {"value": rng[1], "unit": unit}}]
    if flag:
        r["interpretation"] = [{"coding": [{"code": flag}], "text": flag}]
    return r


DATA = {
    ("Patient", ""): [{
        "resourceType": "Patient", "id": PATIENT_ID,
        "name": [{"given": ["Camila"], "family": "Lopez"}],
        "birthDate": "1987-09-12", "gender": "female",
    }],
    ("Condition", "problem-list-item"): [
        {"resourceType": "Condition", "id": "c1",
         "code": {"text": "Type 2 diabetes mellitus"}, "onsetDateTime": "2019-03-04"},
        {"resourceType": "Condition", "id": "c2",
         "code": {"text": "Essential hypertension"}, "onsetDateTime": "2021-11-20"},
        # Real diagnoses carry characters a Windows cp1252 console cannot
        # encode. Keep one here so the export can't regress to crashing on them.
        {"resourceType": "Condition", "id": "c3",
         "code": {"text": "β-thalassemia minor"}, "onsetDateTime": "2018-02-11"},
    ],
    ("AllergyIntolerance", ""): [
        {"resourceType": "AllergyIntolerance", "id": "a1",
         "code": {"text": "Penicillin"},
         "reaction": [{"text": "hives"}]},
    ],
    ("MedicationRequest", ""): [
        {"resourceType": "MedicationRequest", "id": "m1", "status": "active",
         "medicationCodeableConcept": {"text": "Metformin 500 mg tablet"},
         "dosageInstruction": [{"text": "Take 1 tablet twice daily with meals"}]},
        {"resourceType": "MedicationRequest", "id": "m2", "status": "active",
         "medicationCodeableConcept": {"text": "Lisinopril 10 mg tablet"},
         "dosageInstruction": [{"text": "Take 1 tablet daily"}]},
    ],
    ("Observation", "laboratory"): [obs(i, *l) for i, l in enumerate(LABS)],
    # Real Epic sends blood pressure as components with no top-level value —
    # 92 of the 254 sandbox vitals look like this.
    ("Observation", "vital-signs"): [{
        "resourceType": "Observation", "id": "v1", "status": "final",
        "category": [{"coding": [{"code": "vital-signs"}]}],
        "code": {"text": "Blood Pressure"}, "effectiveDateTime": "2026-07-14T10:00:00Z",
        "component": [
            {"code": {"text": "Systolic blood pressure"},
             "valueQuantity": {"value": 128, "unit": "mm[Hg]"}},
            {"code": {"text": "Diastolic blood pressure"},
             "valueQuantity": {"value": 78, "unit": "mm[Hg]"}},
        ],
    }, {
        "resourceType": "Observation", "id": "v2", "status": "final",
        "category": [{"coding": [{"code": "vital-signs"}]}],
        "code": {"text": "Weight"}, "effectiveDateTime": "2026-07-14T10:00:00Z",
        "valueQuantity": {"value": 71.2, "unit": "kg"},
    }],
    ("Immunization", ""): [
        {"resourceType": "Immunization", "id": "i1",
         "vaccineCode": {"text": "Influenza, seasonal"},
         "occurrenceDateTime": "2025-10-08"},
    ],
    ("Encounter", ""): [
        {"resourceType": "Encounter", "id": "e1",
         "type": [{"text": "Endocrinology follow-up"}],
         "period": {"start": "2026-07-14T09:30:00Z"},
         "location": [{"location": {"reference": f"{BASE}/Location/loc-1"}}],
         "serviceProvider": {"display": "Stanford Endocrinology"}},
    ],
    ("DocumentReference", "clinical-note"): [
        {"resourceType": "DocumentReference", "id": "dr1",
         "type": {"text": "Progress Note"}, "date": "2026-07-14T11:02:00Z",
         "content": [{"attachment": {"contentType": "text/html",
                                     "url": f"{BASE}/Binary/bin1"}}]},
        # Epic's ids share long prefixes and differ only near the end. Two of
        # them here so a filename built from a truncated id collides and one
        # note is lost — which is exactly what happened against the sandbox.
        {"resourceType": "DocumentReference",
         "id": "ewtIzA62-DkL21MnJY6OyRj8UF8vh7UuasqvGB1pc5CY3",
         "type": {"text": "Imaging Note"}, "date": "2026-07-02T09:00:00Z",
         "content": [{"attachment": {"contentType": "text/html",
                                     "url": f"{BASE}/Binary/bin2"}}]},
        {"resourceType": "DocumentReference",
         "id": "ewtIzA62-DkL21MnJY6OyRhIhI5asLLt7L5RQtNUW6qY3",
         "type": {"text": "Imaging Note"}, "date": "2026-07-02T09:00:00Z",
         "content": [{"attachment": {"contentType": "text/html",
                                     "url": f"{BASE}/Binary/bin3"}}]},
        # Attachment URL is on this origin, so the client's up-front check
        # passes; the server then redirects it off-origin. Skipped, not fetched.
        {"resourceType": "DocumentReference", "id": "dr-offsite",
         "type": {"text": "Offsite Note"}, "date": "2026-06-20T09:00:00Z",
         "content": [{"attachment": {"contentType": "text/html",
                                     "url": f"{BASE}/Binary/offsite-note"}}]},
    ],
    ("DiagnosticReport", "LAB"): [
        {"resourceType": "DiagnosticReport", "id": "d1", "status": "final",
         "code": {"text": "Comprehensive metabolic panel"},
         "effectiveDateTime": "2026-07-14T10:00:00Z"},
    ],
    # This report points at an Observation that no category search returns and
    # at a Practitioner that is not patient-searchable at all. Both read fine
    # by id. Real Epic does exactly this: 118 observations and 173
    # practitioners were reachable only this way in one live chart.
    ("DiagnosticReport", "RAD"): [
        {"resourceType": "DiagnosticReport", "id": "d2", "status": "final",
         "code": {"text": "CT Chest w/o contrast"},
         "effectiveDateTime": "2026-07-02T18:00:00Z",
         "result": [{"reference": f"{BASE}/Observation/imaging-finding-1"}],
         "performer": [{"reference": f"{BASE}/Practitioner/prac-1"}]},
    ],
    # Everything below was pulled and saved by the exporter but never rendered.
    # The mock never served any of it, which is why nothing caught that.
    ("Condition", "encounter-diagnosis"): [
        {"resourceType": "Condition", "id": "ed1", "recordedDate": "2026-07-14",
         "code": {"coding": [{"system": "http://snomed.info/sct",
                              "display": "Stomach ache (finding)"}]}},
    ],
    ("Procedure", ""): [
        {"resourceType": "Procedure", "id": "pr1", "status": "completed",
         "code": {"coding": [{"display": "HC TRANSTHORACIC ECHO"}]},
         "performedDateTime": "2026-07-02T18:30:00Z"},
    ],
    ("Observation", "social-history"): [
        {"resourceType": "Observation", "id": "sh1", "status": "final",
         "category": [{"coding": [{"code": "social-history"}]}],
         "code": {"coding": [{"display": "Tobacco smoking status"}]},
         "effectivePeriod": {"end": "2026-07-14"},
         "valueCodeableConcept": {"text": "Never smoker"}},
    ],
    ("CareTeam", ""): [
        {"resourceType": "CareTeam", "id": "ct1", "status": "active",
         "participant": [{"member": {"display": "Dr. A. Chen"},
                          "role": [{"text": "Primary Care Provider"}]}]},
    ],
    ("CarePlan", "assess-plan"): [
        {"resourceType": "CarePlan", "id": "cp1", "status": "active",
         "category": [{"text": "Assessment and Plan of Treatment"}],
         "addresses": [{"display": "Type 2 diabetes mellitus"}]},
    ],
    ("Goal", ""): [
        {"resourceType": "Goal", "id": "g1", "lifecycleStatus": "active",
         "startDate": "2026-03-01",
         "description": {"text": "Walk 30 minutes daily"}},
    ],
    # Types beyond the original set. Each one labels and dates itself with a
    # different field, which is the whole reason the renderer is generic.
    ("Device", ""): [
        {"resourceType": "Device", "id": "dev1", "status": "active",
         "deviceName": [{"name": "Medtronic Azure XT DR MRI"}],
         "manufacturer": "Medtronic", "model": "W3DR01",
         "udiCarrier": [{"deviceIdentifier": "00643169007222"}]},
    ],
    ("Coverage", ""): [
        {"resourceType": "Coverage", "id": "cov1", "status": "active",
         "type": {"text": "PPO"}, "period": {"start": "2026-01-01"}},
    ],
    ("FamilyMemberHistory", ""): [
        {"resourceType": "FamilyMemberHistory", "id": "fh1", "status": "completed",
         "relationship": {"text": "Mother"}, "date": "2024-05-02",
         "condition": [{"code": {"text": "Type 2 diabetes mellitus"}}]},
    ],
    ("ImagingStudy", ""): [
        {"resourceType": "ImagingStudy", "id": "img1", "status": "available",
         "started": "2026-06-11T17:15:00Z", "description": "CT Abdomen w/ contrast"},
    ],
    ("MedicationDispense", ""): [
        {"resourceType": "MedicationDispense", "id": "md1", "status": "completed",
         "medicationCodeableConcept": {"text": "Metformin 500 mg tablet"},
         "whenHandedOver": "2026-07-15T16:40:00Z"},
    ],
    ("Appointment", ""): [
        {"resourceType": "Appointment", "id": "ap1", "status": "booked",
         "serviceType": [{"text": "Endocrinology follow-up"}],
         "start": "2026-10-02T16:00:00Z"},
    ],
}

# Epic attaches an OperationOutcome to every search bundle, flagged
# search.mode="outcome". These are the real texts a live Epic returns. They are
# diagnostics about the search, not chart content — and the first one means data
# was actually withheld, so the client must not render them as records but must
# not silently drop them either.
SEARCH_OUTCOME = {
    "search": {"mode": "outcome"},
    "resource": {
        "resourceType": "OperationOutcome",
        "issue": [
            {"severity": "warning", "code": "suppressed",
             "details": {"text": "The authenticated client's search request "
                                 "applies to a sub-resource that the client is "
                                 "not authorized for. Results of this sub-type "
                                 "will not be returned."}},
            {"severity": "warning", "code": "processing",
             "details": {"text": "This response includes information available "
                                 "to the authorized user at the time of the "
                                 "request. It may not contain the entire record "
                                 "available in the system."}},
        ],
    },
}

# Entities are left in on purpose. Real Epic notes are full of them, and the
# escaped "&lt;" carries clinical meaning that is lost if it is not decoded.
NOTE_HTML = """<div><p><b>Subjective:</b>&nbsp;Patient reports improved energy since the
last visit. Adherent to metformin. Denies hypoglycemic episodes. Afebrile,
temp &lt; 100 throughout.</p>
<p><b>Assessment:</b> Type 2 diabetes, improving. A1c down from 8.1 to 7.4.</p>
<p><b>Plan:</b>&#8226; Continue metformin 500 mg BID.&#8226; Recheck A1c in three
months.&#8226; Referral to nutrition &amp; diabetes education placed.</p></div>"""

SUPPORTED = sorted({k[0] for k in DATA} | {"Procedure", "CareTeam", "Goal",
                                           "CarePlan", "Practitioner"})

# Readable by id only — never returned by any patient search. Encounter is
# here to be refused, the way Stanford refuses referenced encounters with 403.
READ_BY_ID = {
    ("Observation", "imaging-finding-1"): {
        "resourceType": "Observation", "id": "imaging-finding-1", "status": "final",
        "category": [{"coding": [{"code": "imaging"}]}],
        "code": {"coding": [{"display": "Pulmonary nodule size"}]},
        "effectiveDateTime": "2026-07-02T18:05:00Z",
        "valueQuantity": {"value": 4, "unit": "mm"},
    },
    ("Practitioner", "prac-1"): {
        "resourceType": "Practitioner", "id": "prac-1",
        "name": [{"given": ["Alex"], "family": "Reyes", "prefix": ["Dr."]}],
    },
}
# Encounter and Location stand in for what Stanford actually refuses: the
# record references them, they are not searchable, and reading them by id is
# denied. The export has to say so rather than quietly omitting them.
FORBIDDEN_BY_ID = {"Encounter", "Location"}


class H(BaseHTTPRequestHandler):
    def _send(self, obj, code=200, ctype="application/fhir+json"):
        body = (obj if isinstance(obj, bytes)
                else json.dumps(obj).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        path = u.path

        if path.endswith("/.well-known/smart-configuration"):
            # A base containing "redirect-token" advertises a token endpoint
            # that answers 307. The token URL is whatever discovery says it
            # is, so this is the shape a compromised or misconfigured
            # discovery document would take.
            tok = "token-307" if "redirect-token" in path else "token"
            return self._send({
                "authorization_endpoint": f"http://127.0.0.1:{PORT}/oauth2/authorize",
                "token_endpoint": f"http://127.0.0.1:{PORT}/oauth2/{tok}",
                "code_challenge_methods_supported": ["S256"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "capabilities": ["launch-standalone", "client-public",
                                 "permission-patient", "context-standalone-patient"],
            }, ctype="application/json")

        if path == "/oauth2/authorize":
            # A real Epic shows a MyChart login here. We just bounce straight back.
            STATE.update(q)
            loc = (q["redirect_uri"] + "?"
                   + urllib.parse.urlencode({"code": "AUTHCODE-XYZ",
                                             "state": q.get("state", "")}))
            self.send_response(302)
            self.send_header("Location", loc)
            self.end_headers()
            return

        if path.endswith("/metadata"):
            return self._send({
                "resourceType": "CapabilityStatement",
                "rest": [{"mode": "server",
                          "resource": [{"type": t} for t in SUPPORTED]}],
            })

        if not self._authed():
            return self._send({"resourceType": "OperationOutcome"}, 401)

        # An attachment the server answers with a redirect to somewhere else
        # entirely. The client must refuse without connecting: every request
        # here carries a Bearer token that reads the whole chart.
        if path.endswith("/Binary/offsite-note"):
            self.send_response(302)
            self.send_header("Location", OFF_ORIGIN_REDIRECT)
            self.end_headers()
            return

        # A redirect that stays on this origin is ordinary and must keep
        # working — refusing every redirect outright would be a regression.
        if path.endswith("/Binary/moved-note"):
            self.send_response(302)
            self.send_header("Location", f"{BASE}/Binary/bin1")
            self.end_headers()
            return

        m = re.match(r".*/api/FHIR/R4/Binary/(\w+)$", path)
        if m:
            # Distinct bodies per Binary, so a collision loses visible content.
            bodies = {"bin2": "<p>Imaging note A: chest x-ray, no acute "
                              "cardiopulmonary process identified.</p>",
                      "bin3": "<p>Imaging note B: echocardiogram, normal "
                              "ejection fraction estimated at 60 percent.</p>"}
            html = bodies.get(m.group(1), NOTE_HTML)
            return self._send({"resourceType": "Binary", "contentType": "text/html",
                               "data": base64.b64encode(html.encode()).decode()})

        # Direct read by id: GET [base]/Type/id
        m = re.match(r".*/api/FHIR/R4/(\w+)/([\w.-]+)$", path)
        if m:
            rt, rid = m.group(1), m.group(2)
            if rt in FORBIDDEN_BY_ID:
                return self._send({"resourceType": "OperationOutcome", "issue": [
                    {"severity": "error", "code": "forbidden",
                     "details": {"text": "Not authorized for this resource."}}]}, 403)
            hit = READ_BY_ID.get((rt, rid))
            return self._send(hit) if hit else self._send(
                {"resourceType": "OperationOutcome"}, 404)

        m = re.match(r".*/api/FHIR/R4/(\w+)$", path)
        if not m:
            return self._send({"resourceType": "OperationOutcome"}, 404)
        rtype = m.group(1)
        cat = q.get("category", "")
        items = DATA.get((rtype, cat), [])

        # Exercise the client's pagination handling: serve labs one page at a time.
        page = int(q.get("_page", "0"))
        if rtype == "Observation" and cat == "laboratory":
            chunk, items = items[page * 2:page * 2 + 2], items
            links = []
            if (page + 1) * 2 < len(items):
                nxt = dict(q); nxt["_page"] = str(page + 1)
                links = [{"relation": "next",
                          "url": f"{BASE}/{rtype}?" + urllib.parse.urlencode(nxt)}]
            return self._send({"resourceType": "Bundle", "type": "searchset",
                               "entry": [{"resource": r} for r in chunk]
                                        + [SEARCH_OUTCOME],
                               "link": links})

        return self._send({"resourceType": "Bundle", "type": "searchset",
                           "entry": [{"resource": r} for r in items]
                                    + [SEARCH_OUTCOME]})

    def do_POST(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        form = {k: v[0] for k, v in
                urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
        # A 307 preserves both method and body across hosts, and requests
        # strips only the Authorization *header* — never a form body. Left
        # to follow this, the client would re-POST the authorization code
        # and the PKCE verifier straight to the redirect target.
        if u.path == "/oauth2/token-307":
            self.send_response(307)
            self.send_header("Location", OFF_ORIGIN_REDIRECT)
            self.end_headers()
            return

        if u.path != "/oauth2/token":
            return self._send({}, 404)

        if form.get("grant_type") == "refresh_token":
            return self._send({"access_token": "TOKEN-REFRESHED", "expires_in": 3600,
                               "patient": PATIENT_ID, "token_type": "Bearer"},
                              ctype="application/json")

        # Verify PKCE for real — this is the part worth testing.
        verifier = form.get("code_verifier", "")
        expect = STATE.get("code_challenge", "")
        got = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if not verifier or got != expect:
            return self._send({"error": "invalid_grant",
                               "error_description": "PKCE verification failed"},
                              400, ctype="application/json")
        if form.get("redirect_uri") != STATE.get("redirect_uri"):
            return self._send({"error": "invalid_grant"}, 400, ctype="application/json")

        return self._send({"access_token": "TOKEN-OK", "token_type": "Bearer",
                           "expires_in": 3600, "refresh_token": "REFRESH-OK",
                           "patient": PATIENT_ID,
                           "scope": "patient/*.read openid fhirUser offline_access"},
                          ctype="application/json")

    def _authed(self):
        return self.headers.get("Authorization", "").startswith("Bearer TOKEN-")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"mock Epic on {BASE}")
    HTTPServer(("127.0.0.1", PORT), H).serve_forever()
