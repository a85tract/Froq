// tb_w2_table: the decision unit's testbench. Reads +VEC=<file>, one line each:
//   C <op> <row> <data, hex> <k> <half> <edges, hex> <fence> <val> <c> <e0> <mask> <gap> <lb>
//                                                      one control operation, presented for one cycle
//   Q <port> <val> <cut> <adm> <cap> <cok>             one lookup, its result awaited
//   B <n>                                              the next n Q lines: each port's on consecutive cycles, both at once
//   S                                                  the next C line and the Q line after it, presented in one cycle
//   D                                                  dump the live values (read hierarchically)
//   X                                                  end
// and writes +OUT=<file>:
//   A <port> <val> <r> <act> <ro> <jr> <cycles> <taken>   one per lookup, each port's in order; cycles from the cycle
//                                                      it was presented in to its result, and that cycle
//   V <v> <k> <c> <pending c or -1> <age> <beta> <mask> <jumps>   each live value
//   T <lb>                                             the end of a dump
//   E <lookups> <cycles>
`timescale 1ns/1ps
module tb_w2_table;
  logic clk = 1'b0, rst_n = 1'b0;
  always #1.75 clk = ~clk;                                   // 3.5 ns
  logic        c_v = 1'b0, c_half = 1'b0;
  logic [2:0]  c_op = '0, c_c = '0;
  logic [13:0] c_row = '0;
  logic [67:0] c_data = '0;
  logic [5:0]  c_k = '0, c_val = '0;
  logic [69:0] c_edges = '0;
  logic [4:0]  c_fence = '0;
  logic [9:0]  c_e0 = '0;
  logic [16:0] c_mask = '0, c_gap = '0;
  logic [1:0]  c_lb = '0;
  logic [1:0]  q_v = '0, q_cok = '0;
  logic [11:0] q_val = '0;
  logic [9:0]  q_cut = '0, q_adm = '0, q_cap = '0;
  logic [1:0]  a_v, a_jr;
  logic [11:0] a_val;
  logic [9:0]  a_r, a_ro;
  logic [3:0]  a_act;

  se_w2_table dut (.clk, .rst_n, .c_v, .c_op, .c_row, .c_data, .c_k, .c_half, .c_edges, .c_fence, .c_val, .c_c, .c_e0,
                   .c_mask, .c_gap, .c_lb, .q_v, .q_val, .q_cut, .q_adm, .q_cap, .q_cok, .a_v, .a_val, .a_r, .a_act, .a_ro,
                   .a_jr);

  integer cyc = 0;
  always @(posedge clk) cyc <= cyc + 1;

  typedef struct { int op, row, k, half, fence, val, c, e0, mask, gap, lb; logic [67:0] data; logic [69:0] edges; } ctl_t;
  typedef struct { int port, val, cut, adm, cap, cok; } lk_t;

  integer fi, fo, nlk = 0;
  string  vec, outp, line;

  function automatic ctl_t parse_ctl(string l);
    ctl_t o;
    void'($sscanf(l, "C %d %d %h %d %d %h %d %d %d %d %d %d %d", o.op, o.row, o.data, o.k, o.half, o.edges, o.fence, o.val,
                  o.c, o.e0, o.mask, o.gap, o.lb));
    return o;
  endfunction

  function automatic lk_t parse_lk(string l);
    lk_t q;
    void'($sscanf(l, "Q %d %d %d %d %d %d", q.port, q.val, q.cut, q.adm, q.cap, q.cok));
    return q;
  endfunction

  task automatic set_ctl(ctl_t o);
    c_op = 3'(o.op); c_row = 14'(o.row); c_data = o.data; c_k = 6'(o.k); c_half = o.half[0]; c_edges = o.edges;
    c_fence = 5'(o.fence); c_val = 6'(o.val); c_c = 3'(o.c); c_e0 = 10'(o.e0); c_mask = 17'(o.mask); c_gap = 17'(o.gap);
    c_lb = 2'(o.lb); c_v = 1'b1;
  endtask

  task automatic set_lk(lk_t q);
    q_val[q.port * 6 +: 6] = 6'(q.val); q_cut[q.port * 5 +: 5] = 5'(q.cut); q_adm[q.port * 5 +: 5] = 5'(q.adm);
    q_cap[q.port * 5 +: 5] = 5'(q.cap); q_cok[q.port] = q.cok[0]; q_v[q.port] = 1'b1;
  endtask

  task automatic collect(int port, int t0);                 // the port's next result
    while (!a_v[port]) @(negedge clk);
    $fwrite(fo, "A %0d %0d %0d %0d %0d %0d %0d %0d\n", port, a_val[port * 6 +: 6], a_r[port * 5 +: 5], a_act[port * 2 +: 2],
            a_ro[port * 5 +: 5], a_jr[port], cyc - t0, t0);
    nlk++;
  endtask

  task automatic one(lk_t q, ctl_t o, bit with_ctl);       // a lookup (with a control operation in the same cycle)
    int t0;
    @(negedge clk);
    if (with_ctl) set_ctl(o);
    set_lk(q);
    t0 = cyc;
    @(negedge clk);
    q_v = '0; c_v = 1'b0;
    collect(q.port, t0);
  endtask

  logic [50:0] w0[$], w1[$];                                 // results {val 6, r 5, act 2, ro 5, jr 1, cycle 32}
  int t0q0[$], t0q1[$];                                      // the cycles their lookups were presented in

  task automatic emit(int port, logic [50:0] w, int t0);
    $fwrite(fo, "A %0d %0d %0d %0d %0d %0d %0d %0d\n", port, w[50:45], w[44:40], w[39:38], w[37:33], w[32], int'(w[31:0]) - t0, t0);
    nlk++;
  endtask

  task automatic burst(lk_t qs[$]);                         // each port's lookups on consecutive cycles, both at once
    lk_t p0[$], p1[$];
    int n;
    foreach (qs[i]) if (qs[i].port == 0) p0.push_back(qs[i]); else p1.push_back(qs[i]);
    n = (p0.size() > p1.size()) ? p0.size() : p1.size();
    w0.delete(); w1.delete(); t0q0.delete(); t0q1.delete();
    fork
      begin                                                  // presenting
        for (int k = 0; k < n; k++) begin
          @(negedge clk);
          q_v = '0;
          if (k < p0.size()) begin set_lk(p0[k]); t0q0.push_back(cyc); end
          if (k < p1.size()) begin set_lk(p1[k]); t0q1.push_back(cyc); end
        end
        @(negedge clk);
        q_v = '0;
      end
      begin                                                  // port 0's results
        for (int k = 0; k < p0.size(); k++) begin
          @(negedge clk);
          while (!a_v[0]) @(negedge clk);
          w0.push_back({a_val[5:0], a_r[4:0], a_act[1:0], a_ro[4:0], a_jr[0], 32'(cyc)});
        end
      end
      begin                                                  // port 1's results
        for (int k = 0; k < p1.size(); k++) begin
          @(negedge clk);
          while (!a_v[1]) @(negedge clk);
          w1.push_back({a_val[11:6], a_r[9:5], a_act[3:2], a_ro[9:5], a_jr[1], 32'(cyc)});
        end
      end
    join
    foreach (w0[k]) emit(0, w0[k], t0q0[k]);
    foreach (w1[k]) emit(1, w1[k], t0q1[k]);
  endtask

  task automatic dump();
    @(negedge clk);
    for (int v = 0; v < 64; v++)
      if (dut.vld[v])
        $fwrite(fo, "V %0d %0d %0d %0d %0d %0d %0d %0d\n", v, dut.vst[v][40:35], dut.vc[v], dut.vcpv[v] ? int'(dut.vcp[v]) : -1,
                dut.ve[v], dut.vst[v][34], dut.vst[v][33:17], dut.vst[v][16:0]);
    $fwrite(fo, "T %0d\n", dut.lb);
  endtask

  function automatic string next_line();
    string l;
    l = "";
    while (l.len() == 0 && !$feof(fi)) begin
      void'($fgets(l, fi));
      if (l.len() > 0 && l[l.len() - 1] == "\n") l = l.substr(0, l.len() - 2);
    end
    return l;
  endfunction

  initial begin
    if (!$value$plusargs("VEC=%s", vec)) $fatal(1, "tb_w2_table: no +VEC");
    if (!$value$plusargs("OUT=%s", outp)) $fatal(1, "tb_w2_table: no +OUT");
    fi = $fopen(vec, "r"); fo = $fopen(outp, "w");
    if (fi == 0 || fo == 0) $fatal(1, "tb_w2_table: cannot open the files");
    repeat (4) @(posedge clk);
    rst_n = 1'b1;
    repeat (2) @(posedge clk);
    forever begin
      line = next_line();
      if (line.len() == 0 || line.substr(0, 0) == "X") break;
      if (line.substr(0, 0) == "C") begin
        @(negedge clk);
        set_ctl(parse_ctl(line));
        @(negedge clk);
        c_v = 1'b0;
      end else if (line.substr(0, 0) == "Q") begin
        automatic ctl_t none;
        one(parse_lk(line), none, 1'b0);
      end else if (line.substr(0, 0) == "S") begin
        automatic ctl_t o; automatic lk_t q;
        o = parse_ctl(next_line()); q = parse_lk(next_line());
        one(q, o, 1'b1);
      end else if (line.substr(0, 0) == "B") begin
        automatic lk_t qs[$];
        automatic int n;
        qs.delete();
        void'($sscanf(line, "B %d", n));
        repeat (n) qs.push_back(parse_lk(next_line()));
        burst(qs);
      end else if (line.substr(0, 0) == "D") begin
        dump();
      end
    end
    $fwrite(fo, "E %0d %0d\n", nlk, cyc);
    $fclose(fo); $fclose(fi);
    $finish;
  end
endmodule
