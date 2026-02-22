# Streaming UI Polish — Surgical Fixes (Round 2)

> **For**: Amplifier agent — apply these changes to `amplifier-module-hooks-streaming-ui`
> **Branch**: `feature/session-indicator` (same branch as round 1)
> **Prerequisite**: All 10 items from `STREAMING_UI_REDESIGN.md` are implemented

## Audit Summary

All changes from the original redesign guide are confirmed implemented. Three UX issues remain:

| # | Issue | Root Cause | Fix |
|---|-------|-----------|-----|
| A | No footer/status bar visible | `print_session_footer` skips `depth==0`; StatusBarProvider data never rendered | Add root session summary + inline status line |
| B | Single lines getting busy | Result summaries add noise; rapid fast-tool sequences create dense wall | Dim result part further; add tool burst grouping config |
| C | Space between subtasks & next action | `\n` before agent headers creates excess gap | Conditional spacing based on context |

---

## Fix A: Root Session Footer & Inline Status

### Problem

The current code explicitly skips the footer for root sessions:

```python
# rich_output.py line 164
def print_session_footer(state, cost):
    if state.depth == 0:
        return  # ← Root sessions get NO footer at all
```

And in `__init__.py` `handle_session_end`, the footer is only printed for nested sessions (depth > 0). The `StatusBarProvider` correctly exports data, but **nothing in this module renders it** — that's the CLI's job (Section 7 of the guide). Until the CLI integrates it, the user sees no footer.

### Fix A1: Print a root session summary at session end

In `rich_output.py`, replace `print_session_footer`:

```python
def print_session_footer(
    state: SessionState, cost: Optional[CostEstimate] = None
) -> None:
    """Print session footer — compact summary for all sessions."""
    console = get_console()

    if state.depth == 0:
        # Root session: print a summary rule with key stats
        parts: list[str] = []

        if state.metrics.tool_calls > 0:
            parts.append(f"{state.metrics.tool_calls} tool calls")

        elapsed = state.elapsed_formatted()
        if elapsed:
            parts.append(f"⏱ {elapsed}")

        cost_str = cost.format() if cost else ""
        if cost_str:
            parts.append(cost_str)

        summary = " · ".join(parts)
        if summary:
            console.print(f"[dim]─ {summary}[/]")
        return

    # Nested session: agent completion line with depth prefix
    prefix = _depth_prefix(state.depth)
    color = DEPTH_COLORS[(state.depth - 1) % len(DEPTH_COLORS)]
    elapsed = state.elapsed_formatted()

    parts = [f"[{color}]{CHECK}[/] [{color}]Complete[/]"]

    if state.metrics.tool_calls > 0:
        parts.append(f"[dim]{state.metrics.tool_calls} tool calls[/]")

    parts.append(f"[dim]{BULLET_TRIANGLE} {elapsed}[/]")

    cost_str = cost.format() if cost else ""
    if cost_str:
        parts.append(f"[dim]{cost_str}[/]")

    console.print(f"{prefix}{'  '.join(parts)}")
```

### Fix A2: Print root session footer in `handle_session_end`

In `__init__.py`, update `handle_session_end` to print footer for ALL sessions, not just nested:

Find:
```python
            # Print footer for nested sessions
            if state.depth > 0:
                with self._output_lock:
                    print_session_footer(state, cost)
```

Replace with:
```python
            # Print session footer (root gets summary line, nested gets completion line)
            with self._output_lock:
                print_session_footer(state, cost)
```

This makes the root session end with a clean summary like:
```
─ 47 tool calls · ⏱ 02:34 · $0.12
```

### Fix A3: Inline status fallback (optional, for pre-CLI-integration)

If you want status info between turns before the CLI integrates the prompt_toolkit toolbar, add a `print_inline_status` function to `rich_output.py`:

```python
def print_inline_status(status_text: str) -> None:
    """Print a dim inline status line (fallback for when no prompt_toolkit toolbar exists)."""
    console = get_console()
    console.print(f"[dim]{status_text}[/]")
```

Then in `__init__.py` `handle_session_end`, after the footer, optionally print status bar info:

```python
            # Inline status fallback (when CLI hasn't integrated prompt_toolkit toolbar)
            if self.status_bar and state.depth == 0:
                toolbar_text = self.status_bar.format_toolbar()
                if toolbar_text:
                    with self._output_lock:
                        print_inline_status(toolbar_text)
```

