"""Fixed-point formats and helpers of the Froq engine (plan Sec. 5).

Every number is a Python int in the LSB units of its format. The RTL uses
exactly these widths and rounding points; the golden is written so that a
Verilator run of the RTL must match it bit for bit."""

# ---- formats: (integer bits, fractional bits) --------------------------------
F_S = 24            # survival S: Q1.24, 1.0 == S_ONE
S_ONE = 1 << F_S
S_BITS = 25
F_R = 24            # suffix sum R: Q11.24
R_BITS = 35
F_OMC = 20          # 1 - c: Q0.20
OMC_ONE = 1 << F_OMC
F_XY = 16           # x_k, y_k = (1-c) * r: Q16.16
XY_BITS = 32
F_NS = 8            # n_s: Q8.8
NS_BITS = 16
F_V = 24            # Vhat, Qhat: Q22.24 (46 bits)
V_BITS = 46
V_MAX = (1 << V_BITS) - 1
F_ACC = 48          # composer accumulator: Q8.48 (56 bits)
ACC_BITS = 56
ACC_MAX = (1 << ACC_BITS) - 1
F_OM = 24           # Omega: Q13.24
OM_BITS = 37
OM_MAX = (1 << OM_BITS) - 1
OMACC_BITS = 61     # Omega accumulator: Q13.48
OMACC_MAX = (1 << OMACC_BITS) - 1
BELIEF_FLOOR_SHIFT = 20   # WAIT wherever S[j] < S[0] * 2^-20
MAX_SHIFT = 23      # block shift range 0..23


def rnd(v: int, s: int) -> int:
    """Round half up of v / 2^s (v >= 0)."""
    if s <= 0:
        return v
    return (v + (1 << (s - 1))) >> s


def trunc(v: int, s: int) -> int:
    """Floor of v / 2^s (v >= 0)."""
    return v >> s if s > 0 else v


def sat(v: int, vmax: int, flags: dict | None = None, name: str = "sat") -> int:
    """Saturate v into [0, vmax]; record a sticky flag when it happens."""
    if v > vmax:
        if flags is not None:
            flags[name] = True
        return vmax
    if v < 0:
        if flags is not None:
            flags[name] = True
        return 0
    return v


def block_shift(s_a: int) -> int:
    """Leading-zero shift that lands a Q1.24 survival S[a] > 0 in [0.5, 1).
    Capped at MAX_SHIFT.  Caller handles s_a == 0 (the exhausted case)."""
    assert s_a > 0
    sh = 0
    while (s_a << sh) < (1 << (F_S - 1)) and sh < MAX_SHIFT:
        sh += 1
    return sh


def to_float(v: int, frac: int) -> float:
    return v / float(1 << frac)
