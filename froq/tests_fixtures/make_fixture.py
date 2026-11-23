"""Writes the fixture program `fixture3` (three program wires) as the converter would: `fixture3.lss` (SWIPER's schedule
format, symbolic durations) and `fixture3.events.json` (design Sec. 1.3).  The C2 / C3 tables are the real certificates
(froq/semantics/certificates); nothing here mocks them.

Program (W_p):  T q0; CNOT q0->q1; H q0; CNOT q1->q2; T q1; measure q0, q1, q2 in Z (outputs).
Layout: q0 (0,0), q1 (0,2), q2 (0,4); CNOT mediators at (0,1) / (0,3); T and S resources at (1,0) / (1,2).
Templates are the compiler's (certify.py): T = T1 (|T> resource, Z_q Z_a -> u, X_a readout -> w, frame Z_q^w) then
T2 (the teleported S gadget iff u: |Y> resource, Z_q Z_y -> u', X_y readout -> w', frame Z_q^(u' xor w')); the patched
control-first CNOT (a in |+>; Z_c Z_a -> u; X_a X_t -> v; Z_a readout -> w; frame Z_c^v X_t^(u xor w)); H = a boundary
rotation (busy time, one IDLE in the .lss).  Two speculated values: the ZZ results of the two T gadgets.
Expected chain of value 0 (K >= 6, prediction S / truth skip): T2 (S gadget) -> CNOT0 (Q = rot(-1,Z q0) unchanged, L
grows to {q0, q1}) -> H (rot(-1, X q0)) -> CNOT1 (spectator) -> T1 of the second T (spectator) -> T2 of it -> fence at
the destructive Z measurement of q0 (axis X anticommutes) and the io output of q0.
Run: python -m froq.tests_fixtures.make_fixture
The gadget emitters are module-level (`t_gadget`, `cnot`, `hadamard`, `pauli`) so a test can build a VARIANT program in
memory with `build(program=...)` (C6 fix revision 8: the reviewers' counterexamples with a Pauli gate between another
gadget's members); `build()` with no argument writes fixture3 exactly as before.
"""
from __future__ import annotations

import copy, json, os

HERE = os.path.dirname(os.path.abspath(__file__))

LSS = []          # (name, patches, duration, cond_on, cond_deps, cond_completion, cond_completion_deps, faces, group, group_name)
EVENTS = []
GADGETS = {}
LSS_MAP = {}


def lss(name, patches, dur, cond_on=(), faces=(), group=(), group_name="none"):
    LSS.append([name, [tuple(p) for p in patches], dur, list(cond_on), [], [], [], [tuple(f) for f in faces], list(group), group_name])
    return len(LSS) - 1


def ev(event_id, kind, deps=(), gadget=None, lss_idx=None, **fields):
    e = dict(event_id=event_id, kind=kind, deps=[dict(on=d[0], kind=d[1]) for d in deps], gadget=gadget)
    e.update(fields)
    EVENTS.append(e)
    if lss_idx is not None:                      # index 0 is a valid instruction
        LSS_MAP[event_id] = list(lss_idx) if isinstance(lss_idx, (list, tuple)) else [lss_idx]
    return event_id


def gadget(gid, template, members, inputs, result_bits, predicate=None, parent=None):
    GADGETS[gid] = dict(gadget_id=gid, template_id=template, events=members, inputs=inputs, outputs=dict(inputs), result_bits=result_bits,
                        predicate=predicate, parent=parent)
    return gid


last_on_wire: dict[str, str] = {}      # wire -> last event id (quantum deps)


def q(wire):
    return [(last_on_wire[wire], "quantum")] if wire in last_on_wire else []


def touch(eid, *wires):
    for w in wires:
        last_on_wire[w] = eid


def reset():
    """Empty the module-level document state (one program per build)."""
    del LSS[:], EVENTS[:]
    GADGETS.clear(); LSS_MAP.clear(); last_on_wire.clear()


CELLS = {"q0": (0, 0), "q1": (0, 2), "q2": (0, 4)}


def inputs(cells):
    for w, c in cells.items():
        i = lss("IDLE", [c], "D")
        e = ev("init_%s" % w, "init", lifetime="L_%s" % w, wire=w, state="Zero", role="program", cells=[list(c)], slice=0, lss_idx=i)
        touch(e, w)
        ev("io_in_%s" % w, "io", deps=[(e, "quantum")], io_kind="input", wire=w)


