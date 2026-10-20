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

### Known limits, stated rather than hidden

- Online probing of a single guess is not closed by the matching scheme. It is bounded by the
  one-exchange-per-session rule, and the honest wording is in the envelope limitations.
- The trust anchor for features (phase 5 of the plan) is not built: a feature is only as good
  as the side that declares it, and this version does not change that.
- The plugin manifest carries the interpreter path of the machine that generated it. That is
  correct for a local install and wrong for a redistributed artefact; a portable build is not
  done yet.
