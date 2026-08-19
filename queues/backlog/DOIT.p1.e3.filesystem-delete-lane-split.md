# DOIT.p1.e3.filesystem-delete-lane-split.md - Classify Bounded And Recursive Delete Work

## Goal

A bounded single-entry unlink must not wait behind recursive filesystem work, while subtree deletion remains outside the bounded mutation lane and retains one authorization and receipt owner.

## Current Evidence

- `write`, `rename`, and `mkdir` use the dedicated mutation lane.
- `delete` remains excluded because directory deletion can descend an input-sized subtree. Treating every delete as bounded would recreate interactive-lane blocking.

## 2026-08-18 - Implemented (uncommitted), Plus Two Real Defects Found Outside This Scope

IMPLEMENTED in an isolated worktree, uncommitted, based at `7cb75e3a5`; patch also exported to `/tmp/filesystem-descriptor-residuals-and-delete-lane-split.patch`. Landing still requires commit authorization, `python3 tools/check.py`, and live-server verification.

Shape as specified and singular: `io_ops.delete_path(raw_path, *, recursive=False)` is one function with one signature. Non-directory unlinks. Directory without `recursive` does exactly one `rmdir` and, on `ENOTEMPTY`/`EEXIST`, returns `{"path", "deleted": False, "kind": "dir", "pending": "subtree"}` WITHOUT ever reaching `os.scandir`. Directory with `recursive` runs today's descriptor-pinned walk byte-identically. The facade forwards `recursive` and returns early on `pending`, so `invalidate_path_policy_caches()` and `_reindex_after_mutation` fire only on terminal results. `jobd.py:442` keeps one `"delete"` arm. `complete_filesystem_operation` gained a `delete_escalation` arg; on `pending == "subtree"` it reserves `bulk`, re-produces the same descriptor with `recursive=True` under the SAME `operation_id`/`request_id`, and returns without terminalizing, which is what frees the mutation reservation. If `bulk` is full it raises the typed `service_busy`. The deadline is not extended: one receipt, one deadline. `/api/fs/delete` and the Finder JS are untouched; the browser never sends `recursive`.

MEASUREMENT CORRECTION: a non-recursive delete of a FLAT 20,000-entry directory performs `{'scandir': 1, 'unlink': 20000, 'rmdir': 1}` = 20,001 destructive syscalls. The 20,401 figure quoted earlier came from a NESTED fixture; both are correct for their own fixture and the defect class is identical.

DEFECT FOUND, OUT OF SCOPE, MUST NOT BE LOST - cooperative cancellation: `_delete_directory_contents` has no cancellation or deadline check. When an escalated operation's deadline expires, the completion terminalizes `deadline_expired` while the walk KEEPS DELETING. Deletion continues after the operation has been reported terminal, so the user is told the work stopped when it has not. This is a correctness defect, not a nicety.

DEFECT FOUND, OUT OF SCOPE, MUST NOT BE LOST - partial failure: `_delete_directory_contents` raises on the first failing entry, and because the exception propagates through the facade, `invalidate_path_policy_caches()` and the reindex NEVER FIRE. A partially deleted subtree therefore leaves the search index stale, still advertising files that no longer exist. The typed error also carries only a bare basename, not the full path or the deleted-so-far set.

VERIFIED NOT A HOLE: `delete` is absent from `FILESYSTEM_RETAINED_READ_OPERATIONS`, so it always gets a fresh `uuid` coalesce key, and jobd's inline-ready branch only fires on an already-stored product. A `pending` payload can therefore never escape through the synchronous HTTP return, so the escalation owner is genuinely singular.

STILL FLAGGED NOT MADE: deleting a symlink still reports `kind: "file"`. Introducing `kind: "symlink"` is a UI-visible payload change and was deliberately not made.

## Plan

