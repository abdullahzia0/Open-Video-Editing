---
name: create-motion-graphics
description: Use the supported Open Video Editing title animation subset and identify unsupported motion requests.
---

Call `system_capabilities`. This release supports titles with `animated=true` for a short fade and a lower-third text style. It does not implement logo animation, callouts, progress bars, transitions, pan/zoom tracks or arbitrary scene graphs.

For a request outside this subset, explain the missing implementation and offer a supported alternative only if it meets the user's intent. Never substitute an animated title and claim the full requested graphic was created. Verify terminal artifact evidence.
