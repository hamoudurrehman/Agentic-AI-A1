"""File Janitor agent implementation."""

import json
import asyncio
from time import perf_counter

import httpx
from pydantic import ValidationError

from app.config import settings
from app.models import (
    AgentDecision,
    AgentState,
    ArenaResponse,
    Fault,
    Metrics,
    ToolTrace,
)
from app.prompts import SYSTEM_PROMPT, build_prompt
from app.tools import (
    TOOLS,
    TOOL_INPUTS,
    ToolExecutionError,
    ToolTimeout,
    find_duplicates,
    inspect_file,
    list_files,
    load_sandbox,
    move_file,
)


GEMINI_URL = (
    "https://generativelanguage.googleapis.com/"
    "v1beta/models/{model}:generateContent"
)


def _extract_json(text: str) -> dict:
    """Extract a JSON object from the model response."""

    text = text.strip()

    if text.startswith("```"):
        text = text.replace("```json", "", 1)
        text = text.replace("```", "", 1).strip()

    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1:
        raise ValueError("Model did not return a JSON object.")

    return json.loads(text[start:end + 1])


async def _call_gemini(
    model: str,
    prompt: str,
    fault: Fault,
    fault_state: dict,
):
    """Call Gemini and return JSON plus usage information."""

    if not settings.gemini_api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not configured."
        )

    # Arena fault injection.
    if (
        fault.type == "invalid_agent_decision"
        and not fault_state.get("invalid_agent_decision")
    ):
        fault_state["invalid_agent_decision"] = True

        return (
            {"this_is_not_a_valid_decision": True},
            None,
            None,
        )

    payload = {
        "systemInstruction": {
            "parts": [
                {
                    "text": SYSTEM_PROMPT
                }
            ]
        },
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": settings.max_output_tokens,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingLevel":"low"},
        },
    }

    url = GEMINI_URL.format(model=model)

    async with httpx.AsyncClient(timeout=12.0) as client:
        for attempt in range(2):
            try:
                response = await client.post(
                    url,
                    headers={"x-goog-api-key": settings.gemini_api_key},
                    json=payload,
                )
            except httpx.TransportError:
                if attempt == 1:
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))
                continue

            if response.status_code in (500, 503) and attempt < 1:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue

            response.raise_for_status()
            data = response.json()
            break
    candidates = data.get("candidates", [])

    if not candidates:
        raise RuntimeError("Gemini returned no candidate.")

    parts = candidates[0].get("content", {}).get("parts", [])

    if not parts:
        raise RuntimeError("Gemini returned no response content.")

    text = "".join(
        p.get("text", "")
        for p in parts
        if not p.get("thought")
    )

    result = _extract_json(text)

    usage = data.get("usageMetadata", {})

    input_tokens = usage.get("promptTokenCount")
    output_tokens = usage.get("candidatesTokenCount")

    return result, input_tokens, output_tokens


def _validate_semantics(decision: AgentDecision):
    """Business-level validation after Pydantic validation."""

    if decision.action == "list_files":
        if not decision.path:
            raise ValueError(
                "list_files requires path."
            )

    elif decision.action == "inspect_file":
        if not decision.path:
            raise ValueError(
                "inspect_file requires path."
            )

    elif decision.action == "find_duplicates":
        if not decision.path:
            raise ValueError(
                "find_duplicates requires path."
            )

    elif decision.action == "move_file":

        if not decision.source:
            raise ValueError(
                "move_file requires source."
            )

        if not decision.destination:
            raise ValueError(
                "move_file requires destination."
            )

        if ".." in decision.source.split("/"):
            raise ValueError(
                "Unsafe source path."
            )

        if ".." in decision.destination.split("/"):
            raise ValueError(
                "Unsafe destination path."
            )

    elif decision.action == "clarify":

        if not decision.question:
            raise ValueError(
                "clarify requires question."
            )

    elif decision.action == "finish":

        if not decision.final_response:
            raise ValueError(
                "finish requires final_response."
            )

    elif decision.action in {
        "blocked",
        "approval_required",
    }:

        if not decision.reason:
            raise ValueError(
                f"{decision.action} requires reason."
            )


