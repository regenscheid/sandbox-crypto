# Durable delegated completion

A child persists its typed outcome checkpoint before saving its final run record.
Schema65 retains the immutable original delegation/run identity and bounded (512 KiB)
`ChildEnd`, with separately mutable final persistence status and settlement marker.
The final `WorkRecords.finish` transaction records persistence status alongside work
completion. A checkpoint alone never establishes a returned outcome.

Successful settlement requires an actual saved run and confirmed final persistence;
a failed save explicitly recorded as `saved_status=1` may settle a persistence-error
handback without a final run record. Settlement preserves original typed validation evidence,
ends the original delegation, records its deterministic `work-{delegation_id}` ledger
mutation, then marks settlement durable. These steps can repeat after interruption.
Missing final persistence status and explicit persistence failure produce a labelled
persistence-error handback without a validated result. No model/tool operation replays.

First attempts and Resume use the same settlement. Recovery drains eligible checkpoints
in stable pages of at most100 before parent admission. Missing final records without an explicit failed-save receipt are excluded before
pagination. Ordinary root startup drains before abandoned-tree cleanup;
cleanup preserves pending eligible checkpoints. Parent completion and Resume readiness
remain blocked while a saved checkpoint is unsettled, including the delegation-end/ledger gap.
A reserved future parent maps through `research_continuations` until it is admitted:
its completed children remain handbacks of that stable parent work across repeated Resume.
Acknowledgements and consumption receipts still require actual parent attempts.
