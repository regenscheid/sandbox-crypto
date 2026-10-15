---
name: computation
description: Running computations with the shell, Python and Sage tools and background jobs, and keeping their results as files that can be cited. Use it for scripts, experiments, long runs, and any number that will be reported or recorded.
---

# Computing in the sandbox

All research code runs in the project's sandbox, through the tools.

## The tools

- `python` runs code in the session's Python kernel, and `sage` in its Sage kernel, where Sage syntax applies: ^ is a power and 1/3 is an exact rational. Variables persist between calls until the kernel restarts. Each tool is offered when the environment has the language.
- `shell` runs a bash script in the session workspace. Each call starts afresh: only files carry over, and stdin is empty.
- `start_job` runs a long shell computation in the background (below).
- `read_file`, `write_file` and `list_files` work on the session workspace, or on shared/... for the project's shared area.
- `restart_kernel` starts a kernel afresh, discarding its state.

A kernel loses its state when it restarts, crashes or runs out of memory, or when an idle environment is suspended; the next cell reports the lost state. Save state that took long to compute to a file before relying on it later: pickle, NumPy's save, or Sage's save() and load(). Files survive all of these.

## Scripts of several steps

A script's exit code is its last command's, so a failed step can hide behind a later one that succeeds. Begin a script of several steps with this line, or check each step:

```sh
set -euo pipefail
```

Under pipefail, a pipe into head can end with status 141, which only means head stopped reading. Before saying a script made its files, check that they exist.

## Write results to files

Print the few aggregates a decision needs, and write the rest to files: JSON for structured results, text for logs and tables. Name the inputs, parameters and seed in the file, so the result can be traced and run again.

```python
import json, pathlib

pathlib.Path("results").mkdir(exist_ok=True)
with open("results/collisions.json", "w") as out:
    json.dump({"bits": bits, "trials": trials, "found": found, "seed": seed}, out, indent=1)
```

In Sage, convert Sage Integers with int() before json.dumps, and write very large values as strings.

Where a result claims exactness, compute exactly: integers, rationals and finite-field elements, not floats. A floating-point or sampled estimate is a numerical experiment, and its precision or model belongs beside the value.

## Long output is a sample

Output too long for a tool's result comes back as its head and tail, marked as a sample, and is saved whole to a file the result names. Take no counts, identifiers, rows, or claims about all or none of it from the sample: read the saved file in parts, or compute from it. When the worker itself kept only the start and end of a very large stream, the saved file is a sample too; output that large belongs in a file the script writes.

## Publish, and recorded files

`publish` makes a workspace file an immutable project artifact with its SHA-256, marked as a result. Besides that, the files your calls write are recorded as artifact revisions, unless the project records results only. Either way, `find_artifacts` gives the artifact ID, revision and SHA-256 that pin a file as ledger evidence, in this run or a later one. Publish what you will cite: a result file, a plot, the script that produced them. The recording-results skill shows how to pin them.

## Long runs

Before a long or large run, check the method on a small representative case against known answers, and note its time and memory, unless current pilot evidence already applies. Trivial work needs no pilot. Then run the full computation as a background job:

- `start_job` takes a script (or argv), its purpose and the progress to expect, and returns a job ID. Jobs keep running after the turn ends.
- `job_status` shows a job's state, its CPU and memory use, and the head and tail of its output. Read its full logs with `read_file`, at the stdout and stderr paths it names.
- `wait_jobs` keeps the turn open, without calling the model, until the jobs end, the watcher needs attention, or the user writes.
- `list_jobs` lists the session's jobs, and `cancel_job` stops one, keeping its logs.

A kernel cell is for interactive work. A computation of many minutes belongs in a job, where it survives the turn. A job's files are recorded when its completion notice arrives.

## The shared area

Code starts in the session workspace, so a relative shared/ path there is a folder of the session's own. The project's shared area, shared/... in the file and PDF tools, is `$PROJECT_SHARED` in code. Other sessions of the project see files placed there once your call ends, in every environment. Files over 32 MiB are not copied between environments, so publish large results instead. When two sessions change the same file, the later version is kept beside it as a conflict copy, and the call's notices say so.
