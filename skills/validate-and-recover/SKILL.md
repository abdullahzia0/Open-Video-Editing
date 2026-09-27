---
name: validate-and-recover
description: Inspect Open Video Editing job results, diagnose failures and safely retry unchanged plans.
---

Use `jobs_get` to distinguish queued, running, validating, cancelled, failed, validation_failed and succeeded. After success read `artifacts_get` for checks and warnings. Full decode and metadata checks do not prove aesthetics, caption accuracy or perceptual A/V synchronization.

Use `jobs_retry` only for a resolved transient/environmental failure with a new idempotency key. Repeated identical failures need diagnosis, not indefinite retries. Changing creative scope requires a new plan. Worker interruptions remain failures until a new attempt succeeds. Do not return debug or incomplete output as the final validated asset.
