"""Luxury Radar - setup check: are the secrets present and do the services accept them?

Run it from GitHub (Actions -> "Luxury Radar - Check setup") or locally with a .env. It never prints a secret:
only OK/KO statuses, the Supabase project id carried by the key (it is public: it is in your dashboard URL)
and the names of the Resend domains.
"""

import base64
import json
import os
import sys
import urllib.parse as up

import requests

TABLES = ("articles", "signals", "stories")
OK, KO, WARN = "OK ", "KO ", "?? "
TIMEOUT = 20


def jwt_claims(key: str) -> dict:
    """Payload of a legacy Supabase key (a JWT). Returns {} for new-format keys (sb_secret_...) or garbage."""
    parts = key.split(".")
    if len(parts) != 3:
        return {}
    try:
        return json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except Exception:
        return {}


def check_supabase(env=os.environ) -> list[tuple[str, str]]:
    url, key = (env.get("SUPABASE_URL") or "").strip(), (env.get("SUPABASE_KEY") or "").strip()
    if not url or not key:
        return [(KO, "SUPABASE_URL / SUPABASE_KEY not set: nothing would be kept between runs")]
    out = []
    host = up.urlparse(url).netloc
    if not url.startswith("https://") or not host.endswith(".supabase.co") or up.urlparse(url).path not in ("", "/"):
        out.append((WARN, "SUPABASE_URL should look like https://<project-id>.supabase.co (no path, no /rest/v1)"))
    claims = jwt_claims(key)
    if claims:
        role, ref = claims.get("role"), claims.get("ref", "")
        out.append((OK if role == "service_role" else KO, f"key role: {role} (must be service_role, not anon)"))
        if ref:
            same = host.split(".")[0] == ref
            out.append((OK if same else KO, f"key belongs to project '{ref}'; SUPABASE_URL "
                        f"{'points to the same project' if same else 'points to ANOTHER project'}"))
            out.append((WARN, f"run schema.sql in the project whose id is '{ref}' "
                        f"(the id in your dashboard URL: supabase.com/dashboard/project/{ref})"))
    else:
        out.append((WARN, "key is not a legacy JWT (new-format key?): role/project cannot be inspected"))
    base = url.rstrip("/")
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    for table in TABLES:
        try:
            r = requests.get(f"{base}/rest/v1/{table}?select=*&limit=1", headers=headers, timeout=TIMEOUT)
        except requests.RequestException as e:
            out.append((KO, f"table {table}: unreachable ({type(e).__name__})"))
            continue
        if r.status_code == 200:
            out.append((OK, f"table {table}: exists"))
        elif r.status_code == 404 and "PGRST205" in r.text:
            out.append((KO, f"table {table}: MISSING in this project -> run schema.sql there"))
        elif r.status_code in (401, 403):
            out.append((KO, f"table {table}: key rejected (HTTP {r.status_code})"))
        else:
            out.append((KO, f"table {table}: HTTP {r.status_code}"))
    return out


def check_groq(env=os.environ) -> list[tuple[str, str]]:
    key = (env.get("GROQ_API_KEY") or "").strip()
    if not key:
        return [(WARN, "GROQ_API_KEY not set: the rule-based classifier will be used (less accurate)")]
    model = (env.get("GROQ_MODEL") or "openai/gpt-oss-120b").strip().strip("'\"")
    try:
        r = requests.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT)
    except requests.RequestException as e:
        return [(KO, f"Groq unreachable ({type(e).__name__})")]
    if r.status_code != 200:
        return [(KO, f"Groq key rejected (HTTP {r.status_code})")]
    ids = {m.get("id") for m in r.json().get("data", [])}
    return [(OK, "Groq key accepted"),
            (OK if model in ids else KO, f"model '{model}' " + ("is available" if model in ids else "is NOT available: fix the GROQ_MODEL variable"))]


def check_resend(env=os.environ) -> list[tuple[str, str]]:
    key = (env.get("RESEND_API_KEY") or "").strip()
    recipients = [a for a in (env.get("MAIL_TO") or "").split(",") if a.strip()]
    out = [(OK if recipients else KO, f"MAIL_TO: {len(recipients)} recipient(s)")]
    if not key:
        return out + [(KO, "RESEND_API_KEY not set: no e-mail will be sent")]
    sender = env.get("MAIL_FROM") or "Luxury Radar <onboarding@resend.dev>"
    try:
        r = requests.get("https://api.resend.com/domains", headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT)
    except requests.RequestException as e:
        return out + [(KO, f"Resend unreachable ({type(e).__name__})")]
    if r.status_code == 401 and "restricted" in r.text.lower():
        return out + [(OK, "Resend key accepted (sending-only key: domains cannot be listed)")]
    if r.status_code != 200:
        return out + [(KO, f"Resend key rejected (HTTP {r.status_code})")]
    out.append((OK, "Resend key accepted"))
    domains = {d["name"]: d.get("status") for d in r.json().get("data", [])}
    sender_domain = sender.rsplit("@", 1)[-1].strip("> ")
    if sender_domain == "resend.dev":
        out.append((WARN, "sending from onboarding@resend.dev: Resend then only delivers to your own account address"))
    elif domains.get(sender_domain) == "verified":
        out.append((OK, f"sender domain {sender_domain} is verified"))
    else:
        out.append((KO, f"sender domain {sender_domain} is not verified in Resend (known: {', '.join(domains) or 'none'})"))
    return out


def check_bluesky(env=os.environ) -> list[tuple[str, str]]:
    handle, password = (env.get("BSKY_HANDLE") or "").strip(), (env.get("BSKY_APP_PASSWORD") or "").strip()
    if not handle or not password:
        return [(WARN, "Bluesky not configured: social posts will be skipped (optional)")]
    try:
        r = requests.post("https://bsky.social/xrpc/com.atproto.server.createSession", timeout=TIMEOUT,
                          json={"identifier": handle, "password": password})
    except requests.RequestException as e:
        return [(KO, f"Bluesky unreachable ({type(e).__name__})")]
    return [(OK, "Bluesky login works") if r.status_code == 200 else (KO, f"Bluesky login refused (HTTP {r.status_code})")]


def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    failed = False
    for title, check in (("Supabase", check_supabase), ("Groq", check_groq), ("Resend", check_resend),
                         ("Bluesky", check_bluesky)):
        print(f"\n== {title}")
        for status, msg in check():
            print(f"  {status} {msg}")
            failed |= status == KO
    print("\n" + ("Some checks failed." if failed else "Setup looks good."))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
