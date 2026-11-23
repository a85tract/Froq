"""The recovery contract of one speculated value, built on the semantic event stream with the certified C2 / C3 tables
 (design Sec. 2.2 - 2.4; supersedes the coordinate-based "support" / "target" fence rules of and earlier).

What a contract is. A speculated value is the ZZ-parity result u of a T gadget (Sec. 2.1; nothing else is speculated).
Acting on a prediction of u executes the predicted branch of the gadget's second segment T2 (the teleported S or its
absence); if the prediction is wrong the physical state carries a RESIDUAL E_0 = S or S^dagger on the corrected wire.
The contract is the longest prefix of the program, walked forward from the producer along the dependency edges, on which
that residual stays REPAIRABLE: after every transition the residual must be canonical E = P * Q with P a Pauli (a frame,
free) and Q in the library Lib = {I} u {quarter-turns about a weight <= 2 Pauli product on L} (Sec. 2.2), because Lib is
exactly the set of residuals for which C3 certifies a recovery script composed of the compiler's own templates
(Sec. 2.3). State = (E, L, D): the residual, the wires it may have spread to (support), and the classical result ids whose
recorded value is a function of the wrong branch (relabels). The walk consults tables only: a certified gadget instance
is admitted atomically with its C2 row (K_r E = E'_r K_f(r)); an elementary Pauli-product measurement outside a certified
gadget passes the signed-commutation test (anticommutation is a relabel, not a fence); a destructive measurement of a
wire in L reduces the instrument; a `discard traced` of a wire carrying a non-local factor, an uncertified template, a
residual outside Lib, a classical consumer of D (a predicate, an `io output` of a wire in L or of a result in D) or a
T segment failing its row are fences. The contract's own admitted members are exempt from the classical-consumer rule
(they ARE the instrument).

Atoms and steps (Sec. 2.4). A step is one atom: a certified gadget or gadget segment (T1 / T2) whose internal schedule is
fixed once its external prerequisites hold, or one elementary event. A zero-duration atom (a predicted skip, a frame, a
zero-duration DISCARD) is folded FORWARD into the next positive-duration atom, never backwards; a contract with no positive
atom has no site (the RTL needs F >= 1 and 1 <= d_k <= 64) and the value simply WAITs. A step longer than 64 rounds is
refused (counted). Depth K counts steps.

Prices and reservation (Sec. 2.3 / 2.4). For every step the recovery of the residual AFTER it is looked up in C3 (script =
template instances), lowered with the compiler's router on the PROGRAM'S layout (`Semantics.lower`) and priced at that
placement: P1 = the lowered script's patch-rounds, its cells = the workspace the device RESERVES for the whole unresolved
interval (liveness: nothing else may allocate there), its duration = the recovery's own length. A step whose script cannot
be placed, or whose cells are reserved by another value, is not admitted (`no_recovery_workspace` / `held_overlap`).
The frame after the script (`Step.frame_after`, what the device applies at recovery completion) is Q^dagger P^dagger Q
(algebraic; C3's `frame_after_rule` table is the cross-check in C4) TIMES the C3 script's own frame members commuted to the
script's end (`Lowered.frame_end`; e.g. S^dagger = the S gadget then frame Z, so recovering an S residual is S then Z: S S = Z
alone would be wrong). The device splices only the physical members, so the composed frame is where the script's frames live.

Canonical residuals. The (P, Q) split of a residual is not unique (P rot(s, A) = P A rot(-s, A)); the tables are keyed by
the certifier's canonical representative, so every lookup canonicalises the COMPLETE residual on the template's wires plus
its one spectator wire in the template's numbering (`pauli.canonical_split`), the row's output Q is never overwritten by the
incoming one, and every transition ends with the certifier's canonical form (`Residual.canonical`), signed-Z for T rows and
Lib membership enforced.

Configuration, not code, selects the inputs: `Semantics(cert_dir, compile_backend)` points at the certificate JSONs
(default froq/semantics/certificates) and at the `compile_snippet` implementation ("real" = froq.semantics.compile,
"fixture" = froq/tests_fixtures/compile_stub.py). Table formats are the certifier's actual ones (froq/semantics/certify.py):
keys "P=X0.Z1|Q=rot(+1,Z0Z1)" over the template's wire positions plus ONE spectator index, rot(s,P') = exp(s i pi P'/4),
S ~ rot(-1,Z); T2 has two rows, `T2[pred=asis]` and `T2[pred=flipped]`, selected by whether the predicate id was relabelled."""
from __future__ import annotations

import bisect, collections, hashlib, importlib, json, os, re
from dataclasses import dataclass, field, replace
from types import SimpleNamespace

from swiper.lattice_surgery_schedule import Duration, Instruction

from .events import EventStream, Atom, CONSUMER_KINDS
from .pauli import I, Pauli, Residual, absorb_sign, canonical_split, inverse_residual, pkey

RULES_VERSION = "route-a-v5-D4/wp4-2026-09-13+w10-feedback"   # part of every result's fingerprint (design Sec. 1.2);
                                                     # w10: the classically controlled CZ row CZ[pred=asis]
D_MAX = 64                                           # hw/rtl/se_pkg.sv: a forward edge is 1..D_MAX ticks
# The recovery a contradiction runs (v8 comparison). "cancel": Froq's compiled script that cancels the
# residual where it now sits.  "undo_redo": the baseline that undoes the admitted gates, applies the correction the
# producer needed and redoes the gates (`undo_redo_lowered`); a T gate, or a measurement or discard on the residual's
# support, cannot be undone, so under "undo_redo" it ends the contract.  Part of the shared fingerprint when not "cancel" (run.shared_inputs).
# "ur_routed", logical undo-fixup-redo routed like compensation (froq/ur.py): every
# boundary's recovery is UR's gate list lowered by the same router; it reads and warms its own snippet cache
# (`set_recovery_mode`), so compensation's cache file and fingerprint stay as they are.
# "ur_syn", synthesized UR (the list's composite operator resynthesized by elimination,
# then the peephole pass; per snippet the fewer placed rounds of it and the peephole list), and "ur_peep", peephole UR (a
# named variant): froq/ur_syn.py.  Each reads and warms its own snippet cache.
RECOVERY_MODES = ("cancel", "undo_redo", "ur_routed", "ur_syn", "ur_peep")
UR_MODES = ("ur_routed", "ur_syn", "ur_peep")
RECOVERY_MODE = os.environ.get("E2E_RECOVERY", "cancel")
assert RECOVERY_MODE in RECOVERY_MODES, RECOVERY_MODE
UR_SNIPPETS = "snippets_ur"                          # froq/schedules/<this>: the ur_routed mode's snippet cache
SX_SNIPPETS = "snippets_sx"                          # froq/schedules/<this>: the sx library's snippet cache
UR_CACHES = {"ur_routed": UR_SNIPPETS, "ur_syn": "snippets_ur_syn", "ur_peep": "snippets_ur_peep"}
# UR's reach. "comp": the contracts' reach (C2's rows, the library's weight cap), as round 9;
# "own": a UR walk also passes the library's weight cap and the C2 rows that fence a weight-growing step, on rows certified
# on the fly (`Walker.template`), and stops at a non-commuting T1, a destructive measurement of a kept wire, a classical
# consumer and the declared 8-wire limit (UR_MAX_WIRES).  Part of the shared fingerprint when "own" (run.shared_inputs).
UR_REACH = os.environ.get("E2E_UR_REACH", "comp")
assert UR_REACH in ("comp", "own"), UR_REACH
UR_MAX_WIRES = 8                                     # the new modes' declared implementation limit (ur.MAX_OPERATOR_WIRES)
UR_VARIANTS = {"ur_routed": ("routed",), "ur_syn": ("syn", "peep"), "ur_peep": ("peep",)}   # the lists `lower_ur` places


def set_recovery_mode(mode: str) -> None:
    """The recovery mode of this process, with the snippet cache the real backend reads (a UR mode: its own directory)."""
    global RECOVERY_MODE
    assert mode in RECOVERY_MODES, mode
    RECOVERY_MODE = mode
    importlib.import_module("froq.semantics.compile").use_snippet_cache(UR_CACHES.get(mode, "snippets"))


def set_library_cache(library: str) -> None:
    """The snippet cache of a run's library, called after `set_recovery_mode` (which sets the UR directories) so that no
    later call resets it: sx reads `snippets_sx` and is refused with a UR recovery mode; another library leaves the directory
    alone, unless an sx run earlier in this process left it on `snippets_sx`."""
    cc = importlib.import_module("froq.semantics.compile")
    if library == "sx":
        if RECOVERY_MODE in UR_MODES:
            raise SystemExit("--library sx has no UR recovery (recovery mode %s)" % RECOVERY_MODE)
        cc.use_snippet_cache(SX_SNIPPETS, cache_refusals=True)     # (P3: a refusal met while warming is stored, 6.2; every call sets it)
    elif os.path.basename(cc.SNIPPET_CACHE_DIR) == SX_SNIPPETS:
        cc.use_snippet_cache(UR_CACHES.get(RECOVERY_MODE, "snippets"))


def set_ur_reach(reach: str) -> None:
    """UR's reach in this process: "comp" or "own" (own: a UR recovery mode only)."""
    global UR_REACH
    assert reach in ("comp", "own"), reach
    UR_REACH = reach


def ur_own() -> bool:
    return UR_REACH == "own" and RECOVERY_MODE in UR_MODES


def in_reach(E: Residual, L) -> bool:
    """`E.in_lib(L)` at the contracts' reach; at UR's own reach the library's weight cap is lifted and only the
    axis inside L remains (UR's 8-wire limit is the walk's, `build_contract`)."""
    if not ur_own():
        return E.in_lib(L)
    return not E.has_q or E.axis.wires <= frozenset(L)


def _ur_reach():
    from . import ur_reach
    return ur_reach


if RECOVERY_MODE in UR_MODES:                        # (E2E_RECOVERY: the cache follows the mode from the start)
    set_recovery_mode(RECOVERY_MODE)
# (gap analysis rule 15, arm Q-nomeas). "nomeas": a contract ends at any gadget that contains a measurement
# (a gadget with a C1 template that has outcome bits, or an elementary measurement), the value's own T2 excepted; "full" is
# Froq's measurement-aware walk. Q walks only (arm D's deferral walks ignore it). Part of the shared fingerprint when
# not "full" (run.shared_inputs), so a Q-nomeas run needs a profile trained under the same mode.
CONTRACT_MODE = os.environ.get("E2E_CONTRACTS", "full")
assert CONTRACT_MODE in ("full", "nomeas", "nomeas_all"), CONTRACT_MODE
# U v6 (round rule 56b, the contract ablation): "nomeas_all" ends every walk -- deferral's and its rebased
# walks too -- at its first gadget that contains a measurement, the value's own T2 excepted, as "nomeas" ends Q's.
# (arm Q-v5scripts): the compensation scripts' C3 table. "shortened" is (c3_recovery.json); "v5" is v5's
# (certify.literal_recovery_script, certified in c3_recovery_v5.json).  Chosen per Semantics, so it is in the tables' hashes.
# (candidate note v2, 6.3): "sx" adds S on the target's X boundary through the teleported S (a |Y> patch on the target's
# right, X_q X_y measured, y read in Z) as a second script for the eight keys rot(+-1, Xi), rot(+-1, Yi): the changed entries
# of its C3 file carry the shortened script as `fallback`.  It has its own C1 file (SX and SXdg beside the old templates)
# and snippet cache; the other libraries read c1_templates.json and `snippets`.
C3_FILES = {"shortened": "c3_recovery.json", "v5": "c3_recovery_v5.json", "sx": "c3_recovery_sx.json"}
C1_FILES = {"sx": "c1_templates_sx.json"}
# SX = rot(-1, X) = stim's SQRT_X (X -> X, Y -> Z, Z -> -Y); SXdg its inverse.  The C2 file has no row for them (it stays
# byte-identical), so `Tables.pauli_through` reads this map; frames are tracked unsigned, as for the C2 rows.
SX_PAULI = {"SX": {"X": ("X", 1), "Y": ("Z", 1), "Z": ("Y", -1)}, "SXdg": {"X": ("X", 1), "Y": ("Z", -1), "Z": ("Y", 1)}}
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CERT_DIR = os.path.join(HERE, "semantics", "certificates")
T_TEMPLATE = "T"                                     # the T injection's gadget template id; its segments are "T1", "T2"
FEEDBACK_ROW = "CZ[pred=asis]"                       # w10: C2 rows of a CZ run iff an external measured bit is 1
TEMPLATE_TO_LLI = {"S": "S", "H": "H", "CNOT_cf_patched": "CNOT", "SX": "SX"}


