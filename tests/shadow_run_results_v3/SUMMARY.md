# Backport CLAW — Shadow Run Results (v3)

**Last updated:** 2026-04-10 13:28:57  
**Total:** 2 patch(es) run, **2 passed** (100%)

> **Success** = at least one fail→pass or newly-passing test observed.

| Type     | elasticsearch |
| -------- | ------------- |
| TYPE-I   |      1/1      |
| TYPE-II  |      1/1      |

---

## Per-patch detail

| Project | Type | Commit | Success | Build | Tests (f→p / newly / p→f) | Details | Category |
| ------- | ---- | ------ | :-----: | :---: | :------------------------: | ------- | -------- |
| elasticsearch | TYPE-II | `595251d5a1` | ✓ | ✓ | 1 / 0 / 0 | F→P: org.elasticsearch.xpack.esql.optimizer.PhysicalPlanOptimizerTests#testPushTopNDistanceAndPushableFieldWithCompoundFilterToSource {default} |  |
| elasticsearch | TYPE-I | `da51c8ccbf` | ✓ | ✓ | 0 / 3 / 0 | Newly: org.elasticsearch.transport.InboundPipelineTests#testDecodeExceptionIsPropagated, org.elasticsearch.transport.InboundPipelineTests#testEnsureBodyIsNotPrematurelyReleased, org.elasticsearch.transport.InboundPipelineTests#testPipelineHandling |  |
