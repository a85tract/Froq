"""Route A WP2 -- numerical certificates for the compiler's lattice-surgery templates.

Design: docs/route-a-semantic-recovery-design.md, Sec. 1.1 (templates and the CNOT defect), Sec. 2.2 (canonical
residuals, the library Lib), Sec. 2.3 (recovery scripts), Sec. 3 (C1 / C2 / C3).

Templates are the pinned compiler's actual lowering (liblsqecc e88b1a4,
src/ls_instructions/ls_instructions_from_gates.cpp and teleported_s_gate_injection_stream.cpp):
  * every joint measurement is a projective Pauli-product measurement, both outcomes kept;
  * measurement bit 0 <-> eigenvalue +1;
  * an ancilla / resource patch is read out in a single-qubit Pauli basis and then discarded (traced out);
  * frame updates are outcome-conditioned Paulis on the live wires (no physical work).

Certificates (all exact linear algebra in numpy, complex128, tolerance 1e-9):
  C1  for every template the Kraus operators {K_r} (one per outcome record r) on its live wires, frame applied,
      satisfy K_r = lambda_r U for the intended U with sum |lambda_r|^2 = 1;
  C2  for every template and every canonical residual E = P Q (P Pauli, Q in Lib) on the template's wires plus one
      spectator wire, an identity K_r E = E'_r K_f(r) with E'_r canonical, Q' independent of r, f a relabel of the
      outcome record; or "fence";
  C3  for every Q in Lib on <= 2 wires the recovery script of Sec. 2.3, lowered to certified templates, implements
      conjugation by Q^dagger as a CP map (Choi matrices compared) after summing over all private outcomes.

Conventions.  Wires are numbered by position in a template's wire list.  rot(s, P') = exp(s * i * pi * P' / 4),
so S ~ rot(-1, Z) and S^dagger ~ rot(+1, Z) up to global phase.  Canonical key: "P=X0.Z1|Q=rot(+1,Z0Z1)".
The decomposition E = P Q is not unique (rot(+1, A) ~ A rot(-1, A)); the canonical representative is the first in
the order (weight of P, P key, Q key), which gives e.g. T X T^dagger ~ S X -> "P=X0|Q=rot(+1,Z0)" as in Sec. 2.2.
"""
from __future__ import annotations

import itertools
import json
import os

import numpy as np

TOL = 1e-9
SQ2 = np.sqrt(2.0)
CERT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certificates")

# ----------------------------------------------------------------------------------------------------------------
# Elementary matrices
# ----------------------------------------------------------------------------------------------------------------
I2 = np.eye(2, dtype=complex)
PX = np.array([[0, 1], [1, 0]], dtype=complex)
PY = np.array([[0, -1j], [1j, 0]], dtype=complex)
PZ = np.array([[1, 0], [0, -1]], dtype=complex)
PAULI = {"I": I2, "X": PX, "Y": PY, "Z": PZ}
H_GATE = np.array([[1, 1], [1, -1]], dtype=complex) / SQ2
S_GATE = np.array([[1, 0], [0, 1j]], dtype=complex)
T_GATE = np.array([[1, 0], [0, np.exp(1j * np.pi / 4)]], dtype=complex)
CNOT_GATE = np.array([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 0, 1], [0, 0, 1, 0]], dtype=complex)  # wire 0 = control
CZ_GATE = np.diag([1, 1, 1, -1]).astype(complex)

KET_ZERO = np.array([1, 0], dtype=complex)
KET_PLUS = np.array([1, 1], dtype=complex) / SQ2
KET_Y = np.array([1, 1j], dtype=complex) / SQ2                        # |Y> = S|+>
KET_T = np.array([1, np.exp(1j * np.pi / 4)], dtype=complex) / SQ2   # |T> = T|+>
# eigenvector of the single-qubit Pauli `basis` with eigenvalue (-1)^bit
EIGVEC = {
    ("Z", 0): KET_ZERO, ("Z", 1): np.array([0, 1], dtype=complex),
    ("X", 0): KET_PLUS, ("X", 1): np.array([1, -1], dtype=complex) / SQ2,
    ("Y", 0): KET_Y, ("Y", 1): np.array([1, -1j], dtype=complex) / SQ2,
}


def kron(*ops):
    out = np.array([[1.0 + 0j]])
    for op in ops:
        out = np.kron(out, op)
    return out


def pauli_matrix(letters: str) -> np.ndarray:
    return kron(*[PAULI[c] for c in letters])


def embed(op: np.ndarray, wires, n: int) -> np.ndarray:
    """Place `op` (acting on len(wires) qubits, in the order given) on wires `wires` of an n-wire register."""
    wires = list(wires)
    k = len(wires)
    rest = [w for w in range(n) if w not in wires]
    full = np.kron(op, np.eye(2 ** (n - k), dtype=complex))
    order = wires + rest                      # axis p of `full` is wire order[p]
    inv = list(np.argsort(order))             # new axis w' takes old axis inv[w']
    t = full.reshape([2] * (2 * n)).transpose(inv + [n + i for i in inv])
    return t.reshape(2 ** n, 2 ** n)


def rot(sign: int, letters: str) -> np.ndarray:
    """exp(sign * i * pi/4 * P) = (I + sign * i * P) / sqrt(2) for a Pauli product P (P^2 = I)."""
    P = pauli_matrix(letters)
    return (np.eye(P.shape[0], dtype=complex) + sign * 1j * P) / SQ2


def proportional(A: np.ndarray, B: np.ndarray):
    """Return lambda with A = lambda * B (exactly, to tolerance), else None."""
    nb = np.vdot(B, B).real
    if nb < TOL:
        return None
    lam = np.vdot(B, A) / nb
    if np.linalg.norm(A - lam * B) <= 1e-8 * max(1.0, np.linalg.norm(A)):
        return lam
    return None


def is_scaled_unitary(M: np.ndarray) -> bool:
    d = M.shape[0]
    G = M.conj().T @ M
    s = np.trace(G).real / d
    return s > TOL and np.linalg.norm(G - s * np.eye(d)) <= 1e-8 * max(1.0, s)


def weight(letters: str) -> int:
    return sum(1 for c in letters if c != "I")


def pauli_key(letters: str) -> str:
    """'XIZ' -> 'X0.Z2'; identity -> 'I'."""
    parts = [f"{c}{i}" for i, c in enumerate(letters) if c != "I"]
    return ".".join(parts) if parts else "I"


def rot_key(sign: int, letters: str) -> str:
    return f"rot({sign:+d},{''.join(f'{c}{i}' for i, c in enumerate(letters) if c != 'I')})"


def residual_key(pkey: str, qkey: str) -> str:
    return f"P={pkey}|Q={qkey}"


