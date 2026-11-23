# Froq: Safe Speculative Execution on Live Quantum States for FTQC

Code and data for the paper by Yicheng Guang and Yueqi Chen (University of Colorado Boulder), arXiv:XXXX.XXXXX.

## What Froq is

A fault-tolerant quantum computer (FTQC) stalls when its program needs a decoded result that the classical decoder has
not finished. Speculative decoders such as SWIPER publish a predicted result early, but the quantum operations that
depend on it still wait for the confirmed one. Froq lets those operations go ahead on the live quantum state. Quantum
states cannot be copied or checkpointed, so a misprediction has to be repaired in place, and Froq makes that repair
safe.

The compiler gives each predicted correction a recovery contract. The contract tracks how a wrong prediction would
spread through the operations that follow and holds the recovery script that undoes it at every step. At run time
Froq reserves the workspace those scripts need before it speculates. Runtime decision tables then choose, for each
pending correction, whether to wait, to defer it while later operations run, or to apply it early. They weigh the
time each choice saves against the expected cost of recovery. On eight programs across 88 configurations, built on
SWIPER, Froq reduces quantum-side time by 9.81 %. Quantum-side time is the program's makespan: the number of syndrome
rounds the quantum processor takes to run it. The reduction is 18.34 % over the 45 configurations that allow
concurrent predictions, and it peaks at 31.1 % at code distance d = 21 (the largest surface code evaluated) with
limited decoder resources.

## Layout

