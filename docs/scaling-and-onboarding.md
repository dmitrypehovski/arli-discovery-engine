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

Today the holder signs the seeker's blinded values again for every pair, and recomputes signatures over
its own list for every seeker. The fix, measured rather than asserted: one key per epoch, and the holder's
own tokens computed once per epoch and reused. At thirty two items that took a holder's exchange from 1572
milliseconds to 800, so throughput roughly doubles, and key generation turned out to be only 2.6 percent of
the cold cost, which is why the token table is the part worth caching.

What the epoch cannot remove is signing the seeker's blinded values: that work is per exchange, and only the
holder can do it. So the honest ceiling on one core is about 1.25 exchanges a second at thirty two items,
and a search of fifty candidates is about forty seconds of one core. Beyond that it fans out across cores,
which is why the number of candidates per search is a budget a product decides and states.

Two things follow, and both must be written down rather than discovered: tokens are stable within an epoch,
so two seekers comparing notes can tell they hold the same item (they could already, by intersecting their
own lists, but saying it is not optional); and an epoch must expire, and a revoked agent's epoch must stop
being served.

### Measured at scale, not argued

The index was built at four sizes, up to a million agents, and the numbers answer the question that was
asked first: can this hold when the network is not a room but a million agents.

| agents | index build | a search spends selecting | agents sharing a bucket | offered |
|---|---|---|---|---|
| 1,000 | 0.021 s | 0.3 ms | 166 | 50 |
| 10,000 | 0.313 s | 2.2 ms | 1,603 | 50 |
| 100,000 | 1.767 s | 14.8 ms | 15,957 | 50 |
| 1,000,000 | 16.5 s | 145 ms | 159,650 | 50 |

Two things read off that table. Building a million-agent index takes sixteen seconds, so it is a job that
runs once and then updates incrementally, not something a search waits for. And selection stays in
milliseconds: 145 ms to choose from a million, against 0.3 ms from a thousand. A search does not slow down
with the network; the exchanges it runs do not multiply either, because they are bounded by the candidate
limit.

The uncomfortable number is in the last column. In a million agents, 159,650 of them share a bucket with the
seeker, and fifty are offered. At a thousand agents it was 166 sharing and fifty offered, so the ratio was
bearable; at a million the search is choosing a hundredth of a percent. That is not a leak, it is a budget,
and the honest presentation of it is a product decision with a price on it: broaden the candidate count,
narrow the buckets, or tell the user plainly that they saw fifty of a hundred and fifty-nine thousand.

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

### How the two questions become one system

The person with an agent and the person without one are the same record at two moments, and that is the
whole answer to putting this together. One intake, one profile, one index, three doors.

1. **Describe what you need, in your own words.** That is the only thing everybody can do, and it is the
   door for the person who has no agent. The intake turns the words into the vocabulary of the network:
   goods stay goods, city, industry and capacity stay attributes, and the taxonomy of 31 by 335 is what
   suggests which buckets are meant. The words are kept for the person, the tokens are kept for the
   comparison. Nothing else is asked for at this step.
2. **You get an agent, hosted or your own.** Hosted means we keep the identity, the vault and an address,
   and the person gets a working member of the network in one step, with the vault encrypted under a key we
   do not hold so the sentence "we cannot read your list" stays true. Connected means they run their own
   agent at their own address and hand nothing over, which is what this repository already ships. Same
   records either way, and moving between the two carries the identity across.
3. **Search works the same for both.** The seeker's own items map to buckets, the index returns candidates,
   and real private exchanges run with those candidates. Whether the counterparty is hosted by us or
   connected from somewhere else is invisible at that point, which is the property that makes one network
   out of two populations instead of two products.
4. **A try, before either.** Someone who wants to see the thing work holds a handful of items against a
   pool of consenting peers and gets a real result in about a minute. The pool has to hold something real
   we can deliver, or the demonstration is a fiction and the person finds out later.

The taxonomy is what makes step one possible, and that is its job in this design: not decoration on a
website, but the shared vocabulary that lets a person who has never touched an agent say what they want in
words a machine can compare. Thirty-one categories and three hundred and thirty-five subcategories is
enough to place almost any business, and small enough that two sides told the same slug will use the same
one.

## What has to be in the text, and what is product

Normative text: the bucket profile and what it reveals; that an index may rank candidates without holding
any item; that epochs exist and what expiry means; that a hosted vault must state who can read it.

Product, not normative: how many candidates a search tries, the budget per search, the intake's wording,
the pricing, and whether a room is demonstration or real, which is now a switch in the service.

## Order of work, smallest first

1. ~~Bucket profile and prefilter over the existing room~~ done, measured on a thousand agents: fifty
   exchanges in 38 seconds instead of a thousand in 857, identical matches.
2. ~~The epoch token table, measured the same way~~ done: 1572 ms to 800 ms at thirty two items, and the
   finding that the token table, not the key, is what is worth caching.
3. ~~The index measured at a million agents~~ done, and it found the number that matters: at a million, a
   search offers fifty candidates out of a hundred and fifty-nine thousand sharing a bucket.
4. Intake: their words to buckets, a hosted vault whose key we do not hold, and a try mode against a pool
   that holds something real we can actually deliver. This is the door for the person with no agent and it
   is the one still to build.
5. The text gains the clauses, the version moves, both implementations follow. Clauses 4.16 and 4.17 are
   in at 1.5.0 in both wording editions, the second implementation carries the epoch, and the bucket
   profile still needs its clause.



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

## Sifting over the wire, measured on the live domain

The stand measurement above is the mechanism. This is the same thing running where a visitor can reach it: two
agents at public addresses we host, connected to the live room, each publishing a profile in the same bucket
space, and a search that spends an exchange only on the one whose bucket it shares.

- `https://arliwork.com/demo-agent` publishes one bucket in `manufacturing-and-production`.
- `https://arliwork.com/demo-agent-b` publishes one bucket in `agriculture-horticulture-landscaping`.
- A search asking for packaging: ten agents in the room checked, two judged by profile, one skipped with no
  exchange spent on it, one asked, and the asked one matched 10000 of 10000 with nothing on the wire.
- A search asking for irrigation: the same, mirrored, with the other agent skipped.

Costs that are stated rather than hidden: an agent that publishes no comparable profile is asked on every
search, and the live room said exactly that before the demo agent was restarted with the taxonomy it now
resolves in. An agent whose profile sits in another bucket space is never skipped on that ground. The public
page names how many were skipped and that they were not asked, which is what clause 4.20 requires of a
selection.
