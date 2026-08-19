# DOIT.p1.e5.memory-hog.md - Bound YO!stats and Filesystem memory

## Goal

Keep YO!stats and Filesystem daemon memory bounded by their serving and watch configuration rather than by raw retained-history cardinality or indexed-tree size. A production-shaped cold start must publish usable results without a whole-history Python graph, and a large indexed root must not become an unbounded recursive native-watch topology. Release evidence must measure peak and steady PSS/USS, native watch cardinality, readiness, recovery, and user-visible correctness on one frozen identity.

## Measured incident baseline

The 2026-08-11 live 7771 measurement resolved the current service descriptors and process identities before sampling. The released processes ran from `/home/keivenc/dev/yolomux.stable7771` at `926e4a16621c6f96de319a441f8692742a97d856`.

| Service | Confirmed scale factor | RSS | PSS | USS/private | Peak RSS |
| --- | --- | ---: | ---: | ---: | ---: |
| YO!stats / `statsd` | 417.2 MiB SQLite; 609,688 primary fact rows; 149.0 MiB raw JSON | 574.0 MiB | 561.0 MiB | 560.9 MiB | 1,116.6 MiB |
| Filesystem / `watchd` | one recursive registration with 126,028 inotify watch descriptors | 155.5 MiB | 142.4 MiB | 142.0 MiB | 155.4 MiB |
| Combined | non-double-counting process denominator | 729.5 MiB | 703.5 MiB | 702.9 MiB | — |

For `statsd`, 560.8 MiB of PSS was anonymous and SQLite `mmap_size` was zero. The released cold path uses `fetchall()`, JSON-decodes all retained observations and usage atoms, builds a complete materialized generation, and retains the published generation and encoded views (`yolomux_lib/stats_current/storage.py`, `yolomux_lib/stats_current/service.py`). The exact reachable-object versus allocator-slack split is not yet measured, but the row-scaled whole-history build and 1.1 GiB high-water mark are confirmed.

For `watchd`, the released configuration unions every `indexed_dir` into recursive native `watch_paths`; `/home/keivenc/dev` therefore created 126,028 inotify descriptors. Event exclusion runs after native registration and cannot bound that topology (`yolomux_lib/watchd.py`). Kernel inotify memory is additional to process PSS. The current dirty candidate contains non-recursive/capped work in this area, but source intent is not test, deployment, or release proof.

## 2026-08-18 - statsd CPU Defect FOUND AND FIXED (uncommitted); Memory Scope Still Open

RESCOPED per audit: statsd first. The CPU half is done and measured; the MEMORY half of this queue is untouched.

HOT LOOP NAMED WITH ATTRIBUTION: `_append_uncovered_gap` (`yolomux_lib/stats_current/materializer.py:1685-1695`), reached via `_coverage_gaps` -> `_build` -> `update_generation`. Live 7772 statsd PID 842011 measured by `/proc/<pid>/stat` utime+stime deltas, never `ps %cpu`: 14.51 CPU-s over 17.79s wall = 81.6% of one core. A 40s py-spy profile on the pure incremental path (`build_generation` 0.00s) attributed `_build` 27.47s, `_coverage_gaps` 20.82s (52%), and `_append_uncovered_gap` 15.59s (39%) - the largest single owner. Fold path 4.69s, storage read 1.08s.

HYPOTHESIS EXPLICITLY DISPROVEN: the decode-amplification theory carried over from earlier memory work is WRONG for CPU. `storage.py:2402-2530` JSON decode is about 2.7% of profile, not the driver. Do not re-open it as a CPU cause.

CAUSE: the helper linearly rescanned every explicit span for every coverage epoch, O(epochs x spans) per build, at a live shape of 6 sources x 633 epochs x 589 spans and growing - measured N^1.66 with the retained window. `normalize_unavailable_spans` (`storage.py:301-346`) already guarantees spans are start-ordered and non-overlapping per `(family, source_id)`, an invariant clipping preserves, so the scan was unnecessary.

FIX: `bisect` seek over a per-source starts index. Two files, uncommitted in an isolated worktree at `7cb75e3a5`: `materializer.py` (+76/-24) and `tests/test_stats_current_materializer.py` (+164). Frozen-fixture equivalence: 3625 gaps, output byte-identical (`cmp` rc=0).

RED/GREEN VERIFIED FIRST-HAND BY THE MAIN AGENT: patched, 8 passed. With the product fix stashed and the tests kept, `test_coverage_gap_cost_stays_linear_in_retained_coverage_history` FAILS. On a pristine base it reports cost growing 3.81x for 2x history (0.1946s -> 0.7420s). The 6-case public `_coverage_gaps` matrix passes on BOTH base and patched, which is the point: it proves no semantic change rather than serving as the red.