| Path | What it holds | Paper |
|---|---|---|
| `froq/` | The simulator: Froq inside SWIPER-SIM. `froq/run.py` runs one simulation; `froq/configuration.py` runs a configuration in the paper's terms and holds the selector | 4 to 6 |
| `froq/contract.py`, `froq/pauli.py`, `froq/events.py`, `froq/semantics/` | Recovery contracts: the walk of a misprediction's light cone, the steps and their recovery scripts, the recovery library and its certificates, the compiler of recovery scripts | 4 |
| `froq/device.py` | The runtime in the simulator: static reservation, admission of speculative steps, splicing of recovery scripts, settlement | 5.1, 5.2 |
| the other modules in `froq/` | The decision (waiting, deferring, applying), its recurrence, the settings, and the runtime decision tables | 5.2 to 5.5 |
| `froq/data/` | Fitted prices (`prices/`: the arrival distribution, the waits, the price for delaying later decodes, the overhead price), the prior rate, decoder-latency tables, hardware limits, and the profiles built ahead of the campaign | 5.2, 5.6, 6 |
| `froq/schedules/` | Compiled programs: lattice-surgery schedules of all eight programs (the four MQT programs' compressed with xz, see below), their QASM and manifests, and the cache of compiled recovery scripts | 6 |
| `third_party/swiper/` | SWIPER-SIM, MIT licence, with two files changed (`CHANGES.md`) | 6 |
| `hw/` | RTL of the decision unit and the reservation manager, bit-exact Python models of both, Verilator testbenches | 5.5, 5.6 |
| `results/` | Per-run and per-configuration records of every experiment in the evaluation (tab-separated) | 7 |
| `evaluation/` | Scripts that regenerate the evaluation's numbers, tables and figure data from `results/` and compare them with the paper | 7 |
| `tests/` | End-to-end checks of the release | |
| `scripts/` | An example batch script for running the configurations on a cluster | |

The records in `results/`:

| File | Rows | What one row is | Paper |
|---|---:|---|---|
| `main/settings.tsv`, `main/selection.tsv` | 88 | A configuration: its decoder cores and the setting the selector picked; the mean log makespan and censored runs of every setting on the training seeds | 6, 7.1 |
| `main/runs.tsv` | 1,200 | A test run of Froq or SWIPER alone: makespan (rounds), censored flag, peak workspace (cells) | 7.1, 7.3 |
| `main/corrections.tsv`, `main/delays.tsv`, `main/recovery_price.tsv`, `main/profiles.tsv`, `main/compile_checks.tsv` | 50 to 600 | Per run or per configuration: what happened to each pending correction, where the waiting went, recovery prices against measured recovery time, profiles per table, recovery scripts checked | 5.5, 7.2, 7.3 |
| `sensitivity/runs.tsv`, `sensitivity/mispredictions.tsv` | 3,969; 3 | Runs at each stress point; misprediction counts | 7.4 |
| `supporting/runs.tsv`, `supporting/caps.tsv`, `supporting/leads.tsv` | 5,623; 34; 1,200 | Runs of the fixed rules, the simpler admission, the other prediction source and the capped workspace; how early each prediction arrived | 7.2 to 7.4 |
| `programs/programs.tsv` | 8 | A program: qubits, T gates, T depth, peak decoder demand P at each distance | 6 |
| `hardware/*.tsv`, `compile/build_times.tsv` | 3 to 20 | Synthesis results per block, latencies in cycles, script store sizing; time to build scripts and profiles | 5.6, 7.3 |
| `introduction/waits.tsv` | 384 | A run of SWIPER alone at d = 13 or 21 and a budget of 100 or 150 % (seeds 6940 to 6951): its mean wait per T gate in rounds | 1 |

Training seeds 6600 to 6609 select each configuration's setting, and test seeds 7200 to 7211 measure it.

## Install

Python 3.11 or 3.12 on Linux. From the repository root:

```
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

The RTL tests also need Verilator 5 (checked with 5.040).

## Quick start

Regenerate the evaluation's numbers, tables and figure data (about 2 s):

```
python evaluation/regenerate.py
```

This writes `evaluation/out/` and compares everything with the paper. `numbers.tsv` holds 106 printed numbers, each
next to the regenerated value, and the four files are `data/cutbars.dat`, `data/stress.dat`,
`tables/tab_programs.tex` and `tables/tab_cost.tex`. The script exits with status 0 only if all of them match.
`--arxiv DIR` compares with an unpacked arXiv source instead of the copies in `evaluation/paper/`.

Run one test seed of a configuration end to end, SWIPER alone and then Froq (about 3 s each on one core):

```
python -m froq.configuration run --program Adder-4 --distance 17 --budget 100 --system swiper --seed 7200
python -m froq.configuration run --program Adder-4 --distance 17 --budget 100 --system froq --setting concurrent/0 --seed 7200 --censor-at 2030
```

They print makespans of 2030 and 1935 rounds, the values recorded in `results/main/runs.tsv`. `--budget` is the
decoder budget in percent of the program's peak decoder demand. `--setting` is one of `wait`, `concurrent/0`,
`concurrent/d`, `concurrent/4d`, `one/0`, `one/d`, `one/4d`: concurrent predictions or one prediction at a time,
with a margin of 0, d or 4d rounds. `--censor-at` is SWIPER alone's makespan for the same seed, and a Froq run is
censored past four times it.

A whole configuration, as Section 6 describes it:

```
python -m froq.configuration select --program Adder-4 --distance 17 --budget 100 --jobs 8
python -m froq.configuration test   --program Adder-4 --distance 17 --budget 100 --jobs 8
```

`select` runs SWIPER alone and the six Froq settings on the ten training seeds (70 runs), prints each setting's mean log
makespan next to the recorded one, and applies the selector. `test` runs the paper's pick and SWIPER alone on the
twelve test seeds (24 runs) and prints every makespan next to the recorded one, then the configuration's reduction. On
8 cores each command takes about a minute for Adder-4.

To run the tests:

```
python -m unittest discover -t . -s froq      # unit tests of the simulator (about 40 s)
python -m unittest discover -s tests          # regeneration and two recorded runs (about 10 s)
```

## Full reproduction

```
python -m froq.configuration all --part 1/1 --jobs 8 --out runs
```

`all` runs `select` and then `test` for each configuration. `--part i/n` takes every n-th configuration starting with
the i-th, so n machines can each take one part. `scripts/example_batch.sh` does this with a Slurm array; replace its
placeholders first. A run whose output directory already holds a finished record is not run again, so a part that
was interrupted can simply be started again.

The schedules of Grover-5, QFT-10, QPE-10 and QPE-5 are stored compressed (`froq/schedules/mqt/*.xz`, 19 MB for
324 MB), and so are the compiled recovery scripts for Adder-8, Adder-16 and Adder-32
(`froq/schedules/snippets_sx/*.xz`, 0.2 MB for 59 MB). Every command of `froq.configuration` first unpacks them next
to the compressed files and checks each file's SHA-256 against the program's manifest or against
`froq/schedules/snippets_sx/SHA256SUMS`; `python -m froq.configuration unpack` does only that step, for use before
calling `froq.run` directly. Unpacking takes about 3 s.

By the campaign's per-run estimates, the 6,160 training runs take about 835 core-hours and the 1,200 test runs about
190 core-hours, about 1,000 core-hours in all. Every run uses one core. Grover-5 accounts for about 600 of
those core-hours. The longest single run takes about 1.7 hours and the largest needs about 14 GB of memory, so memory
limits how many runs fit on a node.

With this release, `all` runs all 88 configurations end to end, and the records of all 88 are in `results/`. One
run of QPE-5 at d = 13 takes about a minute and under 1 GB of memory; one run of Adder-8 at d = 13 takes about 5 s.

The runs behind the other experiments of Sections 7.2 to 7.4 (stress points, fixed rules, capped workspace, simpler
admission, the other prediction source) change a few simulator options; `python -m froq.run --help` lists them. Their
records are in `results/sensitivity/` and `results/supporting/`, and `evaluation/` regenerates every number from
them.

## What is not included

- The modified liblsqecc slicer that compiled the recovery scripts, and the converter that produced the schedules.
  Their outputs for all eight programs are included, so no run needs them.
- The raw output of each simulation (one JSON record per run). `results/` keeps every field the evaluation uses, and
  rerunning a configuration writes the full records again.
- The runs that fitted the prices and the prior rate, and the fitting scripts. The fitted files are in
  `froq/data/prices/` and `froq/data/`. Their fitted values are the campaign's; their provenance fields name the
  fitting runs by file name and SHA-256 and say what each fit is.
- The FPGA synthesis scripts and reports, and the RTL of the controller's other blocks. Their results are in
  `results/hardware/`.
- The data behind the claim in Section 5.6 about the event queue's depth, which `evaluation/regenerate.py` does not
  regenerate.

Code comments refer to internal design notes and earlier experiments that are not part of this release.

## RTL

`hw/rtl/` holds the two blocks whose latency the simulator charges:

- `decision_unit.sv`, the decision unit (Section 5.5). It holds the runtime decision tables for 64 profiles on each
  branch, returns an action 8 cycles after a lookup, and serves 64 corrections in at most 32 cycles on its two ports.
- `reservation_manager.sv`, the reservation manager for static reservation (Section 5.1).

Each block has a bit-exact Python model in `hw/golden/` and a testbench in `hw/tb/`. To simulate them, from `hw/`:

```
python -m tests.run_decision_unit all          # about 20 s
python -m tests.run_reservation_manager all    # about 100 s
```

Each script builds the testbench with Verilator, runs directed and random cases against the model, and prints the
cases that failed and the cycles per operation. Build files go to the system's temporary directory; set
`SE_BUILD_ROOT` to change that.

The latencies the paper uses (Section 5.6): both blocks close timing at a 3.5 ns clock on an AMD Versal V80 in
out-of-context synthesis, so a 1 µs syndrome round is 286 cycles. A dependent access to the tables takes 5 cycles,
and every simulated run charges it. A registration takes 18 cycles (6.3 % of a round). A settlement reaches its
script's first instruction in 28 cycles (9.8 % of a round), 18 of them in routed blocks and 10 estimated. The
simulator applies these limits with `--hw-limits improved`, which reads them from `froq/data/`.

## Licence

Froq's own code is licensed under the Apache License 2.0 (`LICENSE`, `NOTICE`). `third_party/swiper/` is
SWIPER-SIM and stays under its own MIT licence (`third_party/swiper/LICENSE`).

## How to cite

```
@misc{guang2026froq,
  title         = {Froq: Safe Speculative Execution on Live Quantum States for {FTQC}},
  author        = {Guang, Yicheng and Chen, Yueqi},
  year          = {2026},
  eprint        = {XXXX.XXXXX},
  archivePrefix = {arXiv}
}
```
