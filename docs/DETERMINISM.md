# Why fixed seeds and threshold bands

Two design choices in this project look like fussiness and are not. This page
is the argument for both, with the numbers that make it.

## A simulation an agent cannot reproduce is a rumour

Suppose `simulate` samples fresh randomness on every call. An agent raises the
retry limit, re-runs, and reads 81% success where it read 83% before. What has
it learned?

Nothing. The difference is somewhere between its edit and the noise floor, and
it has no way to tell which. But it will report *something*, because that is
what a language model does with an ambiguous observation — and the something
it reports will sound confident. Plausible-and-wrong is the characteristic
failure of an agent working without ground truth, and non-reproducible tooling
manufactures it.

So every run in this package is a pure function of `(subject, seed)`:

```
groundtruth replay standard_checkout --seed 7    # same five steps, every time, every machine
```

That buys three things that compound:

1. **A specific failing run, not a statistic about runs like it.** When
   `simulate` reports that seed 431 got stuck, the agent replays 431 and reads
   the twelve steps that actually executed. No inference required.
2. **A fingerprint that means something.** Identical inputs produce an
   identical hash over the batch, so a *changed* fingerprint is genuinely
   changed behaviour — even when every threshold still passes. That catches the
   refactor that was supposed to be a no-op and was not.
3. **A gate that gates.** A threshold that passes locally passes in CI, because
   both ran the same batch. A gate that fails for reasons unrelated to the diff
   is a gate people learn to re-run until it goes green, which is worse than no
   gate at all — it costs the same time and provides false assurance.

`simulate --check-determinism` re-runs a sample of seeds and compares. When it
reports `NOT DETERMINISTIC`, the cause is almost always one of: reading a
clock, iterating a `set` or a pre-3.7 dict, a module-level RNG shared between
runs, or a hash seed leaking into ordering. The library refuses to let that
batch pass a gate — numbers derived from an irreproducible run are not
evidence.

## Why a band, and why the run count is part of the gate

Here is the same flow, same seed, at two batch sizes:

```
runs=500     success 85.4%   abandoned 14.4%   payment_failed 0.2%
runs=20000   success 88.0%   abandoned 11.5%   payment_failed 0.4%
```

Nothing changed but the sample size. The 500-run batch is not wrong — it is
one honest sample from a distribution whose true success rate is 88.0%, and at
n=500 the standard error is about 1.5 points. Two-and-a-half points of drift
from sampling alone.

That is the whole case for two rules this package enforces:

**Thresholds are bands, not point comparisons.** `rate:success >= 0.80` has
eight points of headroom over the true 88%, which is wide enough that sampling
noise cannot trip it and narrow enough that a real regression will. A gate
written as "must equal last week's number" fails on noise; a gate written as
"must be roughly fine" never fails at all. The band is where the judgement
goes.

**The run count is part of the gate's definition, not a knob.** CI pins
`--runs 2000 --seed 0`. Change either and you have changed what the gate
measures, which is why it lives in the workflow file next to the threshold
rather than defaulting to whatever felt fast that day.

## Choosing the numbers

A rough starting point for a rate threshold: the standard error of a
proportion is `sqrt(p(1-p)/n)`. At p≈0.88, n=2000 gives ≈0.7 points. Put the
bound at least three standard errors from the observed value and sampling noise
will trip it about once in a thousand runs; put it closer and you will spend
your time re-running CI.

For percentile metrics, more runs matter more: a p95 estimated from 200 runs is
the average of the top ten observations and moves around a lot. If a p95
threshold is the one that keeps flapping, raise the run count before you widen
the band.

If a gate does start flapping, the honest fixes are in this order: raise the
run count, then widen the band and write down why, then — last — question
whether the metric is measuring what you meant. Deleting the threshold is not
on the list.

## Seed policy

`seed_policy = "offset"` (the default) means run `i` uses `base_seed + i`. It
is trivially predictable, which is the point: "run 42 failed" is replayable by
hand without consulting a table.

`seed_policy = "hash"` derives each seed from SHA-256 of `base:index` instead.
Reach for it only if your engine consumes the seed somewhere that makes
near-neighbours correlate — a linear congruential generator, or hashing the run
index into an unrelated part of the seed space. Python's own `random.Random`
scrambles its state on seeding, so for stdlib RNGs, `offset` is fine and easier
to debug.
