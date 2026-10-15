<!-- SPDX-License-Identifier: MIT -->
# MAIN prompt provenance

A MAIN run's record keeps pins of what its model was given: each section of the system
message, the offered tools, and the notice framings installed when the run started
([prompt inventory](ORCHESTRATOR-PROMPTS.md#prompt-inventory)). The pins identify
content. They grant nothing and change no tool, dispatch, planning or closing
behavior. [Research framing](STOCK-RESEARCH-PROMPTS.md) is explicitly selected; the
[small-scale trial guidance](PILOT-COMPUTATIONS.md) is part of the ordinary MAIN base.
Watcher and consistency reviewer calls keep their own provenance.

## Sections

The system message is its sections' texts joined by a blank line, in order
([`loop.py`](../packages/orchestrator/src/sandbox_science/orchestrator/loop.py)
`_sections`):

1. `main`, version `sandbox-main-3`: `SYSTEM_PROMPT`. A caller's other system text is
   a `custom` section with no version; supplying the exact built-in text counts as
   `main`, which says what the text is, not who supplied it.
2. `stock`, with the selected template's version, when a research template is
   selected. A custom base cannot be combined with a template.
3. `memory`, with no version, when the run has the project's stores and the project
   memory has entries: the framing `sandbox-memory-run-1` and the accepted entries
   ([project memory](MEMORY.md)). Its text is not from the catalog, so the section pin
   has no version; the `memory` pin below names the framing's.
4. `planning`, version `sandbox-main-plan-1`, when the plan tools are offered. Plan
   tools offered without their hook, or the hook without the tools, refuse before any
   model call.

## Pins

[`prompts.py`](../packages/orchestrator/src/sandbox_science/orchestrator/prompts.py)
builds `PromptPins` (format `sandbox-prompt-pins-1`) once, before the first model call:

- `sections`: each section's ID, version, UTF-8 byte count and SHA-256;
- `tools`: the offered tool names, in order, and the byte count and SHA-256 of their
  schemas as compact UTF-8 JSON (`name`, `description`, `parameters`) with each
  object's keys sorted and arrays and tools in their order;
- `notices`: the ID, version, byte count and SHA-256 of every notice framing in
  [`notices.py`](../packages/orchestrator/src/sandbox_science/orchestrator/notices.py);
- `memory`, for a run with the project's stores: the memory framing's version, the
  memory's version, the entries given (`mem-N@R`, in order), how many the bound left
  out, and the section's byte count and SHA-256 (0 bytes when no entries were given).
  A record older than the memory has none.

A version names exact text. `test_prompts.py` maps every installed version (the MAIN
base, the planning guidance, the research templates, the notice framings, the memory
framing, the watcher prompt and the reviewer rubric) to the SHA-256 of its text, so a
text cannot change without a new version.

The pins are an observation, not a self-check (owner decision 3). They are not
verified again before each model call, and `RunRecord` does not check them against
its messages when it is read. The harness's own tool specifications are constants in
code, outside anything a model can change. A connector tool's name, description and
schema come from its MCP server when the run starts, bounded and framed as the
server's ([MCP connectors](MCP-CONNECTORS.md#the-harnesss-client)), and are in the tools pin.

## Record

`RunRecord.prompt_provenance` holds the pins. A record without them (older than
provenance) stays unknown. An older run's `sandbox-main-bundle-1` or
`sandbox-main-bundle-2` provenance is kept as read, without being interpreted or
checked. No alias, endpoint, credential, task text or error text is in the pins.
Failed runs keep their pins with the rest of their record, and so does a run the user
cancels through the run service. An interrupted command-line run and setup refusals
keep no record.

## Tests

`test_prompts.py` covers the version table, section joining and pins, the tools pin,
a run's pins against its model input with and without plan tools, the CLI's record,
and older records. `test_stock_prompts.py` and `test_pilot_guidance.py` cover the
templates and the base text at the model boundary. They use scripted models; they do
not test instruction adherence.