---

## Fix B: Reduce Single-Line Noise

### Problem

The merged single-line format shows `▸ Header (result)` which is correct but creates visual density when many fast tools fire in sequence. The result summary part like `(143 lines)`, `(8 files)`, `(done)` adds useful info but fights with the header for attention.

### Fix B1: Add separator character between header and result

In `rich_output.py`, in `print_tool_merged`, the current format is:
```python
result_part = f" [{style}]{summary}[/]" if summary else ""
```

Change to add a dim separator that breaks up the visual flow:

```python
result_part = f" [dim]→[/] [{style}]{summary}[/]" if summary else ""
```

This changes output from:
```
▸ Read: src/auth.py (143 lines)
```
to:
```
▸ Read: src/auth.py → (143 lines)
```

The `→` creates a visual pause that makes lines easier to scan.

### Fix B2: Simplify result summaries for common fast tools

In `formatting.py`, update `format_result_summary` to be more terse for fast tools:

Find the `read_file` handler:
```python
    if key == "read_file":
        if not content.strip():
            return "(empty)"
        lines = content.count("\n") + 1
        return f"({lines} lines)"
```

Replace with:
```python
    if key == "read_file":
        if not content.strip():
            return "(empty)"
        lines = content.count("\n") + 1
        return f"({lines}L)"
```

Find the `bash`/`shell` handler:
```python
    if key in ("bash", "shell"):
        stripped = content.strip()
        if not stripped:
            return "(no output)"
        lines = stripped.split("\n")
        return f"({len(lines)} lines)"
```

Replace with:
```python
    if key in ("bash", "shell"):
        stripped = content.strip()
        if not stripped:
            return "(∅)"
        lines = stripped.split("\n")
        return f"({len(lines)}L)"
```

Find the `grep` handler:
```python
    if key == "grep":
        stripped = content.strip()
        if not stripped or "no matches" in stripped.lower():
            return "(no matches)"
        lines = [line for line in stripped.split("\n") if line.strip()]
        return f"({len(lines)} matches)"
```

Replace with:
```python
    if key == "grep":
        stripped = content.strip()
        if not stripped or "no matches" in stripped.lower():
            return "(0)"
        lines = [line for line in stripped.split("\n") if line.strip()]
        return f"({len(lines)} hits)"
```

Find the `glob` handler:
```python
    if key == "glob":
        stripped = content.strip()
        if not stripped:
            return "(no files)"
        lines = [line for line in stripped.split("\n") if line.strip()]
        return f"({len(lines)} files)"
```

Replace with:
```python
    if key == "glob":
        stripped = content.strip()
        if not stripped:
            return "(0)"
        lines = [line for line in stripped.split("\n") if line.strip()]
        return f"({len(lines)})"
```

Find the write/edit handler:
```python
    if key in ("write_file", "edit_file"):
        return "(done)"
```

Replace with:
```python
    if key in ("write_file", "edit_file"):
        return "(✓)"
```

This tightens the output from:
```
▸ Read: src/auth.py → (143 lines)
▸ Glob: **/*.py → (8 files)
▸ Edit: src/auth.py (+3, -1) → (done)
▸ Grep: "validate" → (15 matches)
```
to:
```
▸ Read: src/auth.py → (143L)
▸ Glob: **/*.py → (8)
▸ Edit: src/auth.py (+3, -1) → (✓)
▸ Grep: "validate" → (15 hits)
```

### Fix B3: Tool Burst Grouping (config-driven, future enhancement)

Amplifier's "Predictive Tool Burst Grouping" idea is excellent and directly addresses the core noise problem. This is a larger feature that should be its own PR. The approach:

1. Add `pending_burst: list[tuple[str, str, Any]]` to `SessionState` (list of `(tool_name, header, result)`)
2. In `handle_tool_post`, instead of immediately printing, append to burst list
3. Use a `threading.Timer` (200ms) to flush the burst — if another tool:pre arrives before the timer fires, cancel the timer and keep accumulating
4. When the burst flushes, classify and render:
   - All Reads in same dir → `▸ Explored src/auth/ — 5 files (578L)`
   - Mixed → `▸ 6 ops — 5 reads, 1 grep (7 hits)`
5. Config: `tool_grouping: "auto" | "none"` (default: `"auto"`)

