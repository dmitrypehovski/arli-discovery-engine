# Scaling the network, and onboarding the people who have no agent yet

Two questions, one answer, because they are the same question asked from two sides. How does a network of
agents, from thousands to millions, keep a search that works? And how does someone with no agent at all
get into that same network? Both are solved by the same model if the model is right: one agent record,
two data planes, and an index that holds no lists.

## The numbers we are planning against

Measured, not estimated:

| what | number | where it comes from |
|---|---|---|
| taxonomy | 31 categories, 335 subcategories | `skill_taxonomy_v2.json`, each category carries 6 to 16 subcategories |
| one private exchange, 8 items | 461 ms | evaluation battery, 20 October |
| one private exchange, 32 items | 1391 ms | same |
| one private exchange, 64 items | 2723 ms | same, and 64 is the cap |

A search is one exchange per agent, so the honest arithmetic is brutal: 1,000 agents is eight to forty
five minutes serially depending on list size; 100,000 agents is thirteen hours to three days; a million
agents is not a matter of a bigger machine.
The cost is per pair, and the pair count grows with the square of the network. Anything that claims to
scale this by adding servers is selling a promise the arithmetic does not support.

## The wall, and the three layers that move it

### Layer A: a public bucket profile, and an index that holds no lists

Each agent publishes which buckets it holds items in, how many items per bucket, and coarse attributes:
region, whether it is demand or supply, and its claim level. Twenty items land in perhaps six to ten
buckets out of 10,385. No item value is published, but the bucket membership is.

That profile is what makes a room indexable without an index holder owning anyone's list: bucket to agent
is a lookup table. A million agents at ten buckets each is ten million rows, which is small for any
database, sharded by region or vertical.

**The trade has to be stated, not hidden.** Bucket membership is coarse information: a competitor learns
that an agent holds something in "procurement and purchasing", not what. Today the text says the room
never shows a feature list, which stays true, and it must add that the bucket profile is public by design
and why. A reader who objects to that trade can keep their agent out of the index and be reachable only
by address, which is exactly what the direct exchange already allows.

### Layer B: the prefilter, so exchanges happen with candidates only

A search maps the seeker's own items to buckets, takes the candidates from the posting lists, ranks them
by bucket overlap and freshness, and runs real private exchanges with a bounded number of them, in
parallel. The seeker learns the same thing it learns today, the bucket never enters the result.

For 50 candidates at 32 items: about 70 seconds on one core, about five seconds across sixteen. The
number that matters is candidates per search, and that is a product decision with a budget attached, not
a protocol one.

### Layer C: one signing per epoch, not per exchange

Today the holder signs the seeker's blinded values again for every pair, which is why 64 items cost 2.7
seconds of somebody's CPU. The fix is old and unglamorous: the holder signs its own list once per epoch
under an epoch key, keeps that table, and serves it to as many seekers as ask. A seeker fetches one table
and does its own comparison, so the heavy side is the side that asked, which is where cost belongs.

This is the change that turns thousands of searches a day into millions on the same hardware, and it is
the change that makes revocation and epochs interact: a table must expire, and a revoked agent's table
must stop being served.

## The same model, seen from the person with no agent

The three ways to exist are one record:

- **Hosted.** We keep the identity, the vault and an address. Nothing to install. The price is real and
  must be said: if we host the vault, "we cannot read your list" becomes "we hold your list"; either the
  vault is encrypted with a key we do not hold, or the sentence changes.
- **Connected.** Their own agent at their own address, joined by one line. The shape this repository
  already ships.
- **Ephemeral.** A try before either: a vault with a handful of items, matched against a pool of
  consenting peers so a person sees the mechanics and a real result in a minute.

Registration is one intake: they describe what they need in their own words, the taxonomy of 31 by 335
suggests the buckets, and both are stored, the words for the person and the tokens for the comparison.
That is the job the taxonomy was always going to do, and it is why it is worth having.

The upgrade path is the unification: an ephemeral agent becomes hosted, a hosted agent becomes connected,
and the identity travels. Same key, same records, same place in the index. Nobody starts again.

## What has to be in the text, and what is product

Normative text: the bucket profile and what it reveals; that an index may rank candidates without holding
any item; that epochs exist and what expiry means; that a hosted vault must state who can read it.

Product, not normative: how many candidates a search tries, the budget per search, the intake's wording,
the pricing, and whether a room is demonstration or real, which is now a switch in the service.

## Order of work, smallest first

1. Bucket profile and prefilter over the existing room, with a measurement that answers one question with
   numbers: with 1,000 agents, how many exchanges are attempted with and without the prefilter, how long
   each takes, and are the matches identical. That turns the scaling claim into a measurement.
2. The epoch token table, measured the same way against the same 1,000 agents.
3. Intake: their words to buckets, a hosted vault, and a try mode against a pool that holds something
   real we can actually deliver.
4. The text gains the clauses, the version moves, both implementations follow.

## What this does not solve

- The bucket profile leaks coarse information. By design, stated, and avoidable by staying out of the index.
- A search is still bounded work per candidate. At a million agents a search must be budgeted, and a
  budget is a product decision with a price attached.
- Jurisdiction: the taxonomy carries it, and a network that spans borders will need cells for it. That is
  a later, deliberate step, not a default.