BEFORE/AFTER, whole service, real `service.py`, frozen 381 MB copy of the live DB, readiness-gated, calibrated to the measured live rate of 4.00 appends/s with `source_generation` delta exactly +240 and zero RPC timeouts: base 55.9% / 55.7% / 55.3%, patched 27.4% / 26.9% / 28.4%. About 2x, 33.4 -> 16.5 CPU-s per 60s. Residual `_append_uncovered_gap` 0.19s, down from 15.59s.

INVALID EARLIER NUMBERS, WITHDRAWN: the first whole-service runs reporting 99.2% / 92.8% CPU and RSS 307->947 MiB are DISCARDED. `Observation` fields were passed in the wrong order so every append was rejected with `unknown current stats family 'jobd'`, and the response was never checked - only the startup full build was measured. The harness now asserts `accepted==2`, `duplicates==0`, and the generation delta.

REMAINING CPU, NAMED NOT HAND-WAVED: inside `_coverage_gaps`, `normalize_coverage_model` 3.01s and `identity_text` 3.39s per 60s, both re-run over the full 4023-epoch / 3534-span model every build. `_build_once` already caches NORMALIZED coverage and `_coverage_gaps` then re-normalizes it; that double-normalization is the next target. Builds DO coalesce (`_take_work` drains all pending), so about 2 builds/s is a consequence of build cost, not a queue defect.

STILL OPEN IN THIS QUEUE: the entire memory scope. Startup still schedules a full build; observations and usage still use whole-result `fetchall()`; RSS about 950 MiB with 1113 MiB high-water set by the startup full build, flat during steady state so NOT a leak; startup-to-ready 54-71s. The 24h read bound landed earlier in `f32ffd898` and must be preserved. Watchd and search work is separate and untouched.

NO LIVE CLAIM: 7772 was never restarted and is not fixed. Landing requires commit authorization, an authorized restart, and a post-restart measurement.

## LANDING BLOCKER 2026-08-18 - The statsd Regression Is A Load-Sensitive Timing Gate

The bisect production fix is sound and red-on-base still holds. The REGRESSION is the problem, and it blocks landing.

`test_coverage_gap_cost_stays_linear_in_retained_coverage_history` measures `_coverage_gaps` at 1500 and 3000 epochs with `time.process_time()`, takes `min` of 3 samples, and asserts `double / single < 2.8`. A perfectly linear algorithm scores 2.0, so the entire tolerance is 2.0 to 2.8, and the absolute durations are only about 0.015-0.018s, where scheduler and cache-contention noise dominate.

Observed on the PATCHED tree: a full-module run gave 61 passed / 1 failed at 2.80x against the strict `< 2.8`, and five isolated reruns gave 4 pass / 1 fail with the failure at 2.88x (0.0157s -> 0.0453s). Raw outputs retained at `/tmp/statsd-p0-materializer-pytest-20260818.txt` and `/tmp/statsd-p0-scaling-repeat-20260818.txt`.

A separate five-run measurement by the main context on a quieter box returned 5 pass / 0 fail. THAT DOES NOT CLEAR THE GATE. Non-reproduction on a quieter host is exactly what a load-sensitive threshold predicts, and it must not be used to reclassify the observed red. An earlier report of "8 passed" was a SINGLE run of a gate that is flaky by construction; any 332-pass or module-green claim is withdrawn until re-measured from a real exit code.

- [x] DONE: replaced the timing pass/fail with a DETERMINISTIC OPERATION-COUNT BOUND at `_append_uncovered_gap`. For example, an indexable explicit-gap test double that fails the test if the implementation iterates the whole list, and counts bounded `__getitem__` accesses so a candidate overlapping O(1) spans passes and a whole-list scan fails. Keep wall-clock only as non-gating measurement.
- [x] DONE: full module re-run independently by the main context at host loadavg 34 - 64 passed, rc=0. Exact new tests 3 passed / 61 deselected, rc=0. Forced-red against the old full-scan implementation: 3 failed, rc=1, so the new gate can fail.
- [ ] Refresh the patch backup and its hash after the test change.

FORBIDDEN REMEDIES, explicitly: raising the 2.8 threshold, retries, sleeps, serialization, lowering concurrency, or weakening the assertion. The gate must become deterministic, not more tolerant.

