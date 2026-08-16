# Continuous evaluation regression

1. Block promotion immediately and identify the pinned release, dataset version,
   model ids, retrieval mode and failed gate. Do not edit the golden set to make
   the release pass.
2. Compare the candidate with the last passing baseline for answer quality,
   citations, latency and cost. Inspect sampled traces without prompt content.
3. Roll back production if the failing release is receiving traffic. If it is a
   canary, set its weight to zero.
4. Fix code/configuration, create a new immutable release and rerun the exact
   pinned dataset. Dataset corrections require a new audited dataset version.
5. Resolve only after the independent judge gate passes and the release record
   points to that evaluation run.