def t_gadget(cells, n, wire, rcell, inside_s=None, before_s=None):
    """T then the predicated S (T2).  `inside_s()`: extra emission between the S merge / readout and the S target busy
    (a foreign gate inside the T2's physical span on `wire`; revision-8 regression).  `before_s()`: extra emission between
    the T1 (injection merge, readout, resource discard) and the T2 (a foreign gate between the producer and its T2;
    revision-9 regression)."""
    c = cells[wire]
    gid = "g_T%d" % n; sid = "g_T%d_S" % n
    u, w = "u%d" % n, "w%d" % n
    i_inj = lss("INJECT_T", [rcell], "D")
    i_zz = lss("MERGE", [c, rcell], "D", faces=[(c, rcell)])
    i_rd = lss("DISCARD", [rcell], 0)
    g_first = len(LSS)
    group = [g_first, g_first + 1, g_first + 2, g_first + 3]
    LSS[i_zz][4] = list(group)                                 # conditional_dependencies of the producer
    i_sm = lss("MERGE", [c, rcell], "D", cond_on=[i_zz], faces=[(c, rcell)], group=group, group_name="CONDITIONAL_S")
    i_sy = lss("Y_MEAS", [rcell], "HALF_D_PLUS_2", cond_on=[i_zz], group=group, group_name="CONDITIONAL_S")
    i_si = lss("IDLE", [c], "HALF_D_PLUS_2", cond_on=[i_zz], group=group, group_name="CONDITIONAL_S")
    i_sd = lss("DISCARD", [rcell], 0, cond_on=[i_zz], group=group, group_name="CONDITIONAL_S")
    r, y = "r%d" % n, "y%d" % n
    e_ri = ev("%s_res_init" % gid, "init", gadget=gid, lifetime="L_%s" % r, wire=r, state="T", role="resource", cells=[list(rcell)], lss_idx=i_inj)
    touch(e_ri, r)
    e_zz = ev("%s_zz" % gid, "measure_product", deps=q(wire) + q(r), gadget=gid, signed_observable={wire: "Z", r: "Z"}, sign=1, result_id=u,
              lifetimes=["L_%s" % wire, "L_%s" % r], cells=[list(c), list(rcell)], lss_idx=i_zz)
    touch(e_zz, wire, r)
    e_xr = ev("%s_xread" % gid, "measure_single", deps=q(r), gadget=gid, wire=r, observable="X", sign=1, result_id=w, destructive=True, lss_idx=i_rd)
    touch(e_xr, r)
    e_rd = ev("%s_res_discard" % gid, "discard", deps=q(r), gadget=gid, lifetime="L_%s" % r, wire=r, discard_kind="measured", lss_idx=i_rd)
    touch(e_rd, r)
    if before_s is not None:
        before_s()
    # nested S gadget, predicated on u
    e_yi = ev("%s_res_init" % sid, "init", deps=[(e_rd, "physical")], gadget=sid, lifetime="L_%s" % y, wire=y, state="Y", role="resource", cells=[list(rcell)], lss_idx=i_sm)
    touch(e_yi, y)
    e_szz = ev("%s_zz" % sid, "measure_product", deps=q(wire) + q(y) + [(e_zz, "classical")], gadget=sid, signed_observable={wire: "Z", y: "Z"}, sign=1,
               result_id=u + "s", lifetimes=["L_%s" % wire, "L_%s" % y], cells=[list(c), list(rcell)], predicate={"xor": [u]}, lss_idx=i_sm)
    touch(e_szz, wire, y)
    e_sxr = ev("%s_xread" % sid, "measure_single", deps=q(y), gadget=sid, wire=y, observable="X", sign=1, result_id=w + "s", destructive=True,
               predicate={"xor": [u]}, lss_idx=i_sy)
    touch(e_sxr, y)
    if inside_s is not None:
        inside_s()
    e_sbusy = ev("%s_target_busy" % sid, "busy_region", deps=q(wire), gadget=sid, lifetime="L_%s" % wire, cells=[list(c)], duration="HALF_D_PLUS_2",
                 reason="S gadget target idle", predicate={"xor": [u]}, lss_idx=i_si)
    touch(e_sbusy, wire)
    e_sd = ev("%s_res_discard" % sid, "discard", deps=q(y), gadget=sid, lifetime="L_%s" % y, wire=y, discard_kind="measured", predicate={"xor": [u]}, lss_idx=i_sd)
    touch(e_sd, y)
    e_sf = ev("%s_frame" % sid, "frame_update", deps=q(wire) + [(e_szz, "classical"), (e_sxr, "classical")], gadget=sid, wire=wire, pauli="Z",
              predicate={"xor": [u + "s", w + "s"]})
    touch(e_sf, wire)
    e_tf = ev("%s_frame" % gid, "frame_update", deps=q(wire) + [(e_xr, "classical")], gadget=gid, wire=wire, pauli="Z", predicate={"xor": [w]})
    touch(e_tf, wire)
    gadget(sid, "S", [e_yi, e_szz, e_sxr, e_sbusy, e_sd, e_sf], {"q": wire}, {"u": u + "s", "w": w + "s"}, predicate={"xor": [u]}, parent=gid)
    gadget(gid, "T", [e_ri, e_zz, e_xr, e_rd, sid, e_tf], {"q": wire}, {"u": u, "w": w})



