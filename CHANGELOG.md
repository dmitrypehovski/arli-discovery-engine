# Changelog

Two version numbers live here and they mean different things.

- **Package version** is the version of the code: the engine, the bindings, the conformance
  suite. It moves when code moves.
- **Extension version** is the version of the normative text: what an implementation must do
  to be answerable as conforming. It moves when the promise moves.

They are not required to match, and they do not match today: the text is at 1.0.0 while the
code is at 0.1.0 and walks there in steps. Merging them into one number would misstate one of
the two.

The extension history itself is in `spec/CHANGELOG.md`.

## 0.1.0, not published

First version of the package. Nothing has been published to any index: no registry entry, no
public repository, no licence applied to a public copy. Publication is a deliberate step and
it waits for the legal check in `spec/PUBLISHING.md`.

### Added

- Engine core with no transport: intent with types, statuses and provenance, capabilities with
  declared effects, pair key agreement (ECDH P-256 with HKDF-SHA256), matching with a declared
  floor and closed blind RSA intersection, signed evidence envelopes, the negotiation grammar,
  consent, retention with marking and purging, and a key registry with rotation separate from
  principal revocation.
- A2A binding over HTTP JSON-RPC.
- MCP binding over the standard stream transport, one tool per method.
- Plugin manifest generated from the surface, never hand-written.
- Client wrapper for Python, one method per surface method, refusal codes preserved.
- Command line client: `python -m arli_engine.client.cli`.
- Conformance suite, 26 checks, including deliberately wrong implementations that it catches.
- Normative specification in English (`spec/SPEC.en.md`) with a Russian working translation,
  clause numbers kept identical by a test.

### Fixed

- Card signatures are now the raw R||S pair that JWS ES256 defines, not the DER form. Our own
  verifier accepted the DER form because both sides agreed with each other rather than with the
  text; a third party verifying our cards by the JWS rules would have rejected them. Found by the
  second implementation in Rust, not by us. Card verification now has a single path, and a DER
  signature is refused with a reason that says why.
- The plugin manifest is portable: it names a console script instead of the interpreter path of
  the machine that built it, so the artefact built here and the one built by CI agree. Caught by
  the CI run itself.
- The envelope identifier is recomputable from the nine body fields on both sides.

### Known limits, stated rather than hidden

- Online probing of a single guess is not closed by the matching scheme. It is bounded by the
  one-exchange-per-session rule, and the honest wording is in the envelope limitations.
- The trust anchor for features (phase 5 of the plan) is not built: a feature is only as good
  as the side that declares it, and this version does not change that.

### Changed

- `arli-discovery-agent` now runs one agent with `--port`, `--name`, `--features` and keeps its key
  in `~/.arli-agent/NAME.pem` (owner-only), so an address keeps its identity across restarts. The
  pair of agents used by the conformance suite moved to `arli-discovery-pair`. A participant needs
  one agent, and the old command gave them two with a key that died with the process.
- The card names the extension version in its `extensions` entry, as the second implementation and
  the independent example already did: a visitor can tell which text an agent speaks without asking.
- `GET /api/health` answers on every agent: `ok`, the agent's name, the extension version, the count
  of features held at home and the key id. A run can tell "up" from "not up" without a card fetch.
- `start_agent` takes a bind address, because a container has to bind `0.0.0.0` while everything
  local stays on the loopback.

### Added

- `examples/independent_agent.py`: the holder side of a match written from the text alone, importing
  nothing from this engine. It connected to the live service, passed all three checks and matched two
  of four features at 5000 basis points with nothing on the wire. Six tests keep it honest, including
  one that fails if the file ever imports our code.
- `docs/host-your-agent.md`: what a participant's address has to serve, a unit file, TLS in front,
  tunnel caveats, and the failures a host will actually see.
