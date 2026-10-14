"""Render an agent trace as a diagram.

This is the answer to "do I need to draw the workflow in n8n?" — no. The
diagram is generated from what the run actually did, so it cannot drift from
the implementation and it cannot lie. It is also templating only: no LLM call,
no network, deterministic output for a given trace.

Two views:

* :func:`render_mermaid_sequence` — participants and messages. Loops fall out
  naturally, which is exactly what a static canvas cannot show.
* :func:`render_mermaid_flowchart` — the chosen path, for comparing runs.

:func:`render_html` wraps the sequence diagram plus a step timeline.
"""
from __future__ import annotations

import html
import re
from typing import Any, Iterable, Optional

# --- sanitising -----------------------------------------------------------

_MERMAID_SPECIALS = str.maketrans({
    '"': "'",
    "\n": " ",
    "\r": " ",
    "\t": " ",
    ";": ",",
    "#": ".",
})


def mermaid_id(name: str) -> str:
    """A participant identifier Mermaid will accept verbatim."""
    cleaned = re.sub(r"[^0-9A-Za-z_]", "_", name or "unknown")
    if not cleaned or cleaned[0].isdigit():
        cleaned = "t_" + cleaned
    return cleaned


def mermaid_label(text: Any, *, limit: int = 70) -> str:
    """A human-readable label safe to drop inside a Mermaid string."""
    raw = "" if text is None else str(text)
    raw = " ".join(raw.split())
    if len(raw) > limit:
        raw = raw[: limit - 3].rstrip() + "..."
    return raw.translate(_MERMAID_SPECIALS).replace("...", "…")


def _summarise_arguments(arguments: Any, limit: int = 48) -> str:
    if not isinstance(arguments, dict):
        return ""
    parts = []
    for key, value in arguments.items():
        parts.append(f"{key}={value}")
    joined = ", ".join(parts)
    return joined[:limit] + ("…" if len(joined) > limit else "")


def _steps(run: Any) -> list[dict]:
    """Accept an AgentRun, a dict, or a raw list of step dicts."""
    if hasattr(run, "as_dict"):
        return run.as_dict()["steps"]
    if isinstance(run, dict):
        return run.get("steps") or []
    return list(run or [])


def _meta(run: Any) -> dict:
    if hasattr(run, "as_dict"):
        return run.as_dict()
    if isinstance(run, dict):
        return run
    return {}


# --- Mermaid: sequence ----------------------------------------------------

def render_mermaid_sequence(run: Any, *, title: Optional[str] = None) -> str:
    """Sequence diagram of the full run, including repeated tool calls."""
    steps = _steps(run)
    meta = _meta(run)

    lines = ["sequenceDiagram", "    autonumber"]
    lines.append("    participant U as Goal")
    lines.append("    participant A as Agent")
    seen: list[str] = []
    for step in steps:
        for call in step.get("tool_calls", []):
            name = call.get("name", "unknown")
            if name not in seen:
                seen.append(name)
    for name in seen:
        lines.append(f"    participant {mermaid_id(name)} as {mermaid_label(name, limit=28)}")

    lines.append(f"    U->>A: {mermaid_label(meta.get('goal', ''), limit=60) or 'start'}")

    if not steps:
        lines.append("    Note over A: stopped before any round ran")
    # The final round's text is the conclusion. Putting 3,000 characters into a
    # diagram label makes the diagram unreadable and repeats the whole answer, so
    # a long final thought is abbreviated with a pointer to the rendered section.
    conclusion_text = (meta.get("final_output") or "").strip()
    for step in steps:
        round_no = step.get("round", "?")
        thought = (step.get("thought") or "").strip()
        if conclusion_text and thought == conclusion_text and len(thought) > 400:
            thought = thought[:90].rstrip() + "… (full answer in the conclusion)"
        if thought:
            lines.append(f"    Note over A: round {round_no}: {mermaid_label(thought, limit=110)}")
        calls = step.get("tool_calls", [])
        if not calls:
            lines.append(f"    A-->>U: round {round_no}: no tool call")
        for call in calls:
            pid = mermaid_id(call.get("name", "unknown"))
            args = _summarise_arguments(call.get("arguments"))
            label = f"{call.get('name', 'unknown')}({args})" if args else call.get("name", "unknown")
            lines.append(f"    A->>{pid}: {mermaid_label(label, limit=68)}")
            status = "ok" if call.get("ok") else "FAILED"
            observation = mermaid_label(call.get("observation", ""), limit=56)
            lines.append(f"    {pid}-->>A: {status}: {observation}")

    status = meta.get("status", "unknown")
    reason = mermaid_label(meta.get("stop_reason", ""), limit=48)
    lines.append(f"    Note over U,A: {status} — {reason}" if reason else f"    Note over U,A: {status}")

    body = "\n".join(lines)
    return f"---\ntitle: {mermaid_label(title or meta.get('goal') or 'agent run', limit=80)}\n---\n{body}"