**I strongly recommend implementing this.** It would make Amplifier's output dramatically cleaner than any competitor. But do it as a separate feature branch after these polish fixes land.

---

## Fix C: Spacing Between Sections

### Problem

The `\n` before nested agent headers in `print_session_header` creates a blank line. This is good for visual separation between major sections, but creates too much gap when agents spawn right after a tool call.

### Fix C1: Remove the leading `\n` from agent headers

In `rich_output.py`, in `print_session_header`:

Find:
```python
        console.print(
            f"\n{prefix}[{color} bold]{BULLET_TRIANGLE} {agent_name}[/]{type_part}{desc_part}"
        )
```

Replace with:
```python
        console.print(
            f"{prefix}[{color} bold]{BULLET_TRIANGLE} {agent_name}[/]{type_part}{desc_part}"
        )
```

The depth prefix with `│` characters already creates visual separation. Adding `\n` on top creates too much white space.

### Fix C2: Add a thin separator before agent completion footers

Instead of the blank line before agent headers, add a subtle marker after agent completion so the "boundary" between agent blocks is clearer.

In `rich_output.py`, in `print_session_footer`, for nested sessions, add a blank line AFTER the footer (not before the header):

Find (nested session part of `print_session_footer`):
```python
    console.print(f"{prefix}{'  '.join(parts)}")
```

Replace with:
```python
    console.print(f"{prefix}{'  '.join(parts)}")
    console.print()  # blank line after agent completion
```

This creates the pattern:
```
│ ▸ Explorer (explorer) — Survey auth module
│ ▸ Read: src/auth.py → (143L)
│ ▸ Read: src/auth/middleware.py → (89L)
│ ✓ Complete  3 tool calls  ▸ 00:05
                                       ← blank line here, after completion
▸ Edit: src/main.py (+3, -1) → (✓)    ← parent's next action, no gap before
```

---

## Summary of All Changes

```
Files to modify:
├── rich_output.py     ← Fix A1 (root footer), Fix B1 (separator), Fix C1/C2 (spacing)
├── __init__.py        ← Fix A2 (root footer in session_end), Fix A3 (inline status)
└── formatting.py      ← Fix B2 (terse result summaries)

Optional future:
└── __init__.py + state.py ← Fix B3 (tool burst grouping, separate PR)
```

### Expected Before → After

**Before (current):**
```
⠇ Thinking...
✓ Reasoned for 12s
↓ 87,234 (89% cached) · ↑ 1,234 · Σ 88,468 · ⏱ 00:12

▸ Task: Survey auth module (explorer)

│ ▸ Explorer (explorer) — Survey the authentication module structure
│ ▸ Read: src/auth.py (143 lines)
│ ▸ Read: src/auth/middleware.py (89 lines)
│ ▸ Read: src/auth/tokens.py (234 lines)
│ ▸ Grep: "validate" (15 matches)
│ ✓ Complete  4 tool calls  ▸ 00:05

▸ Edit: src/auth.py (+3, -1) (done)
```

**After (with these fixes):**
```
⠇ Thinking...
✓ Reasoned for 12s
↓ 87,234 (89% cached) · ↑ 1,234 · Σ 88,468 · ⏱ 00:12

▸ Task: Survey auth module (explorer)
│ ▸ Explorer (explorer) — Survey the authentication module structure
│ ▸ Read: src/auth.py → (143L)
│ ▸ Read: src/auth/middleware.py → (89L)
│ ▸ Read: src/auth/tokens.py → (234L)
│ ▸ Grep: "validate" → (15 hits)
│ ✓ Complete  4 tool calls  ▸ 00:05

▸ Edit: src/auth.py (+3, -1) → (✓)
─ 47 tool calls · ⏱ 02:34 · $0.12
```

**After (with future tool burst grouping):**
```
⠇ Thinking...
✓ Reasoned for 12s
↓ 87,234 (89% cached) · ↑ 1,234 · Σ 88,468 · ⏱ 00:12

▸ Task: Survey auth module (explorer)
│ ▸ Explorer (explorer) — Survey the authentication module structure
│ ▸ Explored src/auth/ — 3 files (466L) + 1 search (15 hits)
│ ✓ Complete  4 tool calls  ▸ 00:05

▸ Edit: src/auth.py (+3, -1) → (✓)
─ 47 tool calls · ⏱ 02:34 · $0.12
```
