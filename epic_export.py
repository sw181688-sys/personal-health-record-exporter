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
    # --- Beyond the original set -------------------------------------------
    # All of these are advertised by Stanford and are patient-searchable. They
    # are only reachable if the matching API is also enabled on the app's Epic
    # registration; without it the server answers 403, which cmd_pull logs and
    # skips. Listing them here therefore costs nothing and does NOT affect
    # automatic-distribution eligibility — that is decided by the registration,
    # not by what the client asks for.
    ("Device", {}),                    # implanted devices, with UDI
    ("Coverage", {}),                  # health insurance on file
    ("FamilyMemberHistory", {}),
    ("ImagingStudy", {}),              # study metadata, never the images
    ("MedicationDispense", {}),        # what was actually dispensed
    ("ImmunizationRecommendation", {}),  # vaccines due
    ("ServiceRequest", {}),            # orders and referrals
    ("Appointment", {}),
    ("QuestionnaireResponse", {}),     # intake forms, SDOH screenings
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
        r = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            check=False, capture_output=True, text=True,
        )
        # Never claim the file is protected when it isn't — this holds an
        # access token for the whole medical record.
        if r.returncode != 0:
            log(f"WARNING: could not restrict permissions on {path}")
            log(f"         {(r.stderr or r.stdout or '').strip()[:200]}")
            log("         treat that file as readable by other accounts.")
    else:
        os.chmod(path, 0o700 if path.is_dir() else 0o600)


def state_dir(out: Path) -> Path:
    d = out / ".auth"
    d.mkdir(parents=True, exist_ok=True)
    lock_down(d)
    return d