# ----------------------------------------------------------------------------------------------------------------
# Kraus operators of a template lowering
# ----------------------------------------------------------------------------------------------------------------
class Circuit:
    """A template lowering: live wires 0..n_live-1, ancillas n_live.. (initial states given), then a list of steps.

    Steps: ("mpm", {wire: letter}, bit)  joint Pauli-product measurement, outcome bit recorded;
           ("readout", anc_wire, basis, bit)  single-qubit measurement of an ancilla (its last use);
           ("gate", U, [wires]);  ("cgate", U, [wires], bit)  gate applied iff bit == 1;
           the frame is a function bits -> Pauli letters on the live wires, applied last.
    """

    def __init__(self, n_live: int, anc_inits, frame=None):
        self.n_live = n_live
        self.anc_inits = list(anc_inits)
        self.n = n_live + len(self.anc_inits)
        self.steps = []
        self.bits = []
        self.frame = frame or (lambda bits: "I" * n_live)

    def mpm(self, obs: dict, bit: str):
        self.steps.append(("mpm", dict(obs), bit)); self.bits.append(bit); return self

    def readout(self, anc_wire: int, basis: str, bit: str):
        self.steps.append(("readout", anc_wire, basis, bit)); self.bits.append(bit); return self

    def gate(self, U, wires):
        self.steps.append(("gate", U, list(wires))); return self

    def cgate(self, U, wires, bit):
        self.steps.append(("cgate", U, list(wires), bit)); return self

    def kraus(self) -> dict:
        """{record tuple (bits in self.bits order): K on the live wires, frame applied}."""
        n, nl = self.n, self.n_live
        d = 2 ** n
        ket = kron(np.eye(2 ** nl, dtype=complex), kron(*[v.reshape(2, 1) for v in self.anc_inits]))
        out = {}
        for rec in itertools.product((0, 1), repeat=len(self.bits)):
            bits = dict(zip(self.bits, rec))
            M = np.eye(d, dtype=complex)
            final = {}
            for step in self.steps:
                if step[0] == "mpm":
                    _, obs, bit = step
                    letters = "".join(obs.get(w, "I") for w in range(n))
                    P = pauli_matrix(letters)
                    M = 0.5 * (np.eye(d) + (-1) ** bits[bit] * P) @ M
                elif step[0] == "readout":
                    _, w, basis, bit = step
                    v = EIGVEC[(basis, bits[bit])]
                    M = embed(np.outer(v, v.conj()), [w], n) @ M
                    final[w] = v
                elif step[0] == "gate":
                    M = embed(step[1], step[2], n) @ M
                elif step[0] == "cgate":
                    if bits[step[3]] == 1:
                        M = embed(step[1], step[2], n) @ M
            assert sorted(final) == list(range(nl, n)), "every ancilla must be read out"
            bra = kron(np.eye(2 ** nl, dtype=complex), kron(*[final[w].conj().reshape(1, 2) for w in range(nl, n)]))
            K = bra @ M @ ket
            out[rec] = pauli_matrix(self.frame(bits)) @ K
        return out


class Template:
    def __init__(self, tid, wires, bits, kraus, intended, description, source):
        self.id = tid
        self.wires = list(wires)          # live wire labels, position = wire index
        self.bits = list(bits)            # outcome-bit names in record order ('' entries for variable records)
        self.kraus = kraus                # {record: K}
        self.intended = intended          # matrix (or None if there is no single intended unitary)
        self.description = description
        self.source = source

    @property
    def n(self):
        return len(self.wires)


def record_str(rec) -> str:
    return "".join(str(b) for b in rec)


# ----------------------------------------------------------------------------------------------------------------
# The templates
# ----------------------------------------------------------------------------------------------------------------
def frame_bits(fmt):
    """fmt: list of (wire, letter, fn(bits)->0/1); returns a frame function."""
    def f(bits):
        letters = []
        n = 1 + max([w for w, _, _ in fmt], default=0)
        for w in range(n):
            x = z = 0
            for ww, letter, fn in fmt:
                if ww == w and fn(bits):
                    if letter == "X": x ^= 1
                    elif letter == "Z": z ^= 1
                    elif letter == "Y": x ^= 1; z ^= 1
            letters.append({(0, 0): "I", (1, 0): "X", (0, 1): "Z", (1, 1): "Y"}[(x, z)])
        return "".join(letters)
    return f


def s_gadget_circuit(frame_extra_z: int = 0) -> Circuit:
    """Teleported S (Twists mode): resource |Y>, measure Z_q Z_y (u), read y in X (w), frame Z_q^(u xor w [xor 1])."""
    c = Circuit(1, [KET_Y], frame=frame_bits([(0, "Z", lambda b: b["u"] ^ b["w"] ^ frame_extra_z)]))
    return c.mpm({0: "Z", 1: "Z"}, "u").readout(1, "X", "w")


def sx_gadget_circuit(frame_extra_x: int = 0) -> Circuit:
    """Teleported S on the target's X boundary: resource |Y> to the right of the target, measure X_q X_y (u),
    read y in Z (w), frame X_q^(1 xor u xor w [xor 1]). Intended rot(-1, X) = SQRT_X; with the extra 1, rot(+1, X)."""
    c = Circuit(1, [KET_Y], frame=frame_bits([(0, "X", lambda b: 1 ^ b["u"] ^ b["w"] ^ frame_extra_x)]))
    return c.mpm({0: "X", 1: "X"}, "u").readout(1, "Z", "w")


def t1_circuit() -> Circuit:
    """T injection segment T1: magic state |T>, measure Z_q Z_a (u), read a in X (w), frame Z_q^w."""
    c = Circuit(1, [KET_T], frame=frame_bits([(0, "Z", lambda b: b["w"])]))
    return c.mpm({0: "Z", 1: "Z"}, "u").readout(1, "X", "w")


def cnot_circuit(order: str, anc_init, readout_basis: str, frame=None) -> Circuit:
    """Compiler CNOT lowering.  Wires: 0 = control, 1 = target, 2 = mediator ancilla."""
    c = Circuit(2, [anc_init], frame=frame)
    if order == "control_first":
        c.mpm({0: "Z", 2: "Z"}, "u").mpm({2: "X", 1: "X"}, "v")
    else:
        c.mpm({2: "X", 1: "X"}, "v").mpm({0: "Z", 2: "Z"}, "u")
    return c.readout(2, readout_basis, "w")


CNOT_PATCHED_FRAME = frame_bits([(0, "Z", lambda b: b["v"]), (1, "X", lambda b: b["u"] ^ b["w"])])


def compose_kraus(first: dict, second_for_record) -> dict:
    """Sequential composition; second_for_record(rec1) -> {rec2: K2}; records are concatenated."""
    out = {}
    for r1, K1 in first.items():
        for r2, K2 in second_for_record(r1).items():
            out[r1 + r2] = K2 @ K1
    return out


def derive_pauli_frame(kraus_unframed: dict, U: np.ndarray, n_live: int):
    """For each record find the Pauli F with F K_r ~ U; returns {rec: letters} or None if any record has none."""
    out = {}
    for rec, K in kraus_unframed.items():
        found = None
        for letters in itertools.product("IXYZ", repeat=n_live):
            letters = "".join(letters)
            if proportional(pauli_matrix(letters) @ K, U) is not None:
                found = letters
                break
        if found is None:
            return None
        out[rec] = found
    return out


def fit_affine_bool(values: dict, bit_names):
    """values: {record tuple: 0/1}; find c0 xor sum c_i b_i reproducing all; return formula string or None."""
    nb = len(bit_names)
    for coeffs in itertools.product((0, 1), repeat=nb + 1):
        ok = all((coeffs[0] ^ sum(c * b for c, b in zip(coeffs[1:], rec))) % 2 == v for rec, v in values.items())
        if ok:
            terms = [name for c, name in zip(coeffs[1:], bit_names) if c]
            if coeffs[0]:
                terms = ["1"] + terms
            return "+".join(terms) if terms else "0"
    return None


def pauli_formula(p_by_record: dict, bit_names):
    """Write an outcome-dependent Pauli as X_i^(f) Z_i^(g) with affine Boolean exponents, or None."""
    n = len(next(iter(p_by_record.values())))
    parts = []
    xz = {"I": (0, 0), "X": (1, 0), "Z": (0, 1), "Y": (1, 1)}
    for w in range(n):
        for comp, idx in (("X", 0), ("Z", 1)):
            vals = {rec: xz[letters[w]][idx] for rec, letters in p_by_record.items()}
            f = fit_affine_bool(vals, bit_names)
            if f is None:
                return None
            if f == "0":
                continue
            parts.append(f"{comp}{w}" if f == "1" else f"{comp}{w}^({f})")
    return ".".join(parts) if parts else "I"


