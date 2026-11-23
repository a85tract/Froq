"""Sets that keep their hash-table layout across a checkpoint (gap analysis rule 38, checkpoint and resume).

A CPython set iterates in the order of its hash table's slots, and where each element sits depends on the set's
history: the order of its insertions and deletions, the deleted entries ("dummies") they leave, and its resizes. A
pickled set comes back rebuilt from its elements, so it can iterate in another order. The run iterates sets whose
order matters: the dispatch visits the frontier in set order, and upstream's syndrome generation walks each
instruction's patches and the active patches in set order, which fixes the order in which windows are built and
numbered. A resumed run could therefore diverge from the whole run (split check on ccb0f57: D-lazy and
H-nci-lead at B05 did).

`reduce(s)` records a set's table as it is -- its mask, fill, used, finger and cached hash, each element's slot, the
dummies' slots -- and `rebuild` writes that table into the new set, so the resumed set iterates, and behaves under
later insertions and deletions, exactly as the original. Only sets whose elements hash the same in every process are
recorded so: ints, bools, floats other than NaN, and tuples and frozensets of those. A string hashes differently in
each process (hash randomisation), and so does None under CPython 3.11, whose hash is its address; a set holding either, at any depth, is pickled as before: its order differs between two whole runs in two
processes, so it cannot matter to a run that reproduces across processes. The checkpoint counts such sets (`run.py`).

The table is read and written through ctypes, on the CPython 3.11 layout of PySetObject (Include/cpython/setobject.h).
`OK` is set only if `_selfcheck` confirms that layout on the running interpreter, `INTERPRETER` names it, and
`reduce` and `rebuild` refuse to work without it (finding 3): `froq.run` checks both before it writes or reads a
checkpoint, so an unsupported interpreter never falls back to an ordinary pickle."""
from __future__ import annotations

import ctypes, gc, random, sys

INTERPRETER = "%s-%d.%d.%d-%dbit" % (sys.implementation.name, *sys.version_info[:3], 8 * ctypes.sizeof(ctypes.c_void_p))

_W = ctypes.c_ssize_t
FILL, USED, MASK, TABLE, HASH, FINGER, SMALL = 2, 3, 4, 5, 6, 7, 8      # word offsets in PySetObject (3.11)
MINSIZE = 8
_DUMMY = None

_malloc = ctypes.pythonapi.PyMem_Malloc
_malloc.restype, _malloc.argtypes = ctypes.c_void_p, [ctypes.c_size_t]
_free = ctypes.pythonapi.PyMem_Free
_free.restype, _free.argtypes = None, [ctypes.c_void_p]
_incref = ctypes.pythonapi.Py_IncRef
_incref.restype, _incref.argtypes = None, [ctypes.py_object]
_decref = ctypes.pythonapi.Py_DecRef
_decref.restype, _decref.argtypes = None, [ctypes.py_object]


def _words(s):
    return (_W * (SMALL + 2 * MINSIZE)).from_address(id(s))


def _table(s):
    w = _words(s)
    return (_W * (2 * (w[MASK] + 1))).from_address(w[TABLE])


def _stable(x) -> bool:
    t = type(x)
    if t is int or t is bool or (t is float and x == x):               # (a NaN hashes by identity; None by its address)
        return True
    if t is tuple or t is frozenset:
        return all(_stable(y) for y in x)
    return False


def read(s) -> tuple:
    """(mask, fill, used, finger, hash, slots of the elements in iteration order, slots of the dummies)."""
    w = _words(s)
    ent = _table(s)[:]
    slots, dummies = [], []
    for i in range(w[MASK] + 1):
        k = ent[2 * i]
        if k:
            (dummies if k == _DUMMY else slots).append(i)
    return w[MASK], w[FILL], w[USED], w[FINGER], w[HASH], slots, dummies


def reduce(s):
    """A pickle reduction that rebuilds s with its table, or None when an element hashes differently in another process
    (pickle s as usual)."""
    if not OK:
        raise RuntimeError("set layouts cannot be recorded on this interpreter (%s)" % INTERPRETER)
    if not all(_stable(x) for x in s):
        return None
    elems = list(s)
    layout = read(s)
    if len(layout[5]) != len(elems):
        return None
    state = getattr(s, "__dict__", None) or None
    return (rebuild, (type(s), elems, layout), state)


def rebuild(cls, elems, layout):
    if not OK:
        raise RuntimeError("set layouts cannot be written on this interpreter (%s)" % INTERPRETER)
    if not all(_stable(x) for x in elems):
        raise RuntimeError("a recorded set holds an element whose hash differs between processes")
    return _rebuild(cls, elems, layout)


