// se_resv: the scheduler's reservation manager for static reservation (Section 5.1 of the paper).
//
// Two stores on two operation ports, and a read-only status port. The golden model is hw/golden/resv.py, written from
// the simulator's reservation logic (froq/device.py).
// - The slot port (d_*): RM-D's operations, LAZY, ACT, REL, STARTED, HELD, TICK and TOTALS, one at a time (d_v / d_rdy;
//   one result pulse dr_v per operation, in order).
// - The cell port (w_*): RM-W's operations, MASKW, the cell updates, QUERY, GUARD_ADD and GUARD_SUB, one at a time
//   (w_v / w_rdy; wr_v).
// - The status port (s_*): the held bits of up to four cells, one check a cycle.
// The two stores share no state, so the two operation ports run independently: RM-D's TICK no longer holds RM-W. An
// operation sent to the other store's port is answered and does nothing.
//
// RM-D, the decoder-core reservations (rules 41c, 44a, 50c): N_R slots in distributed RAM, a slot's id returned by its
// reserve and named by the later operations. A slot holds its kind (lazy or ACT), its cores, the round it counts from,
// its end (ACTs only), its started cores (rule 50c) and whether it counts now. The totals are kept current:
//   total      the cores the pool counts now;
//   unstarted  the sum of max(0, cores - started) over the slots that count.
// TICK starts the next round and scans the slots, one a cycle: an ACT past its end expires, an ACT whose effect round
// has come starts to count. Rounds are a 16-bit wrapping counter; a reservation's life stays below 2^15 rounds.
//
// RM-W, the workspace reservations: N_C cells, each with three saturating counters in flip-flops -- ws (the live
// frames' candidate workspaces), tgt (decided, unspliced materializations) and lk (running scripts). Operands are two
// cell masks: A, the cells operated on, and B, the asking frame's own cells. An update moves every cell of A one up or
// down; QUERY answers whether a cell of A is held beyond its share in B (another frame's), one flag per counter.
// The frames' guards are kept by frame number (N_G frames, distributed RAM): GUARD_ADD f stores mask A as frame f's
// guard and moves every cell of A one up in ws; GUARD_SUB f moves every cell of frame f's guard one down in ws and
// clears the guard. So a guard is written once, at registration, and the frame's end or rebase needs no mask load. The
// guard memory is cleared at configuration, not by rst_n.
// The status port gives, for each cell asked, {lk > 0, tgt > 0, ws > 0} (the dispatcher's guard check, `_defer_hold`).
// A check sees every cell operation executed before the cycle it is taken in, and none executed in or after it.
//
// Cycles: an operation is accepted in one cycle and executed in the next, its result registered there; QUERY takes one
// more (the hit vectors, then their OR) and TICK N_R + 1 more (the scan, then its totals). A check is registered in the
// cycle it is presented, read within its cell's group of 64 in the next and selected by the group in the third.
module se_resv #(
  parameter int N_R = 64,
  parameter int N_C = 512,
  parameter int N_G = 64,                    // the frames whose guards are kept by number
  parameter int CW  = 10,                    // cores of one reservation
  parameter int TW  = 16,                    // the totals
  parameter int CB  = $clog2(N_C),           // a cell id
  parameter int GB  = $clog2(N_G)            // a frame number
) (
  input  logic          clk,
  input  logic          rst_n,
  input  logic [7:0]    act_lat,             // rounds from an ACT's decision to its effect
  // the slot port (RM-D)
  input  logic          d_v,
  output logic          d_rdy,
  input  logic [4:0]    d_op,
  input  logic [5:0]    d_slot,
  input  logic [CW-1:0] d_cores,             // LAZY: the lazy demand; ACT: demand_b
  input  logic [CW-1:0] d_held,              // ACT: the frame's held reservation
  input  logic [15:0]   d_s,                 // ACT: S_b, rounds
  input  logic [15:0]   d_r,                 // ACT: R_b, rounds
  output logic          dr_v,
  output logic [4:0]    dr_op,
  output logic          dr_ok,               // LAZY / ACT: a slot was free
  output logic [5:0]    dr_slot,
  output logic [7:0]    dr_held,
  output logic [TW-1:0] dr_total,
  output logic [TW-1:0] dr_unstarted,
  // the cell port (RM-W)
  input  logic          w_v,
  output logic          w_rdy,
  input  logic [4:0]    w_op,
  input  logic [3:0]    w_word,              // MASKW: the 64-bit word of the mask
  input  logic          w_sel,               // MASKW: mask A (0) or B (1)
  input  logic [63:0]   w_data,
  input  logic [GB-1:0] w_frame,             // GUARD_ADD / GUARD_SUB: the frame
  output logic          wr_v,
  output logic [4:0]    wr_op,
  output logic          wr_q_ws,
  output logic          wr_q_tgt,
  output logic          wr_q_lock,
  // the status port
  input  logic          s_v,
  input  logic [3:0]    s_cv,                // which of the four cells are asked
  input  logic [4*CB-1:0] s_cells,           // four cell ids, the first in the low bits
  output logic          sr_v,
  output logic [4*3-1:0]  sr_held            // per cell, the first low: {lk > 0, tgt > 0, ws > 0}; 0 if not asked
);
  localparam logic [4:0] OP_LAZY = 5'd0, OP_ACT = 5'd1, OP_REL = 5'd2, OP_STARTED = 5'd3, OP_HELD = 5'd4, OP_TICK = 5'd5,
                         OP_TOTALS = 5'd6, OP_MASKW = 5'd7, OP_WS_ADD = 5'd8, OP_WS_SUB = 5'd9, OP_TGT_ADD = 5'd10,
                         OP_TGT_SUB = 5'd11, OP_LOCK_ADD = 5'd12, OP_LOCK_SUB = 5'd13, OP_QUERY = 5'd14,
                         OP_GUARD_ADD = 5'd15, OP_GUARD_SUB = 5'd16;
  localparam int NW = (N_C + 63) / 64;       // mask words, and the status port's groups of 64 cells
  if (N_C % 64 != 0) begin : g_bad_n_c $error("se_resv: N_C must be a multiple of 64"); end

  // ============================================================================================ RM-D, the slot port
  typedef struct packed {
    logic          kind;                     // 1: ACT, 0: lazy
    logic [CW-1:0] cores;
    logic [15:0]   frm;                      // counts from this round on
    logic [15:0]   fin;                      // ACT: its last round
    logic [CW-1:0] started;
    logic          counting;
  } slot_t;
  slot_t        sl [N_R];                    // distributed RAM: one read, one write a cycle
  logic [N_R-1:0] valid;
  logic [15:0]  now;
  logic [TW-1:0] total, unstarted;

  typedef enum logic [1:0] {D_IDLE, D_EXEC, D_SCAN, D_TDONE} dst_t;
  dst_t dst;
  logic [4:0]    qd_op;
  logic [5:0]    qd_slot, idx;
  logic [CW-1:0] qd_cores, qd_held;
  logic [15:0]   qd_s, qd_r;

  assign d_rdy = (dst == D_IDLE);

  // the first free slot
  logic [5:0] free_i; logic free_v;
  always_comb begin
    free_v = 1'b0; free_i = '0;
    for (int i = N_R - 1; i >= 0; i--)
      if (!valid[i]) begin free_v = 1'b1; free_i = 6'(i); end
  end

  wire  [5:0]  ra = (dst == D_SCAN) ? idx : qd_slot;
  slot_t rd;
  assign rd = sl[ra];
  wire  [CW-1:0] rd_open = (rd.cores > rd.started) ? (rd.cores - rd.started) : '0;     // max(0, cores - started)
  wire  [15:0]   d_end = rd.fin - now;                                                   // < 0: past its end
  wire  [15:0]   d_frm = now - rd.frm;                                                   // >= 0: counts from now on
  wire  [CW-1:0] act_dem = (qd_cores > qd_held) ? (qd_cores - qd_held) : '0;

  // the slot RAM's one write port: a single enable, address and word a cycle, so that the table maps to distributed RAM
  // (written from four statements with different addresses it was built in flip-flops; run resv3 at 3.5 ns)
  logic        sl_we;
  logic [5:0]  sl_wa;
  slot_t       sl_wd;
  always_comb begin
    sl_we = 1'b0; sl_wa = ra; sl_wd = rd;
    if (dst == D_EXEC && qd_op == OP_LAZY && free_v) begin
      sl_we = 1'b1; sl_wa = free_i;
      sl_wd = '{kind: 1'b0, cores: qd_cores, frm: now, fin: '0, started: '0, counting: 1'b1};
    end else if (dst == D_EXEC && qd_op == OP_ACT && free_v) begin
      sl_we = 1'b1; sl_wa = free_i;
      sl_wd = '{kind: 1'b1, cores: act_dem, frm: now + 16'(act_lat), fin: now + 16'(act_lat) + qd_s + qd_r, started: '0,
                counting: 1'b0};
    end else if (dst == D_EXEC && qd_op == OP_STARTED && valid[qd_slot] && rd.kind && rd.started != {CW{1'b1}}) begin
      sl_we = 1'b1; sl_wa = qd_slot; sl_wd.started = rd.started + 1'b1;
    end else if (dst == D_SCAN && valid[idx] && rd.kind && !rd.counting && d_end[15] == 1'b0 && d_frm[15] == 1'b0) begin
      sl_we = 1'b1; sl_wa = idx; sl_wd.counting = 1'b1;
    end
  end
  always_ff @(posedge clk)
    if (sl_we) sl[sl_wa] <= sl_wd;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      dst <= D_IDLE; valid <= '0; now <= '0; total <= '0; unstarted <= '0; idx <= '0;
      qd_op <= '0; qd_slot <= '0; qd_cores <= '0; qd_held <= '0; qd_s <= '0; qd_r <= '0;
      dr_v <= 1'b0; dr_op <= '0; dr_ok <= 1'b0; dr_slot <= '0; dr_held <= '0; dr_total <= '0; dr_unstarted <= '0;
    end else begin
      dr_v <= 1'b0;
      case (dst)
        D_IDLE: if (d_v) begin
          qd_op <= d_op; qd_slot <= d_slot; qd_cores <= d_cores; qd_held <= d_held; qd_s <= d_s; qd_r <= d_r;
          dst <= D_EXEC;
        end
        D_EXEC: begin
          dst <= D_IDLE;
          dr_op <= qd_op; dr_ok <= 1'b0; dr_slot <= '0; dr_held <= '0;
          case (qd_op)
            OP_LAZY: begin
              dr_v <= 1'b1; dr_ok <= free_v; dr_slot <= free_i;
              if (free_v) begin valid[free_i] <= 1'b1; total <= total + TW'(qd_cores); unstarted <= unstarted + TW'(qd_cores); end
            end
            OP_ACT: begin
              dr_v <= 1'b1; dr_ok <= free_v; dr_slot <= free_i;
              if (free_v) valid[free_i] <= 1'b1;
            end
            OP_REL: begin
              dr_v <= 1'b1;
              if (valid[qd_slot]) begin
                valid[qd_slot] <= 1'b0;
                if (rd.counting) begin total <= total - TW'(rd.cores); unstarted <= unstarted - TW'(rd_open); end
              end
            end
            OP_STARTED: begin
              dr_v <= 1'b1;
              if (valid[qd_slot] && rd.kind && rd.started != {CW{1'b1}} && rd.counting && rd.cores > rd.started)
                unstarted <= unstarted - 1'b1;
            end
            OP_HELD: begin
              dr_v <= 1'b1;
              dr_held <= (valid[qd_slot] && !rd.kind) ? ((rd.cores > CW'(255)) ? 8'd255 : 8'(rd.cores)) : 8'd0;
            end
            OP_TICK: begin now <= now + 1'b1; idx <= '0; dst <= D_SCAN; end
            OP_TOTALS: begin dr_v <= 1'b1; dr_total <= total; dr_unstarted <= unstarted; end
            default: dr_v <= 1'b1;                                  // the cell port's codes, and unknown ones: nothing
          endcase
        end
        D_SCAN: begin
          if (valid[idx]) begin
            if (rd.kind && d_end[15]) begin                          // past its end: expires
              valid[idx] <= 1'b0;
              if (rd.counting) begin total <= total - TW'(rd.cores); unstarted <= unstarted - TW'(rd_open); end
            end else if (rd.kind && !rd.counting && !d_frm[15]) begin   // its effect round has come
              total <= total + TW'(rd.cores); unstarted <= unstarted + TW'(rd_open);
            end
          end
          if (idx == 6'(N_R - 1)) dst <= D_TDONE;                  // (the last slot's change lands at this edge)
          idx <= idx + 1'b1;
        end
        D_TDONE: begin
          dst <= D_IDLE; dr_v <= 1'b1; dr_op <= OP_TICK;
          dr_total <= total; dr_unstarted <= unstarted;
        end
        default: dst <= D_IDLE;
      endcase
    end
  end

  // ============================================================================================ RM-W, the cell port
  typedef enum logic [1:0] {W_IDLE, W_EXEC, W_QOR} wst_t;
  wst_t wst;
  logic [4:0]    qw_op;
  logic [3:0]    qw_word;
  logic          qw_sel;
  logic [63:0]   qw_data;
  logic [GB-1:0] qw_frame;

  assign w_rdy = (wst == W_IDLE);

  logic [6:0]   ws  [N_C];
  logic [3:0]   tgt [N_C];
  logic [2:0]   lk  [N_C];
  logic [N_C-1:0] ma, mb;
  logic           hit_ws [N_C], hit_tgt [N_C], hit_lk [N_C];   // per cell: held beyond the asker's share
  logic           any_ws, any_tgt, any_lk;
  always_comb begin
    any_ws = 1'b0; any_tgt = 1'b0; any_lk = 1'b0;
    for (int c = 0; c < N_C; c++) begin any_ws |= hit_ws[c]; any_tgt |= hit_tgt[c]; any_lk |= hit_lk[c]; end
  end
  wire wexec = (wst == W_EXEC);
  wire u_ws_up = wexec && qw_op == OP_WS_ADD,    u_ws_dn = wexec && qw_op == OP_WS_SUB;
  wire u_tg_up = wexec && qw_op == OP_TGT_ADD,   u_tg_dn = wexec && qw_op == OP_TGT_SUB;
  wire u_lk_up = wexec && qw_op == OP_LOCK_ADD,  u_lk_dn = wexec && qw_op == OP_LOCK_SUB;
  wire u_g_add = wexec && qw_op == OP_GUARD_ADD, u_g_sub = wexec && qw_op == OP_GUARD_SUB;
  wire u_query = wexec && qw_op == OP_QUERY;

  // the frames' guards, by frame number: distributed RAM with one port, read and written at the operation's frame (0 at
  // configuration, the RAM's initial value; rst_n does not clear it)
  logic [N_C-1:0] gm [N_G];
  wire  [N_C-1:0] gm_rd = gm[qw_frame];
  always_ff @(posedge clk)
    if (u_g_add || u_g_sub) gm[qw_frame] <= u_g_add ? ma : '0;

  for (genvar c = 0; c < N_C; c++) begin : g_cell
    wire ws_up = (u_ws_up || u_g_add) && ma[c];                     // GUARD_ADD: mask A, as WS_ADD
    wire ws_dn = (u_ws_dn && ma[c]) || (u_g_sub && gm_rd[c]);       // GUARD_SUB: the frame's stored guard
    always_ff @(posedge clk or negedge rst_n) begin
      if (!rst_n) begin
        ws[c] <= '0; tgt[c] <= '0; lk[c] <= '0; hit_ws[c] <= 1'b0; hit_tgt[c] <= 1'b0; hit_lk[c] <= 1'b0;
      end else begin
        if (ws_up && ws[c] != 7'h7F) ws[c] <= ws[c] + 7'd1;
        if (ws_dn && ws[c] != 7'h00) ws[c] <= ws[c] - 7'd1;
        if (ma[c]) begin
          if (u_tg_up && tgt[c] != 4'hF) tgt[c] <= tgt[c] + 4'd1;
          if (u_tg_dn && tgt[c] != 4'h0) tgt[c] <= tgt[c] - 4'd1;
          if (u_lk_up && lk[c] != 3'h7) lk[c] <= lk[c] + 3'd1;
          if (u_lk_dn && lk[c] != 3'h0) lk[c] <= lk[c] - 3'd1;
        end
        if (u_query) begin                                          // held beyond the asking frame's own share
          hit_ws[c]  <= ma[c] && (ws[c]  > 7'(mb[c]));
          hit_tgt[c] <= ma[c] && (tgt[c] > 4'(mb[c]));
          hit_lk[c]  <= ma[c] && (lk[c]  > 3'(mb[c]));
        end
      end
    end
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      wst <= W_IDLE; qw_op <= '0; qw_word <= '0; qw_sel <= 1'b0; qw_data <= '0; qw_frame <= '0;
      ma <= '0; mb <= '0;
      wr_v <= 1'b0; wr_op <= '0; wr_q_ws <= 1'b0; wr_q_tgt <= 1'b0; wr_q_lock <= 1'b0;
    end else begin
      wr_v <= 1'b0;
      case (wst)
        W_IDLE: if (w_v) begin
          qw_op <= w_op; qw_word <= w_word; qw_sel <= w_sel; qw_data <= w_data; qw_frame <= w_frame;
          wst <= W_EXEC;
        end
        W_EXEC: begin
          wst <= W_IDLE;
          wr_op <= qw_op;
          case (qw_op)
            OP_MASKW: begin
              wr_v <= 1'b1;
              for (int w = 0; w < NW; w++)
                if (qw_word == 4'(w))
                  for (int b = 0; b < 64; b++) begin
                    if (qw_sel) mb[w * 64 + b] <= qw_data[b];
                    else        ma[w * 64 + b] <= qw_data[b];
                  end
            end
            OP_QUERY: wst <= W_QOR;
            default: wr_v <= 1'b1;                                  // updates and guards (g_cell, gm); others: nothing
          endcase
        end
        W_QOR: begin
          wst <= W_IDLE; wr_v <= 1'b1; wr_op <= OP_QUERY;
          wr_q_ws <= any_ws; wr_q_tgt <= any_tgt; wr_q_lock <= any_lk;
        end
        default: wst <= W_IDLE;
      endcase
    end
  end

  // ============================================================================================ the status port
  logic [N_C-1:0] nz_ws, nz_tgt, nz_lk;                           // each counter above 0
  always_comb
    for (int c = 0; c < N_C; c++) begin nz_ws[c] = |ws[c]; nz_tgt[c] = |tgt[c]; nz_lk[c] = |lk[c]; end

  logic          s0_v, s1_v;
  logic [3:0]    s0_cv, s1_cv;
  logic [CB-1:0] s0_cell [4];
  logic [CB-7:0] s1_grp  [4];                                      // the cell's group of 64
  logic [2:0]    s1_part [4][NW];                                  // its bits at its offset, in every group
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      s0_v <= 1'b0; s1_v <= 1'b0; sr_v <= 1'b0; s0_cv <= '0; s1_cv <= '0; sr_held <= '0;
      for (int i = 0; i < 4; i++) begin
        s0_cell[i] <= '0; s1_grp[i] <= '0;
        for (int g = 0; g < NW; g++) s1_part[i][g] <= '0;
      end
    end else begin
      s0_v <= s_v; s0_cv <= s_cv;                                   // taken
      for (int i = 0; i < 4; i++) s0_cell[i] <= s_cells[i * CB +: CB];
      s1_v <= s0_v; s1_cv <= s0_cv;                                 // in each group: a 64:1 select by the low 6 bits
      for (int i = 0; i < 4; i++) begin
        s1_grp[i] <= s0_cell[i][CB-1:6];
        for (int g = 0; g < NW; g++)
          s1_part[i][g] <= {nz_lk[g * 64 + int'(s0_cell[i][5:0])], nz_tgt[g * 64 + int'(s0_cell[i][5:0])],
                            nz_ws[g * 64 + int'(s0_cell[i][5:0])]};
      end
      sr_v <= s1_v;                                                 // the group: an NW:1 select by the high bits
      for (int i = 0; i < 4; i++) sr_held[i * 3 +: 3] <= s1_cv[i] ? s1_part[i][s1_grp[i]] : 3'b000;
    end
  end
endmodule
