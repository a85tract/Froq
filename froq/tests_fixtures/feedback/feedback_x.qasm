// w10 test fixture: a T value whose residual an H turns into an X-type quarter-turn before a classically controlled CZ
// (CZ^m rot(X0) CZ^-m depends on m: the C2 row CZ[pred=asis] is a fence)
OPENQASM 2.0;
include "qelib1.inc";
qreg q[3];
creg m[1];
h q[2];
measure q[2] -> m[0];
t q[0];
h q[0];
if(m==1) cz q[0],q[1];
