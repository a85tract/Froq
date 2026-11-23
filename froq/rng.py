"""Keyed random streams: every random decision is drawn from a stream named by its semantic key (what, which instruction /
window / attempt), so the four policy arms see identical program truths, predictions and decoder samples wherever the same
event occurs, and policy-dependent extra events draw fresh, reproducible numbers.

A key names a VALUE, not a position in a stream: the same key yields the same number however often, and in whatever order,
it is asked for (findings A3 / B2 -- the generator cache used to hand a repeated key an ADVANCED stream, so
a draw depended on how many times its key had been used before). A caller that needs a sequence of numbers puts the index
in the key (a window's attempt number, a step index), which is what makes the sequence reproducible in another run."""
import hashlib
import numpy as np

_Generator, _PCG64 = np.random.Generator, np.random.PCG64


class KeyedRNG:
    def __init__(self, seed: int, prefix: tuple = ()):
        self.seed = int(seed)
        self.prefix = tuple(prefix)          # (the decoder salt's ("salt", k) before every key; = none, as before)
        self._head = "%d|" % self.seed + "".join(str(x) + "|" for x in self.prefix)   # (speed: the key text's fixed start)

    def stream(self, *key) -> np.random.Generator:
        """A generator seeded by `key` alone: repeated calls with one key return generators in the SAME state, so the
        numbers drawn from a repeated key repeat (never an advanced stream).  Nothing is cached: the state that a cache
        would carry is exactly the order dependence this class exists to remove."""
        if not key:
            h = hashlib.sha256(("%d|" % self.seed + "|".join(str(x) for x in self.prefix)).encode()).digest()
        else:                                # (speed: the same text; default_rng(int) is Generator(PCG64(int)) by numpy's definition)
            h = hashlib.sha256((self._head + "|".join(map(str, key))).encode()).digest()
        return _Generator(_PCG64(int.from_bytes(h[:8], "little")))

    def uniform(self, *key) -> float:
        """One uniform number for a one-shot decision identified by `key` (a fresh stream per key: order-independent)."""
        return float(self.stream(*key).random())