- [x] Reproduce a one-entry unlink delayed behind held interactive/bulk work and a recursive delete whose work exceeds the mutation-lane bound. DONE red-first: `filesystem_operation_priority("delete")` returned `interactive` and the one-entry unlink probe sat `queued` while held work ran (`assert 'queued' == 'running'`). Direct measurement of one non-recursive delete on a flat 20,000-entry directory: `{'scandir': 1, 'unlink': 20000, 'rmdir': 1}` = 20,001 destructive syscalls.
- [x] Split the existing delete operation into an authorized bounded unlink case and a subtree case without adding a second path policy or route. DONE: `io_ops.delete_path(raw_path, *, recursive=False)` - one function, one signature, no second path policy and no second route. Non-directory unlinks; directory without `recursive` does exactly one `rmdir` and returns `pending: "subtree"` on ENOTEMPTY/EEXIST without ever reaching `os.scandir`; directory with `recursive` runs the existing descriptor-pinned walk byte-identically.
- [x] Route bounded unlink through the mutation lane and recursive deletion through a bulk receipt, preserving symlink safety, exact target authority, conflict behavior, Git/index invalidation, failure normalization, and UI state. DONE: `FILESYSTEM_BOUNDED_MUTATIONS` now includes `delete`; `delete` with `recursive is True` maps to `interactive`. `complete_filesystem_operation` gained a `delete_escalation` arg that reserves `bulk` and re-produces the same descriptor with `recursive=True` under the SAME `operation_id`/`request_id`, returning without terminalizing so the mutation reservation frees. Typed `service_busy` when `bulk` is full; deadline not extended. Symlink safety, exact target authority, and conflict behaviour preserved; `/api/fs/delete` and the Finder JS untouched.
- [ ] Add cross-class isolation, recursive receipt, cancellation, partial-failure, file/symlink/directory, and namespace-replacement regressions. PARTIAL: isolation (both directions), recursive receipt, file/symlink/directory and namespace-replacement regressions are added and passing. CANCELLATION and PARTIAL-FAILURE regressions are NOT added - both turned out to be real product defects outside this queue's scope, recorded above, and must be fixed in their own item before a meaningful regression can assert correct behaviour.

## Done Criteria

- [x] The red/green tests prove a held bulk/interactive lane cannot delay bounded unlink and prove recursive deletion never occupies the mutation lane. DONE, verified first-hand by the main agent: `test_bounded_unlink_dispatches_while_recursive_deletes_hold_the_shared_lane` and its siblings run green (4 passed, 556 deselected), and the bound proof patches `os.scandir`/`os.unlink`/`os.rmdir` over 20,000 entries asserting zero scandir, zero unlink, exactly one rmdir.
- [ ] File, symlink, empty directory, nonempty directory, missing target, blocked path, namespace replacement, cancellation, and partial failure each have one typed expected result. PARTIAL: file, symlink, empty dir, nonempty dir, missing target, blocked path and namespace replacement each have a typed result. Cancellation and partial failure do not, for the reason above. Deleting a symlink still reports `kind: "file"`; introducing `kind: "symlink"` is a UI-visible payload change deliberately not made.
- [x] The route, job descriptor, authorization owner, and operation result remain singular; focused tests and an unmodified `python3 tools/check.py` exit 0. DONE for singularity, PARTIAL for the gate. Singular verified: one route, one `"delete"` jobd arm, one authorization owner, one terminal result shape, and `delete` is absent from `FILESYSTEM_RETAINED_READ_OPERATIONS` so a `pending` payload can never escape the synchronous HTTP return. Focused suites measured first-hand at 170 passed. `python3 tools/check.py` NOT run - the patch is uncommitted in an isolated worktree pending commit authorization.
- [ ] Restarted runtime evidence records bounded unlink responsiveness and honest recursive pending/terminal behavior on one unchanged HEAD. NOT DONE: no live-server verification. Requires commit authorization and a shared port, neither of which the implementing agent held.

## Completion

Record the classification boundary, red/green evidence, tests, and runtime proof in `docs/DONE/`, then remove this queue.

## Open Correctness Requirements (2026-08-18) - NOT Closed By The Lane Split

These are OPEN requirements of this queue, not merely observations. Both were reproduced against current source and both remain unchecked.

- [ ] Cooperative cancellation: `_delete_directory_contents` has no cancellation or deadline check, so when an escalated operation's deadline expires the completion terminalizes `deadline_expired` WHILE THE WALK KEEPS DELETING. The user is told the work stopped when it has not. Requires a cooperative token checked per iteration, plus a regression that fails without it.
- [ ] Partial-failure reporting: `_delete_directory_contents` raises on the first failing entry and the exception propagates through the facade, so `invalidate_path_policy_caches()` and the reindex NEVER FIRE. A partially deleted subtree leaves the search index advertising files that no longer exist, and the typed error carries only a bare basename rather than the full path and the deleted-so-far set.

Either both are delivered here, or they are split into their own queue with a recorded reason and this queue's Done Criteria are amended to say so. They must not be closed by implication from the lane split.
