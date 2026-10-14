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
    for step in steps:
        round_no = step.get("round", "?")
        thought = step.get("thought")
        if thought:
            lines.append(f"    Note over A: round {round_no}: {mermaid_label(thought, limit=64)}")
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
        lines.append(f"    participant {mermaid_id(name)} as {mermaid_label(name, limit=24)}")

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

def render_html(run: Any, *, title: Optional[str] = None) -> str:
    """Standalone page: sequence diagram plus a per-round timeline."""
    meta = _meta(run)
    steps = _steps(run)
    heading = html.escape(title or meta.get("goal") or "Agent run")
    diagram = html.escape(render_mermaid_sequence(run, title=title))

    rows = []
    for step in steps:
        calls = step.get("tool_calls", [])
        if calls:
            call_html = "".join(
                f"""<div class="call {'ok' if c.get('ok') else 'bad'}">
                      <div class="call-head">
                        <span class="tool">{html.escape(str(c.get('name', '?')))}</span>
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
                  <div class="thought">{html.escape(str(step.get('thought') or '')) or '<em>no text</em>'}</div>
                  {call_html}
                </li>"""
        )

    tool_sequence = " → ".join(mermaid_label(x, limit=30) for x in meta.get("tool_sequence", [])) or "(none)"
    stats = [
        ("status", meta.get("status", "?")),
        ("stop reason", meta.get("stop_reason", "")),
        ("rounds", meta.get("rounds", 0)),
        ("tool calls", meta.get("tool_calls", 0)),
        ("tokens", f"{meta.get('prompt_tokens', 0)} in / {meta.get('completion_tokens', 0)} out"),
        ("elapsed", f"{meta.get('elapsed', 0)}s"),
    ]
    stats_html = "".join(
        f"<div><dt>{html.escape(str(k))}</dt><dd>{html.escape(str(v))}</dd></div>" for k, v in stats
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>{heading}</title>
<script type="module">
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
  mermaid.initialize({{ startOnLoad: true, theme: "neutral" }});
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
  .seq {{ margin: 0 0 2rem; overflow-x: auto; }}
  ol {{ list-style: none; padding: 0; margin: 0; }}
  li {{ border-left: 3px solid color-mix(in srgb, currentColor 22%, transparent);
        padding: 0 0 1.1rem 1.1rem; margin: 0 0 1.1rem; }}
  .round {{ font-weight: 700; font-size: .8rem; letter-spacing: .06em; text-transform: uppercase; opacity: .6; }}
  .thought {{ margin: .35rem 0 .6rem; }}
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
</style>
</head>
<body>
  <h1>{heading}</h1>
  <p class="sub">tool path: <code>{html.escape(tool_sequence)}</code></p>
  <dl>{stats_html}</dl>
  <div class="seq"><pre class="mermaid">{diagram}</pre></div>
  <ol>{''.join(rows)}</ol>
</body>
</html>"""
