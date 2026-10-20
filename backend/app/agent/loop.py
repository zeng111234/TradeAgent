"""Bounded ReAct loop — the thing that makes this an agent.

The model sees a goal plus a list of tools. It chooses which tools to call,
reads the observations, and decides what to do next. Nothing about the order
is hardcoded here; that is the whole point.

Design commitments
------------------
* **Bounded.** Rounds, tool calls, tokens and wall-clock are all capped. An
  agent that can run forever is an outage waiting to happen.
* **Recoverable.** A tool that throws, a name that does not exist, or
  arguments that are not valid JSON all become observations the model can
  react to — never a crash. "It can only observe and propose" is enforced by
  which tools are registered, not by trusting the prompt.
* **Observable.** Every round, call, argument, observation and latency lands
  in a :class:`TraceStep` so the run can be replayed, audited and rendered.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .protocol import LLMClient, Message, ToolCall, parse_arguments
from .prompts import fingerprint as prompt_fingerprint
from .tools import ToolRegistry

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """You are an autonomous foreign-trade assistant.

You are given a goal and a set of tools. Work towards the goal by calling
tools and reading their results. You decide what to do next — there is no
fixed sequence.

How to work:
1. Start by finding out what you need. Do not guess values you could look up.
2. Call one or a few tools at a time, and read each result before deciding.
3. If a tool fails or returns something unusable, adapt: try different
   arguments, a different tool, or a different approach. Do not repeat the
   same failing call unchanged.
4. Stop as soon as you can answer. Do not call tools you do not need.

Rules you must not break:
- You can investigate, read, and prepare drafts. You cannot send anything to
  a customer. Drafts are created for human review; a person sends them.
- Never invent customer data. If a lookup returns nothing, say so.
- Cite concrete evidence (names, scores, dates) in your final answer.

Distinguish what you know from what you are assuming. These are different
claims and must not be blended:

- "The record shows 3 emails sent and 0 replies" is a fact you verified.
- "They have lost interest" is an inference. Say it is an inference, and say
  what evidence would settle it.
- If a tool could not have told you something — the data does not exist, the
  source only carries one side of a conversation, a field is empty — say that
  plainly and name it. A gap you name is useful. A gap you paper over with a
  confident sentence is worse than no answer, because somebody will act on it.

Do not manufacture a conclusion to look decisive. If the honest answer is
"I cannot determine this from the available data, and here is what would
settle it", that is a complete and correct answer. Flag anything you could
not verify instead of assuming it is fine.