# --- Mermaid: flowchart ---------------------------------------------------

def render_mermaid_flowchart(run: Any, *, title: Optional[str] = None) -> str:
    """The path taken, as a graph. Fast to read, easy to diff between runs."""
    steps = _steps(run)
    meta = _meta(run)

    lines = ["flowchart TD"]
    lines.append(f'    G["{mermaid_label(meta.get("goal", "goal"), limit=60)}"]')

    previous = "G"
    counter = 0
    for step in steps:
        calls = step.get("tool_calls", [])
        if not calls:
            counter += 1
            node = f"N{counter}"
            label = mermaid_label(step.get("thought") or "no tool call", limit=52)
            lines.append(f'    {node}["{label}"]')
            lines.append(f"    {previous} --> {node}")
            previous = node
            continue
        for call in calls:
            counter += 1
            node = f"N{counter}"
            name = call.get("name", "unknown")
            args = _summarise_arguments(call.get("arguments"), limit=40)
            label = f"{name}({args})" if args else name
            shape_open, shape_close = ("{{", "}}") if call.get("ok") else ("[/", "/]")
            lines.append(f'    {node}{shape_open}"{mermaid_label(label, limit=60)}"{shape_close}')
            lines.append(f"    {previous} --> {node}")
            previous = node

    counter += 1
    end = f"E{counter}"
    status = meta.get("status", "unknown")
    reason = mermaid_label(meta.get("stop_reason", ""), limit=44)
    lines.append(f'    {end}(["{status}: {reason}"])' if reason else f'    {end}(["{status}"])')
    lines.append(f"    {previous} --> {end}")

    body = "\n".join(lines)
    header = f"---\ntitle: {mermaid_label(title or meta.get('goal') or 'agent run', limit=80)}\n---\n"
    return header + body


# --- Comparing runs -------------------------------------------------------

def render_mermaid_sequence_comparison(
    runs: Iterable[tuple[str, Any]],
    *,
    title: str = "Same code, different goals",
) -> str:
    """One diagram with a participant per run, so divergence is visible.

    Used to demonstrate the acceptance criterion: identical loop, identical
    registry, different goals -> different tool sequences.
    """
    entries = [(label, _steps(run), _meta(run)) for label, run in runs]

    lines = ["sequenceDiagram", "    autonumber"]
    lines.append("    participant U as Goal")
    lines.append("    participant A as Agent")
    all_tools: list[str] = []
    for _, steps, _ in entries:
        for step in steps:
            for call in step.get("tool_calls", []):
                if call.get("name") not in all_tools:
                    all_tools.append(call.get("name"))
    for name in all_tools:
        # Tool names are already identifiers; no need to ellipse them, and a
        # long name must stay readable because it is the participant's label.
        lines.append(f"    participant {mermaid_id(name)} as {mermaid_label(name, limit=120)}")

    for label, steps, meta in entries:
        lines.append(f"    Note over U,A: {mermaid_label(label, limit=60)}")
        lines.append(f"    U->>A: {mermaid_label(meta.get('goal', ''), limit=52)}")
        for step in steps:
            for call in step.get("tool_calls", []):
                pid = mermaid_id(call.get("name", "unknown"))
                args = _summarise_arguments(call.get("arguments"), limit=34)
                text = f"{call.get('name')}({args})" if args else call.get("name")
                lines.append(f"    A->>{pid}: {mermaid_label(text, limit=56)}")
                lines.append(f"    {pid}-->>A: {'ok' if call.get('ok') else 'FAILED'}")
        lines.append(f"    A-->>U: {mermaid_label(meta.get('status', 'unknown'), limit=40)}")

    body = "\n".join(lines)
    return f"---\ntitle: {mermaid_label(title, limit=80)}\n---\n{body}"


