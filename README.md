# ARLI Discovery Extension

Discovery and matching for independent agents by declared intent, with verifiable evidence
and no index holder. An extension to A2A 1.0, with a normative specification, a conformance
suite and a reference implementation.

Status: **published under Apache-2.0**. Sources: https://github.com/dmitrypehovski/arli-discovery-engine,
mirrored on our own domain at https://arliwork.com/plugin/arli-discovery-engine.git. The normative text
lives separately, see below.

One honest note for a reader arriving today: the repository sits under a personal account,
because the company has no organisation on that platform yet. A transfer keeps the history and
the links. The code, the comments and the documentation are all in English.

## The idea in one paragraph

Matching today happens inside somebody's index: a middle party reads both intents and decides
who meets whom. This extension removes the middle party. Each side derives a pair key by
agreement, matching runs as a closed blind RSA intersection, a declared floor decides what
counts as a find, and every conclusion and every refusal travels in a signed evidence
envelope. What the caller learns is the intersection among its own features, not the shape of
the other side.

## What is here

- The normative text lives in a separate artefact, `../arli-spec`: `SPEC.en.md` (English,
  the published one), `SPEC.md` (Russian working translation, clause numbers kept identical)
  and `CHANGELOG.md`. The engine depends on that text and keeps no copy of it.
- `arli_engine/conformance`: the conformance suite, 30 checks, including deliberately wrong
  implementations that it catches.
- `examples/two_agents/main.py`: two live agents, a full round trip, a signature that breaks
  on an edit.

## Quick start

As a user of the package, without our repository:

```
pip install arli_engine-0.1.0-py3-none-any.whl
arli-discovery-pair --port-a 8841 --port-b 8842           # a pair, for the conformance suite
arli-discovery-agent --port 8811 --name my-agent --features "skill:brand-design,city:shenzhen"
arli-discovery-conformance --target http://127.0.0.1:8841 --holder http://127.0.0.1:8842
arli-discovery-client http://127.0.0.1:8841 arli/registry/check '{"kid": "nope"}'
```

The agent command prints both addresses and the exact conformance command for them. The MCP
binding runs as a process and speaks newline-delimited JSON-RPC:

```
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | arli-discovery-mcp arli-agent
```

From the repository, with the tests and the worked example:

```
.venv/bin/python -m pytest tests -q
.venv/bin/python examples/two_agents/main.py
.venv/bin/python tools/serve_pair.py 8841 8842
.venv/bin/python -m arli_engine.conformance --target http://127.0.0.1:8841 --holder http://127.0.0.1:8842
```

## What it deliberately does not do

- It does not rank the world. A smaller intersection is a smaller answer, not a worse one.
- It does not fill a result up to a limit, and it does not score anything with a constant.
- It does not treat silence as consent, and an agent's consent does not open a connection.
- It does not prove who is behind an agent. A signed card proves the publisher of the card,
  not a unique human.
- It does not close online probing of a single guess. Any set intersection answers for every
  element a side submits; that is closed by frequency rules, not by the scheme. The honest
  wording is in the envelope limitations, and the numbers are in `DECISIONS.md`.

## Licence

Apache License 2.0 for the specification text and the code, so that anyone may implement,
ship and sell an implementation. The name ARLI and the conformance mark are reserved, see
`TRADEMARK.md`. Contribution terms are in `CONTRIBUTING.md`.

Copyright 2026 Shenzhen Capernaum Technology Co., Ltd. (ARLI).
