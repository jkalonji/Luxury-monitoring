import base64
import json

import check_setup
from check_setup import KO, OK, WARN, check_bluesky, check_groq, check_resend, check_supabase, jwt_claims


def make_jwt(**claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'HS256'})}.{enc(claims)}.signature"


class Resp:
    def __init__(self, status=200, text="", data=None):
        self.status_code, self.text, self._data = status, text, data or {}

    def json(self):
        return self._data


def statuses(results):
    return [s for s, _ in results]


def test_jwt_claims():
    assert jwt_claims(make_jwt(role="service_role", ref="abc123")) == {"role": "service_role", "ref": "abc123"}
    assert jwt_claims("sb_secret_xxx") == {} and jwt_claims("a.b.c") == {}


def supabase_env(ref="abc123", role="service_role", url=None):
    return {"SUPABASE_URL": url or f"https://{ref}.supabase.co", "SUPABASE_KEY": make_jwt(role=role, ref=ref)}


def test_supabase_missing_tables_is_reported_with_project_id(monkeypatch):
    monkeypatch.setattr(check_setup.requests, "get",
                        lambda url, **kw: Resp(404, "{'code': 'PGRST205'}"))
    res = check_supabase(supabase_env())
    text = "\n".join(m for _, m in res)
    assert "table articles: MISSING" in text and "points to project 'abc123'" in text
    assert "supabase.com/dashboard/project/abc123" in text
    assert KO in statuses(res)


def test_supabase_all_good(monkeypatch):
    monkeypatch.setattr(check_setup.requests, "get", lambda url, **kw: Resp(200))
    assert KO not in statuses(check_supabase(supabase_env()))


def test_supabase_detects_anon_key_wrong_project_and_bad_url(monkeypatch):
    monkeypatch.setattr(check_setup.requests, "get", lambda url, **kw: Resp(200))
    assert KO in statuses(check_supabase(supabase_env(role="anon")))
    res = check_supabase({**supabase_env(), "SUPABASE_URL": "https://other.supabase.co"})
    assert any(s == KO and "'abc123' but SUPABASE_URL points to 'other'" in m for s, m in res)
    res = check_supabase({**supabase_env(), "SUPABASE_URL": "https://abc123.supabase.co/rest/v1/"})
    assert any(s == WARN and "should look like" in m for s, m in res)
    assert statuses(check_supabase({})) == [KO]


def test_supabase_output_never_contains_the_key(monkeypatch):
    monkeypatch.setattr(check_setup.requests, "get", lambda url, **kw: Resp(404, "PGRST205"))
    env = supabase_env()
    assert env["SUPABASE_KEY"] not in "\n".join(m for _, m in check_supabase(env))


def test_groq_checks_model_availability(monkeypatch):
    monkeypatch.setattr(check_setup.requests, "get",
                        lambda url, **kw: Resp(200, data={"data": [{"id": "openai/gpt-oss-120b"}]}))
    assert KO not in statuses(check_groq({"GROQ_API_KEY": "k"}))
    res = check_groq({"GROQ_API_KEY": "k", "GROQ_MODEL": "gone-model"})
    assert KO in statuses(res) and "NOT available" in res[-1][1]
    monkeypatch.setattr(check_setup.requests, "get", lambda url, **kw: Resp(401))
    assert KO in statuses(check_groq({"GROQ_API_KEY": "bad"}))
    assert statuses(check_groq({})) == [WARN]


def test_resend_domain_verification(monkeypatch):
    domains = Resp(200, data={"data": [{"name": "mydomain.com", "status": "verified"}]})
    monkeypatch.setattr(check_setup.requests, "get", lambda url, **kw: domains)
    good = check_resend({"RESEND_API_KEY": "k", "MAIL_TO": "a@x.com", "MAIL_FROM": "Radar <r@mydomain.com>"})
    assert KO not in statuses(good)
    bad = check_resend({"RESEND_API_KEY": "k", "MAIL_TO": "a@x.com", "MAIL_FROM": "Radar <r@other.com>"})
    assert any(s == KO and "not verified" in m for s, m in bad)
    default = check_resend({"RESEND_API_KEY": "k", "MAIL_TO": "a@x.com"})
    assert any(s == WARN and "resend.dev" in m for s, m in default)
    assert KO in statuses(check_resend({"RESEND_API_KEY": "k"}))            # no recipient
    monkeypatch.setattr(check_setup.requests, "get", lambda url, **kw: Resp(401, "restricted_api_key"))
    assert KO not in statuses(check_resend({"RESEND_API_KEY": "k", "MAIL_TO": "a@x.com"}))


def test_bluesky(monkeypatch):
    assert statuses(check_bluesky({})) == [WARN]
    monkeypatch.setattr(check_setup.requests, "post", lambda url, **kw: Resp(200))
    assert statuses(check_bluesky({"BSKY_HANDLE": "h", "BSKY_APP_PASSWORD": "p"})) == [OK]
    monkeypatch.setattr(check_setup.requests, "post", lambda url, **kw: Resp(401))
    assert statuses(check_bluesky({"BSKY_HANDLE": "h", "BSKY_APP_PASSWORD": "p"})) == [KO]


def test_supabase_new_format_keys(monkeypatch):
    monkeypatch.setattr(check_setup.requests, "get", lambda url, **kw: Resp(404, "PGRST205"))
    res = check_supabase({"SUPABASE_URL": "https://proj42.supabase.co", "SUPABASE_KEY": "sb_secret_abcdef"})
    text = "|".join(m for _, m in res)
    assert "new-format secret key" in text and "points to project 'proj42'" in text and "project 'proj42'" in text
    assert "sb_secret_abcdef" not in text
    pub = check_supabase({"SUPABASE_URL": "https://proj42.supabase.co", "SUPABASE_KEY": "sb_publishable_x"})
    assert any(s == KO and "PUBLISHABLE" in m for s, m in pub)