# --- HTML -----------------------------------------------------------------

BLOCK_BULLET = re.compile(r"[-*+][ \t]+(?=[A-Za-z\u4e00-\u9fff])")
BLOCK_NUMBER = re.compile(r"\d{1,2}[.)][ \t]+(?=[A-Za-z\u4e00-\u9fff])")
BLOCK_HEADING = re.compile(r"#{1,6}(?=\s)")
BLOCK_RULE = re.compile(r"-{3,}(?=\s|$)")
# A block marker that is immediately preceded by one of these is mid-sentence
# punctuation, not the start of a block.
NOT_A_BOUNDARY = "(["


def normalise_markdown(text: str) -> str:
    """Put block-level markdown back on its own lines.

    Models frequently return an entire answer as one long line, with the block
    structure implied by markers rather than by newlines::

        ## Answer **Reasoning:** ... **Numbers used:** - euro: 1 - abc: 2

    A block parser cannot see any of that, so the answer renders as one dense
    paragraph and markers meant as formatting show up literally.

    Written as a left-to-right scanner rather than a set of regex substitutions.
    Three substitution-based attempts each fixed the target case and broke
    something else — an inline table shredded into one cell per line, the "2" in
    "(id 2)" read as an ordered-list marker, and well-formed tables and lists
    damaged by boundaries matching at position zero. A scanner makes the rule
    explicit and the failures visible.

    The rule: a block marker becomes a line of its own when the preceding
    character is beyond doubt a sentence boundary — whitespace (or the start of
    the text) immediately after ``. ! ? : ;`` — and anything else is left alone.
    """
    if not text:
        return text

    # Protect fenced blocks: their contents must never be reflowed.
    fences: list[str] = []

    def _stash(match: re.Match) -> str:
        fences.append(match.group(0))
        return f"\x00FENCE{len(fences) - 1}\x00"

    text = re.sub(r"```.*?```", _stash, text, flags=re.DOTALL)

    out: list[str] = []
    for line in text.split("\n"):
        if "\x00FENCE" in line:
            out.append(line)
            continue
        out.extend(_split_inline_blocks(line))

    text = "\n".join(out)
    for index, fence in enumerate(fences):
        text = text.replace(f"\x00FENCE{index}\x00", fence)

    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _at_sentence_boundary(line: str, position: int) -> bool:
    """True when a marker starting at ``position`` begins a new block.

    Position 0 always qualifies: the line already starts there, so splitting is
    a no-op and well-formed input is returned unchanged.
    """
    if position == 0:
        return True
    if line[position - 1] not in " \t":
        return False

    back = position - 2
    while back >= 0 and line[back] in " \t":
        back -= 1
    # Closing markdown punctuation belongs to the previous run, not to the
    # boundary: in "**Numbers used:** - item" the character before the bullet is
    # the closing '*', and the sentence boundary is the ':' behind it.
    while back >= 0 and line[back] in "*_`":
        back -= 1
        while back >= 0 and line[back] in " \t":
            back -= 1

    if back < 0:
        return True                      # only indentation before it
    if line[back] in "([":
        return False                     # "(id 2)" and "[1. x]" are not blocks
    if line[back] == "." and back + 1 < len(line) and line[back + 1].isdigit():
        # A dot inside a number is not a sentence ending, so "score 72.0 - ABC"
        # keeps its hyphen. Decimal points are common in this domain — scores,
        # prices, percentages — and the false split was visible.
        return False
    return line[back] in ".!?:;"


