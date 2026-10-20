# Adopting this

For somebody arriving to build on the extension or to check an implementation. Steps are real
commands with the output they produce; nothing here is aspirational.

## 1. Take the pieces

The normative text and the implementation live in separate repositories on purpose: the promise
changes on a different clock than the code.

```
git clone https://github.com/dmitrypehovski/arli-discovery-engine.git
git clone https://github.com/dmitrypehovski/arli-discovery.git
```

The engine reads the text only for its checks; point it at the clone if it is not a sibling:

```
export ARLI_SPEC_DIR="$PWD/arli-discovery"
```

Both are Apache-2.0. The name ARLI and the conformance mark are not, see `TRADEMARK.md`.

## 2. See it run before reading much

```
cd arli-discovery-engine
python -m pip install cryptography httpx pytest
python -m pytest tests -q                     # 171 passed, 2 skipped without the text
python -m arli_engine.serve --port-a 8841 --port-b 8842 &
python -m arli_engine.conformance --target http://127.0.0.1:8841 --holder http://127.0.0.1:8842
```

The suite prints 30 checks and one line: `conforms`, or the exact list of divergences with the
check codes. The suite is the promise; where it and the text disagree, the suite is right.

`python examples/two_agents/main.py` walks the whole protocol on two live agents and ends with
six evidence envelopes you can inspect.

## 3. The shape you are adopting

- An agent is an A2A server: a signed card at `/.well-known/agent-card.json` and a capability
  document at `/.well-known/arli-capabilities.json`, with every operation's effects declared.
- The surface is 26 methods, listed once in `arli_engine/core/surface.py`. Bindings (A2A over
  HTTP, MCP over streams, the plugin manifest) must expose exactly that list; the engine refuses
  to start if a binding disagrees with it.
- Matching has a declared floor: the caller says `floor_basis_points`, and a find below it is
  not returned rather than padded. `0` means "anything counts", and saying it out loud is the
  point.
- Every conclusion and every refusal carries an envelope. A refusal carries a code from a closed
  list, so a caller can tell "no match" from "the floor was not declared".

## 4. Check your own implementation

Point the suite at your agent:

```
python -m arli_engine.conformance --target https://your-agent.example --holder https://your-holder.example
```

Expect `conforms` or a per-check list. The suite contains deliberately wrong implementations and
fails them, so passing it means something. If a check fails, the code in the output is the name
of the rule you broke, and the text carries the same code.

## 5. What you get and what you do not

You get a matching layer with no index holder, signed evidence, closed code lists, and a suite
that can fail you. You do not get verified features: this implementation relays and verifies
other people's statements about features and issues none of its own, so until an issuer exists a
feature is only as good as the side declaring it. `SECURITY.md` says this and the other limits
without softening them.

## 6. Bringing changes back

Read `CONTRIBUTING.md`. In short: inbound equals outbound, a normative change edits both texts
and keeps clause numbers identical, a behavioural change adds a check plus a wrong fixture that
the check catches, and public-facing text stays English.
