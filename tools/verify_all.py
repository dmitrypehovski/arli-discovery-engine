"""Check everything we claimed, in one command, and print a verdict.

Why this exists. "It works" is a claim until somebody runs it. The pieces of this product are verified in
different ways — a test suite, a conformance kit, a stand with two processes, a restart, the live domain — and each
of those lived in a chat message. This runs them in order, takes a number from each, and says pass, fail or
skipped-with-a-reason. Anything it cannot check is reported as skipped rather than counted as fine.

Run:

    python -m tools.verify_all              # everything, several minutes
    python -m tools.verify_all --fast       # skip the two test suites, keep the rest
    python -m tools.verify_all --only live  # one group; groups: engine, spec, conformance, road, durability, live, install
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
SPEC = HERE.parent / "arli-spec"
RUST = HERE.parent / "arli-engine-rs" / "target" / "release" / "arli-discovery-rs"
PY = sys.executable
ROOM = "https://match.arliwork.com"
TAXONOMY = "/llm-gateway/skill_taxonomy_v2.json"

results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool | None, detail: str) -> None:
    results.append((name, "pass" if ok is True else ("fail" if ok is False else "skipped"), detail))
    mark = {"pass": "ok  ", "fail": "FAIL", "skipped": "skip"}[results[-1][1]]
    print(f"  [{mark}] {name}: {detail}")


def run(command: list[str], cwd: Path | None = None, timeout: int = 900, env: dict | None = None
        ) -> tuple[int, str]:
    finished = subprocess.run(command, cwd=str(cwd or HERE), capture_output=True, text=True, timeout=timeout,
                              env=env or os.environ.copy())
    return finished.returncode, finished.stdout + finished.stderr


def kill_leftovers(*needles: str) -> None:
    me, parent = os.getpid(), os.getppid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) in (me, parent):
            continue
        try:
            cmdline = open("/proc/%s/cmdline" % pid, "rb").read().decode(errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        if any(needle in cmdline for needle in needles):
            try:
                os.kill(int(pid), signal.SIGTERM)
            except Exception:  # noqa: BLE001
                pass


# --- groups --------------------------------------------------------------------------------------------

def check_engine(fast: bool) -> None:
    if fast:
        record("engine tests", None, "skipped by --fast; run without it to check all of them")
        return
    code, out = run([PY, "-m", "pytest", "tests", "-q"], timeout=1200)
    found = re.search(r"(\d+) passed", out)
    record("engine tests", code == 0 and bool(found),
           f"{found.group(1) if found else '?'} passed" if code == 0 else out.strip()[-300:])


def check_spec(fast: bool) -> None:
    code, out = run([PY, "-m", "pytest", "tests", "-q"], cwd=SPEC, timeout=300)
    found = re.search(r"(\d+) passed", out)
    record("spec text", code == 0 and bool(found),
           f"{found.group(1) if found else '?'} passed" if code == 0 else out.strip()[-300:])


def check_conformance() -> None:
    if not RUST.exists():
        record("conformance", None, f"no Rust binary at {RUST}; build it to check both implementations")
        return
    env = os.environ.copy()
    env["PYTHONPATH"] = str(HERE)
    env["ARLI_TAXONOMY"] = TAXONOMY
    started = [
        subprocess.Popen([str(RUST), "--port", "8931", "--name", "rs-holder",
                          "--features", "offer:irrigation,offer:packaging"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen([str(RUST), "--port", "8932", "--name", "rs-peer",
                          "--features", "offer:irrigation,offer:packaging"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen([PY, "-m", "arli_engine.serve", "--port-a", "8941", "--port-b", "8942"],
                         cwd=str(HERE), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]
    time.sleep(8)
    pairs = [("Rust judging Rust", 8932, 8931), ("Python judging Python", 8942, 8941),
             ("Python judging Rust", 8942, 8931), ("Rust judging Python", 8932, 8941)]
    try:
        for label, target, holder in pairs:
            code, out = run([PY, "-m", "arli_engine.conformance",
                             "--target", f"http://127.0.0.1:{target}",
                             "--holder", f"http://127.0.0.1:{holder}"], timeout=400)
            found = re.search(r"total checks (\d+), mismatches (\d+)", out)
            if not found:
                record(f"conformance: {label}", False, out.strip()[-200:])
                continue
            total, bad = int(found.group(1)), int(found.group(2))
            record(f"conformance: {label}", bad == 0 and code == 0, f"{total - bad} of {total}, {bad} mismatches")
    finally:
        for process in started:
            process.terminate()
        time.sleep(1)
        kill_leftovers("--port 8931", "--port 8932", "--port-a 8941")


def _room(port: int, db: str) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(PYTHONPATH=str(HERE), ARLI_TAXONOMY=TAXONOMY, ARLI_MATCH_ROOM_MODE="real",
               ARLI_MATCH_DB=db, ARLI_MATCH_ALLOW_PRIVATE="1")
    return subprocess.Popen([PY, "-u", "-m", "app.service", "--port", str(port)], cwd=str(HERE), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _json(url: str, payload: dict | None = None, timeout: int = 240, method: str | None = None) -> tuple[int, dict]:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method or ("POST" if body else "GET"),
                                     headers={"Content-Type": "application/json"} if body else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read())
        except Exception:  # noqa: BLE001
            return error.code, {}


def check_road() -> None:
    """A person and an agent with no address: registered by calling, found, introduced through its own poll."""
    db, key = "/tmp/verify-road.sqlite3", "/tmp/verify-road.pem"
    for path in (db, key, "/tmp/verify-road-agent.log", "/tmp/verify-road-pull.log"):
        if os.path.exists(path):
            os.remove(path)
    from arli_engine.core.signing import KeyPair

    open(key, "w").write(KeyPair.generate().private_pem.decode())
    env = os.environ.copy()
    env.update(PYTHONPATH=str(HERE), ARLI_TAXONOMY=TAXONOMY,
               ARLI_DECLARED_AREAS="manufacturing-and-production")
    room = _room(8951, db)
    time.sleep(4)
    agent = loop = None
    try:
        agent = subprocess.Popen([PY, "-u", "-m", "arli_engine.agent", "--port", "8952",
                                  "--name", "verify-no-address", "--features", "offer:packaging",
                                  "--key-file", key], cwd=str(HERE), env=env,
                                 stdout=open("/tmp/verify-road-agent.log", "w"), stderr=subprocess.STDOUT)
        time.sleep(3)
        loop = subprocess.Popen([PY, "-u", "-m", "arli_engine.pull", "--attach", "--room",
                                 "http://127.0.0.1:8951", "--port", "8952"], cwd=str(HERE), env=env,
                                stdout=open("/tmp/verify-road-pull.log", "w"), stderr=subprocess.STDOUT)
        for _ in range(25):
            time.sleep(1)
            if "registered as" in Path("/tmp/verify-road-pull.log").read_text():
                break
        registered = "registered as" in Path("/tmp/verify-road-pull.log").read_text()
        record("no-address agent registers by calling", registered,
               "the room cannot dial it and does not need to" if registered else "see /tmp/verify-road-pull.log")

        import http.cookiejar
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        base = "http://127.0.0.1:8951"

        def call(path: str, payload: dict | None = None) -> tuple[int, dict]:
            body = json.dumps(payload).encode() if payload is not None else None
            request = urllib.request.Request(base + path, data=body,
                                             method="POST" if body else "GET",
                                             headers={"Content-Type": "application/json"} if body else {})
            try:
                with opener.open(request, timeout=240) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                try:
                    return error.code, json.loads(error.read())
                except Exception:  # noqa: BLE001
                    return error.code, {}

        call("/api/app/register", {"email": "verify@example.com", "password": "verify-password"})
        code, made = call("/api/app/intake", {"text": "We need packaging and warehousing near shenzhen"})
        features = made.get("read", {}).get("features", [])
        record("words become an agent", code == 200 and bool(features), f"{len(features)} items from one sentence")

        code, scan = call("/api/scan", {"my_features": features, "floor_basis_points": 3000, "my_kind": "demand"})
        rows = [row for row in scan.get("rows", []) if row.get("id") == "verify-no-address"]
        recorded = bool(rows) and rows[0].get("opened") is True and rows[0].get("askable") is True
        record("the no-address agent is found and askable", recorded,
               f"overlap {rows[0].get('overlap_basis_points')} bps, pull {rows[0].get('pull')}" if rows else "not offered")

        code, intro = call("/api/app/introductions", {"peer_id": "verify-no-address", "note": "we match on packaging"})
        introduced = code == 200 and intro.get("state") == "introduced" and intro.get("reached_by") == "poll"
        record("introduction travels through its own poll", introduced,
               f"state {intro.get('state')}, reached by {intro.get('reached_by')}")

        code, terms = call("/api/app/introductions/%s/agreement" % intro.get("id"),
                           {"subject": "Packaging and warehousing", "amount_minor": 1200000,
                            "currency": "CNY", "deliver_by": "2026-12-01"})
        record("terms can be put on the table", code == 200 and terms.get("state") == "proposed",
               f"state {terms.get('state')}; the other side signs from its own interface, not ours")
    finally:
        for process in (loop, agent, room):
            if process:
                process.terminate()
        time.sleep(1)
        kill_leftovers("--port 8952", "--port 8951")


def check_durability() -> None:
    """A room restart must not cost the registration, and must not kill the agent's poll."""
    db, key = "/tmp/verify-durable.sqlite3", "/tmp/verify-durable.pem"
    for path in (db, key, "/tmp/verify-durable-pull.log"):
        if os.path.exists(path):
            os.remove(path)
    from arli_engine.core.signing import KeyPair

    open(key, "w").write(KeyPair.generate().private_pem.decode())
    env = os.environ.copy()
    env.update(PYTHONPATH=str(HERE), ARLI_TAXONOMY=TAXONOMY,
               ARLI_DECLARED_AREAS="manufacturing-and-production")
    room = _room(8961, db)
    time.sleep(4)
    agent = loop = None
    try:
        agent = subprocess.Popen([PY, "-u", "-m", "arli_engine.agent", "--port", "8962",
                                  "--name", "verify-durable", "--features", "offer:packaging",
                                  "--key-file", key], cwd=str(HERE), env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)
        loop = subprocess.Popen([PY, "-u", "-m", "arli_engine.pull", "--attach", "--room",
                                 "http://127.0.0.1:8961", "--port", "8962"], cwd=str(HERE), env=env,
                                stdout=open("/tmp/verify-durable-pull.log", "w"), stderr=subprocess.STDOUT)
        for _ in range(25):
            time.sleep(1)
            if "registered as" in Path("/tmp/verify-durable-pull.log").read_text():
                break

        def found() -> tuple[bool, str]:
            code, scan = _json("http://127.0.0.1:8961/api/scan",
                               {"my_features": ["need:packaging"], "floor_basis_points": 3000, "my_kind": "demand"})
            rows = [row for row in scan.get("rows", []) if row.get("id") == "verify-durable"]
            if not rows:
                return False, "not offered"
            return rows[0].get("opened") is True, f"basis {rows[0].get('overlap_basis_points')}"

        before, detail = found()
        room.terminate()
        room.wait(timeout=30)
        time.sleep(1)
        room = _room(8961, db)
        time.sleep(6)
        after, detail_after = found()
        log = Path("/tmp/verify-durable-pull.log").read_text()
        record("registration survives a room restart", before and after,
               f"before: {detail}; after: {detail_after}")
        record("the agent did not have to register again", log.count("registered as") == 1,
               f"{log.count('registered as')} registration(s) in its log, loop alive: {loop.poll() is None}")
    finally:
        for process in (loop, agent, room):
            if process:
                process.terminate()
        time.sleep(1)
        kill_leftovers("--port 8962", "--port 8961")


