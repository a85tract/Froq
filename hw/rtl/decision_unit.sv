// se_w2_table: the decision unit, which holds the runtime decision tables (Section 5.5 of the paper). The golden
// model is hw/golden/w2_table.py.
//
// The table: 64 slots x 6 c-states x 3 load bins x 8 age bins = 9,216 rows of 17 lanes, 4 bits a lane, row
// r = 8(18k + 3c + l) + j, in nine depth banks of 1,024 x 72 bits (68 used), each two BRAM36 in true-dual-port mode, so
// that the two lookup ports read independently: port A serves lookup port 0 and the row writes, port B lookup port 1.
// A lane's bits [2:0] are the slot's β = 0 half (codes 0 AWC, 1 ACW, 2 WAC, 3 WCA, 4 CAW, 5 CWA, 6 AWC, 7 ACW, in
// ADVANCE A, WAIT W, COMMIT C; under a jump ADVANCE stays only under 0 and 1) and bit 3 its β = 1 half (0 AW, 1 WA; no
// jump). Each half of a slot has 7 age-bin edges and a fence, in distributed RAM.
//
// A value (64): its slot, c-state (a prediction's applies from the next round), its age e (10 bits, saturating at 1,023,
// so it never wraps however long the value lives), β, its mask and its jump vector (lane a's block passes a lane of the
// gap mask), in distributed RAM and registers. The control port takes one operation a cycle, applied at the clock edge:
// ROW and SLOT at program load (no lookup in flight), REG, REB (the rebase), PRED, END and RND (a round's start: every
// age one up, saturating; the load bin; the pending c-states).
//
// A lookup (one per carrying value per round) reads one row at the value's actual age bin and scans it from the cut:
// lanes outside the mask are passed; at a lane of the mask ADVANCE is eligible if the next lane of the mask lies within
// min(adm, cap, fence) and the jump rule allows it, COMMIT only at the first lane at or after the cut (β = 0, cok), WAIT
// always; the scan stops at the first lane whose first eligible action is not ADVANCE (r, its action) and, with adm
// ignored, at r0. jr: r's action would be ADVANCE if its jump were allowed. A lookup sees every control operation
// presented in or before the cycle it is presented in.
//
// Each lookup port takes one lookup a cycle. A lookup is registered (S0); reads the value and its slot half (S1);
// forms its row base and limits (S2), its age bin, row address and lane vectors (S3); reads the row (S4, and the BRAM36's
// output register, S5); decodes its 17 lanes (S6); and encodes r and r0 (S7).
module se_w2_table (
  input  logic          clk,
  input  logic          rst_n,
  // the control port
  input  logic          c_v,
  input  logic [2:0]    c_op,                // 0 ROW, 1 SLOT, 2 REG, 3 REB, 4 PRED, 5 END, 6 RND
  input  logic [13:0]   c_row,               // ROW: the row
  input  logic [67:0]   c_data,              // ROW: its 17 lanes, lane 0 low
  input  logic [5:0]    c_k,                 // SLOT, REG, REB: the slot
  input  logic          c_half,              // SLOT: the half (β)
  input  logic [69:0]   c_edges,             // SLOT: 7 age-bin edges of 10 bits, the first low
  input  logic [4:0]    c_fence,             // SLOT: the half's fence
  input  logic [5:0]    c_val,               // REG, REB, PRED, END: the value
  input  logic [2:0]    c_c,                 // REG, PRED: the c-state (0 to 5)
  input  logic [9:0]    c_e0,                // REG: its age, the rounds since its producer ended (saturated at 1,023)
  input  logic [16:0]   c_mask,              // REG, REB: the boundaries it can stand at
  input  logic [16:0]   c_gap,               // REG, REB: the statically feasible boundaries the mask lacks
  input  logic [1:0]    c_lb,                // RND: the new round's load bin (0 to 2)
  // the two lookup ports, port i in bits [i]
  input  logic [1:0]    q_v,
  input  logic [11:0]   q_val,               // 6 bits a port: the value
  input  logic [9:0]    q_cut,               // 5 bits a port: the cut
  input  logic [9:0]    q_adm,               // D's admissible run: the furthest boundary admissible now
  input  logic [9:0]    q_cap,               // the frame's cap
  input  logic [1:0]    q_cok,               // COMMIT's run-time checks passed
  output logic [1:0]    a_v,
  output logic [11:0]   a_val,
  output logic [9:0]    a_r,
  output logic [3:0]    a_act,               // 2 bits a port: 0 WAIT, 1 COMMIT, 3 error
  output logic [9:0]    a_ro,
  output logic [1:0]    a_jr
);
  localparam logic [2:0] OP_ROW = 3'd0, OP_SLOT = 3'd1, OP_REG = 3'd2, OP_REB = 3'd3, OP_PRED = 3'd4, OP_END = 3'd5,
                         OP_RND = 3'd6;
  localparam int NV = 64, NB = 9;

  function automatic logic [16:0] jumps(input logic [16:0] m, input logic [16:0] g);   // a gap lane before the next
    logic [16:0] out; logic done;                                                       // lane of the mask
    out = '0;
    for (int a = 0; a < 17; a++) begin
      done = 1'b0;
      for (int b = a + 1; b < 17; b++)
        if (!done) begin
          if (m[b]) done = 1'b1;
          else if (g[b]) begin out[a] = 1'b1; done = 1'b1; end
        end
    end
    return out;
  endfunction

  function automatic logic [1:0] lane_act(input logic [3:0] f, input logic beta, input logic x, input logic jm,
                                          input logic cm);                              // 0 W, 1 C, 2 A
    logic a_ok;
    if (!beta) begin
      a_ok = x && (!jm || f[2:0] == 3'd0 || f[2:0] == 3'd1);
      case (f[2:0])
        3'd0, 3'd6: lane_act = a_ok ? 2'd2 : 2'd0;                                      // A W C
        3'd1, 3'd7: lane_act = a_ok ? 2'd2 : (cm ? 2'd1 : 2'd0);                        // A C W
        3'd4:       lane_act = cm ? 2'd1 : (a_ok ? 2'd2 : 2'd0);                        // C A W
        3'd5:       lane_act = cm ? 2'd1 : 2'd0;                                        // C W A
        default:    lane_act = 2'd0;                                                    // 2 W A C, 3 W C A
      endcase
    end else
      lane_act = (!f[3] && x && !jm) ? 2'd2 : 2'd0;                                    // 0 A W, 1 W A
  endfunction

  function automatic logic [4:0] bin17(input logic [16:0] oh);                        // a one-hot lane's number
    bin17 = '0;
    for (int a = 0; a < 17; a++) if (oh[a]) bin17 = bin17 | 5'(a);
  endfunction

  // ============================================================================================ the control port
  wire w_row = c_v && c_op == OP_ROW, w_slot = c_v && c_op == OP_SLOT, w_reg = c_v && c_op == OP_REG;
  wire w_reb = c_v && c_op == OP_REB, w_pred = c_v && c_op == OP_PRED, w_end = c_v && c_op == OP_END;
  wire w_rnd = c_v && c_op == OP_RND;

  logic [40:0] vst [NV];                                           // {slot 6, beta 1, mask 17, jumps 17}
  logic [74:0] sd  [2 * NV];                                       // per slot half {edges 70, fence 5}, at {half, k}
  always_ff @(posedge clk) if (w_reg || w_reb) vst[c_val] <= {c_k, w_reb, c_mask, jumps(c_mask, c_gap)};
  always_ff @(posedge clk) if (w_slot) sd[{c_half, c_k}] <= {c_edges, c_fence};

  logic [NV-1:0] vld, vcpv;
  logic [2:0]    vc [NV], vcp [NV];
  logic [9:0]    ve [NV];                                         // the ages, saturating
  logic [1:0]    lb;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) begin
      vld <= '0; vcpv <= '0; lb <= '0;
    end else begin
      if (w_reg) begin vld[c_val] <= 1'b1; vcpv[c_val] <= 1'b0; end
      if (w_end) vld[c_val] <= 1'b0;
      if (w_pred) vcpv[c_val] <= 1'b1;
      if (w_rnd) begin lb <= c_lb; vcpv <= '0; end
    end
  always_ff @(posedge clk) begin
    if (w_reg) begin vc[c_val] <= c_c; ve[c_val] <= c_e0; end
    if (w_pred) vcp[c_val] <= c_c;
    if (w_rnd)
      for (int v = 0; v < NV; v++) begin
        if (vcpv[v]) vc[v] <= vcp[v];
        if (ve[v] != 10'd1023) ve[v] <= ve[v] + 10'd1;
      end
  end

  // ============================================================================================ the table
  logic [19:0]  offs;                                              // the two ports' BRAM addresses
  logic [2 * NB * 72 - 1:0] bout;                                  // every bank's two registered outputs
  wire  [9:0]   pa_addr = w_row ? c_row[9:0] : offs[9:0];
  for (genvar b = 0; b < NB; b++) begin : g_bank
    (* ram_style = "block" *) logic [71:0] mem [1024];
    logic [71:0] qa, qa_r, qb, qb_r;
    always_ff @(posedge clk) begin
      if (w_row && c_row[13:10] == 4'(b)) mem[pa_addr] <= {4'b0, c_data};
      qa <= mem[pa_addr];
      qa_r <= qa;                                                  // the BRAM36's output register
    end
    always_ff @(posedge clk) begin
      qb <= mem[offs[19:10]];
      qb_r <= qb;
    end
    assign bout[b * 72 +: 72] = qa_r;
    assign bout[(NB + b) * 72 +: 72] = qb_r;
  end

  // ============================================================================================ the lookup ports
  for (genvar i = 0; i < 2; i++) begin : g_port
    // S0: the request
    logic s0_v, s0_cok; logic [5:0] s0_val; logic [4:0] s0_cut, s0_adm, s0_cap;
    // S1: the value, its slot half, its age and the load bin
    logic s1_v, s1_cok, s1_vld, s1_beta; logic [5:0] s1_val, s1_k; logic [4:0] s1_cut, s1_adm, s1_cap, s1_fence;
    logic [2:0] s1_c; logic [9:0] s1_e; logic [1:0] s1_lb; logic [16:0] s1_mask, s1_jmp; logic [69:0] s1_edges;
    // S2: the row base, the limits
    logic s2_v, s2_cok, s2_vld, s2_beta; logic [5:0] s2_val; logic [4:0] s2_cut, s2_lim, s2_limo; logic [9:0] s2_e;
    logic [10:0] s2_base; logic [16:0] s2_mask, s2_jmp; logic [69:0] s2_edges;
    // S3 to S5: the row's address, the lane vectors
    logic s3_v, s4_v, s5_v; logic [5:0] s3_val, s4_val, s5_val; logic s3_err, s4_err, s5_err, s3_beta, s4_beta, s5_beta;
    logic [3:0] s3_bank, s4_bank, s5_bank; logic [9:0] s3_off;
    logic [16:0] s3_inm, s4_inm, s5_inm, s3_cm, s4_cm, s5_cm, s3_adv, s4_adv, s5_adv, s3_advo, s4_advo, s5_advo;
    logic [16:0] s3_jmp, s4_jmp, s5_jmp;
    // S6: the decoded lanes
    logic s6_v, s6_err; logic [5:0] s6_val; logic [16:0] s6_inm, s6_a, s6_ao, s6_c, s6_nja;
    // S7: the result
    logic o_v, o_jr; logic [5:0] o_val; logic [4:0] o_r, o_ro; logic [1:0] o_act;
    assign a_v[i] = o_v; assign a_val[i * 6 +: 6] = o_val; assign a_r[i * 5 +: 5] = o_r; assign a_ro[i * 5 +: 5] = o_ro;
    assign a_act[i * 2 +: 2] = o_act; assign a_jr[i] = o_jr;

    always_ff @(posedge clk or negedge rst_n)
      if (!rst_n) begin
        s0_v <= 1'b0; s1_v <= 1'b0; s2_v <= 1'b0; s3_v <= 1'b0; s4_v <= 1'b0; s5_v <= 1'b0; s6_v <= 1'b0;
        o_v <= 1'b0;
      end else begin
        s0_v <= q_v[i]; s1_v <= s0_v; s2_v <= s1_v; s3_v <= s2_v; s4_v <= s3_v; s5_v <= s4_v; s6_v <= s5_v;
        o_v <= s6_v;
      end

    // S0
    always_ff @(posedge clk) begin
      s0_val <= q_val[i * 6 +: 6]; s0_cut <= q_cut[i * 5 +: 5]; s0_adm <= q_adm[i * 5 +: 5]; s0_cap <= q_cap[i * 5 +: 5];
      s0_cok <= q_cok[i];
    end

    // S1: the value's state as of this cycle (every control operation through the request's cycle)
    wire [40:0] st = vst[s0_val];
    wire [74:0] sh = sd[{st[34], st[40:35]}];
    always_ff @(posedge clk) begin
      s1_val <= s0_val; s1_cut <= s0_cut; s1_adm <= s0_adm; s1_cap <= s0_cap; s1_cok <= s0_cok;
      s1_vld <= vld[s0_val]; s1_k <= st[40:35]; s1_beta <= st[34]; s1_mask <= st[33:17]; s1_jmp <= st[16:0];
      s1_c <= vc[s0_val]; s1_e <= ve[s0_val]; s1_lb <= lb;
      s1_edges <= sh[74:5]; s1_fence <= sh[4:0];
    end

    // S2
    wire [4:0]  m_cf = (s1_cap < s1_fence) ? s1_cap : s1_fence;
    always_ff @(posedge clk) begin
      s2_val <= s1_val; s2_cut <= s1_cut; s2_cok <= s1_cok; s2_vld <= s1_vld; s2_beta <= s1_beta;
      s2_mask <= s1_mask; s2_jmp <= s1_jmp; s2_edges <= s1_edges;
      s2_e <= s1_e;
      s2_base <= {s1_k, 4'b0} + {4'b0, s1_k, 1'b0} + {7'b0, s1_c, 1'b0} + {8'b0, s1_c} + {9'b0, s1_lb};   // 18k + 3c + l
      s2_limo <= m_cf;
      s2_lim <= (s1_adm < m_cf) ? s1_adm : m_cf;
    end

    // S3: the age bin and the row's address; the lanes at or after the cut, the first of them, ADVANCE's limit tests
    logic [2:0] j;
    always_comb begin
      j = '0;
      for (int x = 0; x < 7; x++) j = j + 3'(s2_edges[x * 10 +: 10] <= s2_e);
    end
    wire [13:0] row = {s2_base, j};
    wire [16:0] ge_cut = (s2_cut > 5'd16) ? 17'd0 : ~((17'd1 << s2_cut) - 17'd1);
    wire [16:0] inm = s2_mask & ge_cut;
    wire [16:0] first = inm & (~inm + 17'd1);
    wire [16:0] le_lim  = (s2_lim  >= 5'd16) ? '1 : ((17'd1 << (s2_lim  + 5'd1)) - 17'd1);
    wire [16:0] le_limo = (s2_limo >= 5'd16) ? '1 : ((17'd1 << (s2_limo + 5'd1)) - 17'd1);
    logic [16:0] adv, advo;
    always_comb
      for (int a = 0; a < 17; a++) begin
        adv[a]  = |((s2_mask & le_lim)  & ~((17'd2 << a) - 17'd1));   // a lane of the mask after a, within the limit
        advo[a] = |((s2_mask & le_limo) & ~((17'd2 << a) - 17'd1));
      end
    always_ff @(posedge clk) begin
      s3_val <= s2_val; s3_beta <= s2_beta; s3_bank <= row[13:10]; s3_off <= row[9:0];
      s3_err <= !s2_vld || inm == '0;
      s3_inm <= inm; s3_cm <= (s2_cok && !s2_beta) ? first : '0; s3_adv <= adv; s3_advo <= advo; s3_jmp <= s2_jmp;
    end
    assign offs[i * 10 +: 10] = s3_off;

    // S4 and S5: the row's read and the BRAM36's output register
    always_ff @(posedge clk) begin
      s4_val <= s3_val; s4_err <= s3_err; s4_beta <= s3_beta; s4_bank <= s3_bank;
      s4_inm <= s3_inm; s4_cm <= s3_cm; s4_adv <= s3_adv; s4_advo <= s3_advo; s4_jmp <= s3_jmp;
      s5_val <= s4_val; s5_err <= s4_err; s5_beta <= s4_beta; s5_bank <= s4_bank;
      s5_inm <= s4_inm; s5_cm <= s4_cm; s5_adv <= s4_adv; s5_advo <= s4_advo; s5_jmp <= s4_jmp;
    end

    // S6: the bank, and each lane's first eligible action (with the limit, without D's limit, with the jump allowed)
    logic [67:0] rw;
    always_comb begin
      rw = '0;
      for (int b = 0; b < NB; b++) if (s5_bank == 4'(b)) rw = bout[(i * NB + b) * 72 +: 68];
    end
    always_ff @(posedge clk) begin
      s6_val <= s5_val; s6_err <= s5_err; s6_inm <= s5_inm;
      for (int a = 0; a < 17; a++) begin
        s6_a[a]   <= lane_act(rw[a * 4 +: 4], s5_beta, s5_adv[a],  s5_jmp[a], s5_cm[a]) == 2'd2;
        s6_ao[a]  <= lane_act(rw[a * 4 +: 4], s5_beta, s5_advo[a], s5_jmp[a], s5_cm[a]) == 2'd2;
        s6_c[a]   <= lane_act(rw[a * 4 +: 4], s5_beta, s5_adv[a],  s5_jmp[a], s5_cm[a]) == 2'd1;
        s6_nja[a] <= lane_act(rw[a * 4 +: 4], s5_beta, s5_adv[a],  1'b0,      s5_cm[a]) == 2'd2;
      end
    end

    // S7: the first lane of the mask that does not ADVANCE, with and without D's limit
    wire [16:0] stop = s6_inm & ~s6_a, stopo = s6_inm & ~s6_ao;
    wire [16:0] sf = stop & (~stop + 17'd1), sfo = stopo & (~stopo + 17'd1);
    always_ff @(posedge clk) begin
      o_val <= s6_val;
      o_r   <= s6_err ? 5'd0 : bin17(sf);
      o_ro  <= s6_err ? 5'd0 : bin17(sfo);
      o_act <= s6_err ? 2'd3 : ((sf & s6_c) != '0 ? 2'd1 : 2'd0);
      o_jr  <= !s6_err && (sf & s6_nja) != '0;
    end
  end
endmodule