def build_templates() -> dict:
    src_gates = "ls_instructions_from_gates.cpp"
    src_s = "teleported_s_gate_injection_stream.cpp"
    T = {}

    def add(tid, wires, bits, kraus, intended, description, source):
        T[tid] = Template(tid, wires, bits, kraus, intended, description, source)

    # Paulis: frame only (no physical instruction).
    for p in "XYZ":
        add(p, ["q"], [], {(): PAULI[p]}, PAULI[p], f"Pauli {p}: frame_update only", "frame")
    # H: transversal logical H (the boundary rotation is geometric).
    add("H", ["q"], [], {(): H_GATE}, H_GATE, "transversal logical H (SingleQubitOp H)", src_gates)
    # Teleported S (Twists mode) and S^dagger = S gadget then frame Z.
    add("S", ["q"], ["u", "w"], s_gadget_circuit(0).kraus(), S_GATE,
        "teleported S: |Y> resource; Z_q Z_y (u); X_y readout (w); frame Z_q^(u xor w)", src_s)
    add("Sdg", ["q"], ["u", "w"], s_gadget_circuit(1).kraus(), S_GATE.conj().T,
        "S^dagger for recovery: the S gadget followed by frame Z_q  (frame Z_q^(u xor w xor 1))", src_s + " + frame")
    # T injection segments.
    t1 = t1_circuit().kraus()
    add("T1", ["q"], ["u", "w"], t1, None,
        "T injection segment T1: |T> resource; Z_q Z_a (u); X_a readout (w); frame Z_q^w.  "
        "Branch operator ~ T for u = 0 and ~ T^dagger for u = 1 (S^u is applied by T2)", src_gates)
    # T2 = the conditional S, executed iff u = 1; in Twists mode the S instruction is the teleported S gadget.
    s_k = T["S"].kraus
    t2_full = compose_kraus({(0,): np.eye(2, dtype=complex), (1,): np.eye(2, dtype=complex)},
                            lambda r: s_k if r[0] == 1 else {(): np.eye(2, dtype=complex)})
    add("T2", ["q"], ["u(pred)", "..."], t2_full, None,
        "T injection segment T2: S iff u = 1 (records: '0' = skip; '1uw' = the S gadget with its bits u, w)",
        src_gates + " (SingleQubitOp S, predicated on u) -> " + src_s)
    add("T2_S", ["q"], ["u", "w"], s_k, S_GATE, "T2 taken branch (u = 1): the teleported S gadget", src_s)
    add("T2_skip", ["q"], [], {(): np.eye(2, dtype=complex)}, np.eye(2, dtype=complex),
        "T2 skipped branch (u = 0): nothing executed", src_gates)
    # Composite T = T1 then T2 (S gadget when u = 1).
    t_full = compose_kraus(t1, lambda r: s_k if r[0] == 1 else {(): np.eye(2, dtype=complex)})
    add("T", ["q"], ["u", "w", "..."], t_full, T_GATE,
        "T injection = T1 then T2 (records: 'uw' for u = 0; 'uw' + S gadget bits for u = 1)", src_gates)
    add("T_ideal", ["q"], ["u", "w"], compose_kraus(t1, lambda r: {(): S_GATE if r[0] == 1 else np.eye(2)}),
        T_GATE, "T injection with T2 modelled as an ideal conditional S (4 records; used for the C2 T row)",
        src_gates)
    add("Tdg_as_compiled", ["q"], ["u", "w", "..."], t_full, T_GATE.conj().T,
        "T^dagger as the compiler lowers it: make_t_gate_instructions ignores is_dagger, so the SAME "
        "instructions are emitted and T is executed.  Deliberately failing certificate (documentation).", src_gates)
    # CNOTs.
    add("CNOT_cf_patched", ["c", "t"], ["u", "v", "w"],
        cnot_circuit("control_first", KET_PLUS, "Z", CNOT_PATCHED_FRAME).kraus(), CNOT_GATE,
        "patched ZX_WITH_MBM_CONTROL_FIRST: a in |+>; Z_c Z_a (u); X_a X_t (v); Z_a readout (w); "
        "frame Z_c^v X_t^(u xor w)", src_gates + " + D1 patch")
    # CZ: ls_instruction_stream.cpp lowers a ControlledGate with a Z target (`cz q[a],q[b]`: control a = first QASM
    # argument, target b = second) as SingleQubitOp H on the TARGET, make_cnot_instructions(control, target, same
    # cnot_type / ancilla placement annotations as a CNOT), SingleQubitOp H on the target again.
    Ht = np.kron(I2, H_GATE)
    add("CZ", ["c", "t"], ["u", "v", "w"], {r: Ht @ K @ Ht for r, K in T["CNOT_cf_patched"].kraus.items()}, CZ_GATE,
        "CZ as emitted: H(t); ZX_WITH_MBM_CONTROL_FIRST CNOT(c -> t) [patched: Z_a readout + frame]; H(t).  Mediator "
        "placement = the CNOT's annotation / default rule.  With the unpatched CNOT inside, every branch has rank 2 "
        "(H is unitary), i.e. the emitted CZ is not a CZ before the D1 patch.",
        "ls_instruction_stream.cpp (ControlledGate, target gate Z) + " + src_gates + " + D1 patch")
    add("CNOT_cf_unpatched", ["c", "t"], ["u", "v", "w"],
        cnot_circuit("control_first", KET_PLUS, "X").kraus(), CNOT_GATE,
        "pinned ZX_WITH_MBM_CONTROL_FIRST as emitted: a in |+>; Z_c Z_a (u); X_a X_t (v); X_a readout (w). "
        "Deliberately failing certificate (Finding R2): every branch has rank 2.", src_gates)
    # Target-first with the compiler's |+> ancilla and the Z readout patch (as requested): derive a frame if any.
    tf_unframed = cnot_circuit("target_first", KET_PLUS, "Z").kraus()
    tf_frame = derive_pauli_frame(tf_unframed, CNOT_GATE, 2)
    add("CNOT_tf_Zreadout", ["c", "t"], ["u", "v", "w"],
        {r: pauli_matrix(tf_frame[r]) @ K for r, K in tf_unframed.items()} if tf_frame else tf_unframed, CNOT_GATE,
        "ZX_WITH_MBM_TARGET_FIRST with the compiler's |+> ancilla and a Z readout: X_a X_t (v); Z_c Z_a (u); "
        "Z_a readout (w).  " + ("frame " + str(tf_frame) if tf_frame else
                                "NO Pauli frame makes it a CNOT: X_a X_t on a fresh |+> ancilla measures X_t."),
        src_gates)
    # Target-first dual that does work: ancilla |0>, X_a X_t (v), Z_c Z_a (u), X_a readout (w); derive the frame.
    tfd_unframed = cnot_circuit("target_first", KET_ZERO, "X").kraus()
    tfd_frame = derive_pauli_frame(tfd_unframed, CNOT_GATE, 2)
    assert tfd_frame is not None
    tfd_formula = pauli_formula(tfd_frame, ["u", "v", "w"])
    add("CNOT_tf_dual", ["c", "t"], ["u", "v", "w"],
        {r: pauli_matrix(tfd_frame[r]) @ K for r, K in tfd_unframed.items()}, CNOT_GATE,
        f"target-first dual (NOT what the compiler emits): a in |0>; X_a X_t (v); Z_c Z_a (u); X_a readout (w); "
        f"derived frame {tfd_formula}", "derived")
    T["CNOT_tf_dual"].frame_formula = tfd_formula
    T["CNOT_tf_dual"].frame_table = {record_str(r): pauli_key(f) for r, f in tfd_frame.items()}
    return T


def build_templates_sx() -> dict:
    """`build_templates` (unchanged) plus SX = rot(-1, X) and SXdg = rot(+1, X): the SX gadget with frame
    X^(1 xor u xor w) and X^(u xor w) (candidates/cheap-repair.md 6.3)."""
    T = build_templates()
    src = "SX gadget (r18; cheap-repair.md 6.1, 6.3): teleported_s_gate_injection_stream.cpp's S gadget on the X boundary"
    T["SX"] = Template("SX", ["q"], ["u", "w"], sx_gadget_circuit(0).kraus(), rot(-1, "X"),
                       "teleported S on the X boundary: |Y> resource to the right; X_q X_y (u); Z_y readout (w); "
                       "frame X_q^(1 xor u xor w)", src)
    T["SXdg"] = Template("SXdg", ["q"], ["u", "w"], sx_gadget_circuit(1).kraus(), rot(+1, "X"),
                         "SX^dagger for recovery: the SX gadget followed by frame X_q  (frame X_q^(u xor w))",
                         src + " + frame")
    return T


