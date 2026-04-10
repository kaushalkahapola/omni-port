# Backport CLAW — Shadow Run Results (v3)

**Last updated:** 2026-04-10 16:05:57  
**Total:** 4 patch(es) run, **4 passed** (100%)

> **Success** = at least one fail→pass or newly-passing test observed.

| Type     |     crate     | elasticsearch |
| -------- | ------------- | ------------- |
| TYPE-I   |       -       |      1/1      |
| TYPE-II  |      1/1      |      1/1      |
| TYPE-III |      1/1      |       -       |

---

## Per-patch detail

| Project | Type | Commit | Success | Build | Tests (f→p / newly / p→f) | Details | Category |
| ------- | ---- | ------ | :-----: | :---: | :------------------------: | ------- | -------- |
| crate | TYPE-III | `a9b402eced` | ✓ | ✓ | 0 / 3 / 0 | Newly: io.crate.expression.reference.sys.snapshot.SysSnapshotsTest#testUnavailableSnapshotsAreFilteredOut, io.crate.expression.reference.sys.snapshot.SysSnapshotsTest#test_current_snapshot_does_not_fail_if_get_repository_data_returns_failed_future, io.crate.expression.reference.sys.snapshot.SysSnapshotsTest#test_snapshot_in_progress_shown_in_sys_snapshots |  |
| crate | TYPE-II | `3247cc6e48` | ✓ | ✓ | 1 / 0 / 0 | F→P: io.crate.operation.aggregation.HyperLogLogDistinctAggregationTest#test_terminate_partial_without_initialization_returns_0 |  |
| elasticsearch | TYPE-II | `595251d5a1` | ✓ | ✓ | 1 / 0 / 0 | F→P: org.elasticsearch.xpack.esql.optimizer.PhysicalPlanOptimizerTests#testPushTopNDistanceAndPushableFieldWithCompoundFilterToSource {default} |  |
| elasticsearch | TYPE-I | `da51c8ccbf` | ✓ | ✓ | 0 / 3 / 0 | Newly: org.elasticsearch.transport.InboundPipelineTests#testDecodeExceptionIsPropagated, org.elasticsearch.transport.InboundPipelineTests#testEnsureBodyIsNotPrematurelyReleased, org.elasticsearch.transport.InboundPipelineTests#testPipelineHandling |  |