async def _execute_tool(
    decision: AgentDecision,
    files,
    fault: Fault,
    fault_state: dict,
):

    tool_name = decision.action

    if tool_name not in TOOLS:
        raise ToolExecutionError(
            f"Unknown tool: {tool_name}"
        )

    # First matching operation receives the requested fault.
    if (
        fault.type == "tool_timeout"
        and not fault_state.get("tool_timeout")
    ):
        fault_state["tool_timeout"] = True
        raise ToolTimeout(
            "Injected tool timeout."
        )

    if (
        fault.type == "malformed_tool_output"
        and not fault_state.get("malformed_tool_output")
    ):
        fault_state["malformed_tool_output"] = True

        # Deliberately invalid tool result.
        return {
            "not": "a valid ToolResult"
        }

    input_model = TOOL_INPUTS[tool_name]

    if tool_name == "list_files":
        args = input_model(
            path=decision.path or "/"
        )

    elif tool_name == "inspect_file":
        args = input_model(
            path=decision.path
        )

    elif tool_name == "find_duplicates":
        args = input_model(
            path=decision.path or "/"
        )

    elif tool_name == "move_file":
        args = input_model(
            source=decision.source,
            destination=decision.destination,
        )

    else:
        raise ToolExecutionError(
            "Unsupported tool."
        )

    tool = TOOLS[tool_name]

    return tool(files, args)

FALLBACK = {
    "gemini-3.5-flash-lite": "gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite": "gemini-3.5-flash-lite",
}

async def _call_with_fallback(model, prompt, fault, fault_state, events, step):
    try:
        return await _call_gemini(model, prompt, fault, fault_state)
    except httpx.HTTPStatusError as exc:
        backup = FALLBACK.get(model)
        if backup and exc.response.status_code in (429, 500, 503):
            events.append({"step": step, "event": "model_fallback",
                           "from": model, "to": backup})
            return await _call_gemini(backup, prompt, fault, fault_state)
        raise