# ----------------------------------------------------------------------------------------------------------------
# C1
# ----------------------------------------------------------------------------------------------------------------
def certify_c1(templates: dict) -> dict:
    out = {}
    for tid, t in templates.items():
        entry = {"wires": t.wires, "outcome_bits": t.bits, "description": t.description, "source": t.source,
                 "n_records": len(t.kraus)}
        if tid == "T1":
            # branch-wise intended: T for u = 0, T^dagger for u = 1 (S^u is deferred to T2)
            lam2 = {}
            ok = True
            for rec, K in t.kraus.items():
                U = T_GATE if rec[0] == 0 else T_GATE.conj().T
                lam = proportional(K, U)
                ok &= lam is not None
                lam2[record_str(rec)] = None if lam is None else round(abs(lam) ** 2, 12)
            total = sum(v for v in lam2.values() if v is not None)
            entry.update({"intended_unitary": "T if u = 0 else T^dagger (S^u applied by T2)",
                          "PASS": bool(ok and abs(total - 1) < 1e-9), "lambda_sq": lam2,
                          "sum_lambda_sq": round(total, 12)})
        elif tid == "T2":
            lam2 = {}
            ok = True
            for rec, K in t.kraus.items():
                U = S_GATE if rec[0] == 1 else np.eye(2)
                lam = proportional(K, U)
                ok &= lam is not None
                lam2[record_str(rec)] = None if lam is None else round(abs(lam) ** 2, 12)
            entry.update({"intended_unitary": "S^u (u = the decoded T1 ZZ result)", "PASS": bool(ok),
                          "lambda_sq": lam2,
                          "note": "records for u = 1 sum to 1 over the S gadget's private bits; u = 0 is one record"})
        else:
            lam2 = {}
            ranks = {}
            ok = True
            for rec, K in t.kraus.items():
                lam = proportional(K, t.intended)
                ok &= lam is not None
                lam2[record_str(rec)] = None if lam is None else round(abs(lam) ** 2, 12)
                ranks[record_str(rec)] = int(np.linalg.matrix_rank(K, tol=1e-9))
            total = sum(v for v in lam2.values() if v is not None)
            entry.update({"intended_unitary": {"X": "X", "Y": "Y", "Z": "Z", "H": "H", "S": "S", "Sdg": "S^dagger",
                                               "SX": "SX = rot(-1,X)", "SXdg": "SX^dagger = rot(+1,X)", "T2_S": "S",
                                               "T2_skip": "I", "T": "T", "T_ideal": "T",
                                               "Tdg_as_compiled": "T^dagger", "CZ": "CZ(c,t)"}.get(tid, "CNOT(c->t)"),
                          "PASS": bool(ok and abs(total - 1) < 1e-9), "lambda_sq": lam2,
                          "sum_lambda_sq": round(total, 12)})
            if not entry["PASS"]:
                entry["branch_ranks"] = ranks
                entry["eig_KdagK"] = {record_str(rec): [round(float(e), 9) for e in
                                                        np.linalg.eigvalsh(K.conj().T @ K)]
                                      for rec, K in t.kraus.items()}
                if tid == "Tdg_as_compiled":
                    entry["deviation"] = ("compiler executes T for a T^dagger gate (is_dagger ignored); recorded, "
                                          "not fixed (design Sec. 1.2)")
                if tid == "CNOT_tf_Zreadout":
                    entry["deviation"] = ("target-first with the compiler's |+> ancilla: the first merge X_a X_t "
                                          "projects the target onto an X eigenstate; no frame repairs a rank loss")
            if hasattr(t, "frame_formula"):
                entry["derived_frame"] = {"formula": t.frame_formula, "table": t.frame_table}
        out[tid] = entry
    out["RESET"] = {
        "wires": ["q"], "outcome_bits": [], "n_records": 0, "kind": "non-unitary", "PASS": None,
        "intended_unitary": None,
        "description": (
            "QASM `reset q[i]` -> PatchReset{i} (ls_instruction_stream.cpp).  Contract semantics: a reset of a program "
            "wire is a destructive operation on that wire -- the state is discarded and a fresh |0> patch appears; the "
            "design (Sec. 1.3) treats it as `discard {traced}` of the old lifetime followed by `init {Zero}` of a new "
            "one, so a reset of a wire in L is a fence (Sec. 2.2 discard rule).  Not a gadget a residual passes "
            "through: no Kraus certificate."),
        "compiler_semantics": (
            "pinned slicer: dense_patch_computation.cpp try_apply_instruction_direct_followup returns {nullptr, {}} "
            "for PatchReset -- no patch change, no measurement, no follow-up instruction; wave_scheduler.cpp "
            "is_immediate -> true (zero duration); ls_instructions.cpp lists the target as an operating patch and "
            "prints the .lss line `Reset <id>`.  The pinned compiler therefore executes NO reset (the patch keeps "
            "its state); the discard + init semantics is executed only if the fork implements it."),
        "source": "ls_instruction_stream.cpp:154-160, dense_patch_computation.cpp:1442, wave_scheduler.cpp:258, "
                  "ls_instructions.cpp:56,210",
    }
    # w10 (review concern 4): `measure q[i] -> m[0]`, the destructive Z readout of a program wire that the
    # measurement-based uncomputation of a temporary AND ends with (Gidney, arXiv:1709.06648; the fork's
    # SCOUTQ_FORK_W10.md lowers it to MeasureSinglePatch{q, Z}; the patch then leaves the lattice).  A measurement, not a
    # unitary: the certificate is that the emitted instrument IS the projective Z instrument {m: |m><m|} (every record's
    # Kraus operator equals its projector, and they are complete).  No frame: nothing is applied to the destroyed wire.
    meas_k = {(0,): np.diag([1, 0]).astype(complex), (1,): np.diag([0, 1]).astype(complex)}   # MeasureSinglePatch Z
    proj = {(m,): np.outer(EIGVEC[("Z", m)], EIGVEC[("Z", m)].conj()) for m in (0, 1)}
    complete = np.allclose(sum(K.conj().T @ K for K in meas_k.values()), np.eye(2))
    out["MEASURE"] = {
        "wires": ["q"], "outcome_bits": ["m"], "n_records": 2, "kind": "measurement",
        "intended_instrument": "projective Z measurement {m: |m><m|}; the wire is discarded after it",
        "PASS": bool(complete and all(np.allclose(meas_k[r], proj[r]) for r in meas_k)),
        "lambda_sq": {"0": 1.0, "1": 1.0},
        "description": ("QASM `measure q[i] -> m[0]` (w10 fork) -> MeasureSinglePatch{i, Z}: a destructive readout of a "
                        "program patch whose bit m is a program result (the classically controlled CZ of the "
                        "uncomputation reads it).  The contract walk treats it as an elementary destructive measurement "
                        "(signed-commutation test and instrument reduction)."),
        "source": "ls_instruction_stream.cpp (Measure, w10) + dense_patch_computation.cpp (SinglePatchMeasurement)"}
    return out


