#!/usr/bin/env python3
"""
epic_export.py — pull your own medical record out of an Epic MyChart system
via the SMART-on-FHIR patient API.

Built for patients and caretakers who want a local, structured copy of the
record instead of clicking through the portal.

Commands
--------
  find-endpoint "stanford"   Search Epic's public directory for a health system
  login                      Run the SMART/PKCE browser login, cache tokens
  pull                       Download every available resource for the patient
  render                     Build a readable HTML + Markdown record from the pull
  all                        login -> pull -> render

Everything is written to --out (default ./record). Nothing leaves your machine.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import http.server
import json
import os
import re
import secrets
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any, Iterable

import requests

# Epic's public directory of every organization's production FHIR endpoint.
EPIC_BRANDS_BUNDLE = "https://open.epic.com/Endpoints/R4"

# Resource types worth pulling for a patient/caretaker, in the order we want
# them to appear in the rendered record. The script intersects this with what
# the server actually advertises in its CapabilityStatement, so an org that
# doesn't expose something is skipped rather than erroring.
#
# Each entry: (resource type, extra query params)
WANTED: list[tuple[str, dict[str, str]]] = [
    ("Patient", {}),
    ("Condition", {"category": "problem-list-item"}),
    ("Condition", {"category": "encounter-diagnosis"}),
    ("AllergyIntolerance", {}),
    ("MedicationRequest", {}),
    ("Immunization", {}),
    ("Observation", {"category": "laboratory"}),
    ("Observation", {"category": "vital-signs"}),
    ("Observation", {"category": "social-history"}),
    ("DiagnosticReport", {"category": "LAB"}),
    ("DiagnosticReport", {"category": "RAD"}),
    ("DocumentReference", {"category": "clinical-note"}),
    ("Encounter", {}),
    ("Procedure", {}),
    ("CarePlan", {"category": "assess-plan"}),
    ("CareTeam", {}),
    ("Goal", {}),
]

# Note: offline_access is deliberately NOT requested by default.
#
# Epic's automatic client distribution has two lanes. An app that does not use
# refresh tokens syncs to every eligible organization with zero per-org work.
# An app that DOES use refresh tokens is only queued at each organization until
# the developer uploads a client credential for that specific organization.
# For a personal export tool, re-authenticating in the browser each run is a far
# smaller cost than dropping into the second lane. Pass --offline-access if you
# want refresh tokens anyway and are willing to do the per-org credential step.
DEFAULT_SCOPES = "openid fhirUser patient/*.read"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"  {msg}", file=sys.stderr)


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def lock_down(path: Path) -> None:
    """Restrict a file or directory to the current user only.

    Windows has no POSIX permission bits: os.chmod() there can only toggle
    the read-only attribute, so passing 0o600/0o700 is silently a no-op as
    far as *who* can read the file — it stays readable by any local account.
    Use icacls (built into Windows, no extra dependency) to strip inherited
    permissions and grant access to the current user only. Elsewhere, a
    plain chmod does the job.
    """
    if os.name == "nt":
        user = os.environ.get("USERNAME") or os.getlogin()
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            check=False, capture_output=True,
        )
    else:
        os.chmod(path, 0o700 if path.is_dir() else 0o600)


def state_dir(out: Path) -> Path:
    d = out / ".auth"
    d.mkdir(parents=True, exist_ok=True)
    lock_down(d)
    return d


def save_json(path: Path, obj: Any, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    if private:
        lock_down(path)


# --------------------------------------------------------------------------
# endpoint discovery
# --------------------------------------------------------------------------

def cmd_find_endpoint(args: argparse.Namespace) -> None:
    """Search Epic's published brands bundle for an organization by name."""
    log(f"downloading Epic endpoint directory ({EPIC_BRANDS_BUNDLE}) ...")
    r = requests.get(EPIC_BRANDS_BUNDLE, timeout=120)
    r.raise_for_status()
    bundle = r.json()

    needle = args.name.lower()
    # The brands bundle interleaves Organization and Endpoint resources;
    # Organizations point at their Endpoint via the endpoint[] reference.
    endpoints: dict[str, dict] = {}
    orgs: list[dict] = []
    for entry in bundle.get("entry", []):
        res = entry.get("resource", {})
        if res.get("resourceType") == "Endpoint":
            endpoints[res.get("id", "")] = res
        elif res.get("resourceType") == "Organization":
            orgs.append(res)

    hits = []
    for org in orgs:
        name = org.get("name", "")
        if needle not in name.lower():
            continue
        for ref in org.get("endpoint", []):
            ep_id = str(ref.get("reference", "")).split("/")[-1]
            ep = endpoints.get(ep_id)
            if ep and ep.get("address"):
                hits.append((name, ep["address"]))

    if not hits:
        # Fall back to a brute scan of Endpoint names — some orgs only appear there.
        for ep in endpoints.values():
            if needle in str(ep.get("name", "")).lower() and ep.get("address"):
                hits.append((ep.get("name", "?"), ep["address"]))

    if not hits:
        die(f"no organization matching {args.name!r} found in Epic's directory")

    seen = set()
    print()
    for name, url in sorted(hits):
        if url in seen:
            continue
        seen.add(url)
        print(f"  {name}\n    {url}\n")