def _split_inline_blocks(line: str) -> list[str]:
    """Split one line at every inline block marker that starts a block."""
    markers = list(BLOCK_HEADING.finditer(line))
    markers += list(BLOCK_BULLET.finditer(line))
    markers += list(BLOCK_NUMBER.finditer(line))
    markers += list(BLOCK_RULE.finditer(line))

    starts = sorted(
        {match.start() for match in markers if _at_sentence_boundary(line, match.start())}
    )
    if not starts:
        return [line]

    pieces: list[str] = []
    cursor = 0
    for start in starts:
        piece = line[cursor:start].strip()
        if piece:
            pieces.append(piece)
        cursor = start
    tail = line[cursor:].strip()
    if tail:
        pieces.append(tail)
    return pieces or [line]


def render_markdown(text: str) -> str:
    """Render the model's markdown to HTML, safely.

    The whole string is escaped **first**, then a small subset of markdown is
    converted. Escaping first is what makes this safe: any HTML the model emits
    is already inert by the time structure is added, so there is no sanitiser to
    get wrong and no dependency to keep patched.

    The subset is what agents actually produce: headings, bold, inline code,
    fenced blocks, bullet and numbered lists, tables, and horizontal rules.
    Anything unrecognised passes through as escaped text, which is the honest
    failure mode — the conclusion is the part a human reads, and showing it with
    literal `**` is worse than showing it plainly.
    """
    if not text:
        return ""

    escaped = html.escape(normalise_markdown(text))

    # Fenced blocks first, so their contents are not processed as markdown.
    blocks: list[str] = []

    def _stash(match: re.Match) -> str:
        blocks.append(match.group(1))
        return f"\x00BLOCK{len(blocks) - 1}\x00"

    escaped = re.sub(r"```[a-zA-Z0-9]*\n(.*?)```", _stash, escaped, flags=re.DOTALL)

    lines = escaped.split("\n")
    out: list[str] = []
    list_stack: list[str] = []       # "ul" or "ol"
    in_table = False
    table_rows: list[list[str]] = []

    def close_list() -> None:
        while list_stack:
            out.append(f"</{list_stack.pop()}>")

    def flush_table() -> None:
        nonlocal in_table, table_rows
        if not in_table:
            return
        if table_rows:
            head, *body = table_rows
            out.append("<table><thead><tr>")
            out.extend(f"<th>{cell}</th>" for cell in head)
            out.append("</tr></thead><tbody>")
            for row in body:
                out.append("<tr>")
                out.extend(f"<td>{cell}</td>" for cell in row)
                out.append("</tr>")
            out.append("</tbody></table>")
        in_table = False
        table_rows = []

    for line in lines:
        stripped = line.strip()

        # Tables: a row of pipes, with the separator row dropped.
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue                       # the |---|---| separator
            close_list()
            in_table = True
            table_rows.append(cells)
            continue
        flush_table()

        # Headings.
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            close_list()
            level = min(len(heading.group(1)) + 1, 6)   # h1 stays the page title
            out.append(f"<h{level}>{heading.group(2)}</h{level}>")
            continue

        # Horizontal rule.
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            close_list()
            out.append("<hr/>")
            continue

        # Bullet list.
        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        if bullet:
            if not list_stack or list_stack[-1] != "ul":
                close_list()
                list_stack.append("ul")
                out.append("<ul>")
            out.append(f"<li>{bullet.group(1)}</li>")
            continue

        # Numbered list.
        numbered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if numbered:
            if not list_stack or list_stack[-1] != "ol":
                close_list()
                list_stack.append("ol")
                out.append("<ol>")
            out.append(f"<li>{numbered.group(1)}</li>")
            continue

        close_list()

        if not stripped:
            continue
        # A stashed code block is already a block element and must not be wrapped
        # in a paragraph — <p><pre> is invalid and browsers reparent it.
        if stripped.startswith("\x00BLOCK"):
            out.append(stripped)
            continue

        out.append(f"<p>{stripped}</p>")

    close_list()
    flush_table()

    result = "\n".join(out)

    # Inline formatting, applied after block structure.
    result = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", result)
    result = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", result)
    result = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", result)

    for index, block in enumerate(blocks):
        result = result.replace(f"\x00BLOCK{index}\x00", f'<pre class="md-block">{block}</pre>')

    return result