# ----------------------------------------------------------------------------------------------------------------
# Canonical residuals and the library
# ----------------------------------------------------------------------------------------------------------------
class Canon:
    """Canonical decomposition E = P Q (P Pauli, Q in Lib) on n wires, up to global phase. Lib's axes have weight at
    most `max_weight` (2: the library's Lib; own-reach rows, froq/ur_reach.py, take every axis on n wires)."""

    def __init__(self, n: int, max_weight: int = 2):
        self.n = n
        self.d = 2 ** n
        self.max_weight = max_weight
        self.paulis = ["".join(p) for p in itertools.product("IXYZ", repeat=n)]
        self.lib = [("I", np.eye(self.d, dtype=complex))]
        for letters in self.paulis:
            if 1 <= weight(letters) <= max_weight:
                for s in (+1, -1):
                    self.lib.append((rot_key(s, letters), rot(s, letters)))
        cands = []
        for p in self.paulis:
            Pm = pauli_matrix(p)
            for qkey, Qm in self.lib:
                cands.append(((weight(p), pauli_key(p), qkey), pauli_key(p), qkey, Pm @ Qm))
        cands.sort(key=lambda c: c[0])
        self.cands = [(c[1], c[2], c[3]) for c in cands]
        self.Cc = (np.array([c[2].flatten() for c in self.cands]) / np.sqrt(self.d)).conj()

    def decompose(self, M: np.ndarray):
        if M.shape != (self.d, self.d) or not is_scaled_unitary(M):
            return None
        v = M.flatten()
        ov = np.abs(self.Cc @ v) / np.linalg.norm(v)
        hits = np.nonzero(ov > 1 - 1e-9)[0]
        if len(hits) == 0:
            return None
        pk, qk, _ = self.cands[hits[0]]
        return pk, qk

    def key(self, M: np.ndarray):
        dec = self.decompose(M)
        return None if dec is None else residual_key(*dec)

    def residuals(self) -> dict:
        """Distinct canonical residuals on n wires: {key: (pkey, qkey, matrix of the canonical representative)}.

        A candidate is canonical iff it is the first candidate (in canonical order) proportional to itself; the
        overlaps are computed blockwise against the whole candidate set.
        """
        if hasattr(self, "_residuals"):
            return self._residuals
        C = self.Cc.conj()
        out = {}
        block = 256
        for start in range(0, len(self.cands), block):
            ov = np.abs(self.Cc @ C[start:start + block].T)          # (N_cand, block)
            first = np.argmax(ov > 1 - 1e-9, axis=0)                 # first canonical-order match per column
            for j, idx in enumerate(first):
                if idx == start + j:
                    pk, qk, M = self.cands[idx]
                    out[residual_key(pk, qk)] = (pk, qk, M)
        self._residuals = out
        return out


_CANON = {}


def canon(n: int, max_weight: int = 2) -> Canon:
    if (n, max_weight) not in _CANON:
        _CANON[(n, max_weight)] = Canon(n, max_weight)
    return _CANON[(n, max_weight)]


def canonicalize(matrix: np.ndarray, n_wires: int):
    """Key 'P=...|Q=...' if `matrix` ~ P Q with Q in Lib (up to global phase), else None."""
    return canon(n_wires).key(np.asarray(matrix, dtype=complex))


def lib_matrix(qkey: str, n: int) -> np.ndarray:
    for k, M in canon(n).lib:
        if k == qkey:
            return M
    raise KeyError(qkey)


def parse_qkey(qkey: str):
    """'rot(+1,Z0Z1)' -> (+1, {0: 'Z', 1: 'Z'}); 'I' -> (0, {})."""
    if qkey == "I":
        return 0, {}
    sign = int(qkey[4:6])
    body = qkey[7:-1]
    return sign, {int(body[i + 1]): body[i] for i in range(0, len(body), 2)}


# ----------------------------------------------------------------------------------------------------------------
# C2
# ----------------------------------------------------------------------------------------------------------------
def relabel_candidates(records):
    """Identity first, then XOR masks over the outcome bits (records of equal length only)."""
    recs = list(records)
    yield "identity", {r: r for r in recs}
    lengths = {len(r) for r in recs}
    if len(lengths) != 1:
        return
    nb = lengths.pop()
    rset = set(recs)
    for mask in itertools.product((0, 1), repeat=nb):
        if any(mask):
            f = {r: tuple(a ^ b for a, b in zip(r, mask)) for r in recs}
            if all(v in rset for v in f.values()):
                yield mask, f


def propagate(kraus_emb: dict, E: np.ndarray, cn: Canon, candidates):
    """Find f and canonical E'_r with K_r E = E'_r K_f(r) for all r (K_r ~ unitary).

    Returns (f_name, f, {r: (pkey, qkey)}) or None.  Among the valid relabels the first one whose Pauli factor
    P'_r is independent of r is preferred (the natural anticommutation relabel); otherwise the first valid one.
    """
    d = cn.d
    fallback = None
    seen = []                                            # (E'', decomposition) reused across proportional E''
    for fname, f in candidates:
        decs = {}
        qs = set()
        ok = True
        for r, K in kraus_emb.items():
            B = kraus_emb[f[r]]
            nb = np.vdot(B, B).real / d                  # |lambda_f(r)|^2
            Epp = K @ E @ B.conj().T / nb
            if np.linalg.norm(K @ E - Epp @ B) > 1e-8:
                ok = False
                break
            dec = next((dd for M, dd in seen if proportional(Epp, M) is not None), False)
            if dec is False:
                dec = cn.decompose(Epp)
                seen.append((Epp, dec))
            if dec is None:
                ok = False
                break
            decs[r] = dec
            qs.add(dec[1])
        if ok and len(qs) == 1:
            if len({p for p, _ in decs.values()}) == 1:
                return fname, f, decs
            if fallback is None:
                fallback = (fname, f, decs)
    return fallback


def signed_z(E: np.ndarray, wire: int, n: int):
    Zq = embed(PZ, [wire], n)
    lam = proportional(E @ Zq @ E.conj().T, Zq)
    if lam is None:
        return None
    return int(np.sign(lam.real))


def c2_table_for(kraus: dict, n_t: int, bit_names, candidates_fn, n_spectator: int = 1, t_wire=None) -> dict:
    n_tot = n_t + n_spectator
    cn = canon(n_tot)
    kraus_emb = {r: embed(K, list(range(n_t)), n_tot) for r, K in kraus.items()}
    table = {}
    for ekey, (pk, qk, E) in cn.residuals().items():
        res = propagate(kraus_emb, E, cn, candidates_fn(kraus_emb.keys()))
        entry = {}
        if res is None:
            entry["result"] = "fence"
        else:
            fname, f, decs = res
            qprime = next(iter(decs.values()))[1]
            ps = {r: p for r, (p, _) in decs.items()}
            if len(set(ps.values())) == 1:
                entry["result"] = residual_key(next(iter(ps.values())), qprime)
            else:
                p_letters = {}
                for r, pk_ in ps.items():
                    letters = ["I"] * n_tot
                    if pk_ != "I":
                        for part in pk_.split("."):
                            letters[int(part[1:])] = part[0]
                    p_letters[r] = "".join(letters)
                formula = pauli_formula(p_letters, bit_names) if len({len(r) for r in ps}) == 1 else None
                entry["result"] = {"Q": qprime,
                                   "P_formula": formula,
                                   "P_table": {record_str(r): p for r, p in ps.items()}}
            if fname == "identity":
                entry["relabel"] = "identity"
            else:
                entry["relabel"] = {"xor_mask": {b: m for b, m in zip(bit_names, fname)},
                                    "table": {record_str(r): record_str(v) for r, v in f.items()}}
        if t_wire is not None:
            sz = signed_z(E, t_wire, n_tot)
            entry["signed_Z"] = None if sz is None else sz
        table[ekey] = entry
    return table


