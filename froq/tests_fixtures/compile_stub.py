"""Stand-in for `froq.semantics.compile.compile_snippet` until WP3's converter exposes the compiler's own router.

Why it exists: the builder prices a recovery script by lowering it on the PROGRAM'S layout (design Sec. 2.3: the cells the
lowered script touches are what the contract reserves, its patch-rounds are P1).  Before the fork's router is callable from
Python, this module lowers the three recovery templates with SWIPER's own gadget shapes, so P1 and the reserved footprint
are consistent between the builder and the device (which is all the tests can check).  It is selected by configuration
(`Semantics(compile_backend="fixture")`), never by a silent fallback.

Interface (mirrors the spec the sibling package was given): compile_snippet(lli_text, n_qubits, layout_flags) -> list of
swiper Instructions.  lli_text: one operation per line, "S <i>", "H <i>", "CNOT <i> <j>" over qubit indices 0..n_qubits-1;
layout_flags: {"wire_cells": {i: [x, y]}, "occupied": [[x, y], ...], "reserved": [[x, y], ...], "bbox": [x0, y0, x1, y1]}.
Shapes: S = MERGE(q, y) D; Y_MEAS(y) HALF_D_PLUS_2 || IDLE(q) HALF_D_PLUS_2; DISCARD(y) (SWIPER's CONDITIONAL_S group);
H = IDLE(q) D (the boundary rotation's busy time, as the converter emits it); CNOT = MERGE(c, a) D; MERGE(a, route..., t) D;
DISCARD(a, route...) -- a and the route are free cells found by a shortest path; no route -> returns None (no placement).
"""
from __future__ import annotations

from collections import deque

from swiper.lattice_surgery_schedule import Duration, Instruction


def _neighbours(c):
    x, y = c
    return [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]


def _free_neighbour(cell, blocked, bbox):
    for n in _neighbours(cell):
        if n not in blocked and bbox[0] <= n[0] <= bbox[2] and bbox[1] <= n[1] <= bbox[3]:
            return n
    return None


def _route(src, dst, blocked, bbox):
    """Shortest path of free cells strictly between src and dst (BFS on the 4-grid inside bbox)."""
    q = deque([src]); prev = {src: None}
    while q:
        c = q.popleft()
        for n in _neighbours(c):
            if n == dst:
                path = []; x = c
                while x != src:
                    path.append(x); x = prev[x]
                return list(reversed(path))
            if n in prev or n in blocked or not (bbox[0] <= n[0] <= bbox[2] and bbox[1] <= n[1] <= bbox[3]):
                continue
            prev[n] = c; q.append(n)
    return None


def compile_snippet(lli_text: str, n_qubits: int, layout_flags: dict):
    cells = {int(k): tuple(v) for k, v in layout_flags["wire_cells"].items()}
    blocked = {tuple(c) for c in layout_flags.get("occupied", ())} | {tuple(c) for c in layout_flags.get("reserved", ())} | set(cells.values())
    xs = [c[0] for c in blocked] + [c[0] for c in cells.values()]; ys = [c[1] for c in blocked] + [c[1] for c in cells.values()]
    bbox = layout_flags.get("bbox") or [min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1]
    out = []
    idx = 0

    def emit(name, patches, dur, faces=frozenset()):
        nonlocal idx
        out.append(Instruction(name, idx, frozenset(patches), dur, merge_faces=frozenset(faces), group_name="RECOVERY")); idx += 1

    for line in lli_text.strip().splitlines():
        parts = line.split()
        if not parts:
            continue
        op, args = parts[0].upper(), [int(a) for a in parts[1:]]
        if op == "S":
            q = cells[args[0]]
            y = _free_neighbour(q, blocked, bbox)
            if y is None:
                return None
            emit("MERGE", [q, y], Duration.D, [(q, y)]); emit("Y_MEAS", [y], Duration.HALF_D_PLUS_2)
            emit("IDLE", [q], Duration.HALF_D_PLUS_2); emit("DISCARD", [y], 0)
        elif op == "H":
            emit("IDLE", [cells[args[0]]], Duration.D)
        elif op == "CNOT":
            c, t = cells[args[0]], cells[args[1]]
            a = _free_neighbour(c, blocked, bbox)
            if a is None:
                return None
            route = _route(a, t, blocked | {a}, bbox)
            if route is None:
                return None
            emit("MERGE", [c, a], Duration.D, [(c, a)])
            chain = [a] + route + [t]
            emit("MERGE", chain, Duration.D, [(chain[i], chain[i + 1]) for i in range(len(chain) - 1)])
            for r in [a] + route:
                emit("DISCARD", [r], 0)
        else:
            raise ValueError("unknown recovery operation %r" % line)
    return out
