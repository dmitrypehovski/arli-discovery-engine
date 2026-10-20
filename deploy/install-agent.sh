#!/bin/sh
# ARLI Match: give your agent the ability to join the room, in one line.
#
#   curl -fsSL https://arliwork.com/install-agent.sh | sh -s -- \
#       --name my-agent --features "need:warehouse,city:shenzhen"
#
# What it does, in order: puts the engine in ~/.arli-match/engine, starts your agent with the list
# you passed, and if it can reach the room over a public address (either one you give it with
# --address, or a tunnel it opens itself), it connects and prints what was checked.
#
# It never touches anything outside ~/.arli-match, and it prints every command before using it.

set -eu

HOME_DIR="${ARLI_MATCH_HOME:-$HOME/.arli-match}"
ENGINE_DIR="$HOME_DIR/engine"
REPO="https://arliwork.com/plugin/arli-discovery-engine.git"
ROOM="https://match.arliwork.com"
PORT="${ARLI_MATCH_PORT:-8811}"
NAME=""
FEATURES=""
ADDRESS=""
NO_TUNNEL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --features) FEATURES="$2"; shift 2 ;;
    --address) ADDRESS="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --no-tunnel) NO_TUNNEL=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

say "ARLI Match installer"
say "  home: $HOME_DIR"

if ! have python3; then
  say "python3 is required. Install it, then run this again." >&2
  exit 1
fi
if ! have git; then
  say "git is required. Install it, then run this again." >&2
  exit 1
fi

mkdir -p "$HOME_DIR"
if [ -d "$ENGINE_DIR/.git" ]; then
  say "  engine: updating"
  git -C "$ENGINE_DIR" pull --quiet --ff-only || say "  engine: kept the copy already there"
else
  say "  engine: cloning into $ENGINE_DIR"
  git clone --quiet --depth 1 "$REPO" "$ENGINE_DIR"
fi

if [ ! -x "$ENGINE_DIR/.venv/bin/python" ]; then
  say "  python: creating a private environment"
  python3 -m venv "$ENGINE_DIR/.venv"
fi
say "  engine: installing"
"$ENGINE_DIR/.venv/bin/python" -m pip install --quiet --upgrade pip
"$ENGINE_DIR/.venv/bin/python" -m pip install --quiet -e "$ENGINE_DIR"

if [ -z "$NAME" ]; then
  NAME="$(hostname 2>/dev/null || echo arli-agent)"
fi
if [ -z "$FEATURES" ]; then
  say ""
  say "What is this agent looking for or offering? Short lines, comma separated."
  say "Examples: need:warehouse,city:shenzhen     skill:brand-design,offer:packaging"
  printf 'features: '
  read -r FEATURES || true
fi
if [ -z "$FEATURES" ]; then
  say "No list given, so there is nothing to match on. Run again with --features." >&2
  exit 1
fi

say "  agent: starting $NAME on port $PORT"
"$ENGINE_DIR/.venv/bin/python" -m arli_engine.agent \
  --port "$PORT" --name "$NAME" --features "$FEATURES" \
  --key-file "$HOME_DIR/$NAME.pem" > "$HOME_DIR/$NAME.log" 2>&1 &
AGENT_PID=$!
say "  agent: pid $AGENT_PID, log at $HOME_DIR/$NAME.log"

i=0
while [ $i -lt 20 ]; do
  if "$ENGINE_DIR/.venv/bin/python" - "$PORT" <<'PY' >/dev/null 2>&1
import sys, urllib.request
urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/health", timeout=2).read()
PY
  then break; fi
  i=$((i + 1))
  sleep 0.5
done

if [ "$i" -ge 20 ]; then
  say "The agent did not come up. See $HOME_DIR/$NAME.log" >&2
  exit 1
fi
say "  agent: answering on http://127.0.0.1:$PORT"

TUNNEL_PID=""
if [ -z "$ADDRESS" ] && [ "$NO_TUNNEL" -eq 0 ]; then
  if have cloudflared; then
    say "  address: opening a temporary tunnel (cloudflared)"
    cloudflared tunnel --url "http://127.0.0.1:$PORT" > "$HOME_DIR/$NAME.tunnel.log" 2>&1 &
    TUNNEL_PID=$!
    i=0
    while [ $i -lt 40 ]; do
      ADDRESS="$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$HOME_DIR/$NAME.tunnel.log" 2>/dev/null | head -1 || true)"
      [ -n "$ADDRESS" ] && break
      i=$((i + 1))
      sleep 0.5
    done
    if [ -n "$ADDRESS" ]; then
      say "  address: $ADDRESS (lasts as long as this machine and this tunnel run)"
    else
      say "  address: the tunnel did not report an address in time"
    fi
  else
    say "  address: no cloudflared found, so no public address yet"
  fi
fi

if [ -n "$ADDRESS" ]; then
  # A tunnel reports its address before its edge can route to it. Waiting for the card to answer
  # over the public address is the difference between "connected" and a confusing refusal.
  say ""
  say "  address: waiting until the card answers through $ADDRESS"
  i=0
  while [ $i -lt 40 ]; do
    if "$ENGINE_DIR/.venv/bin/python" - "$ADDRESS" <<'PY' >/dev/null 2>&1
import sys, urllib.request
urllib.request.urlopen(f"{sys.argv[1]}/.well-known/agent-card.json", timeout=5).read()
PY
    then break; fi
    i=$((i + 1))
    sleep 1
  done
  if [ "$i" -ge 40 ]; then
    say "  address: the public address never served the card. Check the tunnel, then paste the address at $ROOM"
    exit 1
  fi
  say "  address: the card answers publicly"
  say ""
  say "Connecting $ADDRESS to the room"
  "$ENGINE_DIR/.venv/bin/python" - "$ROOM" "$ADDRESS" "$NAME" <<'PY' || say "Connect failed; paste the address at $ROOM yourself."
import json, sys, urllib.request
room, address, name = sys.argv[1], sys.argv[2], sys.argv[3]
body = json.dumps({"endpoint": address, "kind": "supply", "headline": f"agent {name}, joined in one line"}).encode()
request = urllib.request.Request(f"{room}/api/connect", data=body, headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(request, timeout=40) as answer:
        data = json.load(answer)
except urllib.error.HTTPError as error:
    # The service says which check failed; printing a bare status code hides the only useful part.
    try:
        body = json.load(error)
        print(f"  refused: {body.get('error')} {body.get('detail', '')}")
    except Exception:
        print(f"  refused: HTTP {error.code}")
    raise SystemExit(1)
except Exception as error:
    print(f"  failed: {type(error).__name__} {error}")
    raise SystemExit(1)
if data.get("connected"):
    print(f"  connected: {data.get('name')}")
    for check, value in (data.get("checks") or {}).items():
        print(f"    {'yes' if value else 'no '}  {check}")
    print(f"  it is in the room: {room}")
else:
    print(f"  refused: {data.get('error')} {data.get('detail', '')}")
    raise SystemExit(1)
PY
else
  say ""
  say "Your agent is running at home but has no public address, so the room cannot reach it yet."
  say "Two ways to finish:"
  say "  a small server with TLS, or a tunnel:  cloudflared tunnel --url http://127.0.0.1:$PORT"
  say "then paste that public address at $ROOM"
fi

say ""
say "Your list stayed on this machine: $HOME_DIR/$NAME.pem is the agent's key."
say "Keep it running while you want to be found. Stop it with: kill $AGENT_PID"
if [ -n "$TUNNEL_PID" ]; then
  say "Stop the tunnel with: kill $TUNNEL_PID"
fi
