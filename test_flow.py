#!/usr/bin/env python3
"""End-to-end test of epic_export.py against the mock Epic server.

Stands in for a real browser by following the authorize redirect with requests.
Verifies: SMART discovery, PKCE round trip, capability filtering, pagination,
Binary note retrieval, token refresh, and rendering.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import epic_export as ex  # noqa: E402
import mock_epic  # noqa: E402

OUT = Path("/tmp/record-test")
FAILS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(label)


def is_locked_down(path: Path) -> tuple[bool, str]:
    """Mirrors epic_export.lock_down()'s two code paths so the test verifies
    what actually happened rather than a POSIX-only st_mode value."""
    if os.name == "nt":
        out = subprocess.run(["icacls", str(path)], capture_output=True,
                              text=True, check=False).stdout
        user = (os.environ.get("USERNAME") or "").lower()
        first_line = out.strip().splitlines()[0] if out.strip() else "no icacls output"
        ok = bool(user) and user in out.lower() and "everyone" not in out.lower() \
            and "builtin\\users" not in out.lower()
        return ok, first_line
    mode = oct(path.stat().st_mode)[-3:]
    return mode == "600", mode


def main() -> int:
    shutil.rmtree(OUT, ignore_errors=True)

    from http.server import HTTPServer
    srv = HTTPServer(("127.0.0.1", mock_epic.PORT), mock_epic.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    print(f"mock Epic up on {mock_epic.BASE}\n")

    # Stand in for the human clicking through MyChart login.
    def fake_open(url: str) -> bool:
        threading.Thread(
            target=lambda: requests.get(url, allow_redirects=True, timeout=10),
            daemon=True).start()
        return True
    webbrowser.open = fake_open

    args = argparse.Namespace(
        out=str(OUT),
        fhir_base=mock_epic.BASE,
        client_id="test-client-id",
        redirect_uri="http://localhost:8765/callback",
        scopes=ex.DEFAULT_SCOPES,
        timeout=25,
        patient_id=None,
        offline_access=True,   # exercise the refresh path
    )

    print("1. login (PKCE)")
    tok = ex.do_login(args)
    check("access token issued", tok.get("access_token") == "TOKEN-OK")
    check("patient context returned", tok.get("patient") == mock_epic.PATIENT_ID)
    check("refresh token stored", bool(tok.get("refresh_token")))
    tf = OUT / ".auth" / "tokens.json"
    locked, detail = is_locked_down(tf)
    check("token file restricted to owner", locked, detail)

    print("\n2. PKCE is actually enforced by the server")
    bad = requests.post(f"http://127.0.0.1:{mock_epic.PORT}/oauth2/token", data={
        "grant_type": "authorization_code", "code": "AUTHCODE-XYZ",
        "redirect_uri": args.redirect_uri, "client_id": "test-client-id",
        "code_verifier": "wrong-verifier"}, timeout=10)
    check("wrong verifier rejected", bad.status_code == 400, f"HTTP {bad.status_code}")

    print("\n3. pull")
    ex.cmd_pull(args)
    raw = OUT / "raw"
    man = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
    check("Patient pulled", (raw / "Patient.json").exists())
    labs = json.loads((raw / "Observation_laboratory.json").read_text(encoding="utf-8"))
    check("pagination followed (5 labs across 3 pages)", len(labs) == 5, f"got {len(labs)}")
    meds = json.loads((raw / "MedicationRequest.json").read_text(encoding="utf-8"))
    check("medications pulled", len(meds) == 2, f"got {len(meds)}")
    # ServiceRequest is in WANTED but absent from the mock's CapabilityStatement,
    # so capability filtering should drop it before any request is made.
    check("unsupported types skipped cleanly",
          not (raw / "ServiceRequest.json").exists())
    check("note body fetched from Binary", man.get("notes") == 3, str(man.get("notes")))
    prog = [p for p in (OUT / "notes").glob("*.txt") if "progress" in p.name]
    body = prog[0].read_text(encoding="utf-8") if prog else ""
    check("note HTML stripped to text", "Assessment:" in body and "<p>" not in body)
    check("note content intact", "A1c down from 8.1 to 7.4" in body)
    # Epic leaves entities in the narrative — 9,511 across 131 of 138 notes in
    # one real chart. "&lt;" mattering clinically ("temp &lt; 100"), bullets,
    # quotes, and CJK all arrived as literal escape sequences.
    allnotes = "\n".join(p.read_text(encoding="utf-8")
                         for p in (OUT / "notes").glob("*.txt"))
    check("HTML entities decoded in note text",
          "&nbsp;" not in allnotes and "&#8226;" not in allnotes
          and "&amp;" not in allnotes)
    check("decoded entities became real characters",
          "temp < 100" in allnotes and "•" in allnotes)
    check("no non-breaking spaces left as \\xa0", "\xa0" not in allnotes)

    # Truncating the FHIR id to build a filename made two Epic notes collide,
    # and one silently overwrote the other. The index must match disk 1:1.
    idx = json.loads((OUT / "notes_index.json").read_text(encoding="utf-8"))
    on_disk = {p.name for p in (OUT / "notes").glob("*.txt")}
    check("every indexed note exists on disk 1:1",
          len({n["file"] for n in idx}) == len(idx) == len(on_disk),
          f"{len(idx)} indexed / {len({n['file'] for n in idx})} distinct / "
          f"{len(on_disk)} files")
    all_text = "\n".join(p.read_text(encoding="utf-8")
                         for p in (OUT / "notes").glob("*.txt"))
    check("notes with near-identical ids both survive",
          "no acute cardiopulmonary" in all_text
          and "ejection fraction" in all_text)

    # A note removed from the record upstream would otherwise linger forever,
    # making the folder look fuller than the index admits.
    orphan = OUT / "notes" / "2019-01-01-note-from-an-old-run-deadbeef.txt"
    orphan.write_text("stale content from a previous export", encoding="utf-8")
    ex.cmd_pull(args)
    check("stale note files pruned on re-pull", not orphan.exists())
    idx2 = json.loads((OUT / "notes_index.json").read_text(encoding="utf-8"))
    check("index still matches disk after re-pull",
          len(idx2) == len(list((OUT / "notes").glob("*.txt"))))

    # A real Epic mixes an OperationOutcome into every search bundle. Treating
    # those as clinical records inflates every count by one and renders them as
    # meaningless "?" rows; dropping them silently hides the server telling you
    # part of the chart was withheld. Both halves are checked here.
    stray = {p.name: [r.get("resourceType") for r in
                      json.loads(p.read_text(encoding="utf-8"))]
             for p in raw.glob("*.json")}
    polluted = {k: v for k, v in stray.items() if "OperationOutcome" in v}
    check("OperationOutcome kept out of saved resources", not polluted,
          ", ".join(polluted) or "none")
    check("counts not inflated by outcome entries",
          man["counts"].get("Patient") == 1, str(man["counts"].get("Patient")))
    notices = man.get("server_notices", [])
    # Count is not pinned: reference resolution can add its own notices when
    # the server refuses something. What matters is that Epic's two search
    # warnings survive deduplication.
    check("server notices captured", len(notices) >= 2, f"got {len(notices)}")
    check("suppression warning surfaced",
          any("will not be returned" in n["message"] for n in notices))
    check("notices deduped across resource types and pages",
          all(len(n["affects"]) == len(set(n["affects"])) for n in notices))
    check("notices record which searches they affect",
          any("Observation_laboratory" in n["affects"] for n in notices))

    print("\n4. token refresh path")
    t = json.loads(tf.read_text(encoding="utf-8")); t["_obtained_at"] = 0; t["expires_in"] = 1
    tf.write_text(json.dumps(t), encoding="utf-8")
    refreshed = ex.load_tokens(OUT)
    check("expired token refreshed", refreshed["access_token"] == "TOKEN-REFRESHED")

    print("\n5. render")
    ex.cmd_render(args)
    md = (OUT / "record.md").read_text(encoding="utf-8")
    html = (OUT / "record.html").read_text(encoding="utf-8")
    check("markdown written", len(md) > 200)
    check("patient name in output", "Camila Lopez" in md)
    check("abnormal lab flagged", "**H**" in md)
    check("lab table rendered to HTML", "<table>" in html and "<td>7.4 %</td>" in html)
    check("meds section present", "Metformin 500 mg tablet" in md)
    check("allergy present", "Penicillin" in md)
    check("note indexed", "Progress Note" in md)
    check("html escaping sane", "<script>" not in html)
    check("non-cp1252 clinical text survives to markdown",
          "β-thalassemia minor" in md)
    # Vitals were pulled and saved but never rendered, so they were invisible
    # in the readable record; blood pressure also needs component handling.
    check("vitals rendered, not just pulled", "## Vitals" in md)
    check("component-based blood pressure renders a value",
          re.search(r"\|\s*128/78", md) is not None)

    # Six resource types were pulled, saved, and never rendered. index.html
    # promises two of them ("Care plan and team", "Procedure") by name.
    for heading, needle in [
        ("Visit diagnoses", "Stomach ache"),
        ("Procedures", "TRANSTHORACIC ECHO"),
        ("Social history", "Never smoker"),
        ("Care team", "Dr. A. Chen"),
        ("Care plans", "Assessment and Plan"),
        ("Goals", "Walk 30 minutes daily"),
    ]:
        check(f"{heading.lower()} rendered", f"## {heading}" in md and needle in md)

    # Types beyond the original set. Each labels and dates itself with a
    # different field, which is why the renderer is generic rather than bespoke.
    for heading, needle in [
        ("Implanted devices", "Medtronic Azure XT DR MRI"),
        ("Insurance coverage", "PPO"),
        ("Family history", "Mother"),
        ("Imaging studies", "CT Abdomen"),
        ("Medications dispensed", "Metformin 500 mg tablet"),
        ("Appointments", "Endocrinology follow-up"),
    ]:
        check(f"{heading.lower()} rendered", f"## {heading}" in md and needle in md)
    check("device UDI and manufacturer surfaced",
          "UDI 00643169007222" in md and "Medtronic," in md)
    check("generic renderer finds each type's own date field",
          "2026-06-11" in md      # ImagingStudy.started
          and "2026-07-15" in md  # MedicationDispense.whenHandedOver
          and "2026-10-02" in md)  # Appointment.start

    # A category search never reaches radiology findings, and Practitioner is
    # not patient-searchable at all — but both read fine by id. Without this
    # the export silently omits them.
    refobs = raw / "Observation_referenced.json"
    check("referenced Observation recovered by direct read", refobs.exists())
    check("recovered observation reaches the record",
          "## Other observations" in md and "Pulmonary nodule size" in md)
    check("non-searchable Practitioner recovered",
          (raw / "Practitioner_referenced.json").exists())
    check("server refusal recorded rather than passed over silently",
          any("declined to release" in n["message"]
              for n in man.get("server_notices", []))
          or "Encounter" not in {t for t, _ in ex.WANTED})

    check("CodeableConcept falls back to coding.display, not '?'",
          "Hemoglobin A1c" in md and "| ? |" not in md)
    check("lab reference range rendered", "4.0–5.6 %" in md)
    check("ordered-but-unresulted lab still listed, not dropped",
          "Vitamin D" in md and "Not performed" in md)

    # FHIR instants are UTC; the record should show the day the patient lived.
    ts = "2026-08-19T02:27:12Z"
    want = (dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
              .astimezone().date().isoformat())
    check("instant converted to local date", ex._local_date(ts) == want, want)
    check("date-only value never shifted",
          ex._local_date("2005-09-20") == "2005-09-20")
    if dt.datetime.now().astimezone().utcoffset() != dt.timedelta(0):
        # Only observable off UTC; CI runners are UTC, so this is conditional.
        check("naive [:10] slicing would have been wrong here",
              ex._local_date(ts) != ts[:10], f"{ex._local_date(ts)} vs {ts[:10]}")
    check("non-cp1252 clinical text survives to HTML",
          "β-thalassemia minor" in html)
    check("no stray '?' rows from outcome entries", "- ?" not in md)
    check("completeness caveat lands in the document",
          "About this export" in md and "will not be returned" in md)
    # The caveat has to survive into the HTML too — checking only the markdown
    # missed a broken <ul> and mangled emphasis the first time around.
    notice_html = html[html.find("About this export"):]
    check("caveat present in HTML", "will not be returned" in notice_html)
    check("notice list not fragmented into one <ul> each",
          notice_html.count("<ul>") == 1, f"{notice_html.count('<ul>')} <ul>")
    # The mangling signature is a tag spliced into the middle of a word, e.g.
    # "CarePlan</em>assessplan". Assert that shape rather than naming a label:
    # the affects list is truncated for readability, so which labels survive
    # depends on how many resource types WANTED happens to contain.
    check("underscored FHIR labels not mangled by emphasis",
          re.search(r"[A-Za-z]+_[a-z]+", notice_html) is not None
          and not re.search(r"[A-Za-z0-9]</?em>[A-Za-z0-9]", notice_html))
    check("no unclosed emphasis left in HTML", "_<" not in notice_html
          and not re.search(r"_\s*</p>", notice_html))

    print("\n6. no-refresh-token default path")
    import copy
    a2 = copy.deepcopy(args); a2.out = "/tmp/record-test2"; a2.offline_access = False
    a2.scopes = ex.DEFAULT_SCOPES
    check("offline_access not in default scopes", "offline_access" not in ex.DEFAULT_SCOPES)
    t2 = ex.do_login(a2)
    tf2 = Path(a2.out) / ".auth" / "tokens.json"
    d = json.loads(tf2.read_text(encoding="utf-8")); d.pop("refresh_token", None)
    d["_obtained_at"] = 0; d["expires_in"] = 1; tf2.write_text(json.dumps(d), encoding="utf-8")
    try:
        ex.load_tokens(Path(a2.out)); ok = False
    except SystemExit:
        ok = True
    check("expired + no refresh token exits with clear message", ok)

    print("\n7. the access token stays on the provider's origin")
    # Every request carries the token in a session header, and the server picks
    # the pagination and attachment URLs. Following one off-origin hands over a
    # credential that reads the entire chart.
    leaked: dict = {}

    class Attacker(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            leaked["auth"] = self.headers.get("Authorization")
            b = json.dumps({"resourceType": "Bundle", "entry": []}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)
        def log_message(self, *a): pass

    class Hostile(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            b = json.dumps({"resourceType": "Bundle", "entry": [], "link": [
                {"relation": "next", "url": "http://127.0.0.1:9202/steal"}]}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)
        def log_message(self, *a): pass

    for prt, hnd in ((9201, Hostile), (9202, Attacker)):
        threading.Thread(target=HTTPServer(("127.0.0.1", prt), hnd).serve_forever,
                         daemon=True).start()
    time.sleep(0.3)
    sess = requests.Session()
    sess.headers.update({"Authorization": "Bearer SECRET"})
    _, warns = ex.fetch_all(sess, "http://127.0.0.1:9201/api/FHIR/R4", "Patient", {})
    check("token not sent to a foreign origin via pagination",
          leaked.get("auth") is None, str(leaked.get("auth")))
    check("truncated pagination is reported, not silent",
          any("another origin" in w for w in warns))
    check("off-origin note attachment refused",
          ex.fetch_note_text(sess, "https://real.example/FHIR/R4",
                             {"url": "https://evil.example/Binary/1",
                              "contentType": "text/plain"}) is None)
    check("same-origin URLs still allowed",
          ex.same_origin(f"{mock_epic.BASE}/Patient?x=1", mock_epic.BASE)
          and not ex.same_origin("https://evil.example/x", mock_epic.BASE))
    # A Location header can hold anything; an out-of-range port made .port
    # raise ValueError and took the export down with it.
    check("unparseable redirect target is not same-origin, and does not raise",
          ex.same_origin("http://127.0.0.1:99999/x", mock_epic.BASE) is False)

    # Everything above tests URLs the client CHOSE to fetch. A redirect is
    # chosen by the server after that check has already passed, so it is the
    # one path that can walk the token off-origin on its own. Containment here
    # was resting entirely on requests dropping the Authorization header
    # across hosts — real behaviour, but nothing this repo asserts or controls.
    redirected: dict = {}

    class RedirectTarget(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            redirected["hit"] = True
            redirected["auth"] = self.headers.get("Authorization")
            b = b'{"resourceType":"Binary","contentType":"text/plain","data":""}'
            self.send_response(200); self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)
        def log_message(self, *a): pass

    threading.Thread(
        target=HTTPServer(("127.0.0.1", mock_epic.REDIRECT_TARGET_PORT),
                          RedirectTarget).serve_forever, daemon=True).start()
    time.sleep(0.3)

    msess = requests.Session()
    msess.headers.update({"Authorization": "Bearer TOKEN-OK"})
    off = ex.fetch_note_text(msess, mock_epic.BASE,
                             {"url": f"{mock_epic.BASE}/Binary/offsite-note",
                              "contentType": "text/html"})
    check("note body refused when the server redirects off-origin", off is None)
    # Stronger than "the token was stripped": the request is never made at all,
    # so the other origin learns nothing — not the token, not that we exist.
    check("off-origin redirect target never contacted",
          not redirected.get("hit"), str(redirected))

    # Refusing every redirect would be a regression, not a fix.
    moved = ex.fetch_note_text(msess, mock_epic.BASE,
                               {"url": f"{mock_epic.BASE}/Binary/moved-note",
                                "contentType": "text/html"})
    check("same-origin redirect still followed",
          bool(moved) and "Assessment:" in moved, (moved or "")[:40])

    # The same guard has to hold on the search path, where the token is also
    # attached and the response is a whole page of records.
    _, rwarns = ex.fetch_all(msess, mock_epic.BASE, "Binary/offsite-note", {})
    check("redirected search reports incompleteness rather than failing quiet",
          any("another origin" in w for w in rwarns), str(rwarns))

    # A pull must not silently drop a note it refused: the DocumentReference
    # for the offsite attachment is in the mock's search results, so the run
    # in section 3 already exercised this path end to end.
    check("refused attachment left the other notes intact",
          man.get("notes") == 3, str(man.get("notes")))

    print("\n8. record HTML escapes server-controlled text")
    ev = Path("/tmp/record-test-xss"); shutil.rmtree(ev, ignore_errors=True)
    (ev / "raw").mkdir(parents=True)
    (ev / "raw" / "Patient.json").write_text(json.dumps([{
        "resourceType": "Patient", "id": "p1",
        "name": [{"given": ["</title><script>alert(1)</script><title>"],
                  "family": "X"}]}]), encoding="utf-8")
    (ev / "manifest.json").write_text(json.dumps({"fhir_base": "https://x/"}),
                                      encoding="utf-8")
    import copy as _c
    a3 = _c.deepcopy(args); a3.out = str(ev)
    ex.cmd_render(a3)
    xss = (ev / "record.html").read_text(encoding="utf-8")
    check("patient name escaped in <title>", "<script>alert(1)" not in xss)

    print("\n9. CLI entrypoint works")
    p = subprocess.run([sys.executable, str(Path(__file__).parent / "epic_export.py"),
                        "--help"], capture_output=True, text=True)
    check("--help exits 0", p.returncode == 0)

    print("\n" + "=" * 46)
    if FAILS:
        print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
        return 1
    print("all checks passed")
    print(f"rendered record: {OUT/'record.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
