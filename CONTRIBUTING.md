# Contributing

Contributions are welcome. Two rules keep the legal side simple and the text honest.

## Licence of a contribution

Inbound equals outbound. By submitting a contribution you agree that it is licensed under
the Apache License, Version 2.0, and you grant Shenzhen Capernaum Technology Co., Ltd. and
all recipients a perpetual, worldwide, non-exclusive, royalty-free licence to use,
reproduce, modify and distribute it. This mirrors the contribution terms the A2A project
uses for its own artefacts.

You represent that you have the legal authority to grant those rights and that the work is
yours or that you hold sufficient rights to submit it.

Sign off every commit, which records the same statement per commit:

```
git commit -s
```

## What a change must carry

- A normative change edits `spec/SPEC.en.md` and `spec/SPEC.md` together, keeps clause
  numbers identical in both, and bumps the version in `spec/CHANGELOG.md`.
- A behavioural change adds or updates a conformance check, and adds a deliberately wrong
  fixture that the check catches. A check that no wrong implementation can fail is void.
- A change that alters a published list of refusal codes updates the machine-readable
  document served at `/.well-known/arli-capabilities.json` in the same commit.
- Public-facing text stays in English. The Russian text is a working translation.

## What will be rejected

- Hand-written plugin manifests, generated files edited by hand.
- A promised behaviour with no check behind it, including honesty claims.
- A closed list that publishes a code no input can reach.

Run before sending:

```
.venv/bin/python -m pytest tests -q
.venv/bin/python -m arli_engine.conformance --target http://127.0.0.1:8841 --holder http://127.0.0.1:8842
```
