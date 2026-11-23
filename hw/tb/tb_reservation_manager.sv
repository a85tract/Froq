// tb_resv: the reservation manager's testbench. Reads +VEC=<file>, one line
// each:
//   L <act_lat>                                        the configuration
//   O <op> <slot> <cores> <held> <s> <r> <word> <sel> <data, hex> <frame>
//                                                      one operation on its port (codes 0-6: the slot port; the others:
//                                                      the cell port), driven when the port is ready, its result awaited
//   B <n>                                              the next n O lines are a batch: each port takes its own operations
//                                                      back to back, the two ports at once
//   K <mask> <c0> <c1> <c2> <c3>                       a status check of the cells whose mask bit is set, awaited
//   KO <mask> <c0> <c1> <c2> <c3>                      a check presented in the same cycle as the next O line's operation
//   KB <n>                                             the next n K lines are presented on consecutive cycles
//   D                                                  dump the state (slots, cells and guards, read hierarchically)
//   X                                                  end
// and writes +OUT=<file>:
//   R <port> <op> <ok> <slot> <held> <total> <unstarted> <q_ws> <q_tgt> <q_lock> <cycles> <taken>
//                                                      one per operation (port 0: slots, 1: cells), each port's in order;
//                                                      cycles from the cycle it was presented in to its result, and that
//                                                      cycle
//   Q <h0> <h1> <h2> <h3> <cycles> <taken>             one per check, in order: each cell's {lock, tgt, ws} > 0 bits
//   S <slot> <kind> <cores> <frm> <fin> <started> <counting>   each valid slot (fin -1 for a lazy one)
//   C <cell> <ws> <tgt> <lk>                           each cell with a non-zero count
//   G <frame> <guard, hex>                             each frame whose guard is not empty
//   N <now> <total> <unstarted>                        the end of a dump
//   E <operations> <checks> <cycles>
`timescale 1ns/1ps
module tb_resv;
  logic clk = 1'b0, rst_n = 1'b0;
  always #1.75 clk = ~clk;                                   // 3.5 ns
  logic [7:0]  act_lat = 8'd7;
  // the slot port
  logic        d_v = 1'b0, d_rdy;
  logic [4:0]  d_op = '0;
  logic [5:0]  d_slot = '0;
  logic [9:0]  d_cores = '0, d_held = '0;
  logic [15:0] d_s = '0, d_r = '0;
  logic        dr_v, dr_ok;
  logic [4:0]  dr_op;
  logic [5:0]  dr_slot;
  logic [7:0]  dr_held;
  logic [15:0] dr_total, dr_unstarted;
  // the cell port
  logic        w_v = 1'b0, w_rdy;
  logic [4:0]  w_op = '0;
  logic [3:0]  w_word = '0;
  logic        w_sel = 1'b0;
  logic [63:0] w_data = '0;
  logic [5:0]  w_frame = '0;
  logic        wr_v, wr_q_ws, wr_q_tgt, wr_q_lock;
  logic [4:0]  wr_op;
  // the status port
  logic        s_v = 1'b0;
  logic [3:0]  s_cv = '0;
  logic [35:0] s_cells = '0;
  logic        sr_v;
  logic [11:0] sr_held;

  se_resv dut (.clk, .rst_n, .act_lat, .d_v, .d_rdy, .d_op, .d_slot, .d_cores, .d_held, .d_s, .d_r, .dr_v, .dr_op, .dr_ok,
               .dr_slot, .dr_held, .dr_total, .dr_unstarted, .w_v, .w_rdy, .w_op, .w_word, .w_sel, .w_data, .w_frame, .wr_v,
               .wr_op, .wr_q_ws, .wr_q_tgt, .wr_q_lock, .s_v, .s_cv, .s_cells, .sr_v, .sr_held);

  integer cyc = 0;
  always @(posedge clk) cyc <= cyc + 1;

  typedef struct {
    int op, slot, cores, held, s, r, word, sel, frame;
    logic [63:0] data;
  } op_t;
  typedef struct { int m, c0, c1, c2, c3; } chk_t;

  integer fi, fo, nops = 0, nchk = 0, a_lat, a_n;
  string  vec, outp, line;

  function automatic op_t parse_op(string l);
    op_t o;
    void'($sscanf(l, "O %d %d %d %d %d %d %d %d %h %d", o.op, o.slot, o.cores, o.held, o.s, o.r, o.word, o.sel, o.data, o.frame));
    return o;
  endfunction

  function automatic chk_t parse_chk(string l);
    chk_t k;
    string tag;
    void'($sscanf(l, "%s %d %d %d %d %d", tag, k.m, k.c0, k.c1, k.c2, k.c3));
    return k;
  endfunction

  task automatic drive_d(op_t o, bit aligned = 1'b0);       // one slot operation: drive it, await its result (aligned: at
    int t0;                                                  // the negedge the last result was seen, back to back)
    if (!aligned) @(negedge clk);
    d_op = 5'(o.op); d_slot = 6'(o.slot); d_cores = 10'(o.cores); d_held = 10'(o.held); d_s = 16'(o.s); d_r = 16'(o.r);
    d_v = 1'b1;
    while (!d_rdy) @(negedge clk);
    t0 = cyc;
    @(negedge clk);                                          // taken at the edge between
    d_v = 1'b0;
    while (!dr_v) @(negedge clk);
    $fwrite(fo, "R 0 %0d %0d %0d %0d %0d %0d 0 0 0 %0d %0d\n", dr_op, dr_ok, dr_slot, dr_held, dr_total, dr_unstarted, cyc - t0, t0);
    nops++;
  endtask

  task automatic drive_w(op_t o, bit aligned = 1'b0);       // one cell operation
    int t0;
    if (!aligned) @(negedge clk);
    w_op = 5'(o.op); w_word = 4'(o.word); w_sel = o.sel[0]; w_data = o.data; w_frame = 6'(o.frame); w_v = 1'b1;
    while (!w_rdy) @(negedge clk);
    t0 = cyc;
    @(negedge clk);
    w_v = 1'b0;
    while (!wr_v) @(negedge clk);
    $fwrite(fo, "R 1 %0d 0 0 0 0 0 %0d %0d %0d %0d %0d\n", wr_op, wr_q_ws, wr_q_tgt, wr_q_lock, cyc - t0, t0);
    nops++;
  endtask

  task automatic do_op(op_t o);
    if (o.op <= 6) drive_d(o); else drive_w(o);
  endtask

  task automatic present(chk_t k, output int t0);           // present one check for one cycle
    s_cv = 4'(k.m); s_cells = {9'(k.c3), 9'(k.c2), 9'(k.c1), 9'(k.c0)}; s_v = 1'b1;
    t0 = cyc;
  endtask

  task automatic collect(int t0);                           // the next check result
    while (!sr_v) @(negedge clk);
    $fwrite(fo, "Q %0d %0d %0d %0d %0d %0d\n", sr_held[2:0], sr_held[5:3], sr_held[8:6], sr_held[11:9], cyc - t0, t0);
    nchk++;
  endtask

  task automatic check1(chk_t k);                           // one check, awaited
    int t0;
    @(negedge clk);
    present(k, t0);
    @(negedge clk);
    s_v = 1'b0;
    collect(t0);
  endtask

  task automatic burst(chk_t ks[$]);                        // checks on consecutive cycles, their results in order
    int t0s[$];
    fork
      begin
        foreach (ks[i]) begin
          int t0;
          @(negedge clk);
          present(ks[i], t0);
          t0s.push_back(t0);
        end
        @(negedge clk);
        s_v = 1'b0;
      end
      begin
        foreach (ks[i]) begin
          @(negedge clk);
          while (!sr_v) @(negedge clk);
          $fwrite(fo, "Q %0d %0d %0d %0d %0d %0d\n", sr_held[2:0], sr_held[5:3], sr_held[8:6], sr_held[11:9], cyc - t0s[i], t0s[i]);
          nchk++;
        end
      end
    join
  endtask

  task automatic batch(op_t os[$]);                         // each port's operations back to back, the two ports at once
    op_t bd[$], bw[$];
    foreach (os[i]) if (os[i].op <= 6) bd.push_back(os[i]); else bw.push_back(os[i]);
    fork
      begin foreach (bd[i]) drive_d(bd[i], i > 0); end
      begin foreach (bw[i]) drive_w(bw[i], i > 0); end
    join
  endtask

  task automatic dump();
    integer fin;
    @(negedge clk);
    for (int i = 0; i < 64; i++)
      if (dut.valid[i]) begin
        fin = dut.sl[i].kind ? integer'(dut.sl[i].fin) : -1;           // (a lazy slot has no end)
        $fwrite(fo, "S %0d %0d %0d %0d %0d %0d %0d\n", i, dut.sl[i].kind, dut.sl[i].cores, dut.sl[i].frm, fin,
                dut.sl[i].started, dut.sl[i].counting);
      end
    for (int c = 0; c < 512; c++)
      if (dut.ws[c] != 0 || dut.tgt[c] != 0 || dut.lk[c] != 0)
        $fwrite(fo, "C %0d %0d %0d %0d\n", c, dut.ws[c], dut.tgt[c], dut.lk[c]);
    for (int f = 0; f < 64; f++)
      if (dut.gm[f] != '0)
        $fwrite(fo, "G %0d %h\n", f, dut.gm[f]);
    $fwrite(fo, "N %0d %0d %0d\n", dut.now, dut.total, dut.unstarted);
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
    if (!$value$plusargs("VEC=%s", vec)) $fatal(1, "tb_resv: no +VEC");
    if (!$value$plusargs("OUT=%s", outp)) $fatal(1, "tb_resv: no +OUT");
    fi = $fopen(vec, "r"); fo = $fopen(outp, "w");
    if (fi == 0 || fo == 0) $fatal(1, "tb_resv: cannot open the files");
    repeat (4) @(posedge clk);
    rst_n = 1'b1;
    repeat (2) @(posedge clk);
    forever begin
      line = next_line();
      if (line.len() == 0 || line.substr(0, 0) == "X") break;
      if (line.substr(0, 0) == "L") begin
        void'($sscanf(line, "L %d", a_lat)); act_lat = 8'(a_lat);
      end else if (line.substr(0, 0) == "O") begin
        do_op(parse_op(line));
      end else if (line.substr(0, 0) == "B") begin
        automatic op_t os[$];                                // (a fresh queue each batch: a static one kept the last)
        os.delete();
        void'($sscanf(line, "B %d", a_n));
        repeat (a_n) os.push_back(parse_op(next_line()));
        batch(os);
      end else if (line.len() >= 2 && line.substr(0, 1) == "KO") begin
        automatic chk_t k; automatic op_t o;
        k = parse_chk(line); o = parse_op(next_line());
        fork
          check1(k);
          do_op(o);
        join
      end else if (line.len() >= 2 && line.substr(0, 1) == "KB") begin
        automatic chk_t ks[$];
        ks.delete();
        void'($sscanf(line, "KB %d", a_n));
        repeat (a_n) ks.push_back(parse_chk(next_line()));
        burst(ks);
      end else if (line.substr(0, 0) == "K") begin
        check1(parse_chk(line));
      end else if (line.substr(0, 0) == "D") begin
        dump();
      end
    end
    $fwrite(fo, "E %0d %0d %0d\n", nops, nchk, cyc);
    $fclose(fo); $fclose(fi);
    $finish;
  end
endmodule