def certify_c2(templates: dict) -> dict:
    out = {"_conventions": {
        "wires": "template wires by position, then one spectator wire (last index) not touched by the template",
        "key": "P=<Pauli>|Q=<I or rot(sign,P')>, rot(s,P') = exp(s i pi P'/4); S ~ rot(-1,Z), S^dagger ~ rot(+1,Z)",
        "canonical_choice": "first of (weight(P), P key, Q key) among the equivalent decompositions",
        "identity": "K_r E = E'_r K_f(r) for every record r; E'_r = P'_r Q' with Q' independent of r",
        "relabel": "identity, or an XOR mask on the outcome bits (table given)",
        "T2": "T2[pred=asis]: predicate u read as recorded; T2[pred=flipped]: predicate id relabelled by T1 (u -> u xor 1)",
        "CZ[pred=asis]": "w10: the CZ run iff an external measured bit m = 1 (K_0 = I, K_1 = CZ), m read as recorded",
        "signed_Z": "sign s with E Z_q E^dagger = s Z_q on the template wire q = 0, or null (design Sec. 2.2 T rule)",
    }}
    clifford = ["X", "Y", "Z", "H", "S", "Sdg", "CNOT_cf_patched", "CZ"]
    for tid in clifford:
        t = templates[tid]
        out[tid] = c2_table_for(t.kraus, t.n, t.bits, relabel_candidates)
    # T1: relabel by XOR mask; T rows carry the signed-Z test.
    t1 = templates["T1"]
    out["T1"] = c2_table_for(t1.kraus, 1, t1.bits, relabel_candidates, t_wire=0)
    # T2 with the predicate as recorded / relabelled by T1 (ideal S; the S gadget is ~ S on every branch by C1).
    t2_ideal = {(0,): np.eye(2, dtype=complex), (1,): S_GATE}
    for sigma, name in ((0, "T2[pred=asis]"), (1, "T2[pred=flipped]")):
        def cands(records, sigma=sigma):
            yield ("identity" if sigma == 0 else (1,)), {r: (r[0] ^ sigma,) for r in records}
        out[name] = c2_table_for(t2_ideal, 1, ["u"], cands, t_wire=0)
        for e in out[name].values():
            if "relabel" in e:
                e["relabel"] = f"predicate u read as u xor {sigma}"
    # w10: the classically controlled CZ of a measurement-based uncomputation, run iff the EXTERNAL measured bit m is 1.
    # The walk admits it only when m is not in D (the prediction cannot have changed m): the predicate is read as
    # recorded.  The CZ template is ~ CZ on every branch (C1), so the ideal CZ stands for it, as the ideal S does for
    # T2: the rows are K_m E = E'_m K_m for K_0 = I, K_1 = CZ; Q' must not depend on m (else fence) and P' may (affine in
    # m: the walk makes it a generator on the measured bit).
    cz_cond = {(0,): np.eye(4, dtype=complex), (1,): CZ_GATE}
    out["CZ[pred=asis]"] = c2_table_for(cz_cond, 2, ["m"], lambda records: iter([("identity", {r: r for r in records})]))
    for e in out["CZ[pred=asis]"].values():
        if "relabel" in e:
            e["relabel"] = "predicate m read as recorded (the measured bit of an earlier MEASURE, not in D)"
    # Composite T (T1 then ideal conditional S): all branches ~ T, identity relabel; signed-Z recorded.
    ti = templates["T_ideal"]
    out["T"] = c2_table_for(ti.kraus, 1, ti.bits, relabel_candidates, t_wire=0)
    return out


def t_rule_check(c2: dict) -> dict:
    """Design Sec. 2.2: T recoverable iff signed-Z AND T E T^dagger canonical.  Check against the T table."""
    cn = canon(2)
    Tm = embed(T_GATE, [0], 2)
    mismatches = []
    for ekey, entry in c2["T"].items():
        _, _, E = cn.residuals()[ekey]
        member = cn.key(Tm @ E @ Tm.conj().T) is not None
        rule = (entry["signed_Z"] is not None) and member
        if rule != (entry["result"] != "fence"):
            mismatches.append(ekey)
    return {"n_checked": len(c2["T"]), "mismatches": mismatches, "PASS": not mismatches}


def segment_chain_check(c2: dict) -> dict:
    """T1 row then T2[pred=sigma] row must reproduce the composite T row (or both fence)."""
    bad = []
    for ekey, e1 in c2["T1"].items():
        target = c2["T"][ekey]["result"]
        if e1["result"] == "fence":
            if target != "fence":
                bad.append(ekey)
            continue
        assert isinstance(e1["result"], str)
        sigma = 0 if e1["relabel"] == "identity" else e1["relabel"]["xor_mask"]["u"]
        e2 = c2["T2[pred=flipped]" if sigma else "T2[pred=asis]"][e1["result"]]
        if e2["result"] != target:
            bad.append(ekey)
    return {"n_checked": len(c2["T1"]), "mismatches": bad, "PASS": not bad}


# ----------------------------------------------------------------------------------------------------------------
# C3
# ----------------------------------------------------------------------------------------------------------------
def member(template: str, wires=None, pauli=None):
    return {"template": template, "wires": list(wires)} if pauli is None else {"template": "frame", "pauli": pauli}


def basis_change(axis: str, w: int):
    """Members applying U with U axis U^dagger = Z: U_Z = I, U_X = H, U_Y = H S^dagger (S^dagger first, then H)."""
    if axis == "Z":
        return []
    if axis == "X":
        return [member("H", [w])]
    return [member("S", [w]), member("frame", pauli=f"Z{w}"), member("H", [w])]


def basis_change_inverse(axis: str, w: int):
    if axis == "Z":
        return []
    if axis == "X":
        return [member("H", [w])]
    return [member("H", [w]), member("S", [w])]        # U^dagger = S H: H first, then S


def s_power(w: int, sign: int):
    """rot(sign, Z_w): sign = -1 is S (gadget); sign = +1 is S^dagger (gadget then frame Z)."""
    return [member("S", [w])] if sign == -1 else [member("S", [w]), member("frame", pauli=f"Z{w}")]


def literal_recovery_script(qkey: str):
    """The v5 library's script for Q^dagger (design Sec. 2.3 as first written): U^dagger S^(-+) U with the basis
    changes U_X = H and U_Y = H S^dagger, and W = CNOT_{a->b} (U_a x U_b) on two wires.  No longer the certified
    library (`recovery_script` is); kept so that old and new scripts can be lowered and compared on the same
    placements."""
    sign, support = parse_qkey(qkey)
    if not support:
        return []
    wires = sorted(support)
    if len(wires) == 1:
        (w,) = wires
        return basis_change(support[w], w) + s_power(w, -sign) + basis_change_inverse(support[w], w)
    a, b = wires
    return (basis_change(support[a], a) + basis_change(support[b], b) + [member("CNOT_cf_patched", [a, b])]
            + s_power(b, -sign) + [member("CNOT_cf_patched", [a, b])]
            + basis_change_inverse(support[b], b) + basis_change_inverse(support[a], a))


# Template timing at distance d as the converter lowers each recovery member (compile.py; C4's primitive-timing rule):
# an H is the transversal H plus the compiler's boundary rotation, three slices of a two-cell IDLE (Duration.D); the
# teleported S is its MERGE (D) on the target and the Y state, then the Y readout and the target's IDLE (HALF_D_PLUS_2);
# the patched CNOT is two MERGEs (D) of at least two cells each (routing cells come on top and depend on the layout).
# (critical path in rounds, patch-rounds).  Used only to CHOOSE among certified equivalent scripts; the duration and
# price P1 the device uses are always read off the script lowered on the program's layout (Sec. 2.4).
def template_timing(tid: str, d: int):
    if tid == "H":
        return 3 * d, 2 * 3 * d
    if tid == "S":
        return d + d // 2 + 2, 2 * (d + d // 2 + 2)
    if tid == "CNOT_cf_patched":
        return 2 * d, 2 * 2 * d
    raise KeyError(tid)


def script_cost(script, d: int):
    """(critical path in rounds, patch-rounds) of a script's physical members under `template_timing`: a member starts
    when every earlier member on one of its wires has finished (the .lss ordering rule)."""
    free, p1 = {}, 0
    for m in script:
        if m["template"] == "frame":
            continue
        dur, pr = template_timing(m["template"], d)
        start = max(free.get(w, 0) for w in m["wires"])
        for w in m["wires"]:
            free[w] = start + dur
        p1 += pr
    return max(free.values(), default=0), p1


