# SPDX-License-Identifier: MIT
"""GFM tables from markdown-it-py 4.2.0, with JavaScript row/cell trimming.

Ported from markdown_it/rules_block/table.py of executablebooks/markdown-it-py
v4.2.0. The injected trim matches markdown-it 15.0.2's String.prototype.trim;
inline line maps and the 65,536 autocompleted-cell limit remain upstream's.
Names use this repository's style; the unused enclosing-pipes regex is omitted.

Upstream licenses (including markdown-it, on which the Python rule is based):

MIT License

Copyright (c) 2020 ExecutableBookProject

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

Copyright (c) 2014 Vitaly Puzrin, Alex Kocharin.

Permission is hereby granted, free of charge, to any person
obtaining a copy of this software and associated documentation
files (the "Software"), to deal in the Software without
restriction, including without limitation the rights to use,
copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following
conditions:

The above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
OTHER DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from markdown_it.common.utils import charStrAt, isStrSpace
from markdown_it.rules_block import StateBlock

HEADER_LINE = re.compile(r"^:?-+:?$")

# Limit the amount of empty autocompleted cells in a table,
# see https://github.com/markdown-it/markdown-it/issues/1000,
# Both pulldown-cmark and commonmark-hs limit the number of cells this way to ~200k.
# We set it to 65k, which can expand user input by a factor of x370
# (256x256 square is 1.8kB expanded into 650kB).
MAX_AUTOCOMPLETED_CELLS = 0x10000


def get_line(state: StateBlock, line: int) -> str:
    pos = state.bMarks[line] + state.tShift[line]
    maximum = state.eMarks[line]

    # return state.src.substr(pos, max - pos)
    return state.src[pos:maximum]


def escaped_split(string: str) -> list[str]:
    result: list[str] = []
    pos = 0
    max = len(string)
    is_escaped = False
    last_pos = 0
    current = ""
    ch = charStrAt(string, pos)

    while pos < max:
        if ch == "|":
            if not is_escaped:
                # pipe separating cells, '|'
                result.append(current + string[last_pos:pos])
                current = ""
                last_pos = pos + 1
            else:
                # escaped pipe, '\|'
                current += string[last_pos : pos - 1]
                last_pos = pos

        is_escaped = ch == "\\"
        pos += 1

        ch = charStrAt(string, pos)

    result.append(current + string[last_pos:])

    return result


def table(
    state: StateBlock,
    start_line: int,
    end_line: int,
    silent: bool,
    *,
    trim: Callable[[str], str],
) -> bool:
    """Find a table with the supplied row/cell whitespace semantics."""
    tbody_lines = None

    # should have at least two lines
    if start_line + 2 > end_line:
        return False

    next_line = start_line + 1

    if state.sCount[next_line] < state.blkIndent:
        return False

    if state.is_code_block(next_line):
        return False

    # first character of the second line should be '|', '-', ':',
    # and no other characters are allowed but spaces;
    # basically, this is the equivalent of /^[-:|][-:|\s]*$/ regexp

    pos = state.bMarks[next_line] + state.tShift[next_line]
    if pos >= state.eMarks[next_line]:
        return False
    first_ch = state.src[pos]
    pos += 1
    if first_ch not in ("|", "-", ":"):
        return False

    if pos >= state.eMarks[next_line]:
        return False
    second_ch = state.src[pos]
    pos += 1
    if second_ch not in ("|", "-", ":") and not isStrSpace(second_ch):
        return False

    # if first character is '-', then second character must not be a space
    # (due to parsing ambiguity with list)
    if first_ch == "-" and isStrSpace(second_ch):
        return False

    while pos < state.eMarks[next_line]:
        ch = state.src[pos]

        if ch not in ("|", "-", ":") and not isStrSpace(ch):
            return False

        pos += 1

    line_text = get_line(state, start_line + 1)

    columns = line_text.split("|")
    aligns: list[str] = []
    for i in range(len(columns)):
        t = trim(columns[i])
        if not t:
            # allow empty columns before and after table, but not in between columns;
            # e.g. allow ` |---| `, disallow ` ---||--- `
            if i == 0 or i == len(columns) - 1:
                continue
            return False

        if not HEADER_LINE.search(t):
            return False
        if charStrAt(t, len(t) - 1) == ":":
            aligns.append("center" if charStrAt(t, 0) == ":" else "right")
        elif charStrAt(t, 0) == ":":
            aligns.append("left")
        else:
            aligns.append("")

    line_text = trim(get_line(state, start_line))
    if "|" not in line_text:
        return False
    if state.is_code_block(start_line):
        return False
    columns = escaped_split(line_text)
    if columns and columns[0] == "":
        columns.pop(0)
    if columns and columns[-1] == "":
        columns.pop()

    # header row will define an amount of columns in the entire table,
    # and align row should be exactly the same (the rest of the rows can differ)
    column_count = len(columns)
    if column_count == 0 or column_count != len(aligns):
        return False

    if silent:
        return True

    old_parent_type = state.parentType
    state.parentType = "table"

    # use 'blockquote' lists for termination because it's
    # the most similar to tables
    terminator_rules = state.md.block.ruler.getRules("blockquote")

    token = state.push("table_open", "table", 1)
    token.map = table_lines = [start_line, 0]

    token = state.push("thead_open", "thead", 1)
    token.map = [start_line, start_line + 1]

    token = state.push("tr_open", "tr", 1)
    token.map = [start_line, start_line + 1]

    for i in range(len(columns)):
        token = state.push("th_open", "th", 1)
        if aligns[i]:
            token.attrs = {"style": "text-align:" + aligns[i]}

        token = state.push("inline", "", 0)
        # note in markdown-it this map was removed in v12.0.0 however, we keep it,
        # since it is helpful to propagate to children tokens
        token.map = [start_line, start_line + 1]
        token.content = trim(columns[i])
        token.children = []

        token = state.push("th_close", "th", -1)

    token = state.push("tr_close", "tr", -1)
    token = state.push("thead_close", "thead", -1)

    autocompleted_cells = 0
    next_line = start_line + 2
    while next_line < end_line:
        if state.sCount[next_line] < state.blkIndent:
            break

        terminate = False
        for i in range(len(terminator_rules)):
            if terminator_rules[i](state, next_line, end_line, True):
                terminate = True
                break

        if terminate:
            break
        line_text = trim(get_line(state, next_line))
        if not line_text:
            break
        if state.is_code_block(next_line):
            break
        columns = escaped_split(line_text)
        if columns and columns[0] == "":
            columns.pop(0)
        if columns and columns[-1] == "":
            columns.pop()

        # note: autocomplete count can be negative if user specifies more columns than header,
        # but that does not affect intended use (which is limiting expansion)
        autocompleted_cells += column_count - len(columns)
        if autocompleted_cells > MAX_AUTOCOMPLETED_CELLS:
            break

        if next_line == start_line + 2:
            token = state.push("tbody_open", "tbody", 1)
            token.map = tbody_lines = [start_line + 2, 0]

        token = state.push("tr_open", "tr", 1)
        token.map = [next_line, next_line + 1]

        for i in range(column_count):
            token = state.push("td_open", "td", 1)
            if aligns[i]:
                token.attrs = {"style": "text-align:" + aligns[i]}

            token = state.push("inline", "", 0)
            # note in markdown-it this map was removed in v12.0.0 however, we keep it,
            # since it is helpful to propagate to children tokens
            token.map = [next_line, next_line + 1]
            try:
                token.content = trim(columns[i]) if columns[i] else ""
            except IndexError:
                token.content = ""
            token.children = []

            token = state.push("td_close", "td", -1)

        token = state.push("tr_close", "tr", -1)

        next_line += 1

    if tbody_lines:
        token = state.push("tbody_close", "tbody", -1)
        tbody_lines[1] = next_line

    token = state.push("table_close", "table", -1)

    table_lines[1] = next_line
    state.parentType = old_parent_type
    state.line = next_line
    return True
