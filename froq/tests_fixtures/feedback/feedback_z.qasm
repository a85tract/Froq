// w10 test fixture: a T value whose Z-type residual meets a CZ classically controlled by an earlier measurement
// (the contract passes it through the C2 rows CZ[pred=asis]: CZ commutes with a Z residual in both branches)
OPENQASM 2.0;
include "qelib1.inc";
qreg q[3];
creg m[1];
h q[2];
measure q[2] -> m[0];
t q[0];
if(m==1) cz q[0],q[1];
h q[1];