def check_live() -> None:
    for path, what in ((f"{ROOM}/", "public page"), (f"{ROOM}/app", "product page"),
                       (f"{ROOM}/spec/", "specification page"), (f"{ROOM}/privacy", "storage page"),
                       (f"{ROOM}/join", "join page"), (f"{ROOM}/room", "room listing page"),
                       (f"{ROOM}/api/health", "health")):
        try:
            with urllib.request.urlopen(path, timeout=30) as response:
                body = response.read().decode(errors="ignore")
            ok = response.status == 200
            record(f"live: {what}", ok, f"{response.status}")
            if what == "storage page":
                record("live: the storage page states the claim", "nothing about the asker" in body,
                       "a search stores nothing is written down" if "nothing about the asker" in body else "missing")
            if what == "specification page":
                version = re.search(r"Discovery Extension v([\d.]+)", body)
                record("live: specification version", bool(version),
                       f"v{version.group(1)}" if version else "no version on the page")
            if what == "public page":
                record("live: the page offers the skill", "plugin marketplace add" in body,
                       "the skill line is on the page" if "plugin marketplace add" in body else "missing")
                record("live: the page links to what the room keeps", "/privacy" in body,
                       "the storage page is linked" if "/privacy" in body else "not linked")
        except Exception as error:  # noqa: BLE001
            record(f"live: {what}", False, f"{type(error).__name__}: {error}")

    code, listing = _json(f"{ROOM}/api/listing")
    published = json.dumps(listing)
    record("live: the room listing separates participants from examples", code == 200 and "examples" in listing,
           f"hosted {listing.get('counts', {}).get('hosted')}, connected {listing.get('counts', {}).get('connected')}, "
           f"examples {listing.get('counts', {}).get('examples')}")
    record("live: the listing publishes no foreign address", "endpoint" not in published
           and "trycloudflare" not in published,
           "a list of addresses would be a target list" if "endpoint" not in published else "an address is in it")

    code, unknown = _json(f"{ROOM}/api/join/check", {"agent": "no-agent-with-this-name"})
    record("live: the join check says no to an unknown name", code == 200 and unknown.get("in_room") is False,
           "an unknown name is not in the room" if unknown.get("in_room") is False else str(unknown)[:120])

    code, known = _json(f"{ROOM}/api/join/check", {"agent": "my-agent"})
    record("live: the join check tells a joined agent how it is reached",
           code == 200 and known.get("in_room") is True and known.get("pull") is True,
           f"{known.get('name')}: {known.get('how')} ({known.get('buckets')} areas)" if known.get("in_room")
           else str(known)[:120])

    code, health = _json(f"{ROOM}/api/health")
    record("live: the room answers with counts", code == 200,
           f"examples {health.get('sample_agents')}, registered {health.get('registered_agents')}, "
           f"connected {health.get('connected_agents')}")

    code, scan = _json(f"{ROOM}/api/scan", {"my_features": ["need:packaging"], "floor_basis_points": 3000,
                                            "my_kind": "demand"})
    pull_rows = [row for row in scan.get("rows", []) if row.get("remote") and row.get("pull")]
    record("live: a search sifts and names what it skipped", code == 200 and scan.get("remote_sifted") is not None,
           f"judged {scan.get('remote_sifted')}, skipped {scan.get('remote_skipped_by_bucket')} "
           f"{scan.get('skipped_ids')}, asked {scan.get('remote_unsifted')}")
    if pull_rows:
        record("live: a no-address agent is reachable and marked", bool(pull_rows[0].get("askable")),
               f"{pull_rows[0]['id']}: pull, askable, basis {pull_rows[0].get('overlap_basis_points')}")


