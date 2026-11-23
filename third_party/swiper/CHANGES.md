# SWIPER-SIM in this repository

This directory is a modified copy of SWIPER-SIM, the simulator of the speculative window decoder SWIPER
(https://github.com/jviszlai/swiper, commit 1c00e097cb7f6d6e82bd88dfb30659f8163e0e33, MIT licence, kept in `LICENSE`).
Only `swiper/` and the top-level files are copied; the upstream benchmarks and notebooks are not.

Froq's simulator (`froq/`) imports this copy and subclasses its device manager. Two upstream files are changed, both
to make long runs faster without changing their results. `froq_changes.diff` is the full patch.

- `swiper/decoder_manager.py`: the set of seen descendants of an instruction is found from per-instruction lists of
  seen successors, not by walking every successor. Edges that Froq's device adds after the instruction graph is built
  (recovery splices) are recorded through a new `note_edge` method. With the environment variable `E2E_SCAN_CHECK`
  set, every query is also answered by the original walk and the two answers are compared.
- `swiper/device_manager.py`: the set of active patches at the start of a round is copied with `set(...)` instead of
  `copy.deepcopy` (the patches are immutable coordinate tuples, so the copy is the same).

Line endings are LF, as in the upstream repository.