def derive_stats(steps: list[dict], meta: Optional[dict] = None) -> dict:
    """Compute the summary numbers from the trace itself.

    The standalone trace page is rendered from a stored run's trace, which
    carries only the steps — not the counts. Defaulting those to zero produced a
    header that read "rounds 0, tool calls 0" while the diagram beneath it drew
    ten tool calls. Deriving them from the steps is both correct and impossible
    to get out of step with what is displayed.

    Refused calls are excluded from the tool path, matching AgentRun.tool_sequence:
    a call that never ran did not shape the run.
    """
    meta = meta or {}
    executed = [
        call.get("name")
        for step in steps
        for call in step.get("tool_calls", [])
        if not call.get("refused_reason")
    ]
    refused = sum(
        1 for step in steps for call in step.get("tool_calls", []) if call.get("refused_reason")
    )
    return {
        "rounds": meta.get("rounds") or len(steps),
        "tool_calls": meta.get("tool_calls") or len(executed),
        "refused_calls": meta.get("refused_calls") or refused,
        "tool_sequence": meta.get("tool_sequence") or executed,
        "prompt_tokens": meta.get("prompt_tokens", 0),
        "completion_tokens": meta.get("completion_tokens", 0),
        "elapsed": meta.get("elapsed", 0),
    }


def render_html(run: Any, *, title: Optional[str] = None) -> str:
    """Standalone page: sequence diagram plus a per-round timeline."""
    meta = _meta(run)
    steps = _steps(run)
    stats_data = derive_stats(steps, meta)
    heading = html.escape(title or meta.get("goal") or "Agent run")
    diagram = html.escape(render_mermaid_sequence(run, title=title))

    rows = []
    # The final round's text is the conclusion, already rendered in full above.
    # Echoing 3,000 characters of prose verbatim in the timeline is noise, so a
    # long final thought is trimmed to its opening and the reader is pointed up.
    conclusion_text = (meta.get("final_output") or "").strip()
    for step in steps:
        thought = (step.get("thought") or "").strip()
        if conclusion_text and thought == conclusion_text and len(thought) > 400:
            shown = html.escape(thought[:400].rstrip()) + "…"
            thought_html = (
                f'<div class="thought">{shown}</div>'
                '<div class="thought-note">The full answer is the conclusion above.</div>'
            )
        else:
            thought_html = (
                f'<div class="thought">{html.escape(thought)}</div>' if thought
                else '<div class="thought muted"><em>no text</em></div>'
            )

        calls = step.get("tool_calls", [])
        if calls:
            call_html = "".join(
                f"""<div class="call {'ok' if c.get('ok') else 'bad'}">
                      <div class="call-head">
                        <span class="tool">{html.escape(str(c.get('name', '?')))}</span>
                        {f'<span class="refused">refused: {html.escape(str(c.get("refused_reason")))}</span>' if c.get("refused_reason") else ''}
                        <span class="elapsed">{c.get('elapsed', 0)}s</span>
                      </div>
                      <pre class="args">{html.escape(str(c.get('arguments', {})))}</pre>
                      <pre class="obs">{html.escape(str(c.get('observation', ''))[:1500])}</pre>
                    </div>"""
                for c in calls
            )
        else:
            call_html = '<div class="call none">no tool call</div>'

        rows.append(
            f"""<li>
                  <div class="round">Round {html.escape(str(step.get('round', '?')))}</div>
                  {thought_html}
                  {call_html}
                </li>"""
        )

    tool_sequence = " → ".join(
        mermaid_label(x, limit=30) for x in stats_data["tool_sequence"]
    ) or "(none)"
    stats = [
        ("status", meta.get("status", "?")),
        ("stop reason", meta.get("stop_reason", "")),
        ("rounds", stats_data["rounds"]),
        ("tool calls", stats_data["tool_calls"]),
        (
            "tokens",
            f"{stats_data['prompt_tokens']} in / {stats_data['completion_tokens']} out",
        ),
        ("elapsed", f"{stats_data['elapsed']}s"),
    ]
    if stats_data["refused_calls"]:
        stats.insert(4, ("refused", stats_data["refused_calls"]))
    stats_html = "".join(
        f"<div><dt>{html.escape(str(k))}</dt><dd>{html.escape(str(v))}</dd></div>" for k, v in stats
    )

    # The conclusion is what a human actually reads. Rendering it as escaped text
    # left literal '##' and '**' on the page, which reads as an error rather than
    # as a result.
    conclusion = (meta.get("final_output") or "").strip()
    if conclusion:
        conclusion_html = (
            '<section class="conclusion"><h2>Conclusion</h2>'
            f'<div class="md">{render_markdown(conclusion)}</div></section>'
        )
    elif meta.get("status") and meta.get("status") != "succeeded":
        conclusion_html = (
            f'<section class="conclusion empty">No conclusion: the run ended '
            f'{html.escape(str(meta.get("status")))} '
            f'({html.escape(str(meta.get("stop_reason") or "no reason recorded"))}).</section>'
        )
    else:
        conclusion_html = ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>{heading}</title>
