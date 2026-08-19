#!/usr/bin/env python3
"""End-to-end test of epic_export.py against the mock Epic server.

Stands in for a real browser by following the authorize redirect with requests.
Verifies: SMART discovery, PKCE round trip, capability filtering, pagination,
Binary note retrieval, token refresh, and rendering.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
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
    check("pagination followed (4 labs across 2 pages)", len(labs) == 4, f"got {len(labs)}")
    meds = json.loads((raw / "MedicationRequest.json").read_text(encoding="utf-8"))
    check("medications pulled", len(meds) == 2, f"got {len(meds)}")
    check("unsupported types skipped cleanly",
          not (raw / "Device.json").exists())
    check("note body fetched from Binary", man.get("notes") == 1, str(man.get("notes")))
    notes = list((OUT / "notes").glob("*.txt"))
    body = notes[0].read_text(encoding="utf-8") if notes else ""
    check("note HTML stripped to text", "Assessment:" in body and "<p>" not in body)
    check("note content intact", "A1c down from 8.1 to 7.4" in body)

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

    print("\n7. CLI entrypoint works")
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