def cnot(cells, n, cw, tw, acell, between=None):
    """Patched control-first CNOT.  `between()`: extra emission between the control merge and the target merge (other
    gadgets whose stream positions precede the target-side work; the target-side instructions are allocated after them so
    the .lss order stays a valid schedule order; revision-8 regression)."""
    c, t = cells[cw], cells[tw]
    gid = "g_CX%d" % n; a = "a%d" % n
    u, v, w = "cu%d" % n, "cv%d" % n, "cw%d" % n
    i1 = lss("MERGE", [c, acell], "D", faces=[(c, acell)])
    if between is None:
        i2 = lss("MERGE", [acell, t], "D", faces=[(acell, t)])
        i3 = lss("DISCARD", [acell], 0)
    e_ai = ev("%s_med_init" % gid, "init", gadget=gid, lifetime="L_%s" % a, wire=a, state="Plus", role="mediator", cells=[list(acell)], lss_idx=i1)
    touch(e_ai, a)
    e_zz = ev("%s_zz" % gid, "measure_product", deps=q(cw) + q(a), gadget=gid, signed_observable={cw: "Z", a: "Z"}, sign=1, result_id=u,
              lifetimes=["L_%s" % cw, "L_%s" % a], cells=[list(c), list(acell)], lss_idx=i1)
    touch(e_zz, cw, a)
    if between is not None:
        between()
        i2 = lss("MERGE", [acell, t], "D", faces=[(acell, t)])
        i3 = lss("DISCARD", [acell], 0)
    e_xx = ev("%s_xx" % gid, "measure_product", deps=q(a) + q(tw), gadget=gid, signed_observable={a: "X", tw: "X"}, sign=1, result_id=v,
              lifetimes=["L_%s" % a, "L_%s" % tw], cells=[list(acell), list(t)], lss_idx=i2)
    touch(e_xx, a, tw)
    e_zr = ev("%s_zread" % gid, "measure_single", deps=q(a), gadget=gid, wire=a, observable="Z", sign=1, result_id=w, destructive=True, lss_idx=i3)
    touch(e_zr, a)
    e_ad = ev("%s_med_discard" % gid, "discard", deps=q(a), gadget=gid, lifetime="L_%s" % a, wire=a, discard_kind="measured", lss_idx=i3)
    touch(e_ad, a)
    e_f1 = ev("%s_frame_c" % gid, "frame_update", deps=q(cw) + [(e_xx, "classical")], gadget=gid, wire=cw, pauli="Z", predicate={"xor": [v]})
    touch(e_f1, cw)
    e_f2 = ev("%s_frame_t" % gid, "frame_update", deps=q(tw) + [(e_zz, "classical"), (e_zr, "classical")], gadget=gid, wire=tw, pauli="X", predicate={"xor": [u, w]})
    touch(e_f2, tw)
    gadget(gid, "CNOT_cf_patched", [e_ai, e_zz, e_xx, e_zr, e_ad, e_f1, e_f2], {"c": cw, "t": tw}, {"u": u, "v": v, "w": w})



def hadamard(cells, n, wire):
    c = cells[wire]; gid = "g_H%d" % n
    i = lss("IDLE", [c], "D")
    e_rot = ev("%s_rotate" % gid, "rotate_boundary", deps=q(wire), gadget=gid, lifetime="L_%s" % wire, busy_region=[list(c)], duration="D", from_orientation="ZX", to_orientation="XZ", lss_idx=i)
    touch(e_rot, wire)
    e_h = ev("%s_clifford" % gid, "clifford", deps=q(wire), gadget=gid, wires=[wire], gate="H")
    touch(e_h, wire)
    gadget(gid, "H", [e_rot, e_h], {"q": wire}, {})


