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

STATE: dict = {}  # holds the pending auth request

LABS = [
    ("2026-07-14", "Hemoglobin A1c", 7.4, "%", "H"),
    ("2026-07-14", "Creatinine", 0.9, "mg/dL", ""),
    ("2026-01-09", "Hemoglobin A1c", 8.1, "%", "H"),
    ("2025-06-02", "Potassium", 3.2, "mmol/L", "L"),
]


def obs(i, date, name, val, unit, flag):
    r = {
        "resourceType": "Observation", "id": f"obs{i}",
        "status": "final",
        "category": [{"coding": [{"code": "laboratory"}]}],
        "code": {"text": name},
        "effectiveDateTime": f"{date}T10:00:00Z",
        "valueQuantity": {"value": val, "unit": unit},
    }
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
    ("Observation", "vital-signs"): [{
        "resourceType": "Observation", "id": "v1", "status": "final",
        "category": [{"coding": [{"code": "vital-signs"}]}],
        "code": {"text": "Blood pressure"}, "effectiveDateTime": "2026-07-14T10:00:00Z",
        "valueString": "128/78 mmHg",
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
         "serviceProvider": {"display": "Stanford Endocrinology"}},
    ],
    ("DocumentReference", "clinical-note"): [
        {"resourceType": "DocumentReference", "id": "dr1",
         "type": {"text": "Progress Note"}, "date": "2026-07-14T11:02:00Z",
         "content": [{"attachment": {"contentType": "text/html",
                                     "url": f"{BASE}/Binary/bin1"}}]},
    ],
    ("DiagnosticReport", "LAB"): [
        {"resourceType": "DiagnosticReport", "id": "d1", "status": "final",
         "code": {"text": "Comprehensive metabolic panel"},
         "effectiveDateTime": "2026-07-14T10:00:00Z"},
    ],
}

NOTE_HTML = """<div><p><b>Subjective:</b> Patient reports improved energy since the
last visit. Adherent to metformin. Denies hypoglycemic episodes.</p>
<p><b>Assessment:</b> Type 2 diabetes, improving. A1c down from 8.1 to 7.4.</p>
<p><b>Plan:</b> Continue metformin 500 mg BID. Recheck A1c in three months.
Referral to nutrition placed.</p></div>"""

SUPPORTED = sorted({k[0] for k in DATA} | {"Procedure", "CareTeam", "Goal", "CarePlan"})


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
            return self._send({
                "authorization_endpoint": f"http://127.0.0.1:{PORT}/oauth2/authorize",
                "token_endpoint": f"http://127.0.0.1:{PORT}/oauth2/token",
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

        m = re.match(r".*/api/FHIR/R4/Binary/(\w+)$", path)
        if m:
            return self._send({"resourceType": "Binary", "contentType": "text/html",
                               "data": base64.b64encode(NOTE_HTML.encode()).decode()})

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
                               "entry": [{"resource": r} for r in chunk],
                               "link": links})

        return self._send({"resourceType": "Bundle", "type": "searchset",
                           "entry": [{"resource": r} for r in items]})

    def do_POST(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        form = {k: v[0] for k, v in
                urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
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