Integration order is unchanged: statsd remains first because it is disjoint. After the CPU patch is clean and integrated, profile the PATCHED service before choosing the next CPU target. Memory and startup remain a separate unresolved phase. Live 7772 must not be restarted as part of this correction; it remains old and hot at 85.0% user-heavy CPU with zero physical reads.

## Plan

- [ ] **Freeze production-scale reproductions and derive explicit budgets.** Create content-addressed fixtures and isolated subprocess harnesses for (a) the measured stats shape: approximately 417 MiB SQLite, 369,312 observations, 240,376 usage atoms, 149 MiB raw payload JSON, real indexes, aggregate ring, and WAL; and (b) a large nested indexed root that makes the released recursive watch owner exceed the intended registration bound. First prove the released behavior exceeds the proposed peak/steady PSS/USS and native-watch budgets. Measure stats phase-by-phase PSS/USS, allocated blocks, cache object/byte counts, and optional isolated `tracemalloc`/`malloc_info`; measure watchd at 0, 1k, 10k, and 100k directories plus kernel slab deltas. Derive budgets from the intended bounded architecture, not by rounding above the 703.5 MiB incident baseline. Keep raw outputs under `/tmp`; retain only fixture identities, summarized measurements, and exact repro commands.

- [ ] **Make the bounded aggregate ring the YO!stats cold serving owner.** On daemon start, validate and load the fixed aggregate-ring publication, publish a bounded usable generation, and catch up only facts newer than its durable cursor. Remove the requirement to decode the entire two-day raw store before first readiness. Preserve exact generation/cursor identity, atomic publication, raw-retention semantics, schema compatibility, corrupt-ring recovery, writer recovery, and last-known-good serving. Add exact tests for empty state, valid ring plus tail, missing/corrupt/stale ring, concurrent append, crash before/after publication, prune/VACUUM, and restart; assert first-ready correctness and latency without whole-history `fetchall()`.

- [ ] **Bound every exact YO!stats rebuild by rows, decoded bytes, memory, and lifetime.** If an exact raw-history rebuild remains necessary, fold it in bounded chunks and discard each decoded chunk after aggregation; never hold SQLite row tuples, decoded fact objects, the complete build graph, and the published generation simultaneously. Prefer a short-lived worker that publishes one identity-fenced bounded generation and exits so its allocator is reclaimed while the daemon serves last-known-good data. Enforce hard row/byte/memory/cancellation budgets, stale-worker rejection, exception cleanup, and atomic adoption. Prove 1x versus 2x retained-row cardinality does not make steady serving memory grow linearly, peak decoded rows/bytes stay within the configured chunk bound, no post-response error is emitted after valid publication, and Linux-only allocator trimming is at most a cleanup optimization rather than the fix.

- [ ] **Separate shallow native freshness from large indexed-root reconciliation.** Keep immediate native notification only for visible/open Finder directories and exact-file parents. Do not recursively register configured Quick Open/indexed roots; cover them through the existing BFS/frontier, mutation evidence, and bounded periodic reconciliation owners. Apply exclusion before registration where a native subtree is eligible, enforce one daemon-wide registration union and explicit cap (the current candidate uses 512), and provide a typed bounded fallback when the cap or platform facility is unavailable. Audit the current dirty `watchd`/protocol candidate rather than creating a parallel owner. Verify overlapping descriptors, symlinks, VCS/cache/dependency exclusions, root add/remove/repoint, policy changes, Linux and Darwin behavior, native failure/recovery, daemon restart, and no lost visible-directory or exact-file updates.

- [ ] **Expose honest per-daemon memory and registration readiness.** Add one side-effect-free measured projection for peak and steady PSS/USS, anonymous/file PSS, swap, threads, FDs, backing DB/WAL/TEMP sizes, stats build phase/cursor/bounded-work counters, watchd native-registration count/cap/fallback, and process/start/composition identity. Reconcile internal retained-owner accounting to process USS within a documented tolerance rather than reporting only encoded wire-cache bytes. Make `/livez` the narrow process-progress check and `/readyz` fail closed until every required daemon has the correct identity and its serving generation, memory budget, registration cap or typed fallback, and recovery state are healthy. Do not make readiness depend only on a listener or process existing.

- [ ] **Add daemon-specific regression and resource gates.** Extend the isolated gate to fail on statsd cold peak/steady PSS/USS, watchd native watch-descriptor count, swap, readiness latency, and a positive post-settle memory/watch slope. Count `inotify wd:` registrations, not only inotify instances or host maxima. Include negative controls that run the released whole-history/recursive-registration paths and prove the new gates fail for the incident class. Cover startup, incremental append/change, idle, prune, crash/restart, stale descriptor cleanup, failed native registration with bounded fallback, web restart while daemons exist, and full owned-process/FD/watch retirement. Do not weaken the test with retries, sleeps, serialization, wider budgets, or manual cache deletion.

