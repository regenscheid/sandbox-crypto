---
name: scientific-writing
description: Planning and writing self-standing scientific living documents from checked research. Use it for research reports, technical notes and papers that need an outline, evidence-aware claims, citations and clear mathematical prose.
---

# Scientific writing from research

Use this with [writing-documents](../writing-documents/SKILL.md) for the document operations, [literature-search](../literature-search/SKILL.md) when source retrieval is available, and [recording-results](../recording-results/SKILL.md) for the project's evidence and status. A delegated writer without literature connectors should request a checked source packet from its parent instead of inventing citations. This skill concerns the document a reader sees; the ledger and work records retain the audit trail.

## Establish the writing brief

Identify the audience, the question the document answers, the scope and parameter sets, and the main result that the evidence presently supports. For a continuing report, call `document_list` and `document_read` before drafting: retain useful exposition and the reader's edits. Read the relevant result statements, proofs, computations and source material. Distinguish established results, proposals, hypotheses and unresolved gaps. Do not silently promote a delegated report or an experimental observation into a theorem.

## Outline before drafting

For a substantial new document or rewrite, make an annotated outline first. Use `show_outline` to inspect an existing document's section structure before changing it. A session workbook document is a useful place to keep a long outline while the project-facing document is being written; a brief revision can use a short outline in working context. The outline itself is Markdown headings plus notes. Include, for each proposed section:

- the question it answers and the point the reader should take away;
- the claims it will make, their assumptions and proof or computation status;
- the specific evidence and literature needed, including what has not yet been read or checked;
- dependencies on earlier definitions or sections, and any figure or table that would clarify it.

Put the key result early enough that a reader can understand why the details matter. One useful path is problem, obstacle, insight, result, consequence, and limitation; adapt it to the material. Check that the outline has a place for definitions, methods sufficient to reproduce the work, limitations and references. Resolve material evidence gaps or label the affected claim as open before drafting it as a result. Keep planning annotations out of the reader-facing prose.

## Draft section by section

If the report is new, use `document_create` with a title and a small Markdown heading skeleton. If it exists, work from the revision returned by `document_read`. Fill or revise one coherent section at a time with `document_patch`, naming only the blocks being changed and giving a short reason. Read the patch outcome and the current revision before continuing, especially if another writer or the user has edited it. If the result is a suggestion, describe it as a suggestion until accepted. Do not rewrite unrelated blocks to make a local change.

Start each section with the question or claim it serves; define notation before using it. State the result and its conditions, then show the argument or evidence that warrants it. Use transitions that explain why the next section follows. Write for a reader who was not present for the research: name the algorithm, variant, parameters, assumptions and sources in words. Internal plan handles, run IDs, agent names, tool calls and checkpoint paths are not substitutes for explanation or bibliography.

Use ordinary Markdown for prose, headings, lists, tables and links. Use $...$ for inline mathematics and $$...$$ for displayed mathematics; define recurring macros in the document front matter as the writing-documents skill explains. Keep equations legible, define every variable, and check that prose agrees with the rendered math. Reserve code fences for code or data, not for mathematical prose.

## Support every scientific claim

Verify a source's authors, title, date, version and relevant passage before citing it. Cite a paper in the prose with a consistent human-readable author–year or numbered label, and provide a References section with enough bibliographic detail and a stable DOI, ePrint, arXiv or publisher locator where available. A ledger reference handle alone is not a citation for an outside reader. Keep internal entry IDs, artifact revisions and quote pins in the ledger or a separate evidence map so claims remain auditable without cluttering the document. If only metadata or an abstract was available, say so and do not attribute a full-text claim to it. Label preprints as preprints and check for a later or withdrawn version; do not use a withdrawn or retracted work as affirmative support.

Match the language to the evidence. A complete proof has explicit hypotheses and a checkable argument. A conditional argument says what remains to be proved. Exact computation, finite search and numerical experiment each state the input, method, coverage and limitations; none becomes a general proof merely by checking many cases. Distinguish a constructed candidate from one accepted by the unchanged verifier. Keep genuine negative findings and limitations when they constrain the conclusion, but remove superseded claims, rescinded findings and abandoned drafts from the current account. The document should describe the best current understanding, not narrate the sequence of agent attempts. Methods and provenance belong where they help reproduction or assessment.

For cryptographic claims, state the security notion, adversary model, oracle or access assumptions, parameters and success or cost measure. Make each reduction's dependencies and any unproved step visible. When comparing with prior work, align the model, parameter set, attack goal, success probability and cost accounting before calling a result stronger or faster. A proof audit, literature check or benchmark review may identify a gap; do not repair a gap by silently changing the claim.

For an attack or implementation result, check that the written algorithm and parameter set match the code or computation that produced the evidence. State what a verifier actually accepted. A benchmark table needs units, hardware, preprocessing and amortization, repetitions, memory and success conditions; do not compare a measured wall time with an asymptotic estimate as if they were the same quantity. Keep a private claim-to-source or claim-to-command map when it helps audit a long report, while leaving the reader-facing text understandable without that map.

Before calling the document complete, read it from the beginning as a new reader. Check that the abstract or opening, main claims, equations, figures, citations and conclusion agree; that every reference is used and every source-dependent claim is cited; and that open questions are visible without overstating the results. Ask for an independent proof or evidence review when one is available and the claim warrants it; classify its findings as confirmed errors, gaps, unstated assumptions or unverified dependencies, then check the fixes. Patch the specific blocks that fail this pass, then report the saved revision and any remaining limitations.
