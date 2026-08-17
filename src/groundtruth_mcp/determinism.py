"""Seeds, and the reason this package is opinionated about them.

A simulation an agent cannot reproduce is a rumour. If `simulate` returns "83%
success" and the agent changes a retry limit and gets "81%", it has learned
nothing: the difference is somewhere between the edit and the noise floor, and
it has no way to tell which. Worse, it will confidently report a regression it
invented, because plausible-and-wrong is the failure mode of an agent with no
ground truth.

So every run in this package is a pure function of `(subject, seed)`. Same
seed, same trace, byte for byte, on every machine. That buys three things:

  1. `replay(seed=7)` hands the agent the *exact* failing run to read, not a
     statistical summary of runs like it.
  2. Two simulations of the same config produce identical fingerprints, so a
     changed fingerprint is genuinely a changed behaviour and CI can say so.
  3. A threshold that passes locally passes in CI, so the gate is a gate and
     not a coin flip that occasionally blocks a correct change.
"""

from __future__ import annotations

import hashlib
from typing import Iterable, Literal, Sequence

SeedPolicy = Literal["offset", "hash"]

# 2**32 - 1: the widest seed `random.Random` treats as a plain int on every
# platform, and comfortably inside numpy's legacy seed range too.
_SEED_SPACE = 0xFFFFFFFF


def seed_for(base: int, index: int, policy: SeedPolicy = "offset") -> int:
    """The seed for run `index` of a batch based at `base`.

    `offset` is `base + index` — trivially predictable, which is the feature:
    a developer reading "run 42 of 100 failed" can replay seed `base + 42`
    without consulting a table.

    `hash` derives each seed from SHA-256 of `base:index` instead. Reach for it
    when your engine consumes the seed somewhere that makes near-neighbours
    correlate — hashing the run index into an unrelated part of the seed space,
    or a linear congruential generator, both leak the pattern into the first
    few draws. Python's own `random.Random` scrambles its state on seeding, so
    for stdlib RNGs `offset` is fine.
    """
    if policy == "offset":
        return (int(base) + int(index)) & _SEED_SPACE
    if policy == "hash":
        digest = hashlib.sha256(f"{int(base)}:{int(index)}".encode()).digest()
        return int.from_bytes(digest[:4], "big")
    raise ValueError(f"unknown seed policy {policy!r} — use 'offset' or 'hash'")


def seeds(base: int, runs: int, policy: SeedPolicy = "offset") -> list[int]:
    if runs < 1:
        raise ValueError(f"runs must be at least 1, got {runs}")
    return [seed_for(base, index, policy) for index in range(runs)]


def fingerprint(values: Iterable[object]) -> str:
    """A short, stable hash over an ordered sequence of values.

    Used to compare two simulation batches for behavioural identity. Truncated
    to 16 hex chars because it is read by humans in CI logs and compared for
    equality, never used as a security primitive.
    """
    hasher = hashlib.sha256()
    for value in values:
        hasher.update(repr(value).encode("utf-8"))
        hasher.update(b"\x1f")
    return hasher.hexdigest()[:16]


def compare_batches(first: Sequence[str], second: Sequence[str]) -> list[int]:
    """Indices where two sequences of per-run fingerprints diverge.

    Non-empty means your engine is not deterministic under a fixed seed: it is
    reading a clock, iterating a set, hashing a pointer, or touching a global.
    Each returned index is directly replayable — that is the point of returning
    indices rather than a bare boolean.
    """
    return [i for i, (a, b) in enumerate(zip(first, second)) if a != b]
