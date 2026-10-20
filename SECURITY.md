# Security

This document states what the engine protects, what it does not, and how to report a problem.
It is written to be read by somebody deciding whether to trust the code, so the limits come
before the claims.

## What is protected, and against whom

- **The other side of a match.** Raw feature values never leave an agent. Matching runs as a
  blind RSA intersection, so the values a counterparty receives are signatures over its own
  submitted elements; without the key holder's private exponent they cannot be compared with
  guesses offline. A test in the suite checks this by trying to recover known features.
- **The number of features on the other side.** It is not disclosed anywhere, and a test fails
  if a field carrying it appears.
- **The pair key.** It is derived by ECDH and HKDF from both agent identifiers and the session,
  and never travels on the wire. A test recovers the whole feature set if a key is passed as a
  field, which is why the field does not exist.
- **Evidence.** Every conclusion and refusal carries an envelope whose identifier recomputes from
  its body and whose signature is checked against the issuer. Editing the body breaks the check.
- **A card's publisher.** The card is signed with JWS ES256 and carries `jwk`, so a reader can
  verify it without a side channel.

## What is not protected

- **Online probing of a single guess.** Any set intersection answers for every element a side
  submits. The scheme does not close that; the one-exchange-per-session rule bounds it. This is
  stated in the envelope limitations and is not a defect to be argued away.
- **Who is behind an agent.** A signed card proves the publisher of the card, not a person. The
  claim levels (`declared`, `third_party_verified`, `topic_owner_verified`) carry an issuer's
  envelope, and this implementation verifies and relays such statements; it issues none of its
  own. Until somebody issues them, a feature is only as good as the side declaring it.
- **Revocation across a network.** The key registry is local to one agent. A network-wide
  revocation needs a shared root of trust, which does not exist yet.
- **Traffic analysis.** Who talks to whom, and when, is visible to whoever carries the traffic.
- **Third-party review.** No external security review has been done. Do not read the tests as
  one: they check the properties the authors thought of.

## Known sharp edges

- The cryptography runs on Python big integers, so an exchange is slow: the measured numbers are
  in `DECISIONS.md`, and an implementation in OpenSSL would be roughly an order of magnitude
  faster. Slow here means seconds, not hours, at the documented feature cap of 64.
- The local stand (`arli_engine.serve`) keeps keys in memory per process and is not a service.
  It exists so a conformance suite has something to check.
- The plugin manifest carries the interpreter path of the machine that generated it, which is
  right for a local install and wrong for a redistributed artefact.

## Reporting

Open an issue, or write to hq@arliwork.com for anything that should not be public. We would
rather hear about a hole than not. There is no bug bounty programme at this time, and this
document does not promise one.
