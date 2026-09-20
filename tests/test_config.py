import glob
import os
import re
import sys
from datetime import timedelta

import yaml

import main
from mailer import build_daily
from models import Article, now_utc
from signals import yesterday
from store import SqliteStore, article_row


def workflows():
    return {os.path.basename(f): yaml.safe_load(open(f, encoding="utf-8")) for f in glob.glob(".github/workflows/*.yml")}


def test_three_workflows_with_expected_triggers():
    wf = workflows()
    assert set(wf) == {"daily.yml", "weekly.yml", "dashboard.yml", "ci.yml", "check.yml"}
    assert wf["daily.yml"][True]["schedule"][0]["cron"] == "0 7 * * *"
    assert wf["weekly.yml"][True]["schedule"][0]["cron"] == "30 7 * * 1"
    for name, w in wf.items():
        if name in ("ci.yml", "check.yml"):
            continue
        assert "workflow_dispatch" in w[True], name
        assert w["concurrency"]["group"] == "luxury-radar" and w["concurrency"]["cancel-in-progress"] is False


def test_daily_workflow_publishes_pages_and_still_deploys_if_collection_fails():
    w = workflows()["daily.yml"]
    assert w["permissions"]["pages"] == "write" and w["permissions"]["id-token"] == "write"
    steps = w["jobs"]["collect-and-publish"]["steps"]
    by_name = {s.get("name"): s for s in steps}
    assert by_name["Run Luxury Radar"]["continue-on-error"] is True
    assert by_name["Run Luxury Radar"]["id"] == "radar"
    assert "python main.py" in by_name["Run Luxury Radar"]["run"]
    assert "dashboard.py --export" in by_name["Generate dashboard"]["run"]
    assert by_name["Fail the run if the collection failed"]["if"] == "steps.radar.outcome == 'failure'"
    order = [s.get("name") for s in steps]
    assert order.index("Deploy to GitHub Pages") < order.index("Fail the run if the collection failed")


def test_workflow_commands_reference_existing_scripts_and_flags():
    for name, w in workflows().items():
        for job in w["jobs"].values():
            for step in job["steps"]:
                for script in re.findall(r"python (\S+\.py)", step.get("run", "")):
                    assert os.path.exists(script), (name, script)
    daily = " ".join(s.get("run", "") for s in workflows()["daily.yml"]["jobs"]["collect-and-publish"]["steps"])
    assert "--backfill-days" in daily and "--dry-run" in daily
    ns = main.parse_args(["--backfill-days", "14", "--dry-run"])
    assert ns.backfill_days == 14 and ns.dry_run


def test_every_secret_used_by_workflows_is_documented_in_env_example():
    env_example = open(".env.example", encoding="utf-8").read()
    used = set()
    for f in glob.glob(".github/workflows/*.yml"):
        used |= set(re.findall(r"(?:secrets|vars)\.([A-Z_]+)", open(f, encoding="utf-8").read()))
    assert used, "workflows should use secrets"
    for var in used:
        assert re.search(rf"^{var}=", env_example, re.M), f"{var} missing from .env.example"


def test_every_env_var_read_by_the_code_is_documented():
    env_example = open(".env.example", encoding="utf-8").read()
    code = "".join(open(f, encoding="utf-8").read() for f in glob.glob("*.py"))
    read = set(re.findall(r"environ(?:\.get)?[\[(]\s*[\"']([A-Z_]+)[\"']", code))
    for var in read:
        assert re.search(rf"^{var}=", env_example, re.M) or var == "LOCAL_DB", f"{var} missing from .env.example"


def test_requirements_cover_third_party_imports():
    reqs = open("requirements.txt", encoding="utf-8").read().lower()
    imports = set()
    for f in glob.glob("*.py"):
        imports |= set(re.findall(r"^\s*(?:from|import) ([a-z_0-9]+)", open(f, encoding="utf-8").read(), re.M))
    local = {os.path.splitext(f)[0] for f in os.listdir(".") if f.endswith(".py")}
    third_party = imports - local - set(sys.stdlib_module_names)
    to_package = {"dotenv": "python-dotenv", "pytrends": "pytrends", "streamlit": None}
    for mod in third_party:
        pkg = to_package.get(mod, mod)
        if pkg:
            assert pkg in reqs, f"{mod} not in requirements.txt"


def test_gitignore_protects_secrets_and_local_data():
    ignore = open(".gitignore", encoding="utf-8").read()
    for entry in (".env", "data/*.db", "output/", "site/"):
        assert entry in ignore


def test_no_secret_like_values_committed():
    for f in glob.glob("*.py") + glob.glob("*.json") + glob.glob(".github/workflows/*.yml") + [".env.example"]:
        text = open(f, encoding="utf-8").read()
        assert not re.search(r"gsk_[A-Za-z0-9]{20,}|re_[A-Za-z0-9]{20,}|eyJ[A-Za-z0-9_-]{30,}", text), f


# ---- the whole chain over several days: stored history -> series -> weak signal -> e-mail -------------

async def test_volume_spike_reaches_the_daily_email(index):
    store = SqliteStore(":memory:")
    now = now_utc()
    arts = []
    for days_ago in range(9, 0, -1):
        count = 2 if days_ago > 1 else 7          # steady 2/day, then a 7-article day yesterday
        for i in range(count):
            arts.append(Article(f"Dior news {days_ago}-{i}", f"https://s/{days_ago}/{i}", f"S{i % 3}", "FR",
                                (now - timedelta(days=days_ago)).replace(hour=9).isoformat(), entities=["dior"],
                                category="Business & Finance", sentiment="Positif"))
    store.save_articles(arts)
    # pretend the pipeline has been collecting since 10 days ago (coverage starts the day after)
    store.conn.execute("UPDATE articles SET collected_at = ?", ((now - timedelta(days=10)).isoformat(),))
    store.conn.commit()

    signals = await main.refresh_signals(store, index, external=False)
    dior = next(s for s in signals if s["entity_id"] == "dior")
    hit = dior["hits"][0]
    assert dior["day"] == yesterday() and hit["metric"] == "articles" and hit["value"] == 7 and hit["ratio"] == 3.5

    _, html, text = build_daily(day=yesterday(), new_rows=[article_row(a) for a in arts[-3:]], clusters=[],
                                signals=signals, index=index)
    assert "×3.5" in html and "Dior" in html and "Dior" in text
