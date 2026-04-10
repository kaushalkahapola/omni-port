# Backport CLAW — Shadow Run Results (v3)

**Last updated:** 2026-04-10 12:58:07  
**Total:** 1 patch(es) run, **1 passed** (100%)

> **Success** = at least one fail→pass or newly-passing test observed.

| Type     | elasticsearch |
| -------- | ------------- |
| TYPE-I   |      1/1      |

---

## Per-patch detail

| Project | Type | Commit | Success | Build | Tests (f→p / newly / p→f) | Details | Category |
| ------- | ---- | ------ | :-----: | :---: | :------------------------: | ------- | -------- |
| elasticsearch | TYPE-I | `da51c8ccbf` | ✓ | ✓ | 0 / 3 / 0 | Newly: org.elasticsearch.transport.InboundPipelineTests#testDecodeExceptionIsPropagated, org.elasticsearch.transport.InboundPipelineTests#testEnsureBodyIsNotPrematurelyReleased, org.elasticsearch.transport.InboundPipelineTests#testPipelineHandling |  |