def read_lss(path: str) -> str:
    """The .lss text for `LatticeSurgerySchedule.from_str`: upstream parses every line, so trailing newlines are stripped."""
    return open(path).read().rstrip("\n")


def parse_lss(text: str, generate_dag_incrementally: bool = True):
    """`LatticeSurgerySchedule.from_str(text, generate_dag_incrementally)` with upstream's `Instruction.from_str` inlined and
    each field's `frozenset(eval(...))` memoized by its text (speed, results bit-identical): Grover-ancilla-5 has 208,217
    lines and 1.46 M such fields, mostly a few distinct texts. A memoized frozenset is the one the first parse built from
    the same text, so its contents and iteration order are the same; frozensets are immutable, so sharing one is safe.
    `froq.test_h3.TestSpeed.test_parse_lss_equals_upstream` checks it against upstream's parser."""
    from swiper.lattice_surgery_schedule import LatticeSurgerySchedule
    memo = {}

    def fs(t):
        f = memo.get(t)
        if f is None:
            f = memo[t] = frozenset(eval(t))
        return f
    instance = LatticeSurgerySchedule(generate_dag_incrementally=generate_dag_incrementally)
    for line in text.split("\n"):
        s = line.split()
        instance._add_instruction(Instruction(
            name=s[0], idx=int(s[1]), patches=fs(s[2]), duration=Duration.from_str(s[3]), conditioned_on_idx=fs(s[4]),
            conditional_dependencies=fs(s[5]), conditioned_on_completion_idx=fs(s[6]),
            conditional_completion_dependencies=fs(s[7]), merge_faces=fs(s[8]), group_instr_indices=fs(s[9]),
            group_name=s[10] if s[10] != 'none' else ''))
    return instance


class SnippetMiss(Exception):
    """Deferral walks only (v9-deferral, `build_contract(deferral=True)`): the script of a boundary has no cached placement
    and this machine has no slicer. The walk ends there (`no_cached_placement`, counted) instead of failing the run;
    every other compilation failure, and every cache miss outside a deferral walk, stays an infrastructure error. Under the
    sx library it is never raised: a miss there fails the run, a deferral walk included (`Semantics._place`)."""


