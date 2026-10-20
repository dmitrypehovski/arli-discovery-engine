# Where the Internet Computer actually helps us, and where it does not

Written because the question was asked twice, and because the tempting answer ("put it all on the IC, it is a
chain") would cost a rewrite and buy nothing. The useful answer is narrower and names three places, one of
which is the ownership question.

## The one-line verdict

Put the **record** and the **identity of owners** on the IC, keep the **matching** where it is. A canister is a
good notary and a bad calculator.

## Where it helps, with the reason

### 1. The agreement record, verifiable by someone who does not trust us

Today a stranger holding an agreement id can check both signatures, and that check runs on our machine: we
recompute the bytes and print whether they hold. The cryptography is honest, the *venue* is ours. A canister
with certified data removes that last dependence: the terms and the signatures are written in an update call,
the canister certifies a Merkle root in the subnet state, and a query answers with a certificate signed by the
subnet's threshold key. The reader verifies the response against the IC root key and the freshness field, and
the words "check it yourself" stop meaning "ask our server to check it".

That is the same claim our product already makes about evidence, made stronger. It is the single best fit for
a canister in this product.

### 2. Ownership and revocation: the registrar you asked about

Yes, this one is worth having, for one reason: **ownership is the only thing in our system that is still
purely our word.**

What we have today:

- An agent proves it holds a key, at an address, by signing a nonce we choose. That proves *reachability and
  possession*, not ownership: a proxy forwarding the signature passes, which the code says out loud.
- The owner of an agent registered here is a row in our SQLite. A stranger cannot check it, and a revocation
  is our announcement rather than a fact anyone can verify.
- Our registry of keys and principals (rotation, revocation reasons) is also local to each deployment.

A canister fixes exactly that: `agent id -> owner principal`, plus `revoked at, reason`, written once and
readable with a certificate. Then:

- an owner is an **Internet Identity principal**, which is a passkey, not a password we hold. That also removes
  the weakest part of our current accounts, where a private key and a vault sit in our store because there is
  nowhere else for them to sit;
- revocation becomes public and neutral: a third party can check that an agent was revoked, and when, without
  asking us;
- an auditor, a bank or another network can check ownership of a counterparty before dealing with it.

What it does **not** do, and this matters more than what it does:

- It does not replace the address proof. Reachability at an address is a live property; a canister cannot
  answer a nonce on behalf of an agent that has to serve A2A over HTTP.
- It does not make an agent "ours" or "safe". It records who claims it, not what it does.
- It does not remove the need for the key. An ICP principal and an A2A agent key are two different things, and
  the registrar has to store the binding between them, which means the binding itself becomes a claim we
  record rather than a fact the network enforces.

### 3. Accounts, if we ever open a public door

Internet Identity gives a passkey login with no email, no password, and no password reset to defend. If a
person ever signs into our product from a browser, that is the login to use, and it pairs with the registrar
above because an owner principal is the same thing in both places.

## Where it does not help

- **The matching itself.** One exchange is RSA blind-signature work, about 800 ms at thirty two items, and the
  measured cost is CPU. A canister bills compute in cycles with per-message instruction limits and no cheap
  long-running background work, so moving the exchange there would trade a measured 800 ms for an unknown and
  probably worse number, and would rewrite a working engine in Rust or Motoko. The matching stays ours.
- **The index.** A million bucket profiles is a lookup table we can shard anywhere; a canister gives us
  replication we do not need at a price we would pay in cycles for every query.
- **The intake.** It is a reader with a taxonomy. It needs no consensus.

## The price, in units that matter

- A trillion cycles is about one dollar. A small canister (a few megabytes, low traffic, a query a second)
  costs cents a month; the number to keep in mind is not the storage but the messages.
- A canister that runs out of cycles **freezes and is eventually uninstalled, and its data is gone**. This is
  not a chain where the state is permanent by default: somebody has to top it up, and the freezing threshold is
  the warning window we choose. A registrar for ownership therefore needs a funded canister, a top-up path and
  a monitoring line, and that is an operational obligation, not a one-off deployment.
- Queries are certified only if we write a Merkle root on every change, which is a small amount of code per
  write and must not be forgotten: an uncertified query is indistinguishable from a certified one to a client
  that does not check.

## Order of work, and what to do first

1. **Nothing, until there are outside participants.** A registrar has no value until a third party wants to
   check ownership of somebody else's agent. Building it before the first real room would be infrastructure for
   an audience of zero.
2. When the first outside agents join: a canister holding `agent id -> owner principal` and revocation, with
   certified reads, and a page that verifies it in the browser. Small, and it makes the ownership claim
   independent of us.
3. When somebody signs into our product from a browser: Internet Identity, tied to the same principals.
4. When an auditor or another network asks us for proof of an agreement: the agreement record in a canister
   with certified reads.

## What has to be answered before any of this is built

- Who funds the canister, and what happens when it freezes: a registrar that silently stops answering is worse
  than one that never existed, because the record it held is gone.
- Whether an owner principal is the same person as the account holder here, or a second identity to manage.
- Whether a revoked agent's history stays readable. A revocation that erases the past is a different promise
  from one that records it.