_GATE_OF = {"H": H_GATE, "S": S_GATE, "SX": rot(-1, "X"), "CNOT_cf_patched": CNOT_GATE}
# CNOT_{c->t} X_c X_t CNOT_{c->t} = X_c, Z_c Z_t -> Z_t, Y_c X_t -> Y_c, Z_c Y_t -> Y_t (all without a sign): a
# weight-2 axis of this form is a quarter-turn on ONE wire between two CNOTs.  {(letter on c, letter on t): (wire of
# the single-wire axis, 0 = c / 1 = t, its letter)}.
_CNOT_REDUCIBLE = {("X", "X"): (0, "X"), ("Z", "Z"): (1, "Z"), ("Y", "X"): (0, "Y"), ("Z", "Y"): (1, "Y")}


def _quarter_turn_members(sign: int, support: dict):
    """Physical members (no frames) of rot(sign, P'), P' = `support` of weight 1 or 2 (design Sec. 2.3, simplified):
      Z: one S gadget (rot(-1, Z) = S, rot(+1, Z) = S^dagger = S then frame Z);
      Y: one H, since R_y(pi/2) = rot(-1, Y) = X H and rot(+1, Y) = Z H;
      X: S H S, since rot(+1, X) = S H S and rot(-1, X) = S^dagger H S^dagger: two S gadgets and one H, shorter than
         H S^(+-) H (an S gadget takes d + d//2 + 2 rounds, an H 3d; `template_timing`);
      Z on one wire c and X on the other t (Z X, X Z): ONE CNOT_{c->t} and the quarter-turns about Z_c and X_t, since
         CNOT_{c->t} ~ rot(-1, Z_c) rot(-1, X_t) rot(+1, Z_c X_t) (the three commute): the S on c runs beside the S H S
         on t, which is shorter than any two-CNOT form;
      weight 2, CNOT-reducible (`_CNOT_REDUCIBLE`, either orientation): CNOT, the single-wire quarter-turn, CNOT
         (Z Z is the v5 script's CNOT S CNOT; Y X, Z Y, X Y, Y Z need one H between the CNOTs, X X an S H S);
      Y Y: one S conjugation, S_a Y_a S_a^dagger = -X_a, and X_a Y_b is reducible.
    The frames that make the members exactly rot(sign, P') are derived by `recovery_script`."""
    wires = sorted(support)
    if len(wires) == 1:
        (w,) = wires
        letter = support[w]
        if letter == "Z":
            return [member("S", [w])]
        if letter == "Y":
            return [member("H", [w])]
        return [member("S", [w]), member("H", [w]), member("S", [w])]
    a, b = wires
    for c, t in ((a, b), (b, a)):
        if (support[c], support[t]) == ("Z", "X"):
            return ([member("CNOT_cf_patched", [c, t])] + _quarter_turn_members(sign, {c: "Z"})
                    + _quarter_turn_members(sign, {t: "X"}))
    for c, t in ((a, b), (b, a)):
        hit = _CNOT_REDUCIBLE.get((support[c], support[t]))
        if hit is not None:
            pos, letter = hit
            core = _quarter_turn_members(sign, {(c, t)[pos]: letter})
            return [member("CNOT_cf_patched", [c, t])] + core + [member("CNOT_cf_patched", [c, t])]
    for w in (a, b):
        if support[w] in "XY":
            turned = dict(support)
            turned[w] = "Y" if support[w] == "X" else "X"            # S X S^dagger = Y, S Y S^dagger = -X
            return [member("S", [w])] + _quarter_turn_members(sign, turned) + [member("S", [w])]
    raise ValueError("no reduction for rot(%+d, %s)" % (sign, support))


def recovery_script(qkey: str):
    """Physical script implementing Q^dagger for Q = rot(s, P') in Lib (design Sec. 2.3): `_quarter_turn_members` of
    rot(-s, P') followed by the ONE frame Pauli F that makes it exact, F (M_n ... M_1) ~ Q^dagger with every member M_i
    its template's intended unitary (C1: every branch of the S gadget and of the patched CNOT is ~ S / CNOT with its
    own outcome frame inside).  The scripts with Y or X axes are shorter than the v5 library's (kept as
    `literal_recovery_script`); `test_certify.TestC3Shortest` checks by search that none of them has a shorter
    equivalent over these templates."""
    sign, support = parse_qkey(qkey)
    if not support:
        return []
    return _with_frame(_quarter_turn_members(-sign, support), qkey)


def _with_frame(phys, qkey: str):
    """`phys` (physical members, each its template's intended unitary) followed by the ONE frame Pauli F with
    F (M_n ... M_1) ~ Q^dagger, Q = `qkey`'s library element."""
    n = 1 + max(parse_qkey(qkey)[1])
    W = np.eye(2 ** n, dtype=complex)
    for m in phys:
        W = embed(_GATE_OF[m["template"]], m["wires"], n) @ W
    target = lib_matrix(qkey, n).conj().T
    for letters in itertools.product("IXYZ", repeat=n):
        letters = "".join(letters)
        if proportional(pauli_matrix(letters) @ W, target) is not None:
            return phys + ([] if weight(letters) == 0 else [member("frame", pauli=pauli_key(letters))])
    raise AssertionError("script members for %s are not Q^dagger up to a Pauli" % qkey)


def _sx_members(support: dict):
    """SX members for a one-wire X or Y axis (cheap-repair.md 6.3), else None. X: one SX gadget, rot(-1, X) =
    SQRT_X, with rot(+1, X) = X rot(-1, X) from the frame. Y: S SX S ~ H, so the frames are the old H script's."""
    if len(support) != 1:
        return None
    (w,) = support
    if support[w] == "X":
        return [member("SX", [w])]
    if support[w] == "Y":
        return [member("S", [w]), member("SX", [w]), member("S", [w])]
    return None


def sx_recovery_script(qkey: str):
    """The sx library's script for Q^dagger: the table of cheap-repair.md 6.3 for the one-wire X and Y keys (four keys
    a wire), `recovery_script` for every other key.  `sx_recovery_script.fallback(qkey)` is the shortened file's
    script for a changed key and None for the others; `certify_c3` writes it as the entry's `fallback`."""
    phys = _sx_members(parse_qkey(qkey)[1])
    return recovery_script(qkey) if phys is None else _with_frame(phys, qkey)


def _sx_fallback(qkey: str):
    return None if _sx_members(parse_qkey(qkey)[1]) is None else recovery_script(qkey)


sx_recovery_script.fallback = _sx_fallback


def superop(kraus_list, d: int) -> np.ndarray:
    """Row-major vectorisation: vec(K X K^dagger) = (K kron conj(K)) vec(X)."""
    Ssum = np.zeros((d * d, d * d), dtype=complex)
    for K in kraus_list:
        Ssum += np.kron(K, K.conj())
    return Ssum


def choi_from_superop(Ssum: np.ndarray, d: int) -> np.ndarray:
    return Ssum.reshape(d, d, d, d).transpose(0, 2, 1, 3).reshape(d * d, d * d)


def member_kraus(m: dict, templates: dict, n: int):
    if m["template"] == "frame":
        letters = ["I"] * n
        for part in m["pauli"].split("."):
            letters[int(part[1:])] = part[0]
        return [pauli_matrix("".join(letters))], 0
    t = templates[m["template"]]
    return [embed(K, m["wires"], n) for K in t.kraus.values()], len(t.bits)