<script type="module">
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
  // securityLevel stays strict: diagram labels contain scraped page text, and
  // Mermaid's own pan/zoom is only available at 'loose'. The controls below are
  // plain CSS transforms over the SVG Mermaid produced.
  mermaid.initialize({{
    startOnLoad: true,
    theme: "neutral",
    securityLevel: "strict",
    sequence: {{ useMaxWidth: false }},
    flowchart: {{ useMaxWidth: false }},
  }});
  // --- zoom and pan, applied after Mermaid draws ---
  document.addEventListener("DOMContentLoaded", function () {{
    var zoom = 1, dragging = false, start = null;
    var viewport = document.getElementById("viewport");
    var canvas = document.getElementById("canvas");
    var label = document.getElementById("zoom-reset");

    function apply() {{
      canvas.style.width = (canvas.dataset.baseWidth || 0) + "px";
      canvas.style.transform = "scale(" + zoom + ")";
      if (label) label.textContent = Math.round(zoom * 100) + "%";
      viewport.style.cursor = zoom > 1 ? "grab" : "default";
    }}

    function measure() {{
      var node = canvas.querySelector("svg");
      if (!node) return false;
      node.style.maxWidth = "none";
      node.style.width = "100%";
      node.style.height = "auto";
      var box = node.getAttribute("viewBox");
      var natural = box ? Number(box.split(/[\\s,]+/)[2]) : 0;
      // Same rule as the app: scale up to fill, never below a readable floor.
      canvas.dataset.baseWidth = Math.round(
        Math.max(viewport.clientWidth, Math.min(natural || viewport.clientWidth, 3000))
      );
      apply();
      return true;
    }}

    function zoomTo(next) {{
      var clamped = Math.min(4, Math.max(0.25, Math.round(next * 100) / 100));
      if (clamped === zoom) return;
      var cx = viewport.scrollLeft + viewport.clientWidth / 2;
      var cy = viewport.scrollTop + viewport.clientHeight / 2;
      var was = zoom;
      zoom = clamped;
      apply();
      var ratio = zoom / was;
      viewport.scrollLeft = cx * ratio - viewport.clientWidth / 2;
      viewport.scrollTop = cy * ratio - viewport.clientHeight / 2;
    }}

    document.getElementById("zoom-in").addEventListener("click", function () {{ zoomTo(zoom + 0.25); }});
    document.getElementById("zoom-out").addEventListener("click", function () {{ zoomTo(zoom - 0.25); }});
    document.getElementById("zoom-reset").addEventListener("click", function () {{
      zoomTo(1);
      viewport.scrollLeft = 0;
      viewport.scrollTop = 0;
    }});
    document.getElementById("zoom-full").addEventListener("click", function () {{
      viewport.classList.toggle("fullscreen");
      document.body.style.overflow = viewport.classList.contains("fullscreen") ? "hidden" : "";
      measure();
    }});
    document.addEventListener("keydown", function (event) {{
      if (event.key === "Escape" && viewport.classList.contains("fullscreen")) {{
        viewport.classList.remove("fullscreen");
        document.body.style.overflow = "";
        measure();
      }}
    }});
    viewport.addEventListener("mousedown", function (event) {{
      if (zoom <= 1 || event.button !== 0) return;
      dragging = true;
      start = {{ x: event.clientX, y: event.clientY,
                 left: viewport.scrollLeft, top: viewport.scrollTop }};
      viewport.style.cursor = "grabbing";
    }});
    viewport.addEventListener("mousemove", function (event) {{
      if (!dragging) return;
      viewport.scrollLeft = start.left - (event.clientX - start.x);
      viewport.scrollTop = start.top - (event.clientY - start.y);
    }});
    window.addEventListener("mouseup", function () {{
      dragging = false;
      viewport.style.cursor = zoom > 1 ? "grab" : "default";
    }});
    viewport.addEventListener("wheel", function (event) {{
      if (!event.ctrlKey) return;
      event.preventDefault();
      zoomTo(zoom + (event.deltaY < 0 ? 0.25 : -0.25));
    }}, {{ passive: false }});

    // Mermaid renders asynchronously; poll briefly for the SVG.
    var tries = 0;
    (function wait() {{
      if (measure() || ++tries > 60) return;
      setTimeout(wait, 100);
    }})();
  }});