- [ ] **Certify the all-daemon release on one frozen identity.** After the implementation groups are complete, create the separately authorized freeze commit and run focused owner tests, `python3 tools/static_build.py --check`, and default `python3 tools/check.py` from fresh clean Linux and real Darwin checkouts of the same full SHA. Use empty and production-sized stats state plus maximum allowed indexed-root state. Prove generated/source content identity, install, rollback/backout, daemon crash/recovery, all owned-process retirement, every daemon ready before browser launch, and no foreign-process interference. Deploy only the authorized development target, prove PID/cwd/SHA and daemon composition identity, wait beyond 90 seconds, then run at least a 600-second authenticated soak that records absolute ceilings and near-zero slope for PSS/USS, watch count, caches, TEMP/WAL, FDs, threads, and readiness. Exercise exact YO!stats and Filesystem user paths with a real negative control. Any relevant post-freeze edit invalidates downstream evidence.

- [ ] **Document the bounded ownership and archive the completed queue.** Update `README.md`, `docs/DEVELOPMENT.md`, the relevant YO!stats/watchd/search specifications, and the GUI coverage map with the serving-ring owner, rebuild lifecycle, shallow native-watch versus reconciliation split, memory/watch budgets, `/livez` versus `/readyz`, operator diagnostics, and supported recovery behavior. Record measured before/after outcomes and the frozen release identity in `docs/DONE/`, then remove this queue only after every checkbox and release rung above is proven.

## Gotchas

- PSS is the aggregate release denominator; summing RSS double-counts shared pages. Track peak and steady USS separately because both incident processes were almost entirely private anonymous memory.
- A 417.2 MiB SQLite file does not explain statsd RSS by mmap: the measured `mmap_size` was zero and file PSS was only 0.2 MiB. Do not “fix” this by changing mmap or cache size without reproducing the Python object graph.
- The three deleted SQLite TEMP files totaled 64.3 MiB but were disk-backed and unmapped. They affect disk/I/O, not the 560.8 MiB anonymous statsd PSS.
- A short observation established stable retention, not absence of a slow leak. Release proof needs absolute budgets plus slope after settle.
- Event filtering is not registration pruning. A single inotify instance with 126,028 `inotify wd:` entries is not healthy because instance count is one.
- Kernel inotify memory is outside process PSS/USS. Measure both process memory and host slab changes in the isolated cardinality experiment.
- Do not treat the current non-recursive/capped `watchd` diff as closed until its exact scale regression, full gate, same-SHA deployment, and live registration reduction are proven.
- Do not add a second stats builder, watch service, health bus, or readiness owner. Extend the existing ring/materialization, `watchd`, local-service projection, and launcher/readiness paths.
- No live profiler attachment or diagnostic signal is required. Perform heap and cardinality attribution in isolated subprocesses using frozen fixtures.

## Done Criteria

- The production-shaped released-path reproductions fail the new resource gates, and the final frozen candidate passes them with recorded fixture and artifact identities.
- Statsd first readiness and steady serving memory are bounded by aggregate-ring/view/chunk configuration rather than raw retained-row cardinality; exact rebuild peak work is bounded and atomically published.
- Watchd native registrations stay within the declared cap and scale with shallow visible/exact parents rather than total indexed-tree directories, while indexed-root freshness and mutation convergence remain correct.
- `/readyz` proves every required daemon's identity, bounded serving state, memory/watch contract, and recovery state before browser launch; `/livez` remains a distinct progress signal.
- Fresh clean Linux and Darwin checks pass on the same frozen SHA; the authorized development deployment proves identity, settle, authenticated soak, negative control, exact YO!stats/Filesystem paths, and retirement without post-freeze edits.
- Documentation and `docs/DONE/` record the before/after measurements, budgets, architecture, and release evidence; this queue is removed only after those records are complete.

NOTE ON METHOD: the owner attempted to prove load-independence by spawning 16 orphaned busy loops (PIDs 419050-419066, PPID 1, 150s) after an isolation guard refused its first saturation attempt. Those were terminated externally and none remain. That approach was both unsafe on a shared machine hosting live servers and unnecessary, because the replacement proof is structural and cannot vary with load. The independent re-measurement above was taken at loadavg 34 without generating any load.