def check_install() -> None:
    for url, what in (("https://arliwork.com/plugin/arli-discovery-engine.git", "engine to install"),
                      ("https://arliwork.com/plugin/arli-skills.git", "skill repository")):
        code, out = run(["git", "ls-remote", url, "refs/heads/main"], timeout=120)
        commit = out.strip().split("\t")[0][:7] if out.strip() else ""
        record(f"install: {what}", code == 0 and bool(commit), f"public at {commit}" if commit else out[-150:])
    try:
        with urllib.request.urlopen("https://arliwork.com/install-agent.sh", timeout=30) as response:
            script = response.read().decode(errors="ignore")
        record("install: the one-line script offers both doors", "ARLI_DECLARED_AREAS" in script and "pull" in script,
               "declared areas and the pull door are in it")
    except Exception as error:  # noqa: BLE001
        record("install: the one-line script", False, f"{type(error).__name__}: {error}")


def check_ops() -> None:
    """The things that are nobody's feature and everybody's outage: a copy of the store, and services that return."""
    import glob
    import sqlite3

    copies = sorted(glob.glob("/var/backups/arli-match/match-*.sqlite3"))
    record("a copy of the room's store exists", bool(copies), copies[-1] if copies else "no copies at all")
    if copies:
        try:
            connection = sqlite3.connect(copies[-1])
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            age_hours = int((time.time() - os.path.getmtime(copies[-1])) / 3600)
            record("the newest copy opens and is current", bool(tables) and age_hours < 72,
                   f"{len(tables)} tables, copied {age_hours}h ago")
        except Exception as error:  # noqa: BLE001
            record("the newest copy opens and is current", False, f"{type(error).__name__}: {error}")

    code, out = run(["systemctl", "is-enabled", "arli-match-backup.timer"], timeout=30)
    record("the daily copy is scheduled", out.strip() == "enabled", out.strip() or "not enabled")
    for unit in ("arli-match", "nginx", "arli-platform"):
        code, out = run(["systemctl", "is-enabled", unit], timeout=30)
        active = run(["systemctl", "is-active", unit], timeout=30)[1].strip()
        record(f"{unit} returns after a reboot", out.strip() == "enabled", f"enabled, active: {active}")


