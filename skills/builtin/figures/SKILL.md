---
name: figures
description: Showing results as figures in documents, ledger statements and chat - charts drawn from recorded data, plots made by code, and references to recorded image files with their pins. Use it whenever a result is better seen than read.
---

# Figures

A figure in text is one of three things, each kept in a form you can edit later:

- **a chart**: a Vega-Lite specification in a fence, drawn by the platform from a recorded data file;
- **a plot made by code**: a script you run in the sandbox that writes an SVG or PDF file, which the text refers to;
- **a diagram** of structure, such as a protocol flow, a reduction or a state machine. The platform does not draw diagrams yet; for now, draw one with code as an SVG file and refer to it, or describe the structure in a list.

Choose a chart for a recorded table and a common encoding: bars, lines, points, areas, rules, a simple facet. Choose code when the figure needs computation, many thousands of points, special scales, or publication styling.

## Data first

A chart reads only recorded files: write the numbers to a CSV, TSV or JSON file with `write_file`, or from your script, and name that file in the chart. A chart holds no data of its own: inline values are refused, so every plotted value can be traced to a file and the run that wrote it. The one exception is a generated sequence of at most 10,000 values, which records no measurement.

## Paths

A text names a recorded file by its path, in an image, as below, or in a chart's data:

```markdown
![Timing by key size](plots/timing.svg)
```

- A plain relative path, such as plots/timing.svg, is a file of your own session's workspace.
- shared/ followed by a path is a file of the project's shared area.
- /sessions/S/ followed by a path, or sessions/S/ as `find_artifacts` and the file tools show it, is a file of session S's workspace. Use this form in text outside that session, such as a project document.

A path with a scheme (https:, data:, file:), a query, or a . or .. segment is not a file reference: as an image it stays a link that never loads, and in a chart it is refused.

## A chart

A chart is a fence whose language is vega-lite, holding a Vega-Lite 6 specification. A minimal one:

```json
{
  "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
  "title": "Signing time by key size",
  "description": "Median signing time in milliseconds for each key size, from data/timing.csv.",
  "data": {"url": "data/timing.csv"},
  "mark": "bar",
  "encoding": {
    "x": {"field": "key_bits", "type": "ordinal", "title": "Key size (bits)"},
    "y": {"field": "median_ms", "type": "quantitative", "title": "Median time (ms)"}
  }
}
```

Write it in the text inside a fence opened with three backticks and vega-lite. Rules the platform applies:

- data come from the paths above, or a sequence; no inline values or datasets, no other addresses, no image marks and no links (href);
- the specification is at most 64 KiB; its data at most 8 MiB in all and 100,000 rows a file; at most 20,000 marks are drawn;
- one text draws at most 20 figures at once; the rest wait for the reader to ask;
- give every chart a title and a description: the description is what a reader without the picture gets.

A chart that breaks a rule is not drawn: the reader sees its source and the reason.

## Plots made by code

Keep the plotting script as a file and run it with `python` or `shell`, so the figure can be made again. Write SVG or PDF; write PNG only for raster content, such as a dense heat map or a photograph. Keep the script, its data and its output together, and refer to the output by its path.

## Pins

When the text is saved, each path it names is pinned to the file's revision at that moment. Writing the file again does not change the figure: the text keeps showing the pinned revision, and the reader sees that a newer one exists. To show the new revision, move the pin:

- in a document, `document_patch` with a repin operation naming the block, the path, and the revision, or no revision for the current one; `document_read` shows each pin beside its path;
- in a ledger statement, `ledger_change` with repin, naming the path and the revision; `ledger_read` shows the pins;
- a chat message's pins never move: send a new message.

Moving a pin makes a new revision of the text, whose words are unchanged.

## Checking a figure

You may not see the figure. Check it the way a reader without the picture would: count the rows the chart plots, check the ranges and units against the data, and make sure the output file is not empty. A reference that does not resolve is listed when the text is saved: fix its path, or record the file first.

## Figures and claims

A figure illustrates; it is not evidence. When a claim or result rests on what a figure shows, cite the data file and the script that made it, not the picture. A caption says only what the data show.
