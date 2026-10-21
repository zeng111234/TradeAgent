"""Live token deltas for a running agent, in process.

The run itself is persisted once per round, which is the right unit for an
audit trail and the wrong unit for a chat window: a round can take twenty
seconds, and until it finishes the database has nothing to show. This module is
the missing channel -- an append-only buffer of deltas that the SSE endpoint
drains while the loop is still writing.

Deliberately in-process. The rest of the run machinery already assumes one
uvicorn worker (``_in_flight`` is a module-level set, and the usage ledger is
in memory), so a Redis pub/sub here would buy a guarantee the rest of the system
does not make. When a run is not local -- another worker, or after a restart --
the endpoint falls back to the per-round snapshots it has always sent, and the
UI degrades to "updates once per round" instead of claiming to be live.

Backpressure is handled by forgetting, not by blocking the model: a slow reader
loses the oldest deltas, and the next ``state()`` gives it the authoritative
full text for the round. A dropped delta is therefore a cosmetic gap, never a
wrong transcript.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: Roughly 2k deltas, or 256 KB of text. A round's answer is capped by
#: ``max_tokens``; the ring exists for readers that stall, not for long runs.
MAX_DELTAS = 2000
MAX_CHARS = 262_144
#: A run that was never closed (a crashed task) must not leak forever.
MAX_AGE_SECONDS = 900.0
#: One drain never returns more than this; the next tick continues.
DRAIN_LIMIT = 400


@dataclass(slots=True)
class _Delta:
    seq: int
    round: int
    kind: str
    text: str

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "round": self.round,
            "kind": self.kind,
            "text": self.text,
        }


class LiveRun:
    """The deltas of one run, plus enough state to resynchronise a reader."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.round = 0
        self.content = ""
        self.reasoning = ""
        self.seq = 0
        self.deltas: deque[_Delta] = deque(maxlen=MAX_DELTAS)
        self.chars = 0
        #: Highest seq that has been evicted. A reader behind this must take a
        #: fresh ``state()`` instead of replaying.
        self.dropped_before_seq = 0
        self.started_at = time.monotonic()
        self.updated_at = self.started_at
        self.closed = False

    # -- writing -----------------------------------------------------------
    def begin_round(self, round_index: int) -> None:
        self.round = int(round_index)
        # The previous round's text is now in the trace; keeping it here would
        # make the next ``state()`` report two rounds at once.
        self.content = ""
        self.reasoning = ""
        self.updated_at = time.monotonic()

    def emit(self, kind: str, text: str) -> None:
        if not text or kind not in ("content", "reasoning"):
            return
        self.seq += 1
        self.updated_at = time.monotonic()
        if kind == "content":
            self.content += text
        else:
            self.reasoning += text

        self.deltas.append(_Delta(self.seq, self.round, kind, text))
        self.chars += len(text)
        while self.chars > MAX_CHARS and len(self.deltas) > 1:
            oldest = self.deltas.popleft()
            self.chars -= len(oldest.text)
            self.dropped_before_seq = oldest.seq

    # -- reading -----------------------------------------------------------
    def state(self) -> dict:
        return {
            "run_id": self.run_id,
            "round": self.round,
            "content": self.content,
            "reasoning": self.reasoning,
            "seq": self.seq,
            "elapsed_ms": int((time.monotonic() - self.started_at) * 1000),
        }

    def since(self, seq: int) -> list[dict]:
        if seq < self.dropped_before_seq:
            # The reader fell behind the ring. ``state()`` is the repair, and
            # the caller sends it before asking for deltas again.
            return []
        out: list[dict] = []
        for delta in self.deltas:
            if delta.seq > seq:
                out.append(delta.as_dict())
                if len(out) >= DRAIN_LIMIT:
                    break
        return out


_runs: dict[str, LiveRun] = {}


def open_run(run_id: str) -> LiveRun:
    """Start buffering deltas for a run. Idempotent."""
    sweep()
    live = _runs.get(run_id)
    if live is None or live.closed:
        live = LiveRun(run_id)
        _runs[run_id] = live
    return live


def get(run_id: str) -> Optional[LiveRun]:
    live = _runs.get(run_id)
    if live is None or live.closed:
        return None
    return live


def begin_round(run_id: str, round_index: int) -> None:
    live = get(run_id)
    if live is not None:
        live.begin_round(round_index)


def sink(run_id: str) -> Callable[[str, str], None]:
    """A callback the model adapter can call from the hot path.

    Never raises: a progress channel that can break the run it reports on is
    worse than no progress channel. Losing a delta costs one repaint; raising
    here would abort a model call that has already been billed.
    """

    def _emit(kind: str, text: str) -> None:
        try:
            live = get(run_id)
            if live is not None:
                live.emit(kind, text)
        except Exception:  # noqa: BLE001 - progress must never break the run
            logger.debug("could not buffer a delta for run %s", run_id, exc_info=True)

    return _emit


def state(run_id: str) -> Optional[dict]:
    live = get(run_id)
    return live.state() if live is not None else None


def since(run_id: str, seq: int) -> list[dict]:
    live = get(run_id)
    return live.since(seq) if live is not None else []


def close_run(run_id: str) -> None:
    """Drop the buffer once the run's final snapshot is written.

    The terminal ``done`` event carries the whole answer, so a reader still
    connected needs nothing from here afterwards.
    """
    live = _runs.get(run_id)
    if live is not None:
        live.closed = True
    _runs.pop(run_id, None)


def sweep(max_age_seconds: float = MAX_AGE_SECONDS) -> int:
    """Forget buffers that were never closed. Returns how many were removed."""
    now = time.monotonic()
    stale = [
        run_id
        for run_id, live in _runs.items()
        if now - live.updated_at > max_age_seconds
    ]
    for run_id in stale:
        _runs.pop(run_id, None)
    return len(stale)