async def run_agent(request, history, model):

    files = load_sandbox()

    state = AgentState(
        goal=request.task,
        files=files,
    )

    observations = []

    events = []

    tool_calls = []

    errors = []

    metrics = Metrics()

    fault_state = {}

    model_calls = 0

    total_input_tokens = 0
    total_output_tokens = 0

    have_input_tokens = False
    have_output_tokens = False

    repair_attempts = 0

    for step in range(1, request.arena_config.max_steps + 1):

        # ----------------------------------------------------------
        # MODEL DECISION
        # ----------------------------------------------------------
        if model_calls >= request.arena_config.max_steps:
            break
        prompt = build_prompt(
            goal=request.task,
            state=state,
            observations=observations,
            history=history,
            external_context=request.external_context,
        )

        try:

            raw_decision, input_tokens, output_tokens = (
                await _call_with_fallback(
                    model=model,
                    prompt=prompt,
                    fault=request.arena_config.fault,
                    fault_state=fault_state,
                    events=events,
                    step=step,
                )
            )

            model_calls += 1

            if input_tokens is not None:
                total_input_tokens += input_tokens
                have_input_tokens = True

            if output_tokens is not None:
                total_output_tokens += output_tokens
                have_output_tokens = True

            try:

                decision = AgentDecision.model_validate(
                    raw_decision
                )

                _validate_semantics(decision)

            except (ValidationError, ValueError) as exc:

                errors.append(
                    {
                        "type": "contract_error",
                        "step": step,
                        "message": str(exc),
                    }
                )

                events.append(
                    {
                        "step": step,
                        "event": "decision_validation_failed",
                        "message": str(exc),
                    }
                )

                # One bounded repair attempt.
                if repair_attempts >= 1:
                    metrics.model_calls = model_calls

                    return ArenaResponse(
                        request_id=request.request_id,
                        status="contract_error",
                        final_response=(
                            "The agent could not produce a valid "
                            "decision within the repair limit."
                        ),
                        steps=step,
                        stop_reason="decision_contract_failed",
                        tool_calls=tool_calls,
                        errors=errors,
                        events=events,
                        metrics=metrics,
                    )

                repair_attempts += 1

                if step >= request.arena_config.max_steps:
                    break

                repair_prompt = build_prompt(
                    goal=request.task,
                    state=state,
                    observations=observations,
                    history=history,
                    external_context=request.external_context,
                    repair_error=str(exc),
                )

                try:

                    raw_decision, input_tokens, output_tokens = (
                        await _call_with_fallback(
                            model=model,
                            prompt=repair_prompt,
                            fault=Fault(type="none"),
                            fault_state=fault_state,
                            events=events,
                            step=step,
                        )
                    )

                    model_calls += 1

                    if input_tokens is not None:
                        total_input_tokens += input_tokens
                        have_input_tokens = True

                    if output_tokens is not None:
                        total_output_tokens += output_tokens
                        have_output_tokens = True

                    decision = AgentDecision.model_validate(
                        raw_decision
                    )

                    _validate_semantics(decision)

                except Exception as repair_error:

                    errors.append(
                        {
                            "type": "repair_failed",
                            "step": step,
                            "message": str(repair_error),
                        }
                    )

                    metrics.model_calls = model_calls

                    return ArenaResponse(
                        request_id=request.request_id,
                        status="contract_error",
                        final_response=(
                            "The agent could not repair its "
                            "decision contract."
                        ),
                        steps=step,
                        stop_reason="repair_failed",
                        tool_calls=tool_calls,
                        errors=errors,
                        events=events,
                        metrics=metrics,
                    )
        except Exception as exc:
            print("MODEL ERROR:", repr(exc))
        except Exception as exc:

            errors.append(
                {
                    "type": "model_error",
                    "step": step,
                    "message": str(exc),
                }
            )

            metrics.model_calls = model_calls

            return ArenaResponse(
                request_id=request.request_id,
                status="failed",
                final_response=(
                    "The agent could not obtain a valid model decision."
                ),
                steps=step,
                stop_reason="model_error",
                tool_calls=tool_calls,
                errors=errors,
                events=events,
                metrics=metrics,
            )

        # ----------------------------------------------------------
        # DECISION HANDLING
        # ----------------------------------------------------------

        events.append(
            {
                "step": step,
                "event": "agent_decision",
                "action": decision.action,
            }
        )

        state.actions.append(decision.action)

        # ----------------------------------------------------------
        # CLARIFICATION
        # ----------------------------------------------------------

        if decision.action == "clarify":

            state.clarification_count += 1

            metrics.model_calls = model_calls

            if have_input_tokens:
                metrics.input_tokens = total_input_tokens

            if have_output_tokens:
                metrics.output_tokens = total_output_tokens

            return ArenaResponse(
                request_id=request.request_id,
                status="needs_clarification",
                final_response=decision.question,
                steps=step,
                stop_reason="clarification_required",
                tool_calls=tool_calls,
                errors=errors,
                events=events,
                metrics=metrics,
            )

        # ----------------------------------------------------------
        # BLOCKED
        # ----------------------------------------------------------

        if decision.action == "blocked":

            metrics.model_calls = model_calls

            return ArenaResponse(
                request_id=request.request_id,
                status="blocked",
                final_response=decision.reason,
                steps=step,
                stop_reason="autonomy_boundary",
                tool_calls=tool_calls,
                errors=errors,
                events=events,
                metrics=metrics,
            )

        # ----------------------------------------------------------
        # APPROVAL REQUIRED
        # ----------------------------------------------------------

        if decision.action == "approval_required":

            metrics.model_calls = model_calls

            return ArenaResponse(
                request_id=request.request_id,
                status="approval_required",
                final_response=decision.reason,
                steps=step,
                stop_reason="approval_required",
                tool_calls=tool_calls,
                errors=errors,
                events=events,
                metrics=metrics,
            )

        # ----------------------------------------------------------
        # FINISH
        # ----------------------------------------------------------

        if decision.action == "finish":

            metrics.model_calls = model_calls

            if have_input_tokens:
                metrics.input_tokens = total_input_tokens

            if have_output_tokens:
                metrics.output_tokens = total_output_tokens

            return ArenaResponse(
                request_id=request.request_id,
                status="completed",
                final_response=decision.final_response,
                steps=step,
                stop_reason="task_completed",
                tool_calls=tool_calls,
                errors=errors,
                events=events,
                metrics=metrics,
            )

        # ----------------------------------------------------------
        # TOOL
        # ----------------------------------------------------------

        tool_name = decision.action

        successful_tool_call = False

        for attempt in range(
            1,
            settings.max_tool_retries + 2,
        ):

            tool_started = perf_counter()

            try:

                result = await _execute_tool(
                    decision,
                    files,
                    request.arena_config.fault,
                    fault_state,
                )

                latency = (
                    perf_counter() - tool_started
                ) * 1000

                # Validate tool output.
                from app.models import ToolResult

                result = ToolResult.model_validate(result)

                tool_calls.append(
                    ToolTrace(
                        step=step,
                        tool=tool_name,
                        attempt=attempt,
                        outcome="success"
                        if result.success
                        else "rejected",
                        latency_ms=latency,
                    )
                )

                observation = (
                    f"{tool_name}: {result.message}; "
                    f"data={json.dumps(result.data)}"
                )

                observations.append(observation)

                state.observations.append(observation)

                events.append(
                    {
                        "step": step,
                        "event": "tool_observation",
                        "tool": tool_name,
                        "message": result.message,
                    }
                )

                successful_tool_call = True

                break

            except ToolTimeout as exc:

                latency = (
                    perf_counter() - tool_started
                ) * 1000

                tool_calls.append(
                    ToolTrace(
                        step=step,
                        tool=tool_name,
                        attempt=attempt,
                        outcome="timeout",
                        latency_ms=latency,
                    )
                )

                observations.append(
                    f"{tool_name} timed out."
                )

                errors.append(
                    {
                        "type": "tool_timeout",
                        "step": step,
                        "tool": tool_name,
                        "attempt": attempt,
                        "message": str(exc),
                    }
                )

                if attempt > settings.max_tool_retries:
                    metrics.model_calls = model_calls

                    return ArenaResponse(
                        request_id=request.request_id,
                        status="tool_error",
                        final_response=(
                            "The requested sandbox operation "
                            "could not be completed."
                        ),
                        steps=step,
                        stop_reason="tool_retry_limit",
                        tool_calls=tool_calls,
                        errors=errors,
                        events=events,
                        metrics=metrics,
                    )

            except ValidationError as exc:

                latency = (
                    perf_counter() - tool_started
                ) * 1000

                tool_calls.append(
                    ToolTrace(
                        step=step,
                        tool=tool_name,
                        attempt=attempt,
                        outcome="malformed_output",
                        latency_ms=latency,
                    )
                )

                observations.append(
                    f"{tool_name} returned malformed output."
                )

                errors.append(
                    {
                        "type": "malformed_tool_output",
                        "step": step,
                        "tool": tool_name,
                        "attempt": attempt,
                        "message": str(exc),
                    }
                )

                if attempt > settings.max_tool_retries:
                    metrics.model_calls = model_calls

                    return ArenaResponse(
                        request_id=request.request_id,
                        status="tool_error",
                        final_response=(
                            "The tool returned invalid data and "
                            "the retry limit was reached."
                        ),
                        steps=step,
                        stop_reason="malformed_tool_retry_limit",
                        tool_calls=tool_calls,
                        errors=errors,
                        events=events,
                        metrics=metrics,
                    )

            except Exception as exc:

                latency = (
                    perf_counter() - tool_started
                ) * 1000

                tool_calls.append(
                    ToolTrace(
                        step=step,
                        tool=tool_name,
                        attempt=attempt,
                        outcome="exception",
                        latency_ms=latency,
                    )
                )

                errors.append(
                    {
                        "type": "tool_exception",
                        "step": step,
                        "tool": tool_name,
                        "attempt": attempt,
                        "message": str(exc),
                    }
                )

                if attempt > settings.max_tool_retries:
                    metrics.model_calls = model_calls

                    return ArenaResponse(
                        request_id=request.request_id,
                        status="tool_error",
                        final_response=(
                            "The sandbox operation failed."
                        ),
                        steps=step,
                        stop_reason="tool_retry_limit",
                        tool_calls=tool_calls,
                        errors=errors,
                        events=events,
                        metrics=metrics,
                    )

        # If the tool failed but retry logic did not return,
        # the next model decision gets the observation.
        if not successful_tool_call:
            continue

    # --------------------------------------------------------------
    # STEP BUDGET EXHAUSTED
    # --------------------------------------------------------------

    metrics.model_calls = model_calls

    if have_input_tokens:
        metrics.input_tokens = total_input_tokens

    if have_output_tokens:
        metrics.output_tokens = total_output_tokens

    events.append(
        {
            "step": request.arena_config.max_steps,
            "event": "agent_stop",
            "reason": "max_steps_reached",
        }
    )

    return ArenaResponse(
        request_id=request.request_id,
        status="budget_exceeded",
        final_response=(
            "The agent reached its execution-step limit "
            "before completing the task."
        ),
        steps=request.arena_config.max_steps,
        stop_reason="max_steps_reached",
        tool_calls=tool_calls,
        errors=errors,
        events=events,
        metrics=metrics,
    )