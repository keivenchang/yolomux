# DOIT.p1.e2.background-owner-live-fleet-acceptance.md - Verify Shared Background Ownership On 7770-7773

## Goal

Prove the shipped single-owner background indexing and cache model on the real Linux fleet without confusing isolated fixture evidence with multi-port acceptance.

## 2026-08-18 Measured Findings - Requirements Retained, One Decision Needed

Nothing below is deleted. The original Plan and Done Criteria remain in force, including explicit mutation permission, per-port pre-state capture, the same-SHA requirement, exact multi-port cache writes, and responsive authenticated UI on every requested port.

WHAT WAS DRIVEN AND PROVEN (partial, under explicit user authorization to drive 7770-7773):

- Per-port pre-state was captured before mutation: PID, CWD, HEAD, state root, and served bundle for 7770, 7771, 7772, and 7773.
- Same-SHA achieved for 7770, 7771, and 7772 at `7cb75e3a5`. 7773 was deliberately EXCLUDED because its worktree is on `dev/0.7.8-7773`, 4 commits ahead with 20 dirty files of unrelated live work; the queue requires unrelated ports and state remain unchanged, so aligning it was refused.
- Exactly one background owner in the durable root, holding all five roles: `search-index`, `session-files`, `stats-sampler`, `tabber-activity`, `watch-roots`. Owner counters `takeover_success=1`, `takeover_failed=0`, `owner_released=0`, `follower_stale_reads=0`.
- Fleet topology restored afterwards; all four ports serve `/healthz` 200 with unauthenticated `/api/ping` 401.

BLOCKING TOPOLOGY FINDING REQUIRING A USER DECISION:

Measured roots: 7770 runs with no `YOLOMUX_ROOT` override on the durable root, while 7771, 7772, and 7773 each run on a private ephemeral root under `/tmp/y1776734304/p777x` with its own background-owner directory. `app.py:start_background_owner` gives a private-root process a `DisabledBackgroundOwner` that bypasses same-root election by design. A driven owner-loss test confirmed the consequence rather than refuting it: after SIGTERM to the 7770 owner the heartbeat aged to 28.0s against a 3.0s unresponsive threshold and the live generation count fell to 0 for 18s while 7771 and 7772 stayed healthy.

Therefore "7771 takeover after restart", "multi-port Tabber/Finder cache writes", and "shared-root Quick Open/search indexing" cannot be satisfied against this fleet as deployed. Declaring them not-applicable would MATERIALLY WEAKEN this acceptance, so it must not be done unilaterally. THE USER MUST DECIDE between: (a) stand up a deliberately shared-root multi-port fixture and run the original criteria there unchanged, or (b) accept durable-root-plus-private-dev-roots as the contract and record precisely which original criteria are being retired and why. Until that decision is recorded, the affected boxes stay open.

TWO MEASUREMENT DEFECTS TO AVOID REPEATING:

- Follower worker-thread absence cannot be shown by thread name: every thread on every fleet process reports `comm=python3`, so such a check is incapable of failing and is not evidence. Use a per-process owner-claim query, or add real thread naming first.
- Authenticated UI responsiveness must be measured on a qualifying host. The 2026-08-18 attempt ran at load 11-17 on 32 CPUs and is contaminated; record host load with every sample and state plainly whether it qualifies.

LATENT ISSUE, RECORDED NOT FIXED: `background_owner_priority` documents "prefer one configured server", but `boot.sh` sets each process's `YOLOMUX_BACKGROUND_OWNER_PRIMARY_PORT` to its own port, so every server computes priority 100 for itself and the tiebreak falls through to `started_at_ns`. Within the single-participant durable root this never fires, so it is latent rather than active.

SCOPE BOUNDARY: the two-host NFS configuration-lock acceptance is a separate concern and must not be merged into this queue. It completed 2026-08-18 and is archived at `docs/DONE/2026-08/DONE.nfs-configuration-lock-acceptance.md`.

## Plan

- [ ] With explicit permission to restart or drive 7770-7773, record each port's PID, CWD, HEAD, state root, owner generation, service roster, and served bundle before mutation.
- [ ] Verify startup ownership order, 7771 takeover after restart, multi-port Tabber/Finder cache writes, shared-root Quick Open/search indexing, follower worker-thread absence, and UI responsiveness during a large index rebuild.
- [ ] Restore the requested fleet topology and retain only bounded evidence under `/tmp`; unrelated ports and state must remain unchanged.

## Done Criteria

- [ ] One current same-SHA run proves exactly one background owner, deterministic takeover, shared cache convergence, no follower worker, and responsive authenticated UI on every requested port.
- [ ] The DONE record links the already-complete isolated 8004-8007 implementation evidence and separately records the real-fleet acceptance result.
