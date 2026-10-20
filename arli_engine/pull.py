"""An agent that calls the room instead of waiting to be called.

Why this exists. Most agents have no public address: they sit behind a router somebody else controls, and a
room cannot dial through it. Such an agent could register its areas and be listed, but never be reached, which
makes the listing a lie. This loop turns reachability inside out. The agent signs the room's nonce, registers,
and then holds a poll open; the room queues what it wants to ask and the agent answers on its next poll. The
exchange is the same exchange, with the same envelopes and the same witness rule. Only the direction of the
first packet changed.

What this does not claim. There is no address to test, so the proof of the key is the signature over the room's
nonce and nothing else. A room that wants an address can keep asking for one; a room that accepts this says so
in its own record and in its listing, which is the honest thing to do.

Against an agent that is already up:

    python -m arli_engine.pull --attach --room https://match.arliwork.com --port 8811

Or let it raise the agent itself:

    python -m arli_engine.pull --room https://match.arliwork.com --name my-agent \
      --features "offer:packaging" --key-file ~/.arli-match/my-agent.pem
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from arli_engine.adapters.a2a.server import start_agent
from arli_engine.agent import build_agent
from arli_engine.client import AgentClient

DEFAULT_ROOM = "https://match.arliwork.com"
DEFAULT_PORT = 8811
RETRY_SECONDS = 3


def _post(url: str, payload: dict, timeout: float = 120.0) -> dict:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    return json.loads(body) if body else {}


def _get(url: str, timeout: float = 60.0) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as response:
        if response.status == 204:
            return {}
        body = response.read()
    return json.loads(body) if body else {}


def register(room: str, local: AgentClient, *, kind: str, city: str, headline: str) -> dict:
    """Say hello, sign the room's nonce, and hand over the profile the agent publishes by itself."""
    card = local.get_card()
    hello = _post(f"{room}/api/pull/hello", {"card": card})
    nonce = str(hello.get("nonce") or "")
    if not nonce:
        raise RuntimeError(f"the room did not issue a nonce: {hello}")
    signed = local.call("arli/identity/sign", {"nonce": nonce, "purpose": str(hello.get("purpose") or "pull")})
    profile = local.call("arli/profile")
    return _post(f"{room}/api/pull/register", {
        "card": card, "nonce": nonce, "signature": signed.get("signature", ""),
        "kind": kind, "city": city, "headline": headline, "profile": profile,
    })


def serve(room: str, local: AgentClient, args) -> int:
    """Register, then answer whatever the room asks through the poll, until stopped."""
    while True:
        try:
            answer = register(room, local, kind=args.kind, city=args.city, headline=args.headline)
            break
        except urllib.error.HTTPError as error:
            print(f"registration refused: {error.code} {error.read()[:200].decode(errors='ignore')}")
            return 1
        except urllib.error.URLError as error:
            print(f"room not reachable ({error}); trying again")
            time.sleep(RETRY_SECONDS)
    if not answer.get("registered"):
        print(f"registration did not take: {answer}")
        return 1
    token = str(answer.get("pull_token") or "")
    print(f"registered as {answer.get('id')}: {(answer.get('checks') or {}).get('reachability')}")
    print(answer.get("note", ""))

    served = 0
    while True:
        try:
            call = _get(f"{room}/api/pull?pull_token={token}")
        except urllib.error.HTTPError as error:  # before URLError: it is a subclass of it
            if error.code == 403:
                print("the room no longer knows this token: registering again")
                return serve(room, local, args)
            print(f"poll refused ({error.code}); trying again")
            time.sleep(RETRY_SECONDS)
            continue
        except urllib.error.URLError as error:
            print(f"poll failed ({error}); trying again")
            time.sleep(RETRY_SECONDS)
            continue
        if not call:
            if args.once and served:
                print("one call answered, stopping as asked")
                return 0
            continue
        call_id, method = str(call.get("call_id") or ""), str(call.get("method") or "")
        try:
            result = local.call(method, call.get("params") or {})
            body = {"pull_token": token, "call_id": call_id, "result": result}
        except Exception as error:  # noqa: BLE001  a refusal must travel as a refusal
            body = {"pull_token": token, "call_id": call_id, "error": f"{type(error).__name__}: {error}"}
        try:
            _post(f"{room}/api/pull/answer", body)
        except urllib.error.URLError as error:
            print(f"answer could not be delivered ({error})")
        served += 1
        print(f"answered {method} ({served} so far)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register an agent that has no public address, and serve it.")
    parser.add_argument("--room", default=DEFAULT_ROOM, help="the room to register with")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="local port the agent listens on")
    parser.add_argument("--name", default="", help="the agent's name, when this loop raises the agent")
    parser.add_argument("--features", default="", help="comma separated key:value list, as everywhere else")
    parser.add_argument("--key-file", default="", help="the agent's key, when this loop raises the agent")
    parser.add_argument("--kind", default="supply", help="demand, supply or person")
    parser.add_argument("--city", default="", help="the city it states")
    parser.add_argument("--headline", default="", help="one line about it")
    parser.add_argument("--attach", action="store_true",
                        help="an agent is already running on --port: register that one, do not raise another")
    parser.add_argument("--once", action="store_true", help="register, answer one call, and stop")
    args = parser.parse_args(argv)

    features = [item.strip() for item in args.features.split(",") if item.strip()]
    room = args.room.rstrip("/")
    if args.attach:
        print(f"registering the agent already on 127.0.0.1:{args.port} with {room}")
        with AgentClient(f"http://127.0.0.1:{args.port}") as local:
            return serve(room, local, args)

    if not features:
        print("no list given: an agent with no items has nothing to match on", file=sys.stderr)
        return 2
    if args.key_file and not Path(args.key_file).exists():
        print(f"no key at {args.key_file}: run the installer, or point at the file it wrote", file=sys.stderr)
        return 2

    state = build_agent(args.name or "pull-agent", features, f"http://127.0.0.1:{args.port}",
                        private_pem=Path(args.key_file).read_bytes() if args.key_file else None)
    server, _thread = start_agent(state, args.port, host="127.0.0.1")
    print(f"agent on 127.0.0.1:{args.port}, registering with {room}")
    try:
        with AgentClient(f"http://127.0.0.1:{args.port}") as local:
            return serve(room, local, args)
    except KeyboardInterrupt:
        print("stopped")
        return 0
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
