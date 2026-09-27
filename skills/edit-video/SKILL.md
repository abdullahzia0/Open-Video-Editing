---
name: edit-video
description: Perform temporal and geometry edits using Open Video Editing typed plans.
---

Inspect asset metadata and available operations. A trim retains `[start,end)` in the current timeline; to remove an opening interval retain the remainder. Successive operations consume the preceding result. Speed changes affect duration and audio timing.

Choose crop versus padding from the request or a saved preference; ask when a choice would remove meaningful content. Put text operations after timeline edits. Current plans use one source asset. Merging, freeze frames and multi-file split export are unavailable: explain this instead of inventing operations. Verify expected duration/dimensions and terminal render evidence.