def smart_config(base_url: str) -> dict:
    """Fetch the SMART discovery document for a FHIR base URL."""
    url = base_url.rstrip("/") + "/.well-known/smart-configuration"
    r = requests.get(url, headers={"Accept": "application/json"}, timeout=30)
    r.raise_for_status()
    return r.json()


def capability_resources(base_url: str, token: str | None = None) -> set[str]:
    """Ask the server which resource types it actually supports."""
    headers = {"Accept": "application/fhir+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.get(base_url.rstrip("/") + "/metadata", headers=headers, timeout=60)
        r.raise_for_status()
        cap = r.json()
    except Exception as e:  # noqa: BLE001
        log(f"could not read CapabilityStatement ({e}); will try everything")
        return set()
    out: set[str] = set()
    for rest in cap.get("rest", []):
        for res in rest.get("resource", []):
            if res.get("type"):
                out.add(res["type"])
    return out


# --------------------------------------------------------------------------
# SMART on FHIR standalone launch, authorization code + PKCE
# --------------------------------------------------------------------------

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if "code" in qs or "error" in qs:
            _CallbackHandler.result = {k: v[0] for k, v in qs.items()}
            body = b"<html><body style='font:16px system-ui;padding:3rem'>" \
                   b"<h2>Signed in.</h2><p>You can close this tab and return " \
                   b"to your terminal.</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a: Any) -> None:  # silence access logs
        pass


def _self_signed_cert(dirpath: Path) -> tuple[Path, Path]:
    """Generate a throwaway localhost cert so we can serve an https redirect URI.

    Epic requires HTTPS redirect URIs for production apps, so a plain
    http://localhost callback often won't be accepted once you leave the sandbox.
    """
    cert, key = dirpath / "localhost.crt", dirpath / "localhost.key"
    if cert.exists() and key.exists():
        return cert, key
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.timezone.utc)
    crt = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(k.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=825))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
        )
        .sign(k, hashes.SHA256())
    )
    key.write_bytes(
        k.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    lock_down(key)
    cert.write_bytes(crt.public_bytes(serialization.Encoding.PEM))
    return cert, key


def do_login(args: argparse.Namespace) -> dict:
    out = Path(args.out)
    sd = state_dir(out)

    if getattr(args, "offline_access", False) and "offline_access" not in args.scopes:
        args.scopes = args.scopes + " offline_access"

    cfg = smart_config(args.fhir_base)
    authorize = cfg["authorization_endpoint"]
    token_url = cfg["token_endpoint"]
    log(f"authorize: {authorize}")
    log(f"token:     {token_url}")

    redirect = args.redirect_uri
    ru = urllib.parse.urlparse(redirect)
    port = ru.port or (443 if ru.scheme == "https" else 80)
    use_tls = ru.scheme == "https"

    # PKCE
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = secrets.token_urlsafe(24)

    params = {
        "response_type": "code",
        "client_id": args.client_id,
        "redirect_uri": redirect,
        "scope": args.scopes,
        "state": state,
        "aud": args.fhir_base,  # Epic requires aud on standalone launch
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = authorize + ("&" if "?" in authorize else "?") + urllib.parse.urlencode(params)

    # Local listener for the redirect
    _CallbackHandler.result = {}
    srv = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    if use_tls:
        cert, key = _self_signed_cert(sd)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        log("using a self-signed cert for the https callback — your browser will")
        log("warn once about it on the redirect; that page is your own machine.")

    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    print("\nOpening your browser to sign in to the patient portal.")
    print("If it doesn't open, paste this URL:\n")
    print(url + "\n")
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass

    deadline = time.time() + args.timeout
    while not _CallbackHandler.result and time.time() < deadline:
        time.sleep(0.3)
    srv.shutdown()

    res = _CallbackHandler.result
    if not res:
        die("timed out waiting for the browser redirect")
    if "error" in res:
        die(f"authorization failed: {res.get('error')} {res.get('error_description','')}")
    if res.get("state") != state:
        die("state mismatch on the redirect — aborting")

    log("exchanging authorization code for tokens ...")
    tr = requests.post(
        token_url,
        data={
            "grant_type": "authorization_code",
            "code": res["code"],
            "redirect_uri": redirect,
            "client_id": args.client_id,
            "code_verifier": verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=60,
    )
    if tr.status_code != 200:
        die(f"token exchange failed ({tr.status_code}): {tr.text[:500]}")
    tok = tr.json()
    tok["_obtained_at"] = int(time.time())
    tok["_fhir_base"] = args.fhir_base
    tok["_client_id"] = args.client_id
    tok["_token_url"] = token_url
    save_json(sd / "tokens.json", tok, private=True)
    log(f"tokens cached in {sd/'tokens.json'} (mode 600)")
    if tok.get("patient"):
        log(f"patient FHIR id: {tok['patient']}")
    return tok


def load_tokens(out: Path) -> dict:
    p = state_dir(out) / "tokens.json"
    if not p.exists():
        die("no cached tokens — run `login` first")
    tok = json.loads(p.read_text(encoding="utf-8"))
    age = int(time.time()) - tok.get("_obtained_at", 0)
    expired = age > max(tok.get("expires_in", 3600) - 60, 0)

    if expired and not tok.get("refresh_token"):
        die("your access token has expired and this app doesn't use refresh "
            "tokens.\n       Re-run `login` (or use `all`, which logs in and "
            "pulls in one go).")

    # Refresh if we're inside 60s of expiry and we have a refresh token.
    if tok.get("refresh_token") and expired:
        log("access token expired; refreshing ...")
        r = requests.post(
            tok["_token_url"],
            data={
                "grant_type": "refresh_token",
                "refresh_token": tok["refresh_token"],
                "client_id": tok["_client_id"],
            },
            timeout=60,
        )
        if r.status_code == 200:
            new = r.json()
            tok.update(new)
            tok["_obtained_at"] = int(time.time())
            save_json(p, tok, private=True)
        else:
            log(f"refresh failed ({r.status_code}); you may need to `login` again")
    return tok


# --------------------------------------------------------------------------
# pulling
# --------------------------------------------------------------------------

def fetch_all(session: requests.Session, base: str, rtype: str,
              params: dict[str, str]) -> list[dict]:
    """Search a resource type and follow Bundle pagination to the end."""
    url = base.rstrip("/") + f"/{rtype}"
    resources: list[dict] = []
    first = True
    while url:
        r = session.get(url, params=params if first else None, timeout=120)
        first = False
        if r.status_code in (401, 403):
            raise PermissionError(f"{rtype}: {r.status_code}")
        if r.status_code == 404:
            return resources
        if r.status_code >= 400:
            log(f"{rtype}: HTTP {r.status_code} — {r.text[:200]}")
            return resources
        bundle = r.json()
        if bundle.get("resourceType") == rtype:  # a plain read, not a search
            return [bundle]
        for e in bundle.get("entry", []) or []:
            if e.get("resource"):
                resources.append(e["resource"])
        url = None
        for link in bundle.get("link", []) or []:
            if link.get("relation") == "next":
                url = link.get("url")
        params = {}
    return resources


def fetch_note_text(session: requests.Session, base: str, att: dict) -> str | None:
    """Pull the actual text of a clinical note from a DocumentReference attachment."""
    if att.get("data"):
        try:
            return base64.b64decode(att["data"]).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return None
    url = att.get("url")
    if not url:
        return None
    if not url.startswith("http"):
        url = base.rstrip("/") + "/" + url.lstrip("/")
    ctype = att.get("contentType", "")
    accept = "text/plain" if "text" in ctype or "rtf" in ctype else "application/fhir+json"
    try:
        r = session.get(url, headers={"Accept": accept}, timeout=120)
        if r.status_code >= 400:
            return None
        body = r.text
        if body.lstrip().startswith("{"):
            j = r.json()
            if j.get("resourceType") == "Binary" and j.get("data"):
                body = base64.b64decode(j["data"]).decode("utf-8", "replace")
        # Strip HTML/RTF wrapper down to something readable.
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"[ \t]+", " ", body)
        body = re.sub(r"\n\s*\n\s*\n+", "\n\n", body)
        return body.strip()
    except Exception:  # noqa: BLE001
        return None


def cmd_pull(args: argparse.Namespace) -> None:
    out = Path(args.out)
    tok = load_tokens(out)
    base = tok["_fhir_base"]
    patient_id = tok.get("patient") or args.patient_id
    if not patient_id:
        die("no patient id in the token response — pass --patient-id")

    s = requests.Session()
    s.headers.update(
        {
            "Authorization": f"Bearer {tok['access_token']}",
            "Accept": "application/fhir+json",
        }
    )

    supported = capability_resources(base, tok["access_token"])
    if supported:
        log(f"server advertises {len(supported)} resource types")

    raw = out / "raw"
    collected: dict[str, list[dict]] = {}

    for rtype, extra in WANTED:
        if supported and rtype not in supported:
            log(f"skip {rtype} (not supported here)")
            continue
        params = dict(extra)
        if rtype == "Patient":
            params = {"_id": patient_id}
        else:
            params["patient"] = patient_id
        label = rtype + ("_" + extra.get("category", "").replace("-", "") if extra else "")
        try:
            found = fetch_all(s, base, rtype, params)
        except PermissionError as e:
            log(f"{e} — your app's scopes don't cover this")
            continue
        if not found:
            log(f"{label}: none")
            continue
        log(f"{label}: {len(found)}")
        save_json(raw / f"{label}.json", found)
        collected.setdefault(rtype, []).extend(found)

    # Clinical notes: resolve the attachment bodies, which is where the
    # actual narrative lives.
    notes_dir = out / "notes"
    note_index: list[dict] = []
    for dr in collected.get("DocumentReference", []):
        title = (dr.get("type", {}).get("text")
                 or dr.get("description")
                 or "note")
        when = dr.get("date", "")[:10]
        for c in dr.get("content", []) or []:
            att = c.get("attachment", {})
            text = fetch_note_text(s, base, att)
            if not text or len(text) < 20:
                continue
            slug = re.sub(r"[^a-zA-Z0-9]+", "-", f"{when}-{title}").strip("-").lower()[:80]
            fn = notes_dir / f"{slug}-{dr.get('id','x')[:8]}.txt"
            fn.parent.mkdir(parents=True, exist_ok=True)
            fn.write_text(text, encoding="utf-8")
            note_index.append({"date": when, "title": title, "file": fn.name,
                               "chars": len(text)})
            break
    if note_index:
        save_json(out / "notes_index.json", note_index)
        log(f"notes: wrote {len(note_index)} note bodies to {notes_dir}")

    save_json(out / "manifest.json", {
        "pulled_at": dt.datetime.now().isoformat(timespec="seconds"),
        "fhir_base": base,
        "patient_id": patient_id,
        "counts": {k: len(v) for k, v in collected.items()},
        "notes": len(note_index),
    })
    print(f"\nDone. Raw FHIR in {raw}, note text in {notes_dir}")


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _obs_value(o: dict) -> str:
    if "valueQuantity" in o:
        q = o["valueQuantity"]
        return f"{q.get('value','')} {q.get('unit','')}".strip()
    for k in ("valueString", "valueBoolean", "valueInteger"):
        if k in o:
            return str(o[k])
    if "valueCodeableConcept" in o:
        return o["valueCodeableConcept"].get("text", "")
    return ""


def _flag(o: dict) -> str:
    for i in o.get("interpretation", []) or []:
        t = i.get("text") or (i.get("coding", [{}])[0].get("code", ""))
        if t and t.upper() not in ("N", "NORMAL"):
            return t
    return ""


def cmd_render(args: argparse.Namespace) -> None:
    out = Path(args.out)
    raw = out / "raw"
    if not raw.exists():
        die("nothing to render — run `pull` first")

    def load(stem: str) -> list[dict]:
        p = raw / f"{stem}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []

    patient = (load("Patient") or [{}])[0]
    name = ""
    if patient.get("name"):
        n = patient["name"][0]
        name = " ".join(n.get("given", []) + [n.get("family", "")]).strip()

    problems = load("Condition_problemlistitem")
    allergies = load("AllergyIntolerance")
    meds = load("MedicationRequest")
    labs = load("Observation_laboratory")
    vitals = load("Observation_vitalsigns")
    reports = load("DiagnosticReport_LAB") + load("DiagnosticReport_RAD")
    encounters = load("Encounter")
    immunizations = load("Immunization")
    notes = json.loads((out / "notes_index.json").read_text(encoding="utf-8")) if (out / "notes_index.json").exists() else []

    md: list[str] = [f"# Medical record — {name or 'patient'}", ""]
    md.append(f"_Exported {dt.datetime.now():%B %d, %Y} from {json.loads((out/'manifest.json').read_text(encoding='utf-8')).get('fhir_base','')}_")
    md.append("")

    def section(title: str, rows: Iterable[str]) -> None:
        rows = list(rows)
        if not rows:
            return
        md.append(f"## {title}")
        md.append("")
        md.extend(rows)
        md.append("")

    section("Active problems", (
        f"- {c.get('code',{}).get('text','?')}"
        + (f" — onset {c['onsetDateTime'][:10]}" if c.get("onsetDateTime") else "")
        for c in problems
    ))

    section("Allergies", (
        f"- **{a.get('code',{}).get('text','?')}** — "
        + ", ".join(
            r.get("text", "") or ", ".join(
                m.get("manifestation", [{}])[0].get("text", "")
                for m in [r]
            )
            for r in a.get("reaction", [])
        ).strip(", ")
        for a in allergies
    ))

    section("Medications", (
        "- " + (
            m.get("medicationCodeableConcept", {}).get("text")
            or m.get("medicationReference", {}).get("display", "?")
        )
        + (f" — {m['dosageInstruction'][0].get('text','')}"
           if m.get("dosageInstruction") else "")
        + f"  _({m.get('status','')})_"
        for m in meds
    ))

    # Labs: newest first, flag anything abnormal.
    lab_rows = []
    for o in sorted(labs, key=lambda x: x.get("effectiveDateTime", ""), reverse=True):
        val = _obs_value(o)
        if not val:
            continue
        flag = _flag(o)
        lab_rows.append(
            f"| {o.get('effectiveDateTime','')[:10]} | {o.get('code',{}).get('text','?')} "
            f"| {val} | {'**' + flag + '**' if flag else ''} |"
        )
    if lab_rows:
        md += ["## Lab results", "", "| Date | Test | Value | Flag |",
               "|---|---|---|---|"] + lab_rows + [""]

    section("Immunizations", (
        f"- {i.get('vaccineCode',{}).get('text','?')} — {i.get('occurrenceDateTime','')[:10]}"
        for i in immunizations
    ))

    section("Encounters", (
        f"- {e.get('period',{}).get('start','')[:10]} — "
        f"{e.get('type',[{}])[0].get('text','visit') if e.get('type') else 'visit'}"
        + (f" ({e['serviceProvider'].get('display','')})" if e.get("serviceProvider") else "")
        for e in sorted(encounters,
                        key=lambda x: x.get("period", {}).get("start", ""),
                        reverse=True)
    ))

    section("Clinical notes", (
        f"- {n['date']} — {n['title']} → `notes/{n['file']}` ({n['chars']:,} chars)"
        for n in sorted(notes, key=lambda x: x.get("date", ""), reverse=True)
    ))

    section("Reports", (
        f"- {r.get('effectiveDateTime','')[:10]} — {r.get('code',{}).get('text','?')} "
        f"_({r.get('status','')})_"
        for r in sorted(reports,
                        key=lambda x: x.get("effectiveDateTime", ""), reverse=True)
    ))

    md_text = "\n".join(md)
    (out / "record.md").write_text(md_text, encoding="utf-8")

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Medical record — {name}</title>
<style>
 body{{font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
   max-width:52rem;margin:0 auto;padding:3rem 1.5rem;color:#1a1a1a}}
 h1{{font-size:1.9rem;margin-bottom:.2rem}} h2{{margin-top:2.5rem;font-size:1.25rem;
   border-bottom:1px solid #e5e5e5;padding-bottom:.35rem}}
 table{{border-collapse:collapse;width:100%;font-size:.92rem}}
 th,td{{text-align:left;padding:.4rem .6rem;border-bottom:1px solid #eee}}
 th{{background:#fafafa}} code{{background:#f4f4f5;padding:.1rem .35rem;border-radius:3px}}
 li{{margin:.2rem 0}} em{{color:#666}}
 @media(prefers-color-scheme:dark){{body{{background:#141414;color:#e8e8e8}}
   th{{background:#1f1f1f}} th,td{{border-color:#2a2a2a}} h2{{border-color:#2a2a2a}}
   code{{background:#222}} em{{color:#999}}}}
</style></head><body>
{_md_to_html(md_text)}
</body></html>"""
    (out / "record.html").write_text(html, encoding="utf-8")
    print(f"\nWrote {out/'record.md'} and {out/'record.html'}")


def _md_to_html(md: str) -> str:
    """Deliberately tiny markdown renderer — no dependency needed for this subset."""
    lines = md.split("\n")
    html: list[str] = []
    in_ul = in_tbl = False

    def close() -> None:
        nonlocal in_ul, in_tbl
        if in_ul:
            html.append("</ul>")
            in_ul = False
        if in_tbl:
            html.append("</tbody></table>")
            in_tbl = False

    def inline(s: str) -> str:
        s = (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
        s = re.sub(r"_(.+?)_", r"<em>\1</em>", s)
        return s

    for ln in lines:
        if ln.startswith("## "):
            close(); html.append(f"<h2>{inline(ln[3:])}</h2>")
        elif ln.startswith("# "):
            close(); html.append(f"<h1>{inline(ln[2:])}</h1>")
        elif ln.startswith("- "):
            if not in_ul:
                close(); html.append("<ul>"); in_ul = True
            html.append(f"<li>{inline(ln[2:])}</li>")
        elif ln.startswith("|") and set(ln) <= set("|-: "):
            continue
        elif ln.startswith("|"):
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if not in_tbl:
                close()
                html.append("<table><thead><tr>"
                            + "".join(f"<th>{inline(c)}</th>" for c in cells)
                            + "</tr></thead><tbody>")
                in_tbl = True
                continue
            html.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr>")
        elif ln.strip() == "":
            close()
        else:
            close(); html.append(f"<p>{inline(ln)}</p>")
    close()
    return "\n".join(html)


# --------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Export your medical record from an Epic MyChart FHIR API.")
    p.add_argument("--out", default="./record", help="output directory (default ./record)")
    sub = p.add_subparsers(dest="cmd", required=True)

    fe = sub.add_parser("find-endpoint", help="search Epic's directory for a health system")
    fe.add_argument("name")
    fe.set_defaults(func=cmd_find_endpoint)

    def auth_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--fhir-base", required=True,
                        help="e.g. https://sfd.stanfordmed.org/FHIR/api/FHIR/R4/")
        sp.add_argument("--client-id", required=True,
                        help="non-production or production client id from fhir.epic.com")
        sp.add_argument("--redirect-uri", default="https://localhost:8765/callback",
                        help="must match what you registered (default %(default)s)")
        sp.add_argument("--scopes", default=DEFAULT_SCOPES)
        sp.add_argument("--offline-access", action="store_true",
                        help="also request refresh tokens; see the note in "
                             "README before using this with automatic client "
                             "distribution")
        sp.add_argument("--timeout", type=int, default=300)

    li = sub.add_parser("login", help="run the SMART/PKCE browser login")
    auth_args(li)
    li.set_defaults(func=lambda a: (do_login(a), None)[1])

    pl = sub.add_parser("pull", help="download the record")
    pl.add_argument("--patient-id", default=None)
    pl.set_defaults(func=cmd_pull)

    rn = sub.add_parser("render", help="build record.md + record.html")
    rn.set_defaults(func=cmd_render)

    al = sub.add_parser("all", help="login, pull, render")
    auth_args(al)
    al.add_argument("--patient-id", default=None)
    al.set_defaults(func=lambda a: (do_login(a), cmd_pull(a), cmd_render(a))[2])

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