def _rebuild(cls, elems, layout):
    mask, fill, used, finger, fhash, slots, dummies = layout
    if not elems and not dummies:
        return cls()                                                    # (the empty frozenset is a singleton: never written)
    obj = cls(elems)
    collect = gc.isenabled()
    gc.disable()                                                        # no collection may walk the table mid-write
    try:
        _write(obj, elems, layout)
    finally:
        if collect:
            gc.enable()
    if list(obj) != elems or len(obj) != used:
        raise RuntimeError("a rebuilt set does not iterate as its original")
    return obj


def _write(obj, elems, layout):
    mask, fill, used, finger, fhash, slots, dummies = layout
    w = _words(obj)
    n = mask + 1
    small = id(obj) + SMALL * ctypes.sizeof(_W)
    old_ptr, old_n = w[TABLE], w[MASK] + 1
    old = (_W * (2 * old_n)).from_address(old_ptr)
    held = [ctypes.cast(old[2 * i], ctypes.py_object).value for i in range(old_n) if old[2 * i] and old[2 * i] != _DUMMY]
    if old_ptr == small and n != MINSIZE:
        ctypes.memset(small, 0, 2 * MINSIZE * ctypes.sizeof(_W))       # (left behind: no stale entries)
    if n == MINSIZE:
        new_ptr = small
        if old_ptr != small:
            ctypes.memset(small, 0, 2 * MINSIZE * ctypes.sizeof(_W))
    else:
        new_ptr = _malloc(2 * n * ctypes.sizeof(_W))
        if not new_ptr:
            raise MemoryError("set table of %d slots" % n)
        ctypes.memset(new_ptr, 0, 2 * n * ctypes.sizeof(_W))
    for x in elems:                                                     # the new table's references, before the old ones go
        _incref(x)
    if new_ptr == old_ptr:
        ctypes.memset(new_ptr, 0, 2 * n * ctypes.sizeof(_W))
    new = (_W * (2 * n)).from_address(new_ptr)
    for x, i in zip(elems, slots):
        new[2 * i], new[2 * i + 1] = id(x), hash(x)
    for i in dummies:
        new[2 * i], new[2 * i + 1] = _DUMMY, -1
    w[MASK], w[TABLE], w[FILL], w[USED], w[FINGER], w[HASH] = mask, new_ptr, fill, used, finger, fhash
    if old_ptr != small and old_ptr != new_ptr:
        _free(old_ptr)
    for x in held:                                                      # the old table's references
        _decref(x)
    del held


def _selfcheck() -> bool:
    """The layout on this interpreter: field offsets, the dummy entry, and round trips of sets with random histories
    (insertions, deletions, resizes), each then driven on by the same operations as its original."""
    global _DUMMY
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 11) or ctypes.sizeof(ctypes.c_void_p) != 8:
        return False
    s = set()
    w = _words(s)
    if (w[FILL], w[USED], w[MASK], w[TABLE]) != (0, 0, MINSIZE - 1, id(s) + SMALL * 8):
        return False
    s = {1, 2, 3}
    s.discard(2)
    ent = _table(s)[:]
    if (ent[2], ent[3], ent[6], ent[7], ent[5]) != (id(1), 1, id(3), 3, -1) or not ent[4] or ent[4] in (id(1), id(3)):
        return False
    _DUMMY = ent[4]
    rnd = random.Random(29)
    for trial in range(60):
        a = set()
        for _ in range(rnd.randint(0, 300)):
            x = rnd.randrange(rnd.choice((16, 300, 5000)))
            a.add(x) if rnd.random() < 0.6 else a.discard(x)
        if trial % 3 == 0:
            a = frozenset(a) if trial % 2 else a
        b = _rebuild(type(a), list(a), read(a))
        if list(b) != list(a) or read(b) != read(a):
            return False
        if isinstance(a, set):
            for k in range(60):                                           # the same later history, the same order
                x = rnd.randrange(6000)
                if rnd.random() < 0.5:
                    a.add(x); b.add(x)
                else:
                    a.discard(x); b.discard(x)
                if k % 10 == 9 and list(a) != list(b):
                    return False
            if list(a) != list(b):
                return False
    t = (frozenset({(1, 2), (3, 4), (5, 6), (7, 8)}), (9, 2.5))
    b = _rebuild(frozenset, list(t[0]), read(t[0]))
    return list(b) == list(t[0]) and hash(b) == hash(t[0])


try:
    OK = _selfcheck()
except Exception:                                                       # noqa: BLE001 (any surprise: pickle as before)
    OK = False