GROUPS = {"ops": lambda fast: check_ops(), "engine": lambda fast: check_engine(fast), "spec": lambda fast: check_spec(fast),
          "conformance": lambda fast: check_conformance(), "road": lambda fast: check_road(),
          "durability": lambda fast: check_durability(), "live": lambda fast: check_live(),
          "install": lambda fast: check_install()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check everything this product claims, and print a verdict.")
    parser.add_argument("--fast", action="store_true", help="skip the two test suites")
    parser.add_argument("--only", default="", help="one group: " + ", ".join(GROUPS))
    args = parser.parse_args(argv)
    chosen = [args.only] if args.only else list(GROUPS)
    unknown = [name for name in chosen if name not in GROUPS]
    if unknown:
        print(f"unknown group: {unknown[0]}. Groups: {', '.join(GROUPS)}", file=sys.stderr)
        return 2

    started = time.time()
    for name in chosen:
        print(f"\n== {name}")
        try:
            GROUPS[name](args.fast)
        except Exception as error:  # noqa: BLE001
            record(name, False, f"the check itself failed: {type(error).__name__}: {error}")

    passed = sum(1 for _, state, _ in results if state == "pass")
    failed = [item for item in results if item[1] == "fail"]
    skipped = [item for item in results if item[1] == "skipped"]
    print(f"\n== verdict: {passed} passed, {len(failed)} failed, {len(skipped)} skipped "
          f"in {time.time() - started:.0f}s")
    for name, _, detail in failed:
        print(f"   failed: {name}: {detail}")
    for name, _, detail in skipped:
        print(f"   skipped: {name}: {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())