def _script_choi(script, templates: dict, n: int, d: int, Qd: np.ndarray):
    """(private bits, member counts, channel_ok, tp_ok) of a recovery script against conjugation by Q^dagger."""
    Ssum = np.eye(d * d, dtype=complex)
    n_bits = 0
    counts = {"S": 0, "SX": 0, "H": 0, "CNOT_cf_patched": 0, "frame": 0}
    for m in script:
        ks, nb = member_kraus(m, templates, n)
        Ssum = superop(ks, d) @ Ssum
        n_bits += nb
        counts[m["template"]] += 1
    J_script = choi_from_superop(Ssum, d)
    J_target = choi_from_superop(superop([Qd], d), d)
    channel_ok = bool(np.allclose(J_script, J_target, atol=1e-9))
    tp_ok = bool(abs(np.trace(J_script).real - d) < 1e-9)
    return n_bits, counts, channel_ok, tp_ok


def _member_count(counts: dict) -> dict:
    """The old key order and keys; an "SX" count only where a script has an SX member."""
    return {"S_gadget": counts["S"], **({"SX": counts["SX"]} if counts["SX"] else {}), "H": counts["H"],
            "CNOT": counts["CNOT_cf_patched"], "frame": counts["frame"],
            "templates_total": counts["S"] + counts["SX"] + counts["H"] + counts["CNOT_cf_patched"]}


def certify_c3(templates: dict, n: int = 2, script_fn=None) -> dict:
    """`script_fn`: the script for a Q key (default `recovery_script`, the shortened scripts); passes
    `literal_recovery_script` to reproduce v5's table (certificates/c3_recovery_v5.json, arm Q-v5scripts). A
    `script_fn.fallback` (`sx_recovery_script`'s) gives, for the keys whose script changed, the script that stays
    available as the entry's `fallback`, certified on its own (its script, counts and Choi result)."""
    cn = canon(n)
    d = cn.d
    out = {}
    paulis = cn.paulis
    script_fn = script_fn or recovery_script
    fallback_fn = getattr(script_fn, "fallback", None)
    for qkey, Q in cn.lib:
        script = script_fn(qkey)
        Qd = Q.conj().T
        n_bits, counts, channel_ok, tp_ok = _script_choi(script, templates, n, d, Qd)
        # frame rule E^dagger = (Q^dagger P^dagger Q) Q^dagger with Q^dagger P^dagger Q a Pauli
        frame_rule = {}
        rule_ok = True
        for p in paulis:
            P = pauli_matrix(p)
            F = Qd @ P.conj().T @ Q
            fk = None
            for p2 in paulis:
                if proportional(F, pauli_matrix(p2)) is not None:
                    fk = pauli_key(p2)
                    break
            E = P @ Q
            rule_ok &= fk is not None and proportional(F @ Qd, E.conj().T) is not None
            frame_rule[pauli_key(p)] = fk
        out[qkey] = {
            "script": script,
            "n_private_bits": n_bits,
            "member_count": _member_count(counts),
            "frame_after_rule": {"rule": "E^dagger = (Q^dagger P^dagger Q) Q^dagger; table P -> Q^dagger P^dagger Q",
                                 "table": frame_rule, "PASS": bool(rule_ok)},
            "channel_equals_conj_by_Qdagger": bool(channel_ok),
            "trace_preserving": bool(tp_ok),
            "PASS": bool(channel_ok and tp_ok and rule_ok),
        }
        fb = fallback_fn(qkey) if fallback_fn else None
        if fb is not None:
            fb_bits, fb_counts, fb_channel_ok, fb_tp_ok = _script_choi(fb, templates, n, d, Qd)
            out[qkey]["fallback"] = {"script": fb, "n_private_bits": fb_bits, "member_count": _member_count(fb_counts),
                                     "channel_equals_conj_by_Qdagger": fb_channel_ok, "trace_preserving": fb_tp_ok,
                                     "PASS": bool(fb_channel_ok and fb_tp_ok and rule_ok)}
    return out


# ----------------------------------------------------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------------------------------------------------
def run_all(out_dir: str = CERT_DIR, write: bool = True) -> dict:
    templates = build_templates()
    c1 = certify_c1(templates)
    c2 = certify_c2(templates)
    c2["_checks"] = {"T_rule_signedZ_and_membership": t_rule_check(c2), "T1_then_T2_equals_T": segment_chain_check(c2)}
    c3 = certify_c3(templates)
    if write:
        os.makedirs(out_dir, exist_ok=True)
        for name, obj in (("c1_templates.json", c1), ("c2_propagation.json", c2), ("c3_recovery.json", c3)):
            with open(os.path.join(out_dir, name), "w") as fh:
                json.dump(obj, fh, indent=1, sort_keys=False)
    return {"templates": templates, "c1": c1, "c2": c2, "c3": c3}


def run_all_sx(out_dir: str = CERT_DIR, write: bool = True) -> dict:
    """two new certificate files: c1_templates_sx.json (the old file's templates plus SX and SXdg) and
    c3_recovery_sx.json (`sx_recovery_script`, each changed entry with its fallback). The four old files are not
    touched; there is no C2 file for SX."""
    templates = build_templates_sx()
    c1 = certify_c1(templates)
    c3 = certify_c3(templates, n=2, script_fn=sx_recovery_script)
    if write:
        os.makedirs(out_dir, exist_ok=True)
        for name, obj in (("c1_templates_sx.json", c1), ("c3_recovery_sx.json", c3)):
            with open(os.path.join(out_dir, name), "w") as fh:
                json.dump(obj, fh, indent=1, sort_keys=False)
    return {"templates": templates, "c1": c1, "c3": c3}


def summary_lines(res: dict):
    lines = ["C1 templates", f"{'template':20s} {'wires':6s} {'bits':10s} {'records':>7s} {'PASS':5s} note"]
    for tid, e in res["c1"].items():
        note = ""
        if not e["PASS"]:
            note = "ranks " + ",".join(str(v) for v in e.get("branch_ranks", {}).values())
        if e["PASS"] is None:
            note = "non-unitary, no Kraus certificate (destructive: discard + init)"
        lines.append(f"{tid:20s} {len(e['wires']):<6d} {','.join(b for b in e['outcome_bits'] if b):10s} "
                     f"{e['n_records']:>7d} {'n/a' if e['PASS'] is None else str(e['PASS']):5s} {note}")
    lines += ["", "C2 propagation", f"{'template':20s} {'residuals':>9s} {'fences':>7s} {'relabelled':>10s}"]
    for tid, tab in res["c2"].items():
        if tid.startswith("_"):
            continue
        fences = sum(1 for e in tab.values() if e["result"] == "fence")
        rel = sum(1 for e in tab.values() if e.get("relabel") not in (None, "identity", "predicate u read as u xor 0"))
        lines.append(f"{tid:20s} {len(tab):>9d} {fences:>7d} {rel:>10d}")
    for name, chk in res["c2"]["_checks"].items():
        lines.append(f"  check {name}: {'PASS' if chk['PASS'] else 'FAIL'} ({chk['n_checked']} residuals)")
    lines += ["", "C3 recovery scripts (2 wires)", f"{'Q':16s} {'S':>2s} {'H':>2s} {'CNOT':>4s} {'frames':>6s} {'bits':>4s} PASS"]
    for qk, e in res["c3"].items():
        mc = e["member_count"]
        lines.append(f"{qk:16s} {mc['S_gadget']:>2d} {mc['H']:>2d} {mc['CNOT']:>4d} {mc['frame']:>6d} "
                     f"{e['n_private_bits']:>4d} {e['PASS']}")
    return lines


if __name__ == "__main__":
    import sys
    import time
    t0 = time.time()
    if sys.argv[1:] == ["sx"]:
        result = run_all_sx()
        for tid in ("SX", "SXdg"):
            print(f"C1 {tid}: PASS {result['c1'][tid]['PASS']}")
        for qk, e in result["c3"].items():
            if "fallback" in e:
                names = [m["template"] + str(m.get("wires", m.get("pauli"))) for m in e["script"]]
                print(f"C3 {qk:12s} PASS {e['PASS']}  sx {names}  fallback PASS {e['fallback']['PASS']}")
    else:
        result = run_all()
        print("\n".join(summary_lines(result)))
    print(f"\nwrote {CERT_DIR} in {time.time() - t0:.1f} s")