</script>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
         margin: 0 auto; max-width: 1100px; padding: 2rem 1.25rem 4rem; }}
  h1 {{ font-size: 1.35rem; margin: 0 0 .35rem; }}
  .sub {{ opacity: .7; margin: 0 0 1.5rem; }}
  dl {{ display: flex; flex-wrap: wrap; gap: .25rem 1.75rem; margin: 0 0 1.75rem;
        padding: .9rem 1.1rem; border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
        border-radius: 10px; }}
  dl div {{ display: flex; gap: .5rem; }}
  dt {{ opacity: .62; }} dd {{ margin: 0; font-weight: 600; }}
  .diagram {{ margin: 0 0 2rem; }}
  .toolbar {{ display: flex; align-items: center; gap: .5rem; flex-wrap: wrap;
              margin-bottom: .6rem; }}
  .toolbar button {{ font: inherit; font-size: .82rem; padding: .25rem .7rem; cursor: pointer;
                     border: 1px solid color-mix(in srgb, currentColor 22%, transparent);
                     border-radius: 6px; background: transparent; color: inherit; }}
  .toolbar button:disabled {{ opacity: .4; cursor: default; }}
  .toolbar .pct {{ min-width: 3.4rem; }}
  .toolbar .hint {{ opacity: .6; font-size: .78rem; margin-left: .3rem; }}
  .viewport {{ overflow: auto; max-height: 30rem; border-radius: 8px; padding: .5rem;
               background: color-mix(in srgb, currentColor 3%, transparent); }}
  .viewport.fullscreen {{ position: fixed; inset: 24px; z-index: 100;
                          max-height: none; background: Canvas;
                          box-shadow: 0 12px 48px rgba(0,0,0,.25); padding: 1rem; }}
  #canvas {{ display: inline-block; min-width: 100%; transform-origin: 0 0;
             transition: transform .12s ease-out; }}
  #canvas svg {{ display: block; max-width: none !important; height: auto; }}
  ol {{ list-style: none; padding: 0; margin: 0; }}
  li {{ border-left: 3px solid color-mix(in srgb, currentColor 22%, transparent);
        padding: 0 0 1.1rem 1.1rem; margin: 0 0 1.1rem; }}
  .round {{ font-weight: 700; font-size: .8rem; letter-spacing: .06em; text-transform: uppercase; opacity: .6; }}
  .thought {{ margin: .35rem 0 .6rem; }}
  .thought-note {{ font-size: .85rem; opacity: .6; font-style: italic; margin: -.3rem 0 .6rem; }}
  .refused {{ font-size: .75rem; color: #d33; font-weight: 600; }}
  .call {{ border-radius: 8px; padding: .55rem .75rem; margin: .4rem 0;
           background: color-mix(in srgb, currentColor 5%, transparent); }}
  .call.bad {{ background: color-mix(in srgb, #d33 14%, transparent); }}
  .call.none {{ opacity: .6; font-style: italic; }}
  .call-head {{ display: flex; justify-content: space-between; gap: 1rem; }}
  .tool {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-weight: 700; }}
  .elapsed {{ opacity: .6; font-size: .82rem; }}
  pre {{ white-space: pre-wrap; word-break: break-word; font-size: .82rem;
        margin: .3rem 0 0; max-height: 16rem; overflow: auto; }}
  .args {{ opacity: .72; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  .conclusion {{ border-left: 3px solid #409eff;
                 background: color-mix(in srgb, currentColor 5%, transparent);
                 border-radius: 8px; padding: .9rem 1.2rem; margin: 0 0 2rem; }}
  .conclusion h2 {{ font-size: .8rem; letter-spacing: .06em; text-transform: uppercase;
                    opacity: .6; margin: 0 0 .6rem; }}
  .conclusion.empty {{ border-left-color: #e6a23c; opacity: .8; font-style: italic; }}
  .md {{ font-size: 15px; line-height: 1.65; }}
  .md h2, .md h3, .md h4 {{ margin: 1.1rem 0 .4rem; line-height: 1.3; }}
  .md h2 {{ font-size: 1.05rem; }}
  .md h3 {{ font-size: .97rem; }}
  .md p {{ margin: .5rem 0; }}
  .md ul, .md ol {{ margin: .4rem 0 .6rem; padding-left: 1.3rem; }}
  .md li {{ margin: .2rem 0; }}
  .md code {{ background: color-mix(in srgb, currentColor 8%, transparent);
              padding: .1em .35em; border-radius: 4px; font-size: .87em; }}
  .md .md-block {{ background: color-mix(in srgb, currentColor 6%, transparent);
                   padding: .7rem .9rem; border-radius: 6px; overflow-x: auto;
                   font-size: .85rem; white-space: pre-wrap; }}
  .md table {{ border-collapse: collapse; margin: .7rem 0; font-size: .9rem; }}
  .md th, .md td {{ border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
                    padding: .3rem .6rem; text-align: left; }}
  .md th {{ background: color-mix(in srgb, currentColor 6%, transparent); }}
  .md hr {{ border: 0; border-top: 1px solid color-mix(in srgb, currentColor 15%, transparent);
            margin: 1rem 0; }}
</style>
</head>
<body>
  <h1>{heading}</h1>
  <p class="sub">tool path: <code>{html.escape(tool_sequence)}</code></p>
  <dl>{stats_html}</dl>
  <div class="diagram">
    <div class="toolbar">
      <button id="zoom-out" title="Zoom out">−</button>
      <button id="zoom-in" title="Zoom in">+</button>
      <button id="zoom-reset" class="pct" title="Reset to 100%">100%</button>
      <button id="zoom-full">Full screen</button>
      <span class="hint">Ctrl + scroll to zoom, drag to pan</span>
    </div>
    <div class="viewport" id="viewport">
      <div id="canvas"><pre class="mermaid">{diagram}</pre></div>
    </div>
  </div>
  {conclusion_html}
  <ol>{''.join(rows)}</ol>
</body>
</html>"""
