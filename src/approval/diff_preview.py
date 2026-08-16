import difflib
import os
import re
from pathlib import Path

MAX_LINES = 120
TAIL_LINES = 30
MAX_CHARS = 5000
CONTEXT_LINES = 3

PATH_KEYS = ("TargetFile", "AbsolutePath")

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)")

MISSING = object()


def _existing_text(tool_input: dict):
    path = next((tool_input[k] for k in PATH_KEYS if tool_input.get(k)), None)
    if not path:
        return MISSING, None
    if not os.path.isfile(path):
        return "", path
    try:
        return Path(path).read_text(encoding="utf-8"), path
    except (OSError, UnicodeDecodeError):
        return MISSING, path


def _find_replaced_text(tool_input: dict, haystack: str, replacement: str) -> str | None:
    # agy doesn't say which key holds the replaced text, so candidates are checked against the
    # file instead of trusted by name.
    best = None
    for key, value in tool_input.items():
        if key in PATH_KEYS or not isinstance(value, str) or not value or value == replacement:
            continue
        if haystack.count(value) == 1 and (best is None or len(value) > len(best)):
            best = value
    return best


def _entries(old: list[str], new: list[str], first_line: int, width: int) -> list[tuple[int, bool, str]]:
    out, old_no, new_no = [], first_line, first_line
    for line in difflib.unified_diff(old, new, lineterm="", n=CONTEXT_LINES):
        if line.startswith(("---", "+++")):
            continue

        header = HUNK_HEADER.match(line)
        if header:
            # difflib counts from the start of what it was given, which is a slice of the file
            # for chunk edits.
            old_no = int(header.group(1)) + first_line - 1
            new_no = int(header.group(2)) + first_line - 1
            continue

        sign, body = line[0], line[1:]
        number = old_no if sign == "-" else new_no
        # A removed line still sits at new_no, but it doesn't occupy one, so it anchors the gap
        # marker without advancing past it.
        out.append((new_no, sign != "-", f"{sign} {number:>{width}} | {body}"))
        if sign in " -":
            old_no += 1
        if sign in " +":
            new_no += 1
    return out


def _with_gaps(entries: list[tuple[int, bool, str]], total: int, width: int) -> list[str]:
    def gap(count):
        return f"{'':>{width + 5}}... ({count} unchanged {'line' if count == 1 else 'lines'})"

    out, last = [], 0
    for position, occupies, text in entries:
        if position > last + 1:
            out.append(gap(position - last - 1))
        out.append(text)
        last = position if occupies else max(last, position - 1)
    if total > last:
        out.append(gap(total - last))
    return out


def _clip(lines: list[str]) -> str:
    if len(lines) > MAX_LINES:
        head = MAX_LINES - TAIL_LINES - 1
        lines = [*lines[:head], f"... ({len(lines) - head - TAIL_LINES} more lines)", *lines[-TAIL_LINES:]]
    kept, size = [], 0
    for line in lines:
        if size + len(line) > MAX_CHARS:
            kept.append(f"... ({len(lines) - len(kept)} more lines)")
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join(kept)


def _render(name: str, entries, total: int, width: int) -> str | None:
    if not entries:
        return None
    added = sum(1 for *_, text in entries if text.startswith("+"))
    removed = sum(1 for *_, text in entries if text.startswith("-"))
    return f"{name} · +{added} -{removed}\n{_clip(_with_gaps(entries, total, width))}"


def build_diff(tool_input: dict) -> str | None:
    before, path = _existing_text(tool_input)
    if before is MISSING:
        return None
    name = os.path.basename(path)
    on_disk = os.path.isfile(path)

    chunks = tool_input.get("ReplacementChunks")
    if chunks:
        if not on_disk:
            return None
        existing = before.splitlines()
        total = len(existing)
        entries, width = [], len(str(total))
        for chunk in chunks:
            start, end = chunk.get("StartLine"), chunk.get("EndLine")
            replacement = chunk.get("ReplacementContent")
            if not isinstance(start, int) or not isinstance(end, int) or replacement is None:
                return None
            replaced_lines = replacement.splitlines()
            total += len(replaced_lines) - (end - start + 1)
            entries += _entries(existing[start - 1 : end], replaced_lines, start, width)
        return _render(name, entries, total, width)

    if "CodeContent" in tool_input:
        content = tool_input["CodeContent"]
    else:
        replacement = tool_input.get("ReplacementContent")
        if replacement is None or not on_disk:
            return None
        replaced = _find_replaced_text(tool_input, before, replacement)
        if replaced is None:
            return None
        content = before.replace(replaced, replacement, 1)

    old_lines, new_lines = before.splitlines(), content.splitlines()
    width = len(str(max(len(old_lines), len(new_lines))))
    label = f"{name} (new file)" if not on_disk else name
    return _render(label, _entries(old_lines, new_lines, 1, width), len(new_lines), width)