# ---------------------------------------------------------------------------------------------------------- tables
class Tables:
    """The C1 / C2 / C3 certificates as lookups; every key transformation between program wires and the certifier's wire
    positions lives here so that a format change is a one-file change."""

    def __init__(self, cert_dir: str = DEFAULT_CERT_DIR, library: str = "shortened"):
        self.cert_dir = cert_dir
        self.library = library
        self.paths = {k: os.path.join(cert_dir, "%s.json" % k) for k in ("c1_templates", "c2_propagation")}
        self.paths["c1_templates"] = os.path.join(cert_dir, C1_FILES.get(library, "c1_templates.json"))
        self.paths["c3_recovery"] = os.path.join(cert_dir, C3_FILES[library])
        self.c1 = json.load(open(self.paths["c1_templates"]))
        self.c2 = json.load(open(self.paths["c2_propagation"]))
        self.c3 = json.load(open(self.paths["c3_recovery"]))
        self._digests: dict = {}
        for t in ("T2_S", "T2_skip", "S", "T1", "CNOT_cf_patched", "H") + (("SX", "SXdg") if library == "sx" else ()):
            assert self.c1[t]["PASS"], "C1 certificate %s does not PASS" % t
        assert self.c1["T2_S"]["intended_unitary"] == "S" and self.c1["T2_skip"]["intended_unitary"] == "I"
        if library == "sx":                                          # every changed entry and its fallback must be a certified script
            # an entry that differs from the shortened file's, or a key one file lacks, must
            # carry a fallback, or the table is refused (a fallback deleted would otherwise read as an unchanged key, with no form)
            old = json.load(open(os.path.join(cert_dir, C3_FILES["shortened"])))
            for qk in sorted(set(self.c3) | set(old)):
                e = self.c3.get(qk)
                if e is not None and "fallback" in e:
                    self.fallback(qk)
                elif e != old.get(qk):
                    raise ValueError("C3 entry %s of the sx library differs from %s's and has no fallback" % (qk, C3_FILES["shortened"]))

    def has_template(self, tid: str) -> bool:
        return tid in self.c2 or tid == "T2"

    def wires_of(self, tid: str) -> list:
        base = "T" if tid in ("T1", "T2") or tid.startswith("T2[") else tid.split("[")[0]
        return list(self.c1[base]["wires"])

    def measures(self, tid: str) -> bool:
        """(Q-nomeas): the template contains a measurement, i.e. its C1 entry has outcome bits (S, T1, T2, CNOT, CZ,
        MEASURE...; not the Pauli frames, H, RESET or T2_skip). A template C1 does not describe counts as measuring."""
        e = self.c1.get(tid)
        return True if e is None else bool(e.get("outcome_bits"))

    def bits_of(self, tid: str) -> list:
        if tid.startswith("T2"):
            return ["u"]
        if tid == FEEDBACK_ROW:                                  # w10: the rows' one record bit is the measured bit
            return ["m"]
        return list(self.c1[tid]["outcome_bits"])

    def row(self, tid: str, key: str):
        """The C2 entry for a template row and a canonical local key, or None when the table has no such key."""
        return self.c2.get(tid, {}).get(key)

    def entry_residual(self, prediction: bool, truth: bool) -> Residual | None:
        """E_0 on local wire 0 after the predicted T2 branch ran for a different truth (C1 T2_S ~ S, T2_skip ~ I): S was
        applied but not needed -> the state carries S = rot(-1, Z); S was skipped but needed -> S^dagger = rot(+1, Z)."""
        if prediction == truth:
            return None
        return Residual(I, (), -1 if prediction else +1, Pauli.of({0: "Z"}))

    def script(self, qkey: str) -> dict | None:
        e = self.c3.get(qkey)
        return e if e and e.get("PASS") else None

    def fallback(self, qkey: str) -> dict:
        """The sx library's fallback for a changed key: the shortened script with its own Choi result.  Both the entry and
        its fallback must PASS and hold a script; a malformed certificate raises (never a silent "no recovery")."""
        e = self.c3[qkey]
        fb = e.get("fallback")
        if not e.get("PASS") or not isinstance(fb, dict) or not fb.get("PASS") or not isinstance(fb.get("script"), list) \
                or not isinstance(e.get("script"), list):
            raise ValueError("C3 entry %s of the sx library is not a certified script with a certified fallback" % qkey)
        return fb

    def entry_digest(self, qkey: str) -> str:
        """sha256 of the C3 entry's canonical JSON (sorted keys, no spaces): what a lowering is bound to."""
        d = self._digests.get(qkey)
        if d is None:
            d = self._digests[qkey] = hashlib.sha256(json.dumps(self.c3[qkey], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return d

    def pauli_through(self, tid: str, P: Pauli, wires: list) -> Pauli:
        """P -> U P U^dagger for a physical template instance on `wires` (program wires by template position), read from
        the template's C2 row P|Q=I.  The recovery scripts' members (S gadget, H, patched CNOT) are C1-certified as ~ U on
        every branch, so the row is a Pauli with the identity relabel; anything else is a certificate the builder cannot
        use for frame composition and is refused loudly.  SX and SXdg (sx library) have no C2 row: `SX_PAULI`."""
        if tid in SX_PAULI:
            w = wires[0]
            letter = P.as_dict().get(w)
            return P if letter is None else P.without({w}).mul(Pauli.of({w: SX_PAULI[tid][letter][0]}))
        pos = {w: i for i, w in enumerate(wires)}
        key = "P=%s|Q=I" % local_pkey(P.restrict(set(wires)), pos)
        row = self.row(tid, key)
        res = row.get("result") if row else None
        if not isinstance(res, str) or res.split("|Q=")[1] != "I" or row.get("relabel") != "identity":
            raise ValueError("C2 row %s %s does not propagate a Pauli to a Pauli with the identity relabel: %r" % (tid, key, row))
        return P.without(set(wires)).mul(parse_pkey(res.split("|Q=")[0][2:], {i: w for w, i in pos.items()}))

    def script_frames(self, qkey: str, back: dict):
        """The C3 script's frame members for Q = qkey on program wires (`back`: script wire index -> program wire): a tuple
        of (i, Pauli, predicate) in script order, the frame being applied after the first i physical members, and their
        composition at the script's END: a frame F at position i equals (M_n ... M_{i+1}) F (M_n ... M_{i+1})^dagger placed
        last, a Pauli because every physical member is a Clifford template (`pauli_through`).  C3's frames are
        unconditional (the S gadget's own outcome-conditioned frame is inside the template); a predicated frame member
        could not be composed and is refused."""
        return self.frames_of(self.c3[qkey]["script"], back, qkey)

    def frames_of(self, script, back: dict, label: str):
        """`script_frames` for an explicit member list (a C3 script, or a routed UR list: froq/ur.py)."""
        phys = [m for m in script if m["template"] != "frame"]
        frames, i = [], 0
        for m in script:
            if m["template"] != "frame":
                i += 1
                continue
            if m.get("predicate") is not None:
                raise ValueError("predicated frame member in C3 script %s cannot be composed into the final frame" % label)
            frames.append((i, parse_pkey(m["pauli"], back), None))
        end = I
        for i, F, _ in frames:
            G = F
            for m in phys[i:]:
                G = self.pauli_through(m["template"], G, [back[j] for j in m["wires"]])
            end = end.mul(G)
        return tuple(frames), end.unsigned()


def local_pkey(P: Pauli, pos: dict) -> str:
    """The certifier's key of P over the numbered wires in `pos` (factors on other wires left out)."""
    return pkey(P, pos)


def parse_pkey(pk: str, back: dict) -> Pauli:
    if pk == "I":
        return I
    return Pauli.of({back[int(part[1:])]: part[0] for part in pk.split(".")})


def local_qkey(sign: int, axis: Pauli, pos: dict) -> str:
    body = "".join("%s%d" % (p, i) for i, p in sorted((pos[w], p) for w, p in axis.ops))
    return "rot(%+d,%s)" % (sign, body)


def parse_qkey(qk: str, back: dict):
    if qk == "I":
        return 1, I
    sign = int(qk[4:6]); body = qk[7:-1]
    return sign, Pauli.of({back[int(body[i + 1])]: body[i] for i in range(0, len(body), 2)})


# ---------------------------------------------------------------------------------------------------------- semantics config
@dataclass
class Lowered:
    """A recovery script lowered on the program's layout: what the device splices and what the contract reserves.

    `script` is what C3 DECLARES; `instrs` + `events` are the INSTANCE that runs.  Round-2 review A2/B1: keeping only the
    instructions and the declared script let a recovery whose MERGE had lost its joining faces pass C4 and C5a, because
    nothing compared the executed members with the certified circuit.  So the snippet's own semantic event document is
    kept here (`compile.Snippet.events`, the same schema as a program's: gadget records, observables, init states,
    readout bases, frame predicates, `operand_cells` / routing `cells` and the `lss_map` to these instructions), and the
    member list C4 validates and C5a exports is derived from it (`instance_members`), not from `script`."""
    instrs: tuple                    # swiper Instructions, idx 0..n-1 relative, group_name RECOVERY
    cells: frozenset
    duration: int                    # rounds: critical path of the script at this distance
    P1: int                          # patch-rounds at this placement
    frames: tuple                    # the C3 frame members in script order: (i, Pauli on program wires, predicate), applied after the first i physical members (predicate None = unconditional, as C3 emits them)
    script: tuple                    # the C3 members (for C4)
    lli: str
    frame_end: Pauli = I             # the frame members commuted to the script's end (Tables.script_frames); part of Step.frame_after
    events: dict | None = None       # the snippet's own event document; None for a backend without a converter (the fixture stub)
    patches: tuple = ()              # program patch id of each C3 script wire index; the snippet names that wire "q<patch id>"
    wires: tuple = ()                # UR modes only: the program wire of each script wire index (else the residual's sorted wires)
    ur: tuple | None = None          # UR modes only: (kept gadgets ((row, wires), ...), fixup residual key, dropped, pairs, sign frame)
                                     # and, ur_syn / ur_peep, the list placed: "syn" or "peep"
    form: str | None = None          # sx library only: "new" (the sx script), "fallback" (it was refused) or "old_dearer" (the
                                     # shortened script, the sx one not strictly cheaper); None for an unchanged key
    c3_key: str | None = None        # sx library only: the C3 key and the sha256 of its entry (`Tables.entry_digest`) the lowering is bound to
    c3_digest: str | None = None

    def instance_gadgets(self) -> list:
        """The snippet's own gadget records in EXECUTED order, keyed by the first .lss instruction of their members: the
        event list is only ONE topological order (the document says so; only the deps and the schedule are normative), and
        it does put a CNOT's mediator init before the H that runs first.  A member with no instruction keeps its event
        position as the tiebreak."""
        if not self.events:
            return []
        pos = {e["event_id"]: i for i, e in enumerate(self.events["events"])}
        lmap = self.events.get("lss_map", {})
        n = len(self.instrs)

        def key(g):
            ms = [m for m in g.get("events", ()) if m in pos]
            first = [i for m in ms for i in lmap.get(m, ()) if i < n]
            return (min(first) if first else n, min([pos[m] for m in ms] or [pos[g["event_id"]]]))
        return sorted([e for e in self.events["events"] if e.get("kind") == "gadget"], key=key)

    def instance_members(self) -> tuple | None:
        """`instance_gadgets` as (template id, (C3 script wire index, ...)) -- the executed member list, in executed
        order, on the script's own wire numbering.  None when the backend emitted no document (nothing to derive from)."""
        if not self.events:
            return None
        of_patch = {p: i for i, p in enumerate(self.patches)}
        out = []
        for g in self.instance_gadgets():
            ins = g.get("inputs") or ()
            ins = list(ins.values()) if isinstance(ins, dict) else list(ins)
            out.append((g["template_id"], tuple(of_patch.get(int(w[1:]), -1) if w[:1] == "q" and w[1:].isdigit() else -1 for w in ins)))
        return tuple(out)

    def declared_members(self) -> tuple:
        """C3's physical members as (template id, (script wire index, ...)); the frame members are not physical."""
        return tuple((m["template"], tuple(m["wires"])) for m in self.script if m["template"] != "frame")

    def members_agree(self) -> bool:
        """The executed members ARE C3's script, up to reordering members on DISJOINT wires: the compiler schedules
        independent members in parallel (a two-wire script runs S(0) and S(1) together), and disjoint-wire Cliffords
        commute, so that executed order IS the certified one -- the rule C4 already applies to the program's own members.
        Equality of the per-wire subsequences is exactly that condition (and implies equality of the multisets)."""
        inst, decl = self.instance_members(), self.declared_members()
        if inst is None or sorted(inst) != sorted(decl):
            return False
        return all([m for m in inst if w in m[1]] == [m for m in decl if w in m[1]] for w in {i for _t, ws in decl for i in ws})

    def certified_members(self) -> tuple:
        """The members that RUN, in the order C3 anchors its frame members to.  When the instance is C3's script
        reordered among commuting members (the normal case) that is C3's order; when it is NOT -- a member missing,
        added, on other wires, or in an order that does not commute -- the instance's own list is returned, so the C5a
        region is built from what runs and changes with it.  Without a semantic document only C3's list exists."""
        inst = self.instance_members()
        if inst is None:
            return self.declared_members()
        return self.declared_members() if self.members_agree() else inst


class Semantics:
    h_depth = None          # (--h-depth, hdepth.py): RHO as a Fraction when above 1, set by the device; None: off
    price = None            # (sx library only): Lowered -> S + XS in rounds at the run's d, path and decoder model, set by the device

    def __init__(self, cert_dir: str = DEFAULT_CERT_DIR, compile_backend: str = "real", library: str = "shortened"):
        self.tables = Tables(cert_dir, library)
        self.compile_backend = compile_backend
        self.compile_failures: dict = {}                             # Q key -> last router refusal (genuine PlacementRefused only; reported, never fatal)
        self.compile_errors = 0                                      # infrastructure failures seen (cache miss, missing binary, timeout, ...); each is re-raised, so > 0 means a lower layer swallowed one
        self._failed: set = set()                                    # (Q key, patch ids) the router refused: not retried
        self.cache_miss_fences = 0                                   # deferral walks only: boundaries without a cached placement (SnippetMiss)
        self._missed: set = set()                                    # (Q key, patch ids) that missed the cache in a deferral walk: not retried
        self._converter_error: tuple = ()
        self._sx_counts = collections.Counter()                     # sx library only: "X:new" ... (changed keys by axis and form), "unchanged"
        self._sx_entries: dict = {}                                 # sx library only: C3 key -> the digest of the entry the lowerings were bound to
        self._sx_bound: set = set()                                 # sx library only: (key, form, patches, d) whose instance passed C4's check
        if compile_backend == "real":
            mod = importlib.import_module("froq.semantics.compile")
            self._compile = mod.compile_snippet
            self._placement_refused: tuple = (mod.PlacementRefused,)
            self._converter_error = (mod.ConverterError,)
        elif compile_backend == "fixture":
            self._compile = importlib.import_module("froq.tests_fixtures.compile_stub").compile_snippet
            self._placement_refused = ()                             # the stub signals no placement by returning None
        else:
            raise ValueError(compile_backend)

    @staticmethod
    def from_env(default_backend: str = "real", library: str | None = None) -> "Semantics":
        return Semantics(os.environ.get("E2E_CERT_DIR", DEFAULT_CERT_DIR), os.environ.get("E2E_COMPILE_BACKEND", default_backend),
                         library or os.environ.get("E2E_LIBRARY", "shortened"))

    def describe(self) -> dict:
        extra = {} if self.tables.library == "shortened" else dict(library=self.tables.library)      # (Q-v5scripts)
        if self.tables.library == "sx":                              # the lowerings by form, their bindings and C4's instance checks
            extra["sx"] = dict(lowerings=dict(sorted(self._sx_counts.items())), entries=dict(sorted(self._sx_entries.items())),
                               instances_checked=len(self._sx_bound))
        return dict(cert_dir=os.path.relpath(self.tables.cert_dir, os.path.dirname(HERE)), compile_backend=self.compile_backend, rules_version=RULES_VERSION,
                    recovery_compile_errors=self.compile_errors, compile_failures=dict(self.compile_failures), **extra)

    def lower(self, res: Residual, wire_cells: dict, occupied, reserved, d: int, patch_ids: dict | None = None, n_program_qubits: int | None = None,
              miss_fence: bool = False) -> Lowered | None:
        """C3 script of Q^dagger for the residual's Q, lowered by the compiler's router on the program's layout.  Returns
        None when there is no route (the slicer ran and REFUSED placement: `compile.PlacementRefused` -> the step is not
        admissible).  Any other compilation failure -- cache miss without a slicer, missing binary, timeout, malformed
        provenance -- is infrastructure and RAISES (round-3 A2/B1: it must never become a silent `no_recovery_workspace`).
        `miss_fence` (deferral walks only, v9-deferral): a snippet-cache miss on a machine without the slicer raises
        SnippetMiss, which ends the walk as `no_cached_placement`; it is counted, not an infrastructure error."""
        if not res.has_q:
            return Lowered((), frozenset(), 0, 0, (), (), "")
        wires = sorted(res.axis.wires)
        pos = {w: i for i, w in enumerate(wires)}
        qkey = local_qkey(res.qsign, res.axis, pos)
        entry = self.tables.script(qkey)
        if entry is None:
            return None
        back = {i: w for w, i in pos.items()}
        if self.tables.library == "sx":
            return self._lower_sx(res, qkey, entry, wires, back, wire_cells, occupied, reserved, d, patch_ids, n_program_qubits, miss_fence)
        frames, frame_end = self.tables.script_frames(qkey, back)
        return self._place(qkey, entry["script"], wires, frames, frame_end, wire_cells, occupied, reserved, d, patch_ids, n_program_qubits,
                           miss_fence)

    def _lower_sx(self, res: Residual, qkey: str, entry: dict, wires: list, back: dict, wire_cells: dict, occupied, reserved, d: int,
                  patch_ids, n_program_qubits, miss_fence: bool) -> Lowered | None:
        """`lower` under the sx library. A key whose entry carries a `fallback` (the eight changed keys)
        places the sx script first. PlacementRefused (`_place` returns None for it): the fallback, form "fallback". Else the
        fallback is placed too and the two are compared by S + XS (`self.price`, the profile builder's own functions at the
        run's d, path and decoder model): "new" only if the sx script is strictly cheaper, else the fallback, "old_dearer". Any
        other exception (a malformed certificate, a slicer failure) raises out of `_place` as in the other libraries, and so does a
        cache miss, in a deferral walk too (I9: under sx no miss becomes a walk's fence).
        Every lowering is bound to its C3 entry (key, digest) and its placed instance must pass C4's instance check."""
        if "fallback" not in entry:                                  # an unchanged key: the entry's own script (equal to c3_recovery.json's)
            frames, frame_end = self.tables.script_frames(qkey, back)
            low = self._place(qkey, entry["script"], wires, frames, frame_end, wire_cells, occupied, reserved, d, patch_ids,
                              n_program_qubits, miss_fence)
            form = None
        else:
            fb = self.tables.fallback(qkey)
            frames, frame_end = self.tables.frames_of(entry["script"], back, qkey)
            new = self._place(qkey + ":sx", entry["script"], wires, frames, frame_end, wire_cells, occupied, reserved, d, patch_ids,
                              n_program_qubits, miss_fence)           # (a refusal is recorded under "<key>:sx"; the fallback keeps <key>)
            frames, frame_end = self.tables.frames_of(fb["script"], back, qkey)
            old = self._place(qkey, fb["script"], wires, frames, frame_end, wire_cells, occupied, reserved, d, patch_ids,
                              n_program_qubits, miss_fence)
            if new is None:
                low, form = old, "fallback"
            elif old is None or self._sx_price(new) < self._sx_price(old):
                low, form = new, "new"
            else:
                low, form = old, "old_dearer"
        if low is None:
            return None
        low = replace(low, form=form, c3_key=qkey, c3_digest=self.tables.entry_digest(qkey))
        self._sx_bind(low, d)
        self._sx_entries[qkey] = low.c3_digest
        self._sx_counts["unchanged" if form is None else "%s:%s" % (re.match(r"rot\([+-]1,([XYZ])", qkey).group(1), form)] += 1
        return low

    def _sx_price(self, low: Lowered):
        if self.price is None:
            raise RuntimeError("the sx library compares S + XS of its two forms: the device has not set Semantics.price")
        return self.price(low)

    def _sx_bind(self, low: Lowered, d: int) -> None:
        """C4's instance check on a lowering of the sx library (`Lowered.members_agree`, the rule `check_contracts` applies as
        `recovery_instance_members`): the members that run are the declared script's (the entry's, or its fallback's), up to
        reordering members on disjoint wires.  A real-backend lowering with no event document cannot be checked and raises.  The
        placement depends on the script and the patches only, so each instance is checked once."""
        key = (low.c3_key, low.form, low.patches, d)
        if key in self._sx_bound:
            return
        if low.events is None:
            if self.compile_backend == "real":
                raise RuntimeError("the sx library's lowering of %s (form %s) has no event document: C4's instance check cannot run" % (low.c3_key, low.form))
        elif not low.members_agree():
            raise RuntimeError("the sx library's lowering of %s (form %s) fails C4's instance check: executed members %s, declared %s"
                               % (low.c3_key, low.form, low.instance_members(), low.declared_members()))
        self._sx_bound.add(key)

    def lower_ur(self, state, res: Residual, wire: str, wire_cells: dict, occupied, reserved, d: int, patch_ids: dict | None = None,
                 n_program_qubits: int | None = None, miss_fence: bool = False) -> Lowered | None:
        """UR's list for the boundary whose residual is `res` (froq/ur.py: the kept gadgets in `state`
        inverted, the fixup on the producer's `wire`, the kept gadgets again, and the sign frame), placed by `lower`'s
        router (`_place`): None when the router refuses it, as `lower`. `wire_cells` covers every wire of the list
        (`ur_wires`). The builder has ended the walk before a kept gadget without a lowering (`ur.unlowered`). ur_peep places the list after the peephole pass, ur_syn the synthesized list and the peephole
        list and keeps the one with fewer placed rounds (then fewer patch-rounds, then the synthesized one); the record
        `ur` names the list kept (froq/ur_syn.py)."""
        from . import ur as _ur
        assert res.has_q, res                                        # UR keeps Q: a step that would remove it ends the walk
        best, seen = None, []
        for variant in UR_VARIANTS[RECOVERY_MODE]:                   # ur_syn places both lists and keeps the fewer rounds
            script, wires, sign = _ur.script_of(self.tables, state.gates(), state.fix, res, variant)
            if script is None:
                raise RuntimeError("UR list without a lowering (%s): the walk should have ended there" % wires)
            if script in seen:                                       # (the same list twice: the first variant keeps it)
                continue
            seen.append(script)
            assert wires == ur_wires(state, wire), (wires, wire)
            pos = {w: i for i, w in enumerate(wires)}
            frames, frame_end = self.tables.frames_of(script, {i: w for w, i in pos.items()}, "UR")
            key = "ur:" + "|".join("%s %s" % (m["template"], ",".join(map(str, m["wires"]))) for m in script if m["template"] != "frame")
            rec = (state.gates(), state.fix.full_key(), state.dropped, state.pairs, sign) + (() if variant == "routed" else (variant,))
            low = self._place(key, script, wires, frames, frame_end, wire_cells, occupied, reserved, d, patch_ids, n_program_qubits, miss_fence,
                              wires=tuple(wires), ur=rec)
            if low is not None and (best is None or (low.duration, low.P1) < (best.duration, best.P1)):
                best = low                                           # (ties: the earlier variant, the synthesized list)
        return best

    def _place(self, key: str, script, script_wires: list, frames, frame_end, wire_cells: dict, occupied, reserved, d: int, patch_ids, n_program_qubits,
               miss_fence: bool, **extra) -> Lowered | None:
        """`script`'s physical members (wire indices into `wires`) lowered by the compiler's router on the program's layout,
        with its frame members `frames` / `frame_end`: `lower`'s placement, shared by `lower_ur`.  `key` names the script
        in the refusal and cache-miss records (C3's Q key for compensation)."""
        wires = script_wires
        pos = {w: i for i, w in enumerate(wires)}
        lines = ["%s %s" % (TEMPLATE_TO_LLI[m["template"]], " ".join(str(i) for i in m["wires"])) for m in script if m["template"] != "frame"]
        lli = "\n".join(lines)
        doc, patches = None, ()                                      # the instance's semantic document and its wire->patch map
        if not lines:
            instrs = []
        elif self.compile_backend == "fixture":
            flags = dict(wire_cells={pos[w]: list(wire_cells[w]) for w in wires}, occupied=[list(c) for c in sorted(occupied)],
                         reserved=[list(c) for c in sorted(reserved)], d=d)
            instrs = self._compile(lli, len(wires), flags)
        else:
            # the converter's router: the C3 members with the program's PATCH IDS, placed on the program's own layout
            if patch_ids is None or any(w not in patch_ids for w in wires):
                return None
            members = [dict(template=m["template"], wires=[patch_ids[wires[i]] for i in m["wires"]]) for m in script if m["template"] != "frame"]
            fkey = (key, tuple(patch_ids[w] for w in wires))
            if fkey in self._failed:                                 # a script the router already refused on these patches
                return None
            if miss_fence and fkey in self._missed:                  # deferral walk: a placement this machine cannot compile
                self.cache_miss_fences += 1
                raise SnippetMiss(key)
            try:
                snippet = self._compile(members, n_program_qubits or (max(patch_ids.values()) + 1))
            except self._placement_refused as e:
                # a GENUINE geometric refusal: the slicer ran and reported it could not place / route the script on
                # the program's layout (compile.PlacementRefused) -- no placement for this step (counted, not retried)
                self._failed.add(fkey)
                self.compile_failures[key] = str(e).strip().splitlines()[-1][:200] if str(e).strip() else type(e).__name__
                return None
            except Exception as e:
                if self.tables.library == "sx" and isinstance(e, self._converter_error) and "snippet cache miss" in str(e):
                    # under the sx library a cache miss anywhere in a run, a deferral
                    # walk included, fails the run and names the key; library off keeps the walk's fence below
                    self.compile_errors += 1
                    raise self._converter_error[0]("snippet cache miss under --library sx: C3 key %s on patches %s%s (%s)"
                                                   % (key, list(fkey[1]), " in a deferral walk" if miss_fence else "", e)) from e
                if miss_fence and isinstance(e, self._converter_error) and "snippet cache miss" in str(e):
                    # deferral walk (v9-deferral): no cached placement and no slicer here -- the walk ends at this boundary
                    self._missed.add(fkey); self.cache_miss_fences += 1
                    raise SnippetMiss(key) from None
                # EVERYTHING else -- a snippet-cache miss without a slicer, a missing binary, a timeout, malformed
                # provenance -- is an INFRASTRUCTURE failure, never a fence: it raises out of the builder, so C4,
                # training and Q runs fail loudly instead of silently losing every speculation site (round-3 A2/B1)
                self.compile_errors += 1
                raise
            instrs = None if snippet is None else list(snippet.instructions)
            doc = None if snippet is None else snippet.events        # kept: C4 validates the INSTANCE against C1/C3 (A2/B1)
            patches = tuple(patch_ids[w] for w in wires)
        if instrs is None:
            return None
        instrs = tuple(instrs)
        if self.h_depth is not None:                                 # (--h-depth): the script's H gadgets at RHO, so its
            from .hdepth import stretch_instrs                       #  duration, P1, the device's splice and the decision's and U's
            instrs = stretch_instrs(instrs, doc, self.h_depth, d)    #  tables all read the deep rounds' ordinary time
        durs = [Duration.get_true_duration(x.duration, d) for x in instrs]
        return Lowered(instrs, frozenset(p for x in instrs for p in x.patches), critical_path(instrs, durs),
                       sum(durs[i] * len(x.patches) for i, x in enumerate(instrs)), frames, tuple(script), lli, frame_end, doc, patches, **extra)


def critical_path(instrs, durs) -> int:
    """Rounds of a lowered script: an instruction follows every earlier one sharing a patch (the .lss ordering rule), so
    it starts at the latest finish on its patches (kept per patch instead of scanning every earlier pair). With
    nonnegative durations a patch's finishes never decrease, so its last one is its latest and the two rules agree; a
    negative duration would break that, so it raises here, before any placement is built."""
    assert all(du >= 0 for du in durs), ("a negative duration", durs)
    last, end = {}, 0
    for x, du in zip(instrs, durs):
        f = max((last.get(p, 0) for p in x.patches), default=0) + du
        last.update(dict.fromkeys(x.patches, f))
        end = max(end, f)
    return end


def ur_wires(state, wire: str) -> list:
    """The program wires of UR's list (ur_routed, froq/ur.py): the kept gadgets' and the producer's, sorted."""
    return sorted({wire}.union(*(set(g[1]) for g in state.kept)))


def undo_redo_lowered(sched: "ScheduleView", step_instrs: list, own: set, low: Lowered) -> Lowered:
    """The undo-and-redo baseline's recovery after the admitted steps `step_instrs` (.lss indices per step): undo the
    admitted gates in reverse order, apply the producer's correction, redo the gates.  An undone gate is charged as a copy
    of its own instructions (a Clifford gadget's inverse is the same gadget; S^dagger is the S gadget plus a Pauli frame),
    the correction as the producer's own S-branch instructions `own`; duration, cells and price follow from the copies by
    the same rules as a compiled script.  The residual's final frame `low.frame_end` is kept: it is Pauli bookkeeping and
    the corrected state is the same."""
    gates = [[i for i in st if i not in own] for st in step_instrs]
    order = [i for st in reversed(gates) for i in reversed(st)] + sorted(own) + [i for st in gates for i in st]
    instrs = tuple(Instruction(sched.instrs[i].name, j, sched.instrs[i].patches, sched.instrs[i].duration,
                               merge_faces=sched.instrs[i].merge_faces, group_name="RECOVERY") for j, i in enumerate(order))
    durs = [Duration.get_true_duration(x.duration, sched.d) for x in instrs]
    finish = []
    for i, x in enumerate(instrs):
        start = max([finish[j] for j in range(i) if set(instrs[j].patches) & set(x.patches)] or [0])
        finish.append(start + durs[i])
    return Lowered(instrs, frozenset(p for x in instrs for p in x.patches), max(finish or [0]),
                   sum(durs[i] * len(x.patches) for i, x in enumerate(instrs)), low.frames, low.script, "undo_redo", low.frame_end, None, ())


# ---------------------------------------------------------------------------------------------------------- schedule view
class ScheduleView:
    """The .lss facts the builder needs: durations at this distance and predecessor edges among instructions."""

    def __init__(self, instructions: list, dag, d: int, durations: list | None = None):
        self.instrs = instructions                    # swiper Instruction list (index = idx)
        self.dag = dag
        self.d = d
        self.durations = durations if durations is not None else [Duration.get_true_duration(x.duration, d) for x in instructions]

    def critical_path(self, idxs, dur_override: dict | None = None) -> int:
        """Completion time of a set of instructions run back-to-back under their own dependency edges."""
        idxs = sorted(idxs)
        s = set(idxs); done = {}
        for i in idxs:
            dur = (dur_override or {}).get(i, self.durations[i])
            start = max([done[p] for p in self.dag.predecessors(i) if p in s] or [0])
            done[i] = start + dur
        return max(done.values(), default=0)


# ---------------------------------------------------------------------------------------------------------- data model
@dataclass
class Step:
    k: int
    atoms: tuple                       # atom ids folded into this step (zero-duration ones first, the positive one last)
    template: str                      # C2 row of the positive atom ("CNOT_cf_patched", "T2[pred=asis]", "measure", ...)
    instrs: tuple                      # .lss indices admitted with the step
    events: tuple                      # event ids admitted with the step
    duration: int                      # 1..D_MAX rounds, exact
    residual: Residual                 # after the step
    L: frozenset
    D_fixed: frozenset
    D_cond: frozenset
    recovery: Lowered | None           # the lowered recovery script for the residual after this step
    price_P1: int
    reserved_cells: frozenset
    frame_after: tuple                 # (Pauli, ((bit, Pauli), ...)) applied after the script
    at_event: str                      # the event whose layout placed the recovery

    @property
    def residual_key(self) -> str:
        return self.residual.full_key()


@dataclass
class Contract:
    vid: int
    gadget_id: str
    result_id: str
    producer_event: str
    wire: str
    prediction: bool
    truth_assumed: bool
    entry: Residual | None
    steps: list = field(default_factory=list)
    fence_reason: str = "K"
    fence_events: tuple = ()
    fence_instrs: tuple = ()           # .lss instructions of the fence events (or of their first physical successors)
    refusals: dict = field(default_factory=dict)
    trailing_zero_atoms: tuple = ()    # zero-duration atoms after the last positive step: outside the contract
    step_of: dict = field(default_factory=dict)      # .lss idx -> step index
    n_s: int = 1                      # program-wire patches idling while the value waits (policy input)
    notes: list = field(default_factory=list)
    # deferral walks only (v9-deferral): the script and final frame of boundary 0, the entry residual itself (the S
    # that was skipped), lowered at the producer's T2; None when it has no placement (boundary 0 is then not a choice)
    entry_recovery: "Lowered | None" = None
    entry_frame_after: tuple | None = None
    # the rebased walk of the hybrid mode H only: the event that places boundary 0's
    # script, the state (L, D) at boundary 0, the relabelled bits held for the verdict-1 branch, and the rebase record
    # (j, the early residual and its script, the re-walked prefix steps); None / empty in every other walk
    entry_at: str | None = None
    entry_L: frozenset | None = None
    entry_D: frozenset | None = None
    held_bits: frozenset = frozenset()
    rebase: dict | None = None

    @property
    def F(self) -> int:
        return len(self.steps)

    @property
    def has_site(self) -> bool:
        return self.F >= 1

    @property
    def durations(self):
        return [s.duration for s in self.steps]

    @property
    def prices(self):
        return [0] + [s.price_P1 for s in self.steps]

    def site_class(self) -> tuple:
        return (self.F, tuple(self.durations), tuple(self.prices[1:]), self.n_s)

    def admitted_instrs(self, k: int) -> set:
        return {i for s in self.steps[:k] for i in s.instrs}

    def to_dict(self) -> dict:
        return dict(vid=self.vid, gadget=self.gadget_id, result_id=self.result_id, wire=self.wire, prediction=self.prediction,
                    truth_assumed=self.truth_assumed, entry=self.entry.full_key() if self.entry else None, F=self.F, fence_reason=self.fence_reason,
                    fence_events=list(self.fence_events), fence_instrs=list(self.fence_instrs), refusals=dict(self.refusals), n_s=self.n_s,
                    trailing_zero_atoms=list(self.trailing_zero_atoms), notes=list(self.notes),
                    steps=[dict(k=s.k, atoms=list(s.atoms), template=s.template, instrs=list(s.instrs), events=list(s.events), duration=s.duration,
                                residual=s.residual_key, L=sorted(s.L), D_fixed=sorted(s.D_fixed), D_cond=sorted(s.D_cond), P1=s.price_P1,
                                reserved_cells=sorted(list(c) for c in s.reserved_cells), recovery_duration=(s.recovery.duration if s.recovery else 0),
                                recovery_script=[dict(m) for m in (s.recovery.script if s.recovery else ())], at_event=s.at_event,
                                recovery_frames=[[i, F.key(), pred] for i, F, pred in (s.recovery.frames if s.recovery else ())],
                                recovery_frame_end=(s.recovery.frame_end.key() if s.recovery else I.key()),
                                frame_after=[s.frame_after[0].key(), [(b, g.key()) for b, g in s.frame_after[1]]]) for s in self.steps])


class Fence(Exception):
    def __init__(self, reason: str, events=()):
        super().__init__(reason); self.reason = reason; self.events = tuple(events)


# ---------------------------------------------------------------------------------------------------------- transitions
class Walker:
    """The (E, L, D) transition rules of design Sec. 2.2 as table lookups.  Every method returns the new state or raises
    Fence; the walk (`build_contract`) owns atoms, folding, K, prices and reservation."""

    def __init__(self, es: EventStream, tables: Tables):
        self.es, self.t = es, tables

    # -- certified template instance ------------------------------------------------------------------------
    T_ROWS = ("T1", "T")                                           # rows carrying the signed-Z test (plus "T2[...]")

    def template(self, E: Residual, L: frozenset, D_fixed: frozenset, D_cond: frozenset, atom: Atom, row_id: str):
        """The C2 row of the COMPLETE residual.  The key covers the template's wires plus the table's single spectator
        wire = the one wire outside the template that Q's axis reaches (with the P factor on it); P factors on every other
        wire commute with the gadget and are multiplied back afterwards.  A Q whose axis lies entirely on two outside wires
        (not representable in the key) commutes through unchanged, and a Q the row creates on the template's wires would
        multiply it -- two quarter-turns are not in Lib: fence.  The key is canonicalised with the certifier's rule in the
        template's numbering (the table has canonical keys only); the row's output Q IS the new Q.  After the transition:
        signed-Z for the T rows (recorded per input residual), then the complete canonical residual must be in Lib inside
        the new L, else fence (design Sec. 2.2)."""
        t = self.t
        names = t.wires_of(atom.template_id)
        wires = list(atom.inputs)[:len(names)]                    # program wires by template position (c, t / q)
        assert len(wires) == len(names), (atom.atom_id, atom.inputs, names)
        W = set(wires)
        n = len(wires)
        pos = {w: i for i, w in enumerate(wires)}
        q_outside = False                                         # Q entirely on two wires the template does not touch
        A_out = None                                              # (own reach): the outside part one spectator stands for
        own = ur_own()
        if E.has_q:
            outside = sorted(E.axis.wires - W)
            if len(outside) == 1:
                pos[outside[0]] = n                               # the table's spectator wire
            elif outside:
                if E.axis.wires & W:                              # impossible for a weight <= 2 axis; guard anyway
                    if not own:
                        raise Fence("axis_beyond_spectator", atom.events)
                    A_out = E.axis.restrict(set(outside))         # (froq/ur_reach.py: the spectator's Z stands for A_out)
                    pos[_ur_reach().SPECTATOR] = n
                else:
                    q_outside = True
        back = {i: w for w, i in pos.items()}
        key_wires = set(pos)
        P_rest = E.P.without(key_wires)                           # commutes with the gadget: carried over unchanged
        s_key, A_key = (1, I) if q_outside else (E.qsign, E.axis if A_out is None else
                                                 E.axis.without(set(A_out.wires)).mul(Pauli.of({_ur_reach().SPECTATOR: "Z"})).unsigned())
        Pk, sk = canonical_split(E.P.restrict(key_wires), s_key, A_key, pos)
        key = "P=%s|Q=%s" % (local_pkey(Pk, pos), "I" if A_key.is_identity() else local_qkey(sk, A_key, pos))
        row = t.row(row_id, key)
        if own and (row is None or row.get("result") == "fence"):  # UR's own reach, a row certified on the fly
            ext = _ur_reach().ext_row(row_id, key)
            if ext is not None and ext.get("result") != "fence":
                row = ext
            elif row is None:                                     # (a key past C2's whose own-reach row fences too)
                raise Fence("ur_reach_fence:%s:%s" % (row_id, key), atom.events)
        if row is None:
            raise Fence("no_c2_key:%s:%s" % (row_id, key), atom.events)
        if row.get("result") == "fence":
            raise Fence("c2_fence:%s:%s" % (row_id, key), atom.events)
        if (row_id in self.T_ROWS or row_id.startswith("T2")) and row.get("signed_Z") is None:
            raise Fence("t_signed_z:%s:%s" % (row_id, key), atom.events)   # E Z_q E^dagger = +-Z_q required for a T (Sec. 2.2)
        bits = t.bits_of(row_id)
        rid_of = dict(atom.result_bits)
        if row_id.startswith("T2"):
            rid_of = {"u": next(iter(atom.predicate_ids))}
        elif row_id == FEEDBACK_ROW:                              # w10: the generator on the measured bit
            rid_of = {"m": next(iter(atom.predicate_ids))}
        newD_fixed, newD_cond = set(D_fixed), set(D_cond)
        cond = E.cond_map()
        res = row["result"]
        if isinstance(res, str):
            pk2, qk2 = res.split("|Q=")
            Pn = parse_pkey(pk2[2:], back)
            qs, ax = parse_qkey(qk2, back)
        else:
            qs, ax = parse_qkey(res["Q"], back)
            Pn, gens = self._affine(res["P_table"], bits, back)
            if A_out is not None:
                gens = {b: _ur_reach().unspec(g, A_out) for b, g in gens.items()}
            for b, g in gens.items():
                rid = rid_of.get(b)
                if rid is None:
                    raise Fence("table_bit_unmapped:%s" % b, atom.events)
                cond[rid] = cond.get(rid, I).mul(g)
                if rid not in newD_fixed:
                    newD_cond.add(rid)
        if A_out is not None:                                     # the spectator's Z back to the outside axis it stood for
            Pn, ax = _ur_reach().unspec(Pn, A_out), _ur_reach().unspec(ax, A_out)
        if q_outside:
            if not ax.is_identity():                              # the row's own Q times the untouched Q: not a quarter-turn
                raise Fence("q_product_not_in_lib:%s:%s" % (row_id, key), atom.events)
            qs, ax = E.qsign, E.axis
        rel = row.get("relabel")
        if isinstance(rel, dict):
            for b, m in rel.get("xor_mask", {}).items():
                if m and b in rid_of:
                    newD_fixed.add(rid_of[b])
        # conditional generators: each propagates by the template's Pauli rule (linearity), its relabel becomes conditional
        for b, g in list(cond.items()):
            if not (g.wires & set(wires)):
                continue
            gk = "P=%s|Q=I" % local_pkey(g, {w: i for w, i in pos.items() if i < n})
            grow = t.row(row_id, gk)
            if grow is None or grow.get("result") == "fence" or not isinstance(grow.get("result"), str):
                raise Fence("cond_generator:%s:%s" % (row_id, gk), atom.events)
            gpk, gqk = grow["result"].split("|Q=")
            if gqk != "I":
                raise Fence("cond_generator_q:%s:%s" % (row_id, gk), atom.events)
            if row_id in ("T1", "T") or row_id.startswith("T2"):
                if grow.get("relabel") not in ("identity", None) and not isinstance(grow.get("relabel"), str):
                    raise Fence("cond_relabel_through_T:%s" % gk, atom.events)
            grel = grow.get("relabel")
            if isinstance(grel, dict):
                for bb, m in grel.get("xor_mask", {}).items():
                    if m and bb in rid_of and rid_of[bb] not in newD_fixed:
                        newD_cond.add(rid_of[bb])
            cond[b] = g.without(W).mul(parse_pkey(gpk[2:], back))
        newE = Residual(P_rest.mul(Pn), (), qs, ax.unsigned()).with_cond(cond).canonical()
        newL = frozenset(set(L) | W)                              # L extends by the gadget's wires (mediators are internal)
        if not in_reach(newE, newL):
            raise Fence("not_in_lib", atom.events)
        return newE, newL, frozenset(newD_fixed), frozenset(newD_cond - newD_fixed)

    def _affine(self, table: dict, bits: list, back: dict):
        """P_table {record: pkey} -> P(0) and generators G_b with P(rec) = P(0) prod G_b^{bit_b}; raises Fence otherwise."""
        recs = {k: parse_pkey(v, back) for k, v in table.items()}
        zero = "0" * len(bits)
        if zero not in recs:
            raise Fence("table_no_zero_record")
        P0 = recs[zero]
        gens = {}
        for i, b in enumerate(bits):
            e = "".join("1" if j == i else "0" for j in range(len(bits)))
            if e in recs:
                gens[b] = P0.mul(recs[e]).unsigned()
        for rec, P in recs.items():
            acc = P0
            for i, b in enumerate(bits):
                if rec[i] == "1":
                    acc = acc.mul(gens.get(b, I))
            if acc.unsigned() != P.unsigned():
                raise Fence("table_not_affine")
        return P0.unsigned(), {b: g for b, g in gens.items() if not g.is_identity()}

    # -- elementary Pauli-product measurement ----------------------------------------------------------------
    def measure(self, E: Residual, L, D_fixed, D_cond, eid: str):
        ev = self.es.events[eid]
        M, _sign = self.es.observable(eid)
        if E.has_q and not E.axis.commutes(M):
            raise Fence("measurement_axis_anticommutes", [eid])
        newD_fixed, newD_cond = set(D_fixed), set(D_cond)
        rid = ev.result_id
        fixed_flip = not E.P.commutes(M)
        if fixed_flip and rid:
            newD_fixed.add(rid)
        cond_flips = {b for b, g in E.cond if not g.commutes(M)}
        if cond_flips and rid and rid not in newD_fixed:
            newD_cond.add(rid)
        newE, newL = E, set(L)
        if ev.raw.get("destructive"):
            w = ev.raw["wire"]
            if E.has_q and w in E.axis.wires:
                if cond_flips:
                    raise Fence("destructive_reduction_conditional_sign", [eid])
                rest = E.axis.without({w})
                if rest.is_identity():
                    newE = Residual(E.P, E.cond, 1, I)
                else:
                    newE = Residual(E.P, E.cond, E.qsign, rest)
                    # Z_w -> (-1)^{b'} with b' = recorded bit xor the fixed relabel: an outcome-dependent sign, absorbed into P
                    newE = absorb_sign(newE, frozenset([rid]) if rid else frozenset(), fixed_flip)
            newE = Residual(newE.P.without({w}), tuple((b, g.without({w})) for b, g in newE.cond), newE.qsign, newE.axis).canonical()
            newL.discard(w)
        if not in_reach(newE, newL):
            raise Fence("not_in_lib", [eid])
        return newE, frozenset(newL), frozenset(newD_fixed), frozenset(newD_cond)

    def discard(self, E: Residual, L, eid: str):
        w = self.es.events[eid].raw["wire"]
        if E.has_q and w in E.axis.wires and E.axis.weight > 1:
            raise Fence("discard_nonlocal_residual", [eid])
        axis = I if (E.has_q and w in E.axis.wires) else E.axis
        newE = Residual(E.P.without({w}), tuple((b, g.without({w})) for b, g in E.cond), E.qsign if not axis.is_identity() else 1, axis).canonical()
        newL = frozenset(set(L) - {w})
        if not in_reach(newE, newL):
            raise Fence("not_in_lib", [eid])
        return newE, newL

    def frame(self, E: Residual, eid: str):
        """An unconditional (w.r.t. D) frame F on a wire of L: E -> F E F^dagger; a sign flip of Q keyed on the frame's
        predicate would need a generator on an external bit -- conservative fence unless F commutes with the axis."""
        ev = self.es.events[eid]
        F = Pauli.of({ev.raw["wire"]: ev.raw["pauli"]})
        if E.has_q and not E.axis.commutes(F):
            if not ev.predicate_ids:
                return absorb_sign(E, frozenset(), True)
            if len(ev.predicate_ids) == 1:
                return absorb_sign(E, frozenset(ev.predicate_ids), False)
            raise Fence("frame_flips_axis_on_composite_predicate", [eid])
        return E


# ---------------------------------------------------------------------------------------------------------- the walk
@dataclass
class ValueSpec:
    vid: int
    gadget_id: str
    result_id: str
    producer_event: str
    wire: str
    t2: Atom


def values_of(es: EventStream) -> list:
    return [ValueSpec(i, gid, rid, prod, wire, t2) for i, (gid, rid, prod, wire, t2) in enumerate(es.t_gadgets())]


def build_contract(es: EventStream, sem: Semantics, sched: ScheduleView, spec: ValueSpec, prediction: bool, K: int,
                   other_reserved=frozenset(), other_L=frozenset(), other_D=frozenset(), skip_durations: dict | None = None,
                   state=None, deferral: bool = False, claimed=frozenset(), rebase_at: int | None = None) -> Contract:
    """Walk forward from the producer under the assumption that the prediction is WRONG (truth = not prediction; the
    residual after the predicted branch), up to K positive-duration steps or the first fence. `other_*` are the other
    unresolved values' reservations / support / classical dependencies at build time (the overlap invariant is re-checked
    by the device at every admission). `skip_durations` overrides .lss durations (the device passes the zeroed group of a
    predicted skip).

    `deferral` (v9-deferral, arm D): the walk of a DEFERRED correction -- the same residual
    algebra, tables, scripts and fences, with the speculation limits removed: no step-length limit (a step longer than
    D_MAX rounds is carried; no timer is involved), the caller passes an unbounded K and no reservations, and a boundary
    whose script has no cached placement ends the walk (`no_cached_placement`) instead of raising. `claimed`: event ids
    that belong to another live frame's walk; the walk ends at the first atom that contains one (`frame_overlap`), so two
    frames never share a gadget. The script of boundary 0 (the skipped S itself) is lowered too (`entry_recovery`).

    `rebase_at` (deferral walks only; the hybrid mode H, section 3): the rebased contract of an
    early materialization at boundary j = rebase_at. The walk runs as the deferral walk to boundary j, then continues
    from the verdict-0 branch: the residual is the applied correction's inverse (`pauli.inverse_residual`), D restarts
    at the value's own result, the relabelled bits so far are held (`held_bits`: their consumers are fenced, and an event
    predicated on one ends the walk, `rebased_held_bit`), and K counts the steps after j. `steps` are the rebased steps,
    `rebase` records j, the early residual and script and the re-walked prefix, and boundary 0 is the inverse with its
    script placed at boundary j's event (`entry_at`). A walk that ends before j, or whose boundary j or rebased boundary 0
    has no script, returns with `rebase` None."""
    assert rebase_at is None or (deferral and not prediction and rebase_at >= 0), "a rebased walk is a deferral walk's (H)"
    t = sem.tables
    truth = not prediction
    c = Contract(spec.vid, spec.gadget_id, spec.result_id, spec.producer_event, spec.wire, prediction, truth, None)
    E = t.entry_residual(prediction, truth)
    assert E is not None
    E = Residual(E.P, E.cond, E.qsign, E.axis.rename({0: spec.wire}))
    c.entry = E
    ur_on = RECOVERY_MODE in UR_MODES                        # UR's list at every boundary (froq/ur.py; its variants)
    if ur_on:
        from . import ur as _ur
        urs, amap = _ur.URState(E, strict=ur_own()), _ur.amap_of(es)
    if deferral:                                             # boundary 0: the entry residual's own script at the producer's T2
        at0 = spec.t2.events[-1]
        cells0 = {w: es.wire_cell(w, at0) for w in E.axis.wires}
        if all(cells0.values()):
            wc0 = {w: cs[0] for w, cs in cells0.items()}
            try:
                if ur_on:                                    # (no kept gadget yet: the list is the fixup, C3's script of E)
                    low0 = sem.lower_ur(urs, E, spec.wire, wc0, set(es.occupied_cells(at0)) - set(wc0.values()), frozenset(), sched.d,
                                        patch_ids=es.patch_id, n_program_qubits=len(es.program_wires), miss_fence=True)
                else:
                    low0 = sem.lower(E, wc0, set(es.occupied_cells(at0)) - set(wc0.values()), frozenset(), sched.d, patch_ids=es.patch_id,
                                     n_program_qubits=len(es.program_wires), miss_fence=True)
            except SnippetMiss:
                low0 = None
            if low0 is not None:
                fixed0, cond0 = E.recovery_frame()
                c.entry_recovery, c.entry_frame_after = low0, (fixed0.mul(low0.frame_end).unsigned(), cond0)
    L = frozenset([spec.wire]); D_fixed = frozenset([spec.result_id]); D_cond = frozenset()
    held: frozenset = frozenset()                             # H's rebased walk: the verdict-1 branch's relabelled bits
    rebased = rebase_at is None                               # (True: no switch pending)

    def rebase_switch():
        """H: switch, at boundary rebase_at, to the verdict-0 branch of an early
        materialization there."""
        nonlocal E, D_fixed, D_cond, held, rebased, urs
        j = rebase_at
        prefix = list(c.steps)
        if j == 0:
            Ej, at_j, low_j = c.entry, spec.t2.events[-1], c.entry_recovery
        else:
            Ej, at_j, low_j = prefix[j - 1].residual, prefix[j - 1].at_event, prefix[j - 1].recovery
        if low_j is None:
            raise Fence("rebase_no_script")
        Einv = inverse_residual(Ej)
        if ur_on:                                           # the commit's undo: UR's list at j inverted (the same kept chain, the fixup inverted)
            urs = urs.rebased()
        cells = {w: es.wire_cell(w, at_j) for w in (ur_wires(urs, spec.wire) if ur_on else Einv.axis.wires)}
        if not all(cells.values()):
            raise Fence("rebase_no_placement")
        wc = {w: cs[0] for w, cs in cells.items()}
        try:
            if ur_on:
                low = sem.lower_ur(urs, Einv, spec.wire, wc, set(es.occupied_cells(at_j)) - set(wc.values()), frozenset(), sched.d,
                                   patch_ids=es.patch_id, n_program_qubits=len(es.program_wires), miss_fence=True)
            else:
                low = sem.lower(Einv, wc, set(es.occupied_cells(at_j)) - set(wc.values()), frozenset(), sched.d, patch_ids=es.patch_id,
                                n_program_qubits=len(es.program_wires), miss_fence=True)
        except SnippetMiss:
            low = None
        if low is None:
            raise Fence("rebase_no_placement")
        fixed, cond = Einv.recovery_frame()
        c.rebase = dict(j=j, early=Ej, early_recovery=low_j, early_at=at_j, prefix=prefix, L=L, D_fixed=D_fixed, D_cond=D_cond)
        c.held_bits = held = frozenset(D_fixed | D_cond)
        c.steps, c.step_of = [], {}
        E, D_fixed, D_cond = Einv, frozenset([spec.result_id]), frozenset()
        c.entry, c.entry_at, c.entry_L, c.entry_D = E, at_j, L, D_fixed
        c.entry_recovery, c.entry_frame_after = low, (fixed.mul(low.frame_end).unsigned(), cond)
        rebased = True
        if K <= 0:                                          # (rule 33's K_tot): no rebased step is left at all
            raise Fence("K", ())
    walker = Walker(es, t)
    atom_of = certified_atoms(es, t)
    nomeas = (CONTRACT_MODE == "nomeas" and not deferral) or CONTRACT_MODE == "nomeas_all"   # Q-nomeas (Q walks
                                                                         # only); U v6's ablation, every walk (rule 56b)
    measuring: dict = {}

    def gadget_measures(gid) -> bool:
        if gid not in measuring:
            measuring[gid] = any(t.measures(a.template_id) for a in es.atoms_of(gid))
        return measuring[gid]
    processed = set()
    pending_zero: list = []                       # zero-duration atoms waiting to be folded into the next positive atom
    pending_instrs: list = []; pending_events: list = []
    def dur_of(idxs, override=None):
        return sched.critical_path(idxs, override)

    def n_s_of(Lw):
        return sum(1 for w in Lw if es.roles_by_wire.get(w) == "program") or 1

    c.n_s = n_s_of(L)
    own_set: set = set()                                      # the producer's own T2 instructions (undo_redo: the correction)

    def emit_step(atom_ids, template, instrs, events, duration, at_event):
        nonlocal E, L, D_fixed, D_cond
        if duration > D_MAX:
            if not deferral:
                c.refusals["over_64"] = c.refusals.get("over_64", 0) + 1
                raise Fence("over_64", events)
            c.refusals["carried_over_64"] = c.refusals.get("carried_over_64", 0) + 1   # deferral: no timer, the step is carried
        if ur_on:                                           # UR's kept gadgets after this step (ur_static's rule): no script -> the walk ends
            urs.step(es, t, SimpleNamespace(atoms=tuple(atom_ids), template=template), amap, c.gadget_id,
                     c.steps[-1].residual if c.steps else c.entry, E, len(c.steps) + 1, [i for i in instrs if i not in own_set])
            why = urs.block or (_ur.unlowered(urs) and "no_lowering:" + _ur.unlowered(urs)) or \
                (not set(ur_wires(urs, spec.wire)) <= L and "wire_outside_L") or \
                (ur_own() and len(ur_wires(urs, spec.wire)) > UR_MAX_WIRES and "wire_limit")   # (the device holds L only; own-reach limit)
            if why:
                c.refusals["ur_no_script"] = c.refusals.get("ur_no_script", 0) + 1
                raise Fence("ur_no_script:" + why, events)
        # recovery for the residual after this step, placed on the program's layout at this point
        wire_cells = {w: es.wire_cell(w, at_event) for w in (ur_wires(urs, spec.wire) if ur_on else E.axis.wires)}
        for w, cells in wire_cells.items():
            if not cells:
                raise Fence("no_cell_for_wire:%s" % w, events)
            wire_cells[w] = cells[0]
        occupied = set(es.occupied_cells(at_event)) - set(wire_cells.values())
        lower = (lambda *a, **k: sem.lower_ur(urs, E, spec.wire, *a, **k)) if ur_on else (lambda *a, **k: sem.lower(E, *a, **k))
        if deferral:
            try:
                lowered = lower(wire_cells, occupied, other_reserved, sched.d, patch_ids=es.patch_id, n_program_qubits=len(es.program_wires),
                                miss_fence=True)
            except SnippetMiss:
                c.refusals["no_cached_placement"] = c.refusals.get("no_cached_placement", 0) + 1
                raise Fence("no_cached_placement", events)
        else:
            lowered = lower(wire_cells, occupied, other_reserved, sched.d, patch_ids=es.patch_id, n_program_qubits=len(es.program_wires))
        if lowered is None:
            c.refusals["no_recovery_workspace"] = c.refusals.get("no_recovery_workspace", 0) + 1
            raise Fence("no_recovery_workspace", events)
        if RECOVERY_MODE == "undo_redo" and lowered.instrs:
            lowered = undo_redo_lowered(sched, [st.instrs for st in c.steps] + [list(instrs)], own_set, lowered)
        foot = set(lowered.cells) | {c_ for w in L for c_ in es.wire_cell(w, at_event)}
        if foot & set(other_reserved) or (L & set(other_L)) or ((D_fixed | D_cond) & set(other_D)):
            c.refusals["held_overlap"] = c.refusals.get("held_overlap", 0) + 1
            raise Fence("held_overlap", events)
        # the frame the device applies when the spliced physical members are done: Q^dagger P^dagger Q (Sec. 2.2) times the
        # script's own frame members commuted to its end (S^dagger = S gadget then Z, the Y-axis basis changes)
        fixed, cond = E.recovery_frame()
        frame_after = (fixed.mul(lowered.frame_end).unsigned(), cond)
        step = Step(k=len(c.steps), atoms=tuple(atom_ids), template=template, instrs=tuple(sorted(instrs)), events=tuple(events), duration=duration,
                    residual=E, L=L, D_fixed=D_fixed, D_cond=D_cond, recovery=lowered, price_P1=lowered.P1, reserved_cells=lowered.cells,
                    frame_after=frame_after, at_event=at_event)
        c.steps.append(step)
        for i in step.instrs:
            c.step_of[i] = step.k

    def admit_atom(atom_id, template, instrs, events, duration, at_event):
        """Fold zero-duration atoms forward; a positive atom closes a step (and may end the contract at K)."""
        pending_zero.append(atom_id); pending_instrs.extend(instrs); pending_events.extend(events)
        if duration == 0:
            return
        emit_step(list(pending_zero), template, list(pending_instrs), list(pending_events), duration, at_event)
        pending_zero.clear(); pending_instrs.clear(); pending_events.clear()
        if not rebased:
            if len(c.steps) == rebase_at:
                rebase_switch()                             # H: K counts the steps after the rebase
        elif len(c.steps) >= K:
            raise Fence("K", ())

    start = es.events[spec.producer_event].pos
    last_on: dict = {spec.wire: start}                       # wire -> frontier: the last admitted logical event on it
    logical = logical_events(es, t)
    try:
        # contract entry: the predicted branch of the producing gadget's T2 (design Sec. 2.2, round-2 B)
        own = spec.t2
        processed.update(own.events)
        own_instrs = es.lss_of(own.events)
        own_set.update(own_instrs)
        own_dur = 0 if not prediction else dur_of(own_instrs, skip_durations)
        # a gadget with physical work on the corrected wire both before and after the producer has no place in the
        # logical order of any region of this value -- no site.  A foreign logical event inside the producing T gadget's
        # own physical span on the corrected wire (between its injection merge and its T2's last physical event: the gate
        # would sit between the two segments of one certified operation; M1 / N4) likewise -- no site.
        if wire_straddlers(es, t, spec.wire, start):
            raise Fence("wire_has_straddler", ())
        if not contiguous_on_wires(es, t, [e for a in es.atoms_of(spec.gadget_id) for e in a.events], L):
            raise Fence("atom_interleaved_on_wire", ())
        admit_atom(own.atom_id, "T2_S" if prediction else "T2_skip", own_instrs, own.events, own_dur, own.events[-1])
        start = es.events[spec.producer_event].pos
        import heapq
        heap: list = []; queued: set = set()

        def push(eid):
            if eid is not None and eid not in processed and eid not in queued:
                queued.add(eid); heapq.heappush(heap, (es.events[eid].pos, eid))

        def push_next(w, pos):
            """The first not-yet-processed event on wire w after `pos` (processed frames placed late are stepped over)."""
            nxt = es.next_on_wire(w, pos)
            while nxt is not None and nxt in processed:
                nxt = es.next_on_wire(w, es.events[nxt].pos)
            push(nxt)

        def check_new_wires(L_prev, L_now, eids):
            """Two program-order conditions of the residual algebra, tested when an atom is about to be admitted (it
            becomes the fence when either fails). Positions are those of PHYSICAL events: a gadget's frames are
            bookkeeping of the byproduct its certified channel applies at the gadget (revision 7, `atoms_by_wire`).
            (1) A wire joining L through this atom carries no gadget with physical work on it both before and after the
            joining member: such a gadget has no place in the logical order of the region. (2) On every wire of L it
            touches, the atom's physical work is contiguous: no LOGICAL event of another gadget -- physical work, or a gate
            without an instruction such as a Pauli gadget's `clifford` (`logical_events`) -- lies between its first and its
            last physical event there (a template executed with foreign work inside it is not an instance of the certified
            template; K1 / L1: a Pauli gate inside another gadget's span used to be stepped over)."""
            for w in L_now - L_prev:
                pos_w = min([es.events[e].pos for e in eids if es.lss_map.get(e) and w in es.events[e].wires] or [min(es.events[e].pos for e in eids)])
                if wire_straddlers(es, t, w, pos_w):
                    raise Fence("wire_has_straddler", eids)
            if not contiguous_on_wires(es, t, eids, L_now):
                raise Fence("atom_interleaved_on_wire", eids)

        def mark(eids):
            """Advance the per-wire frontier past the LOGICAL members only (physical work and instruction-free gates): a
            gadget's byproduct frame is bookkeeping placed late in the stream, and the next gadget's members may legitimately
            sit between the physical members and that frame. A wire's frontier starts at the joining atom's contact with it,
            not at the producer's position (K1: a joining gadget may have touched its other wire before the
            producer; the gates between that contact and the producer are inside the region)."""
            for e in eids:
                pe = es.events[e]
                if e not in logical:
                    continue
                for w in pe.wires:
                    last_on[w] = max(last_on.get(w, -1), pe.pos)

        def feed(wires_, rids):
            """Frontier: the next event on every wire of L after the last one processed there, every consumer of an id in
            D, and the output commits of L's wires / D's results (checked against the state current when popped)."""
            for w in wires_:
                push_next(w, last_on.get(w, start))
                for out in es.io_outputs_of_wire.get(w, ()):
                    push(out)
            for rid in rids:
                for cons in es.consumers.get(rid, ()):
                    if es.events[cons].pos > start:
                        push(cons)
                for out in es.io_outputs_of_result.get(rid, ()):
                    push(out)
        if not rebased and rebase_at == 0:
            rebase_switch()
        mark(own.events)
        feed(L, D_fixed | D_cond | held)
        while heap:
            pos_, eid = heapq.heappop(heap)
            queued.discard(eid)
            ev = es.events[eid]
            history = (all(pos_ <= last_on.get(w, start) for w in ev.wires & L) if ev.wires & L else pos_ <= start)
            if eid in processed or history:                      # before the frontier on every wire of L it touches (an
                processed.add(eid)                                # output commit or consumer pushed by `feed`): skip
                for w in ev.wires & L:
                    push_next(w, ev.pos)
                continue
            if held and ev.predicate_ids & held:
                raise Fence("rebased_held_bit", [eid])        # H: a bit relabelled in the verdict-1 branch
            touches_L = bool(ev.wires & L)
            on_D = bool(ev.predicate_ids & (D_fixed | D_cond))
            is_output = ev.kind == "io" and ev.raw.get("io_kind", ev.raw.get("kind_io")) == "output" and (
                ev.raw.get("wire") in L or es.events.get(ev.raw.get("commit_event", ""), None) is not None and
                es.events[ev.raw["commit_event"]].result_id in (D_fixed | D_cond))
            if is_output:
                raise Fence("output_commit", [eid])
            if not touches_L and not on_D:
                processed.add(eid); mark([eid])
                for w in ev.wires & L:
                    push_next(w, ev.pos)
                continue
            atom = atom_of.get(eid)
            if atom is not None and t.has_template(atom.template_id):
                if held and (atom.predicate_ids & held or any(es.events[m].predicate_ids & held for m in atom.events)):
                    raise Fence("rebased_held_bit", atom.events)  # H: e.g. a T2 whose predicate the verdict-1 branch relabels
                g = es.gadgets[atom.gadget_id]
                external_pred = atom.predicate_ids - set(g.result_bits.values())
                row_id = atom.template_id
                if RECOVERY_MODE == "undo_redo" and row_id == "T1":
                    raise Fence("undo_non_clifford", atom.events)   # a T gate cannot be undone by running it again
                if row_id == "T2":
                    u = next(iter(atom.predicate_ids))
                    if u in D_cond:
                        raise Fence("t2_predicate_conditionally_relabelled", atom.events)
                    row_id = "T2[pred=flipped]" if u in D_fixed else "T2[pred=asis]"
                elif atom.predicate_ids and is_feedback_atom(es, atom):
                    # w10: a CZ classically controlled by an earlier MEASURE (a measurement-based uncomputation).  Its
                    # bit is a verified program result; if the wrong branch changed it (it is in D) the CZ consumes the
                    # speculation -- a classical consumer; otherwise both branches are certified by CZ[pred=asis]
                    # (Q must not depend on the bit, P may: a generator on it).  Undo-and-redo would have to undo a
                    # classically controlled gate: it ends there (the earlier line's fence, hub v9-adder 0460fc9,
                    # which port left out because its base had no undo-and-redo; recovery mode brings it).
                    if atom.predicate_ids & (D_fixed | D_cond):
                        raise Fence("classical_consumer", atom.events)
                    if RECOVERY_MODE == "undo_redo":
                        raise Fence("undo_conditional", atom.events)
                    row_id = FEEDBACK_ROW
                elif atom.predicate_ids:                       # only the T2 has a certified conditional row (round-7 M2 / N2)
                    raise Fence("conditional_atom", atom.events)
                elif external_pred & (D_fixed | D_cond):
                    raise Fence("classical_consumer", atom.events)
                for m in atom.events:                          # members predicated on an EXTERNAL id in D are consumers
                    mev = es.events[m]
                    if mev.predicate_ids and (mev.predicate_ids - set(g.result_bits.values()) - atom.predicate_ids) & (D_fixed | D_cond):
                        raise Fence("classical_consumer_member", [m])
                # program order on the wires of L (revision 7): a certified atom's logical position on a wire
                # is that of its first PHYSICAL event there; its frames are bookkeeping of the byproduct its certified
                # channel applies at the gadget (certify.py "frame applied last"; design Sec. 1.3 `load_events_logical`;
                # the parity-projector check `scratchpad/c6root/frame_semantics_check.py`: the literal reading -- a Pauli
                # inserted where the frame event sits -- disagrees with the physics whenever the frame's bit is 1).
                # Physical work on a wire of L before the frontier there (the last admitted physical member, or the
                # producer) puts the atom BEFORE the region on that wire: a frame of such an atom is stepped over (root
                # cause 1: the walk used to admit the whole atom through its frame), while physical work of it after the
                # frontier leaves the atom no place in the logical order of the region (it straddles the frontier on one
                # wire, or is before the region on one wire and inside it on another): the contract ends here.
                pre = [w for w in L if any(es.lss_map.get(e) and w in es.events[e].wires and es.events[e].pos < last_on.get(w, start)
                                           for e in atom.events)]
                if pre:
                    if not es.lss_map.get(eid):
                        processed.add(eid)
                        for w in ev.wires & L:
                            push_next(w, ev.pos)
                        continue
                    raise Fence("wire_has_straddler", atom.events)
                # a gadget is popped through its EARLIEST event; its later members may lie behind pending work of the light
                # cone (unprocessed events on wires of L placed before the gadget's last physical event) that the schedule
                # runs FIRST: a pending instruction that precedes one of this gadget's instructions on a shared patch (the
                # wire's own patch, or a routing cell the compiler reused).  Admitting the gadget would put that pending work
                # into a LATER step (or the fence) although this step's member waits for it -- the step could never
                # complete (fermi_hubbard_2_2_Kagome d <= 9, carleman_2_4 d <= 9 at K = 8).  The contract ends here.
                P_X = max([es.events[e].pos for e in atom.events if es.lss_map.get(e)] or [-1])   # -1: a frame-only atom, nothing to run
                X_patches = [(x, set(sched.instrs[x].patches)) for x in es.lss_of(atom.events)]
                for w in (L if X_patches else ()):
                    e = es.next_on_wire(w, last_on.get(w, start))
                    while e is not None and es.events[e].pos < P_X:
                        if e not in processed and e not in atom.events:
                            for i in es.lss_map.get(e, ()):
                                pi = set(sched.instrs[i].patches)
                                if any(x > i and pi & px for x, px in X_patches):
                                    raise Fence("atom_behind_pending_work", atom.events)
                        e = es.next_on_wire(w, es.events[e].pos)
                if deferral and claimed and not claimed.isdisjoint(atom.events):
                    raise Fence("frame_overlap", atom.events)      # the atom belongs to another live frame's walk (v9-deferral)
                if nomeas and gadget_measures(atom.gadget_id):
                    raise Fence("nomeas_gadget", atom.events)      # Q-nomeas: where the full walk would admit the atom
                L_prev = L
                E, L, D_fixed, D_cond = walker.template(E, L, D_fixed, D_cond, atom, row_id)
                if not in_reach(E, L):
                    raise Fence("not_in_lib", atom.events)
                instrs = es.lss_of(atom.events)
                processed.update(atom.events); mark(atom.events)
                check_new_wires(L_prev, L, atom.events)
                admit_atom(atom.atom_id, row_id, instrs, atom.events, dur_of(instrs), atom.events[-1])
                feed(L, D_fixed | D_cond | held)
                continue
            # elementary events (outside every certified gadget)
            if nomeas and atom is not None and gadget_measures(atom.gadget_id):
                raise Fence("nomeas_gadget", [eid])               # Q-nomeas: a member of a measuring gadget without a C2 row
            if on_D and ev.kind in CONSUMER_KINDS | {"measure_product", "measure_single", "clifford", "init", "transfer", "discard"}:
                raise Fence("classical_consumer", [eid])
            if es.lss_map.get(eid) and any(ev.pos < last_on.get(w, start) for w in ev.wires & L):
                raise Fence("wire_has_straddler", [eid])         # physical work on another wire of L before its frontier
            if deferral and claimed and eid in claimed:
                raise Fence("frame_overlap", [eid])              # the event belongs to another live frame's walk (v9-deferral)
            L_prev = L
            k = ev.kind
            if k in ("measure_product", "measure_single"):
                if RECOVERY_MODE == "undo_redo":
                    raise Fence("undo_measurement", [eid])      # a measurement cannot be undone
                if nomeas:
                    raise Fence("nomeas_measurement", [eid])    # Q-nomeas: an elementary measurement ends the contract
                E, L, D_fixed, D_cond = walker.measure(E, L, D_fixed, D_cond, eid)
            elif k == "discard":
                if ev.raw.get("wire") in L:
                    if RECOVERY_MODE == "undo_redo":
                        raise Fence("undo_discard", [eid])
                    E, L = walker.discard(E, L, eid)
            elif k == "frame_update":
                E = walker.frame(E, eid)
            elif k == "correction":
                raise Fence("correction_on_support", [eid])
            elif k == "clifford":                             # a gate outside every certified gadget: the converter emits none
                raise Fence("elementary_clifford", [eid])     # (round-7 review M3 / N1: the old synthetic atom acted on a label)
            elif k in ("init", "transfer", "rotate_boundary", "busy_region", "io"):
                pass
            else:
                raise Fence("unknown_kind:%s" % k, [eid])
            if not in_reach(E, L):
                raise Fence("not_in_lib", [eid])
            instrs = es.lss_of([eid])
            processed.add(eid); mark([eid])
            check_new_wires(L_prev, L, (eid,))
            admit_atom(eid, k, instrs, (eid,), dur_of(instrs) if instrs else 0, eid)
            feed(L, D_fixed | D_cond | held)
        c.fence_reason = "end"
    except Fence as f:
        c.fence_reason = f.reason
        c.fence_events = f.events
    c.trailing_zero_atoms = tuple(pending_zero)
    fence_instrs = set()
    for eid in c.fence_events:
        fence_instrs.update(first_instructions(es, eid))
    c.fence_instrs = tuple(sorted(fence_instrs - set(c.step_of)))
    if state is not None and (c.steps or c.rebase is not None):
        refuse_executed_cone_work(es, c, state)
    return c


def is_feedback_atom(es: EventStream, atom: Atom) -> bool:
    """w10: the atom is a CZ gadget predicated on exactly one EXTERNAL result produced by the destructive readout of a
    MEASURE gadget (the classically controlled CZ of a measurement-based uncomputation, compile.py `_bind_feedback`)."""
    if atom.template_id != "CZ" or len(atom.predicate_ids) != 1:
        return False
    rid = next(iter(atom.predicate_ids))
    if rid in set(es.gadgets[atom.gadget_id].result_bits.values()):
        return False
    prod = es.events.get(es.by_result.get(rid, ""))
    return prod is not None and prod.kind == "measure_single" and prod.gadget in es.gadgets and \
        es.gadgets[prod.gadget].template_id == "MEASURE"


def certified_atoms(es: EventStream, t: Tables) -> dict:
    """event id -> the atom that admits it: the segments of the OUTERMOST gadget whose template the tables certify; a
    composite gadget without its own C2 rows (e.g. `CZ` = H, CNOT, H before WP2 adds its rows) is walked through its member
    gadgets in sequence, each a certified atom of its own; member events outside any certified gadget stay elementary."""
    cache = es.__dict__.setdefault("_certified_atoms", {})
    if id(t) in cache:
        return cache[id(t)]
    atom_of = {}

    def visit(gid):
        g = es.gadgets[gid]
        if t.has_template(g.template_id):
            for a in es.atoms_of(gid):
                for eid in a.events:
                    atom_of[eid] = a
            return
        for m in g.members:
            if m in es.gadgets:
                visit(m)
    for gid, g in es.gadgets.items():
        if g.parent is None:
            visit(gid)
    cache[id(t)] = atom_of
    return atom_of


def first_instructions(es: EventStream, eid: str, seen=None) -> set:
    """The .lss instructions of an event, or -- for an event without physical work (a frame, an io commit) -- of its first
    dependency successors that have some: what the device must hold at a fence."""
    seen = seen if seen is not None else set()
    if eid in seen:
        return set()
    seen.add(eid)
    idxs = set(es.lss_map.get(eid, ()))
    if idxs:
        return idxs
    out = set()
    for succ in es.dag.successors(eid):
        out |= first_instructions(es, succ, seen)
    return out


def logical_events(es: EventStream, t) -> frozenset:
    """Event ids that are logical operations of the program on their wires: every event with an instruction, every
    `clifford` (a gate -- the Pauli gates X / Y / Z carry no instruction: carleman_2_4 g35 is a gadget of template Z whose
    only member is a `clifford`; certify.py certifies them as operations), and every event of a certified atom without
    physical members (a frame-only Pauli template).  A gadget's byproduct `frame_update`s are NOT logical events (see
    `atoms_by_wire`).  Cached on the event stream per table set."""
    cache = es.__dict__.setdefault("_logical_events", {})
    if id(t) in cache:
        return cache[id(t)]
    out = {eid for eid, ev in es.events.items() if es.lss_map.get(eid) or ev.kind == "clifford"}
    seen = set()
    for a in certified_atoms(es, t).values():
        if a.atom_id in seen:
            continue
        seen.add(a.atom_id)
        if not any(es.lss_map.get(e) for e in a.events):
            out.update(a.events)
    cache[id(t)] = frozenset(out)
    return cache[id(t)]


def atoms_by_wire(es: EventStream, t) -> dict:
    """wire -> (list of (first physical position on the wire, last physical position on the wire, atom) sorted by the first
    position, the sorted first positions, the running maximum of the last positions) for every certified atom with physical
    work on that wire; cached on the event stream per table set.  Frames do not count (revision 7): a gadget's
    `frame_update` is bookkeeping of the byproduct its certified channel applies AT the gadget (certify.py: "frame applied
    last" per template; design Sec. 1.3; `scratchpad/c6root/frame_semantics_check.py`), placed in the stream where its
    bits become known -- often after other gadgets' work on the wire (carleman_2_4 g175#0: merge 591, H of g176 598,
    frame 603)."""
    cache = es.__dict__.setdefault("_atoms_by_wire", {})
    if id(t) in cache:
        return cache[id(t)]
    out: dict = collections.defaultdict(list); seen = set()
    for a in certified_atoms(es, t).values():
        if a.atom_id in seen:
            continue
        seen.add(a.atom_id)
        for w in {w for e in a.events if es.lss_map.get(e) for w in es.events[e].wires}:
            pos = [es.events[e].pos for e in a.events if es.lss_map.get(e) and w in es.events[e].wires]
            out[w].append((min(pos), max(pos), a))
    table = {}
    for w, lst in out.items():
        lst.sort(key=lambda x: x[0])
        prefix_max = []; m = -1
        for _, last_phys, _ in lst:
            m = max(m, last_phys); prefix_max.append(m)
        table[w] = (lst, [x[0] for x in lst], prefix_max)
    cache[id(t)] = table
    return table


def wire_straddlers(es: EventStream, t, w: str, pos: int) -> list:
    """Certified atoms that straddle position `pos` on wire `w`: physical work on the wire strictly before it AND strictly
    after it (frames do not count, see `atoms_by_wire`).  Complete interval query over the per-wire table (the scan stops as
    soon as no earlier atom reaches past `pos`); certified atoms are not compact, so no window is assumed."""
    table = atoms_by_wire(es, t)
    if w not in table:
        return []
    lst, keys, prefix_max = table[w]; found = []
    k = bisect.bisect_left(keys, pos) - 1
    while k >= 0 and prefix_max[k] > pos:
        first_phys, last_phys, a = lst[k]
        if last_phys > pos:
            found.append(a)
        k -= 1
    return found


def contiguous_on_wires(es: EventStream, t, eids, wires) -> bool:
    """On every wire of `wires` that the events touch physically, no LOGICAL event of another gadget (`logical_events`:
    physical work, or a gate without an instruction) lies strictly between the first and the last physical event of `eids`
    on that wire; byproduct frames on either side are bookkeeping and do not count."""
    ev_set = set(eids); logical = logical_events(es, t)
    for w in wires:
        pos = sorted(es.events[e].pos for e in eids if es.lss_map.get(e) and w in es.events[e].wires)
        if len(pos) < 2:
            continue
        e = es.next_on_wire(w, pos[0])
        while e is not None and es.events[e].pos < pos[-1]:
            if e not in ev_set and e in logical:
                return False
            e = es.next_on_wire(w, es.events[e].pos)
    return True


def refuse_executed_cone_work(es: EventStream, c: Contract, state) -> None:
    """Cone work that had already started when the contract was built (at the prediction: nothing existed to hold it) means
    the state has moved past the boundary the contract describes (an H on q2 executed before value 1588
    was predicted turns the boundary residual Z(q10)Z(q2) into Z(q10)X(q2)); no boundary of the contract describes the
    state, so the contract loses its site and the value WAITs (`executed_cone_work`). `state(i)` is 0 (not started), 1
    (running) or 2 (completed); the cone is the whole fence (`fence_set`: exits and D consumers included) plus the members of
    steps >= 1 acting on a wire of their step's L_before. Members outside their step's light cone may have run early
    (design Sec. 2.4) and are ignored. (Folding completed work into step 0 was tried and rejected.)"""
    def wires_of(i):
        evs = es.lss_reverse.get(i, ())
        return set().union(*(es.events[e].wires for e in evs)) if evs else set()
    bad = any(state(i) >= 1 for i in fence_set(es, c))
    for s in c.steps[(0 if c.rebase is not None else 1):]:           # H's rebased contract: its first step too (section 3)
        Lb = (set(c.steps[s.k - 1].L) if s.k > 0 else set(c.entry_L)) | {c.wire}
        if any(state(i) >= 1 for i in s.instrs if wires_of(i) & Lb):
            bad = True
    if bad:
        c.refusals["executed_cone_work"] = c.refusals.get("executed_cone_work", 0) + 1
        c.fence_reason = "executed_cone_work"; c.steps = []; c.step_of = {}


def wire_exit(es: EventStream, c: Contract, w: str, admitted_events: set | None = None, admitted: set | None = None):
    """The event on wire `w` at which the value's light cone leaves the contract: the first event after the LAST admitted
    physical event on `w` that carries not-admitted physical work (its own instructions, or -- for a frame / commit -- the
    instructions of its first physical successors). Admitted events and events whose instructions are all admitted
    (a gadget's late-placed frame resolving into its own members) are stepped over, never taken as the exit; the scan starts
    at the admitted member that touched the wire, not at the atom's last event. None: no exit."""
    admitted = set(c.step_of) if admitted is None else admitted
    admitted_events = {e for s in c.steps for e in s.events} if admitted_events is None else admitted_events
    pos = max([es.events[e].pos for e in admitted_events if w in es.events[e].wires and es.lss_map.get(e)] or [es.events[c.producer_event].pos])
    eid = es.next_on_wire(w, pos)
    while eid is not None:
        if eid not in admitted_events and first_instructions(es, eid) - admitted:
            return eid
        eid = es.next_on_wire(w, es.events[eid].pos)
    return None


def fence_set(es: EventStream, c: Contract) -> set:
    """Every .lss instruction that must wait for settlement while the value is unresolved: the fence events' instructions,
    every consumer of an id in D, and on every wire of L the exit event (`wire_exit`; everything later on the wire is
    DAG-ordered behind it). Independent of `c.step_of` except for exempting the admitted members. H's rebased contract
   its re-walked prefix counts as admitted, the state at its boundary 0 is (entry_L,
    entry_D), and the consumers of its held bits are fenced too."""
    admitted = set(c.step_of)
    out = set(c.fence_instrs)
    last = c.steps[-1] if c.steps else None
    L = set(last.L) if last else (set(c.entry_L) if c.entry_L is not None else {c.wire})
    D = set(last.D_fixed | last.D_cond) if last else (set(c.entry_D) if c.entry_D is not None else {c.result_id})
    D |= c.held_bits
    admitted_events = {e for s in c.steps for e in s.events}
    if c.rebase is not None:
        for s in c.rebase["prefix"]:
            admitted.update(s.instrs); admitted_events.update(s.events)
    for rid in D:
        for eid in es.consumers.get(rid, ()):
            if eid not in admitted_events:
                out |= first_instructions(es, eid)
    for w in L:
        eid = wire_exit(es, c, w, admitted_events, admitted)
        if eid is not None:
            out |= first_instructions(es, eid)
    for w in L:
        for eid in es.io_outputs_of_wire.get(w, ()):
            out |= first_instructions(es, eid)
    for eid in c.trailing_zero_atoms:
        if eid in es.events:
            out |= set(es.lss_map.get(eid, ()))
    return out - admitted