Finishing:
When you have enough information, reply with your conclusion as plain text
and make no tool call. Your text reply ends the run."""


class AgentError(Exception):
    """Unrecoverable loop failure (as opposed to a tool failure)."""


def _refused(call: ToolCall, observation: str, *, reason: str) -> ToolInvocation:
    """A tool call that was deliberately not executed.

    Recorded as a normal invocation so the trace shows it, and so the assistant
    message's tool_calls and the tool results stay paired one-for-one. A refused
    call is always visible; a silently dropped one is not.
    """
    return ToolInvocation(
        name=call.name,
        arguments_raw=call.arguments,
        arguments=parse_arguments(call.arguments),
        ok=False,
        observation=observation,
        elapsed=0.0,
        call_id=call.id,
        refused_reason=reason,
    )


@dataclass
class AgentBudget:
    """Hard caps. Any one of them being hit ends the run cleanly."""

    max_rounds: int = 12
    max_tool_calls: int = 25
    max_tokens: int = 60_000
    max_seconds: float = 180.0
    max_calls_per_round: int = 5
    # How many consecutive rounds with no tool call before we give up. One is
    # normal (that is how a run ends); more than one means the model is stuck.
    max_idle_rounds: int = 3
    tool_timeout_seconds: float = 60.0
    # Ceiling for the *request* sent to the model, which is not the same as
    # max_tokens: max_tokens bounds what the run may spend in total, this bounds
    # how large any single request may get. Exceeding it triggers compaction
    # rather than an error. 0 disables compaction.
    context_budget_tokens: int = 24_000
    # Recent messages never compacted: they are what the model is reasoning over.
    keep_recent_messages: int = 6
    # Absolute ``time.monotonic()`` deadline for the **whole task**, inherited by
    # every sub-agent. ``max_seconds`` alone bounds one loop, and one loop can
    # contain up to ``max_calls_per_round`` delegations, each with its own
    # ceiling: 180s + 5 x 90s = 630s for a task advertised as 180s. With a
    # deadline the tree shares one clock, and a child's allowance is capped by
    # what the parent has left. ``None`` means "start counting when I start".
    deadline: Optional[float] = None


@dataclass
class ToolInvocation:
    """One tool call and what came back."""

    name: str
    arguments_raw: str
    arguments: dict
    ok: bool
    observation: str
    elapsed: float
    call_id: str = ""
    # Set when the call was deliberately not executed (budget exhausted, or the
    # response was truncated). An empty string means it was genuinely run.
    refused_reason: str = ""

    @property
    def refused(self) -> bool:
        return bool(self.refused_reason)


@dataclass
class TraceStep:
    """One loop iteration: what the model said, what we did about it."""

    round: int
    thought: str = ""
    tool_calls: list[ToolInvocation] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: Of ``prompt_tokens``, how many the provider served from its prefix cache.
    cached_prompt_tokens: int = 0
    latency: float = 0.0
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "round": self.round,
            "thought": self.thought,
            "latency": round(self.latency, 3),
            "note": self.note,
            # Per round, because "which round cost the money" is a question the
            # aggregate cannot answer: a run can spend it all on the first prompt.
            "prompt_tokens": self.prompt_tokens,
            "cached_prompt_tokens": self.cached_prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "tool_calls": [
                {
                    "name": c.name,
                    "arguments": c.arguments,
                    "ok": c.ok,
                    "observation": c.observation,
                    "elapsed": round(c.elapsed, 3),
                    "call_id": c.call_id,
                    "refused_reason": c.refused_reason,
                }
                for c in self.tool_calls
            ],
        }


@dataclass
class AgentRun:
    """Complete record of one agent execution."""

    goal: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    #: Fingerprint of the system prompt this run used (``prompts.fingerprint``).
    #: Recorded on the row because the row outlives the commit: without it there
    #: is no way to tell whether a change in output quality came from a prompt
    #: edit, a model change, or the data -- and no way to reproduce a run whose
    #: instructions are no longer in the working tree.
    prompt_version: str = ""
    #: The customer this run is about, when the caller named one. Carried on the
    #: run so it reaches the row, where it can be indexed -- the debug view used
    #: to find runs by searching their trace text instead.
    customer_id: Optional[int] = None
    status: str = "running"      # running | succeeded | truncated | error | stalled
    final_output: str = ""
    stop_reason: str = ""
    #: Stable identifier for why the run stopped. ``stop_reason`` is the
    #: human sentence; this is the value a metric can group by. Empty for runs
    #: that have not finished (and for objects built by older callers).
    stop_code: str = ""
    steps: list[TraceStep] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: Cache-hit share of ``prompt_tokens``, summed over rounds. Recorded because
    #: it is the difference between a count and a price: cached input is billed at
    #: a fraction of fresh input by every provider that offers it.
    cached_prompt_tokens: int = 0
    elapsed: float = 0.0
    error: Optional[str] = None
    # One entry per compaction pass: round, tokens freed, observations spilled.
    # Recorded rather than silent, so a run that had to compact says so.
    compactions: list[dict] = field(default_factory=list)

    @property
    def tool_call_count(self) -> int:
        """Executed tool calls. Refused ones are recorded but not counted.

        Budgets are expressed in work actually done, and ``refused`` calls did no
        work. Counting them would also make the budget check self-defeating: a
        round that refuses a hundred calls would exhaust the next round's budget.
        """
        return sum(
            1 for s in self.steps for c in s.tool_calls if not c.refused
        )

    @property
    def refused_call_count(self) -> int:
        return sum(1 for s in self.steps for c in s.tool_calls if c.refused)

    @property
    def tool_sequence(self) -> list[str]:
        """Flattened ordered list of tools actually executed.

        Refused calls are excluded: this is the value the acceptance criterion
        checks, and a call that never ran did not shape the run.
        """
        return [c.name for s in self.steps for c in s.tool_calls if not c.refused]

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "prompt_version": self.prompt_version,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "stop_code": self.stop_code,
            "final_output": self.final_output,
            "error": self.error,
            "rounds": len(self.steps),
            "tool_calls": self.tool_call_count,
            "refused_calls": self.refused_call_count,
            "tool_sequence": self.tool_sequence,
            "did_investigate": self.did_investigate,
            "prompt_tokens": self.prompt_tokens,
            "cached_prompt_tokens": self.cached_prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "elapsed": round(self.elapsed, 3),
            "steps": [s.as_dict() for s in self.steps],
            "compactions": list(self.compactions),
        }

    @property
    def did_investigate(self) -> bool:
        """True once at least one tool has actually been called.

        Distinguishes "the agent researched and concluded" from "the model
        wrote prose without looking anything up".
        """
        return self.tool_call_count > 0


class AgentLoop:
    """Runs a goal to completion against a tool registry."""

    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        *,
        budget: Optional[AgentBudget] = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        on_step: Optional[Callable[["TraceStep", "AgentRun"], None]] = None,
        spill_dir: Optional[Path] = None,
    ) -> None:
        self.llm = llm
        self.budget = budget or AgentBudget()
        self.system_prompt = system_prompt
        # Computed once, from the text that will actually be sent. A version
        # constant would be a second source of truth that can be forgotten; this
        # cannot disagree with the prompt it names.
        self.prompt_version = prompt_fingerprint(system_prompt)
        self.on_step = on_step

        # The spill store holds the full text of observations trimmed by
        # compaction. Created lazily-ish: only when a directory is available, so
        # the loop still works in a read-only filesystem, just without recovery.
        self.spill = None
        if self.budget.context_budget_tokens and spill_dir is not None:
            from .context import SpillStore

            self.spill = SpillStore(Path(spill_dir))

        # A compaction that trims an observation names the file it wrote, so the
        # agent can read the detail back. Registering the tool is what makes that
        # promise true rather than decorative.
        #
        # Done on a copy, never on the caller's registry. Mutating it meant two
        # runs sharing one registry interfered: the second registration was
        # rejected by the duplicate-name guard and the caller's tool list gained
        # an entry as a side effect of merely constructing a loop.
        self.registry = registry.copy() if hasattr(registry, "copy") else registry
        if self.spill is not None and "recall_observation" not in self.registry:
            from .context import build_recall_tool

            self.registry.register(build_recall_tool(self.spill))

    async def run(
        self, goal: str, *, context: str = "", run_id: Optional[str] = None,
        customer_id: Optional[int] = None,
    ) -> AgentRun:
        run = AgentRun(
            goal=goal,
            customer_id=customer_id,
            prompt_version=self.prompt_version,
        )
        if run_id is not None:
            run.run_id = run_id
        started = time.monotonic()

        # One clock for the whole task. A sub-agent inherits this absolute
        # deadline, so "the run takes at most max_seconds" stays true however
        # many children it spawns.
        deadline = started + self.budget.max_seconds
        if self.budget.deadline is not None:
            deadline = min(deadline, self.budget.deadline)

        run.messages = [Message.system(self.system_prompt)]
        if context:
            run.messages.append(Message.system(context))
        run.messages.append(Message.user(goal))

        schemas = self.registry.schemas()
        idle_rounds = 0

        for round_index in range(1, self.budget.max_rounds + 1):
            if deadline - time.monotonic() <= 0:
                return self._finish(
                    run, started, "truncated", "time budget exceeded",
                    stop_code="time_budget",
                )

            # Check the round budget *before* spending a model call on it.
            if round_index > self.budget.max_rounds:
                return self._finish(
                    run, started, "truncated", "round budget exhausted",
                    stop_code="round_budget",
                )

            # The model call is bounded by what is left of the run, not by the
            # client's own timeout. Otherwise the inner layer is longer than the
            # outer one -- a sub-agent with a 90-second allowance and a
            # 120-second model timeout could outlive its own budget, and the
            # parent would wait for a child that had already been written off.
            remaining = deadline - time.monotonic()
            reply = await self._ask(run, schemas, round_index, timeout=remaining)
            step = TraceStep(
                round=round_index,
                thought=reply.content,
                prompt_tokens=reply.prompt_tokens,
                completion_tokens=reply.completion_tokens,
                cached_prompt_tokens=getattr(reply, "cached_prompt_tokens", 0) or 0,
            )
            run.steps.append(step)
            run.prompt_tokens += reply.prompt_tokens
            run.completion_tokens += reply.completion_tokens
            run.cached_prompt_tokens += step.cached_prompt_tokens

            if not reply.tool_calls:
                idle_rounds += 1
                text = reply.content.strip()
                substantive = text and (run.did_investigate or len(text) >= 200)

                if substantive:
                    # A text answer ends the run.
                    return self._finish(
                        run, started, "succeeded", "final answer", text,
                        stop_code="final_answer",
                    )

                # Not substantive enough to be an answer. Nudge, and give up
                # if the model keeps producing nothing usable.
                step.note = (
                    "empty reply, no tool calls" if not text
                    else "prose without investigation; asked to use tools"
                )
                if idle_rounds >= self.budget.max_idle_rounds:
                    return self._finish(
                        run, started, "stalled",
                        "model produced no usable tool calls and no substantive answer",
                        stop_code="stalled",
                    )
                run.messages.append(
                    Message.user(
                        "That reply does not answer the goal — you have not "
                        "looked anything up yet. Call a tool to gather evidence, "
                        "then answer."
                        if not run.did_investigate
                        else "Your last reply was empty. Either call a tool to "
                             "make progress, or give your final answer as text."
                    )
                )
                continue

            idle_rounds = 0
            run.messages.append(Message.assistant(reply.content, reply.tool_calls))

            # --- Invariant 1: a response cut off at the token limit may carry
            # truncated arguments. Such arguments can still *validate* while
            # being silently mangled, so the calls are refused rather than
            # executed. Both reference agents guard this; a naive loop executes
            # the call and corrupts state.
            truncated = (reply.finish_reason or "").lower() in ("length", "max_tokens")
            if truncated and reply.tool_calls:
                step.note = f"refused {len(reply.tool_calls)} call(s): response hit the output token limit"
                for call in reply.tool_calls:
                    observation = (
                        "NOT EXECUTED: the response hit the output token limit, so "
                        f"the arguments for '{call.name}' may be truncated. "
                        "Re-issue the call with complete arguments."
                    )
                    step.tool_calls.append(
                        _refused(call, observation, reason="response_length_exceeded")
                    )
                    run.messages.append(Message.tool_result(call, observation))
                if self.on_step:
                    self.on_step(step, run)
                continue

            # --- Invariant 2: every tool call the assistant message declared
            # must receive exactly one tool result, **in the declared order**.
            # Providers reject a transcript with an unanswered call, and a
            # reordered transcript is its own hazard: a model replaying it would
            # read results against the wrong calls. So the batch is split first,
            # then every result is appended in declaration order.
            budget_left = max(0, self.budget.max_tool_calls - run.tool_call_count)

            to_execute = reply.tool_calls[: self.budget.max_calls_per_round]
            refused_calls = reply.tool_calls[self.budget.max_calls_per_round:]
            if len(to_execute) > budget_left:
                refused_calls = to_execute[budget_left:] + refused_calls
                to_execute = to_execute[:budget_left]

            if refused_calls:
                note = f"{len(refused_calls)} call(s) not executed (budget)"
                step.note = f"{step.note}; {note}" if step.note else note

            next_to_execute = 0
            for call in reply.tool_calls:
                # Re-checked per call, because executing the previous one may
                # have consumed the rest of the task's time.
                out_of_time = deadline - time.monotonic() <= 0
                still_allowed = next_to_execute < len(to_execute) and not out_of_time
                if still_allowed and to_execute[next_to_execute].id == call.id:
                    next_to_execute += 1
                    invocation = await self._execute(call, deadline)
                    step.tool_calls.append(invocation)
                    run.messages.append(Message.tool_result(call, invocation.observation))
                    logger.info(
                        "run %s round %s -> %s (%s, %.2fs)",
                        run.run_id[:8], round_index, call.name,
                        "ok" if invocation.ok else "failed", invocation.elapsed,
                    )
                    continue

                if out_of_time:
                    observation = (
                        "NOT EXECUTED: this run's time budget ran out, so the call "
                        "was not made. Nothing was sent and nothing was changed. "
                        "Report what you have established so far and say what is "
                        "still unverified."
                    )
                    refusal_reason = "run_deadline_reached"
                else:
                    observation = (
                        "NOT EXECUTED: this run's tool-call budget is exhausted, so the "
                        "call was not made. Nothing was sent and nothing was changed. "
                        "Report what you have established so far and say what is still "
                        "unverified."
                    )
                    refusal_reason = "tool_budget_exhausted"
                step.tool_calls.append(
                    _refused(call, observation, reason=refusal_reason)
                )
                run.messages.append(Message.tool_result(call, observation))

            if run.prompt_tokens + run.completion_tokens > self.budget.max_tokens:
                return self._finish(
                    run, started, "truncated", "token budget exhausted",
                    stop_code="token_budget",
                )

            if self.on_step:
                self.on_step(step, run)

        return self._finish(
            run, started, "truncated", "round budget exhausted",
            stop_code="round_budget",
        )

    # -- internals ---------------------------------------------------------

    def _model_label(self) -> str:
        """Which model to file this run's spend under.

        The injected client knows its own model when it is the real adapter; a
        scripted one does not, and the configured model is the honest fallback
        for "what this process was pointed at".
        """
        explicit = getattr(self.llm, "model", None)
        if explicit:
            return str(explicit)
        try:
            from app.config import settings

            return str(settings.OPENAI_MODEL or "unknown")
        except Exception:  # pragma: no cover - configuration is always importable
            return "unknown"

    async def _ask(
        self, run: AgentRun, schemas: list[dict], round_index: int,
        *, timeout: float | None = None,
    ):
        try:
            # The full history is kept on the run for the trace; the model gets a
            # bounded projection of it. Nothing is lost — oversized observations
            # are written to the spill store and stay retrievable by name.
            messages = self._project_for_model(run, round_index)
            call = self.llm.chat(
                messages,
                schemas,
                temperature=0.2,
                max_tokens=1500,
            )
            if timeout is None:
                reply = await call
            else:
                # `wait_for` cancels the in-flight request when the budget runs
                # out, including the SDK's own retry loop -- so a throttled
                # provider cannot spend three attempts' worth of time on a run
                # that has 4 seconds left.
                #
                # A non-positive budget means "no time left", not "no limit".
                # Treating it as unbounded (the first version did) would let a
                # call that starts a microsecond after the deadline run for the
                # client's full timeout -- a hole in exactly the budget this
                # parameter exists to enforce. `wait_for` with a timeout of zero
                # cancels immediately, which is what the budget says.
                reply = await asyncio.wait_for(call, timeout=max(0.0, timeout))

            # Recorded here, not at the end of the run: a round that ends the run
            # -- a final answer, a budget stop, a stall -- is still a round the
            # provider billed for, and only the rounds that returned get counted.
            from app.llm_usage import Usage, record as record_usage

            record_usage(
                "agent_run",
                self._model_label(),
                Usage(
                    prompt_tokens=reply.prompt_tokens,
                    cached_prompt_tokens=getattr(reply, "cached_prompt_tokens", 0) or 0,
                    completion_tokens=reply.completion_tokens,
                ),
            )
            return reply
        except asyncio.TimeoutError as exc:
            raise AgentError(
                f"the model call in round {round_index} exceeded what was left of "
                "the run's time budget and was cancelled"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - surfaced as a run error
            logger.error("LLM call failed in run %s: %s", run.run_id, exc, exc_info=True)
            # The class name is part of the message on purpose. The provider
            # adapter translates a failure into one of ``LLMRateLimited`` /
            # ``LLMTransient`` / ``LLMUnavailable`` before it gets here, and that
            # word is the whole value of the translation -- dropping it leaves a
            # run's ``stop_reason`` saying "the call failed" with no way to tell
            # "come back later" from "an operator has to fix this".
            raise AgentError(
                f"LLM call failed in round {round_index} "
                f"({type(exc).__name__}): {exc}"
            ) from exc

    def _project_for_model(self, messages_owner, round_index: int) -> list[Message]:
        """Return the message list to send, compacting if it is too large."""
        from .context import compact_messages, estimate_tokens

        budget = self.budget.context_budget_tokens
        messages = messages_owner.messages

        if not budget or estimate_tokens(messages) <= budget:
            return list(messages)

        view, report = compact_messages(
            messages,
            budget_tokens=budget,
            keep_recent=self.budget.keep_recent_messages,
            spill=self.spill,
        )
        if report.did_anything:
            logger.info(
                "run %s compacted before round %s: %s",
                messages_owner.run_id[:8], round_index, report.as_note(),
            )
            messages_owner.compactions.append(
                {"round": round_index, **report.__dict__}
            )
        return view

    async def _execute(self, call: ToolCall, deadline: Optional[float] = None) -> ToolInvocation:
        started = time.monotonic()

        # A tool's own ceiling must not outlive the task's. Without this clamp a
        # 60-second tool called with 3 seconds left would overrun the budget by
        # 57, which is the same class of gap the deadline itself closes.
        timeout = self.budget.tool_timeout_seconds
        if deadline is not None:
            timeout = max(0.001, min(timeout, deadline - started))

        try:
            result, _ = await asyncio.wait_for(
                # ``_deadline`` and ``_tool_timeout`` are host-injected: invisible
                # in the schema, unforgeable in the arguments. They are how a tool
                # that starts new work learns what the *task* has left, rather than
                # inventing its own clock.
                self.registry.call(
                    call.name,
                    call.arguments,
                    host={
                        "_deadline": deadline,
                        "_tool_timeout": self.budget.tool_timeout_seconds,
                    },
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            from .tools import ToolResult

            if timeout < self.budget.tool_timeout_seconds:
                message = (
                    f"tool '{call.name}' did not finish before the run's time "
                    f"budget ran out ({timeout:.2f}s were left)"
                )
            else:
                message = (
                    f"tool '{call.name}' timed out after "
                    f"{self.budget.tool_timeout_seconds}s"
                )
            result = ToolResult.failure(message)
        elapsed = time.monotonic() - started
        return ToolInvocation(
            name=call.name,
            arguments_raw=call.arguments,
            arguments=parse_arguments(call.arguments),
            ok=result.ok,
            observation=result.observation,
            elapsed=elapsed,
            call_id=call.id,
        )

    def _finish(
        self,
        run: AgentRun,
        started: float,
        status: str,
        stop_reason: str,
        final_output: str = "",
        step: Optional[TraceStep] = None,
        stop_code: str = "",
    ) -> AgentRun:
        run.status = status
        run.stop_reason = stop_reason
        run.stop_code = stop_code
        if final_output:
            run.final_output = final_output
        elif status == "succeeded":
            run.final_output = ""
        run.elapsed = time.monotonic() - started
        # A budget can be exhausted mid-round, after tool calls have already been
        # answered. Fire the callback so those answers are observable rather than
        # existing only in the message history.
        if step is not None and self.on_step:
            self.on_step(step, run)
        return run


async def run_agent(
    goal: str,
    llm: LLMClient,
    registry: ToolRegistry,
    *,
    budget: Optional[AgentBudget] = None,
    context: str = "",
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    spill_dir: Optional[Path] = None,
    run_id: Optional[str] = None,
    customer_id: Optional[int] = None,
    on_step: Optional[Callable[["TraceStep", "AgentRun"], None]] = None,
) -> AgentRun:
    """Convenience wrapper. ``AgentError`` is converted into a failed run.

    Pass ``spill_dir`` to make compaction lossless: trimmed observations are
    written there and stay retrievable by name. Without it compaction still
    bounds the request, but the trimmed detail is gone for good.

    ``run_id`` lets a caller name the run before it starts, which is what makes
    it possible to write a "running" row first and let someone poll it.
    ``on_step`` fires after each round -- a long analysis reports progress
    through it rather than going silent until the end.
    """
    loop = AgentLoop(llm, registry, budget=budget, system_prompt=system_prompt,
                     spill_dir=spill_dir, on_step=on_step)
    try:
        return await loop.run(goal, context=context, run_id=run_id, customer_id=customer_id)
    except AgentError as exc:
        run = AgentRun(goal=goal, run_id=run_id or uuid.uuid4().hex,
                       status="error", stop_code="error",
                       stop_reason=str(exc), error=str(exc),
                       prompt_version=loop.prompt_version)
        return run