def pauli(cells, n, wire, gate, predicate=None, elementary=False):
    """A Pauli gate as the converter emits it (carleman_2_4 g35: a gadget of template X / Y / Z whose only member is a
    `clifford` event with that gate and NO instruction -- a software frame flip, certify.py "frame_update only").
    `predicate`: a gadget-level predicate (the converter emits none on a Pauli; revision-9 regression M2 / N2).
    `elementary`: the bare `clifford` event outside any gadget (the converter emits none; regression M3 / N1)."""
    gid = "g_%s%d" % (gate, n)
    e = ev("%s_clifford" % gid, "clifford", deps=q(wire), gadget=None if elementary else gid, wires=[wire], gate=gate)
    touch(e, wire)
    if not elementary:
        gadget(gid, gate, [e], {"q": wire}, {}, predicate=predicate)


def outputs(cells):
    for k, w in enumerate(cells):
        i = lss("DISCARD", [cells[w]], 0)
        e_m = ev("meas_%s" % w, "measure_single", deps=q(w), wire=w, observable="Z", sign=1, result_id="m%d" % k, destructive=True, lss_idx=i)
        touch(e_m, w)
        e_d = ev("discard_%s" % w, "discard", deps=q(w), lifetime="L_%s" % w, wire=w, discard_kind="measured", lss_idx=i)
        touch(e_d, w)
        ev("io_out_%s" % w, "io", deps=[(e_m, "classical")], io_kind="output", wire=w, commit_event=e_m)


DEFAULT_PROGRAM = (("T", 0, "q0", (1, 0)), ("CNOT", 0, "q0", "q1", (0, 1)), ("H", 0, "q0"), ("CNOT", 1, "q1", "q2", (0, 3)), ("T", 1, "q1", (1, 2)))


def build(program=DEFAULT_PROGRAM, bench="fixture3", cells=CELLS):
    """(event document, .lss text) of `program`: a sequence of ("T", n, wire, rcell), ("CNOT", n, cw, tw, acell),
    ("H", n, wire), ("X" | "Y" | "Z", n, wire), or a callable emitting its own gadgets (module-level emitters)."""
    reset()
    inputs(cells)
    for op in program:
        if callable(op):
            op()
        elif op[0] == "T":
            t_gadget(cells, *op[1:])
        elif op[0] == "CNOT":
            cnot(cells, *op[1:])
        elif op[0] == "H":
            hadamard(cells, *op[1:])
        elif op[0] in ("X", "Y", "Z"):
            pauli(cells, op[1], op[2], op[0])
        else:
            raise ValueError(op)
    outputs(cells)
    header = dict(bench=bench, program_wires=list(cells), conventions=dict(bit0="+1 eigenvalue of the signed observable",
                  durations="symbolic D / HALF_D / HALF_D_PLUS_2 as in the .lss"), template_inventory=sorted({g["template_id"] for g in GADGETS.values()}),
                  generator="froq/tests_fixtures/make_fixture.py")
    doc = copy.deepcopy(dict(header=header, events=EVENTS, gadgets=GADGETS, lss_map=LSS_MAP))   # a later build() resets the module state
    lines = []
    for i, row in enumerate(LSS):
        name, patches, dur, cond_on, cond_deps, cc, ccd, faces, group, gname = row
        fmt = lambda x: str(x).replace(" ", "")
        lines.append("%s %d %s %s %s %s %s %s %s %s %s" % (name, i, fmt(patches), dur, fmt(cond_on), fmt(cond_deps), fmt(cc), fmt(ccd), fmt(faces), fmt(group), gname))
    return doc, "\n".join(lines)


def main():
    doc, text = build()
    open(os.path.join(HERE, "fixture3.lss"), "w").write(text)      # no trailing newline: upstream parses every line
    json.dump(doc, open(os.path.join(HERE, "fixture3.events.json"), "w"), indent=1)
    print("wrote fixture3: %d lss instructions, %d events, %d gadgets" % (len(LSS), len(EVENTS), len(GADGETS)))


if __name__ == "__main__":
    main()