def save_json(path: Path, obj: Any, private: bool = False) -> None:
    """Write JSON atomically so an interrupted run can't leave a half file.

    A truncated raw/*.json or manifest.json is worse than none: render would
    read it back and quietly produce a short record.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    if private:
        lock_down(tmp)  # never let the token exist unprotected, even briefly
    os.replace(tmp, path)
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
    log(f"tokens cached in {sd/'tokens.json'} (restricted to your account)")
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

def same_origin(url: str, base: str) -> bool:
    """True when url sits on the same scheme+host+port as the FHIR base.

    Every request carries the access token in a session header, and both the
    pagination links and the note attachment URLs are chosen by the server.
    Following one to another origin hands that token — which reads the whole
    chart — to whoever is on the other end. Epic keeps both on the API host,
    so requiring it costs nothing and closes the exfiltration path.
    """
    def parts(u: str) -> tuple:
        p = urllib.parse.urlparse(u)
        return (p.scheme, (p.hostname or "").lower(),
                p.port or (443 if p.scheme == "https" else 80))
    return parts(url) == parts(base)


def outcome_issues(oo: dict) -> list[str]:
    """Readable issue strings from an OperationOutcome."""
    msgs = []
    for iss in oo.get("issue", []) or []:
        text = ((iss.get("details", {}) or {}).get("text")
                or iss.get("diagnostics")
                or iss.get("code", ""))
        if not text:
            continue
        sev = iss.get("severity", "")
        msgs.append(f"{sev}: {text}" if sev else text)
    return msgs


def fetch_all(session: requests.Session, base: str, rtype: str,
              params: dict[str, str]) -> tuple[list[dict], list[str]]:
    """Search a resource type and follow Bundle pagination to the end.

    Returns (resources, warnings). A search Bundle can carry OperationOutcome
    entries alongside the matches — Epic uses them to say things like "results
    of this sub-type will not be returned". Those describe the search, they
    aren't clinical records, so they're pulled out here instead of being saved
    and rendered as if they were part of the chart. The warnings are worth
    keeping: they're how the server tells you the export is incomplete.
    """
    url = base.rstrip("/") + f"/{rtype}"
    resources: list[dict] = []
    warnings: list[str] = []
    first = True
    # A server that returns a next link pointing at the current page (or a
    # cycle of them) would spin here forever. Bound it and say so.
    seen_urls: set[str] = set()
    max_pages = 500
    while url:
        if url in seen_urls or len(seen_urls) >= max_pages:
            reason = ("repeated a pagination link" if url in seen_urls
                      else f"exceeded {max_pages} pages")
            log(f"{rtype}: stopping — the server {reason}")
            warnings.append(
                f"warning: {rtype} may be incomplete — pagination stopped "
                f"after {len(resources)} records ({reason})")
            break
        seen_urls.add(url)
        r = session.get(url, params=params if first else None, timeout=120)
        first = False
        if r.status_code in (401, 403):
            raise PermissionError(f"{rtype}: {r.status_code}")
        if r.status_code == 404:
            return resources, warnings
        if r.status_code >= 400:
            log(f"{rtype}: HTTP {r.status_code} — {r.text[:200]}")
            # Bailing out mid-pagination leaves a partial result that the
            # manifest would otherwise report as a complete count.
            if resources:
                warnings.append(
                    f"warning: {rtype} is incomplete — the server returned "
                    f"HTTP {r.status_code} after {len(resources)} records")
            return resources, warnings
        bundle = r.json()
        if bundle.get("resourceType") == rtype:  # a plain read, not a search
            return [bundle], warnings
        if bundle.get("resourceType") == "OperationOutcome":
            return resources, warnings + outcome_issues(bundle)
        for e in bundle.get("entry", []) or []:
            res = e.get("resource")
            if not res:
                continue
            if (res.get("resourceType") == "OperationOutcome"
                    or (e.get("search", {}) or {}).get("mode") == "outcome"):
                warnings.extend(outcome_issues(res))
                continue
            resources.append(res)
        url = None
        for link in bundle.get("link", []) or []:
            if link.get("relation") == "next":
                nxt = link.get("url")
                if nxt and not same_origin(nxt, base):
                    log(f"{rtype}: refusing to follow pagination to another "
                        f"origin ({urllib.parse.urlparse(nxt).netloc}); "
                        f"results may be incomplete")
                    warnings.append(
                        "warning: pagination stopped early — the server pointed "
                        f"to another origin ({urllib.parse.urlparse(nxt).netloc})")
                    break
                url = nxt
        params = {}
    return resources, warnings


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
    elif not same_origin(url, base):
        # The token would go with it. A note isn't worth leaking the chart.
        log(f"skipping a note attachment on another origin "
            f"({urllib.parse.urlparse(url).netloc})")
        return None
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
    except Exception as e:  # noqa: BLE001
        # Returning None silently turned a timeout or a decode error into a
        # note that simply isn't there. Notes are the most valuable part of
        # the record; a missing one has to be visible.
        log(f"note fetch failed ({type(e).__name__}: {str(e)[:120]})")
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
    # message -> which searches reported it (Epic repeats the same notice on
    # every search, so dedupe rather than printing it twenty times).
    notices: dict[str, list[str]] = {}

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
            found, warns = fetch_all(s, base, rtype, params)
        except PermissionError as e:
            log(f"{e} — your app's scopes don't cover this")
            continue
        for w in warns:
            notices.setdefault(w, []).append(label)
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
            # Was "< 20 chars", which silently discarded real short notes
            # ("No acute distress." is 18). Only skip genuinely empty bodies.
            if not text or not text.strip():
                continue
            slug = re.sub(r"[^a-zA-Z0-9]+", "-", f"{when}-{title}").strip("-").lower()[:80]
            # The id must not be truncated. Epic's DocumentReference ids share
            # long prefixes ("ewtIzA62-DkL21MnJY6OyR..."), so taking the first
            # 8 characters collided and one note silently overwrote another.
            # Hash the whole id: fixed length, stable across runs, no collision.
            digest = hashlib.sha256(dr.get("id", "").encode()).hexdigest()[:12]
            fn = notes_dir / f"{slug}-{digest}.txt"
            fn.parent.mkdir(parents=True, exist_ok=True)
            fn.write_text(text, encoding="utf-8")
            note_index.append({"date": when, "title": title, "file": fn.name,
                               "chars": len(text), "source_id": dr.get("id", "")})
            break
    if note_index:
        indexed = {n["file"] for n in note_index}
        # Filenames are derived from the note id, so a re-pull rewrites the
        # same names. Anything else left in here is from an older run and no
        # longer part of the record — leaving it makes the folder look like it
        # holds more notes than the index admits to.
        for stale in notes_dir.glob("*.txt"):
            if stale.name not in indexed:
                stale.unlink()
                log(f"removed stale note from an earlier run: {stale.name}")
        # The index must describe what is actually on disk.
        on_disk = {p.name for p in notes_dir.glob("*.txt")}
        if len(indexed) != len(note_index) or indexed != on_disk:
            log(f"WARNING: {len(note_index)} notes indexed but "
                f"{len(on_disk)} files on disk — notes may have been lost")
        save_json(out / "notes_index.json", note_index)
        log(f"notes: wrote {len(note_index)} note bodies to {notes_dir}")

    notice_list = [{"message": m, "affects": sorted(set(v))}
                   for m, v in notices.items()]

    save_json(out / "manifest.json", {
        "pulled_at": dt.datetime.now().isoformat(timespec="seconds"),
        "fhir_base": base,
        "patient_id": patient_id,
        "counts": {k: len(v) for k, v in collected.items()},
        "notes": len(note_index),
        "server_notices": notice_list,
    })

    # The server telling you it withheld something matters more than any
    # count above it, so say it last, where it won't scroll away.
    if notice_list:
        log("")
        log(f"{len(notice_list)} notice(s) from the server about this export:")
        for n in notice_list:
            log(f"  - {n['message']}")
            log(f"    affects: {', '.join(n['affects'])}")

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
    # Blood pressure and friends carry no top-level value — the numbers live in
    # components. 92 of Epic's 254 sandbox vitals are this shape, so without
    # this they all render blank.
    parts = []
    for c in o.get("component", []) or []:
        label = (c.get("code", {}) or {}).get("text", "")
        q = c.get("valueQuantity") or {}
        val = f"{q.get('value','')} {q.get('unit','')}".strip()
        if not val:
            val = ((c.get("valueCodeableConcept") or {}).get("text", "")
                   or str(c.get("valueString", "")))
        if val:
            parts.append(f"{label} {val}".strip() if label else val)
    if parts:
        # Systolic/diastolic read better as 122/78 than as two sentences.
        nums = [re.match(r"^\D*([\d.]+)", p) for p in parts]
        if (len(parts) == 2 and all(nums)
                and "blood pressure" in (o.get("code", {}).get("text", "")).lower()):
            unit = (o["component"][0].get("valueQuantity") or {}).get("unit", "")
            return f"{nums[0].group(1)}/{nums[1].group(1)} {unit}".strip()
        return "; ".join(parts)
    return ""


def _concept_text(cc: Any) -> str:
    """Readable label for a CodeableConcept.

    Epic often omits `text` and only sends `coding[].display` — encounter
    diagnoses are like this — so reading `.text` alone renders a bare "?".
    """
    if not isinstance(cc, dict):
        return ""
    if cc.get("text"):
        return str(cc["text"])
    for c in cc.get("coding", []) or []:
        if c.get("display"):
            return str(c["display"])
    return ""


def _local_date(ts: str) -> str:
    """Date as the patient lived it, not as UTC recorded it.

    FHIR instants are usually UTC: a reading at 2026-08-19T02:27Z happened on
    the 18th in California, and slicing the first ten characters dates it a
    day late. Date-only values (onsetDateTime "2005-09-20") carry no zone and
    must be left exactly as they are.
    """
    if not ts:
        return ""
    if len(ts) <= 10:
        return ts[:10]
    try:
        d = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts[:10]
    if d.tzinfo is None:  # no zone means no basis for shifting it
        return ts[:10]
    return d.astimezone().date().isoformat()


def _obs_date(o: dict) -> str:
    """Observations date themselves several ways; social history uses a period."""
    return _local_date(
        o.get("effectiveDateTime")
        or (o.get("effectivePeriod", {}) or {}).get("start")
        or (o.get("effectivePeriod", {}) or {}).get("end")
        or o.get("issued", "")
    )


# Epic labels and dates each resource type with whichever field that type
# happens to use, so these are tried in order of specificity.
_LABEL_KEYS = ("code", "vaccineCode", "medicationCodeableConcept", "type",
               "serviceType", "relationship", "description", "class", "category")
_DATE_KEYS = ("effectiveDateTime", "performedDateTime", "occurrenceDateTime",
              "authoredOn", "authored", "whenHandedOver", "recordedDate",
              "created", "date", "started", "start")


def _resource_label(r: dict) -> str:
    for k in _LABEL_KEYS:
        v = r.get(k)
        if isinstance(v, list):
            v = v[0] if v else None
        if isinstance(v, dict):
            t = _concept_text(v)
            if t:
                return t
        elif isinstance(v, str) and v:
            return v
    for dn in r.get("deviceName", []) or []:   # Device keeps its name apart
        if dn.get("name"):
            return str(dn["name"])
    return str((r.get("medicationReference") or {}).get("display", ""))


def _resource_when(r: dict) -> str:
    for k in _DATE_KEYS:
        v = r.get(k)
        if isinstance(v, str) and v:
            return _local_date(v)
    for k in ("performedPeriod", "effectivePeriod", "period", "servicedPeriod"):
        p = r.get(k) or {}
        if p.get("start"):
            return _local_date(p["start"])
    return ""


def _device_extra(r: dict) -> str:
    """UDI and manufacturer are the point of an implanted-device record."""
    bits = [str(r.get("manufacturer", "")), str(r.get("model", ""))]
    udi = (r.get("udiCarrier") or [{}])[0].get("deviceIdentifier", "")
    if udi:
        bits.append(f"UDI {udi}")
    return ", ".join(b for b in bits if b)


def _ref_range(o: dict) -> str:
    """A lab value without its range is hard for a patient to act on."""
    for r in o.get("referenceRange", []) or []:
        if r.get("text"):
            return str(r["text"])
        lo, hi = r.get("low") or {}, r.get("high") or {}
        unit = lo.get("unit") or hi.get("unit") or ""
        if lo.get("value") is not None and hi.get("value") is not None:
            return f"{lo['value']}–{hi['value']} {unit}".strip()
        if lo.get("value") is not None:
            return f"≥{lo['value']} {unit}".strip()
        if hi.get("value") is not None:
            return f"≤{hi['value']} {unit}".strip()
    return ""


def _flag(o: dict) -> str:
    for i in o.get("interpretation", []) or []:
        t = i.get("text") or (i.get("coding", [{}])[0].get("code", ""))
        if t and t.upper() not in ("N", "NORMAL"):
            return t
    return ""


def html_escape(s: str) -> str:
    """Escape text going anywhere outside _md_to_html, which escapes its own."""
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _affects_phrase(affects: list[str], limit: int = 5) -> str:
    """Name what a notice touched without printing seventeen FHIR labels."""
    if len(affects) <= limit:
        return ", ".join(affects)
    return ", ".join(affects[:limit]) + f", and {len(affects) - limit} more"


def cmd_render(args: argparse.Namespace) -> None:
    out = Path(args.out)
    raw = out / "raw"
    if not raw.exists():
        die("nothing to render — run `pull` first")

    def load(stem: str) -> list[dict]:
        p = raw / f"{stem}.json"
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        # Belt and braces: pull now strips these, but data pulled by an older
        # version still has OperationOutcomes mixed in, and they render as
        # meaningless "?" rows.
        return [r for r in data if r.get("resourceType") != "OperationOutcome"]

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
    # These were pulled and saved but never rendered — 22 records invisible in
    # the sandbox alone, two of which index.html promises patients by name.
    visit_dx = load("Condition_encounterdiagnosis")
    procedures = load("Procedure")
    social = load("Observation_socialhistory")
    care_team = load("CareTeam")
    care_plans = load("CarePlan_assessplan")
    goals = load("Goal")
    notes = json.loads((out / "notes_index.json").read_text(encoding="utf-8")) if (out / "notes_index.json").exists() else []

    manifest_path = out / "manifest.json"
    manifest = (json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest_path.exists() else {})

    md: list[str] = [f"# Medical record — {name or 'patient'}", ""]
    md.append(f"_Exported {dt.datetime.now():%B %d, %Y} from {manifest.get('fhir_base','')}_")
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
        f"- {_concept_text(c.get('code')) or '?'}"
        + (f" — onset {_local_date(c['onsetDateTime'])}"
           if c.get("onsetDateTime") else "")
        for c in problems
    ))

    section("Allergies", (
        f"- **{_concept_text(a.get('code')) or '?'}** — "
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
            _concept_text(m.get("medicationCodeableConcept"))
            or m.get("medicationReference", {}).get("display", "?")
        )
        + (f" — {m['dosageInstruction'][0].get('text','')}"
           if m.get("dosageInstruction") else "")
        + f"  _({m.get('status','')})_"
        for m in meds
    ))

    # Labs: newest first, flag anything abnormal. A value with no reference
    # range is hard to act on, so the range gets its own column.
    lab_rows = []
    for o in sorted(labs, key=lambda x: x.get("effectiveDateTime", ""), reverse=True):
        val = _obs_value(o)
        if not val:
            # Dropping these hid ordered-but-unresulted tests entirely, which
            # reads as though they were never done.
            val = (_concept_text(o.get("dataAbsentReason"))
                   or ("see report" if o.get("hasMember") or o.get("derivedFrom")
                       else "not reported"))
            val = f"_{val}_"
        flag = _flag(o)
        lab_rows.append(
            f"| {_obs_date(o)} | {_concept_text(o.get('code')) or '?'} "
            f"| {val} | {_ref_range(o)} | {'**' + flag + '**' if flag else ''} |"
        )
    if lab_rows:
        md += ["## Lab results", "", "| Date | Test | Value | Reference range | Flag |",
               "|---|---|---|---|---|"] + lab_rows + [""]

    # Vitals were pulled and saved but never rendered, so 254 measurements
    # were invisible in the readable record. Newest first, most recent 60 —
    # the raw JSON keeps the rest.
    vital_rows = []
    for o in sorted(vitals, key=lambda x: x.get("effectiveDateTime", ""),
                    reverse=True):
        val = _obs_value(o)
        if not val:
            continue
        vital_rows.append(
            f"| {_obs_date(o)} "
            f"| {_concept_text(o.get('code')) or '?'} | {val} |")
    if vital_rows:
        shown, extra = vital_rows[:60], max(0, len(vital_rows) - 60)
        md += ["## Vitals", "", "| Date | Measurement | Value |",
               "|---|---|---|"] + shown + [""]
        if extra:
            md += [f"_{extra} older vital-sign readings are in "
                   f"`raw/Observation_vitalsigns.json`._", ""]

    section("Immunizations", (
        f"- {_concept_text(i.get('vaccineCode')) or '?'} — "
        f"{_local_date(i.get('occurrenceDateTime',''))}"
        for i in immunizations
    ))

    section("Social history", (
        f"- {_concept_text(s.get('code')) or '?'}: "
        f"{_obs_value(s) or '_not reported_'}"
        + (f" — {_obs_date(s)}" if _obs_date(s) else "")
        for s in social
    ))

    section("Encounters", (
        f"- {_local_date(e.get('period',{}).get('start',''))} — "
        f"{_concept_text(e.get('type',[{}])[0]) if e.get('type') else 'visit'}"
        + (f" ({e['serviceProvider'].get('display','')})" if e.get("serviceProvider") else "")
        for e in sorted(encounters,
                        key=lambda x: x.get("period", {}).get("start", ""),
                        reverse=True)
    ))

    # What was actually assessed at each visit — distinct from the standing
    # problem list, and previously not rendered at all.
    section("Visit diagnoses", (
        f"- {_local_date(c.get('recordedDate',''))} — "
        f"{_concept_text(c.get('code')) or '?'}"
        for c in sorted(visit_dx, key=lambda x: x.get("recordedDate", ""),
                        reverse=True)
    ))

    section("Procedures", (
        f"- {_local_date(p.get('performedDateTime','') or (p.get('performedPeriod',{}) or {}).get('start',''))} — "
        f"{_concept_text(p.get('code')) or '?'}"
        + (f"  _({p.get('status','')})_" if p.get("status") else "")
        for p in sorted(procedures,
                        key=lambda x: x.get("performedDateTime", ""), reverse=True)
    ))

    section("Care team", (
        f"- {(pt.get('member',{}) or {}).get('display','?')}"
        + (f" — {', '.join(filter(None, (_concept_text(r) for r in pt.get('role', []))))}"
           if pt.get("role") else "")
        for ct in care_team for pt in ct.get("participant", []) or []
    ))

    section("Care plans", (
        f"- {', '.join(filter(None, (_concept_text(c) for c in cp.get('category', [])))) or 'care plan'}"
        + (f"  _({cp.get('status','')})_" if cp.get("status") else "")
        + (f" — addresses {', '.join(a.get('display','') for a in cp.get('addresses', []) if a.get('display'))}"
           if cp.get("addresses") else "")
        for cp in care_plans
    ))

    section("Goals", (
        f"- {_concept_text(g.get('description')) or '?'}"
        + (f" — started {_local_date(g['startDate'])}" if g.get("startDate") else "")
        + (f"  _({g.get('lifecycleStatus','')})_" if g.get("lifecycleStatus") else "")
        for g in sorted(goals, key=lambda x: x.get("startDate", ""), reverse=True)
    ))

    # No real Epic response has been seen for these yet — the sandbox returns
    # none of them — so they get a generic date/label/status row rather than a
    # bespoke layout guessing at field shapes. Refine once real data lands.
    for stem, heading in [
        ("Device", "Implanted devices"),
        ("Coverage", "Insurance coverage"),
        ("FamilyMemberHistory", "Family history"),
        ("ImagingStudy", "Imaging studies"),
        ("MedicationDispense", "Medications dispensed"),
        ("ImmunizationRecommendation", "Vaccines due"),
        ("ServiceRequest", "Orders and referrals"),
        ("Appointment", "Appointments"),
        ("QuestionnaireResponse", "Questionnaires"),
    ]:
        rows = load(stem)
        section(heading, (
            "- "
            + (f"{_resource_when(r)} — " if _resource_when(r) else "")
            + (_resource_label(r) or "(unlabelled)")
            + (f" — {_device_extra(r)}"
               if stem == "Device" and _device_extra(r) else "")
            + (f"  _({r.get('status','')})_" if r.get("status") else "")
            for r in sorted(rows, key=_resource_when, reverse=True)
        ))

    section("Clinical notes", (
        f"- {n['date']} — {n['title']} → `notes/{n['file']}` ({n['chars']:,} chars)"
        for n in sorted(notes, key=lambda x: x.get("date", ""), reverse=True)
    ))

    section("Reports", (
        f"- {_obs_date(r)} — {_concept_text(r.get('code')) or '?'} "
        f"_({r.get('status','')})_"
        for r in sorted(reports,
                        key=lambda x: x.get("effectiveDateTime", ""), reverse=True)
    ))

    # If the server said it held something back, that belongs in the document
    # itself — someone reading this a year from now needs to know it may not
    # be the whole chart. One line per notice: a continuation line would fall
    # out of the <ul> in _md_to_html.
    notices = manifest.get("server_notices", [])
    if notices:
        section("About this export", [
            "These notices came from the server during the export. The full "
            "list of affected searches is in `manifest.json`.",
        ] + [
            f"- {n['message']} _(affects: {_affects_phrase(n['affects'])})_"
            for n in notices
        ])

    md_text = "\n".join(md)
    (out / "record.md").write_text(md_text, encoding="utf-8")

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Medical record — {html_escape(name)}</title>
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
        # Underscores must not emphasize mid-word, or FHIR labels like
        # Observation_laboratory pair up their underscores and shred the line.
        s = re.sub(r"(?<![A-Za-z0-9])_(.+?)_(?![A-Za-z0-9])", r"<em>\1</em>", s)
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
    # A Windows console defaults to cp1252 with strict encoding on stdout, so
    # printing a clinician's name, an org name from Epic's directory, or a
    # server message containing anything outside that codepage kills the run.
    # Nothing here is worth crashing an export over.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # not a reconfigurable stream
            pass

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
