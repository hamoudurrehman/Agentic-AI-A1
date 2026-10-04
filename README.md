# File Janitor — Agent Arena (Assignment 1)

A bounded, single-agent FastAPI service that analyzes and organizes files inside a **synthetic sandbox**. It never touches a real filesystem.

## 1. Problem statement

Given a request such as "find duplicate files in /sandbox/Downloads" or "move the duplicate copies into /sandbox/Duplicates", the agent decides what to do next (list, inspect, find duplicates, move, ask for clarification, finish, or stop), executes sandbox actions through validated tools, reads the results, and stops with an explicit status and stop reason.

**Measurable completion condition:** the run ends with `status = completed` only when the model has issued `finish` after the requested sandbox operations succeeded. Every run ends in exactly one typed status: `completed`, `needs_clarification`, `blocked`, `approval_required`, `tool_error`, `contract_error`, `budget_exceeded` or `failed`.

## 2. Agent Design Canvas

| # | Element | Definition |
|---|---|---|
| 1 | Operational goal | Analyze and organize files in a synthetic sandbox (find duplicates, inspect, list, move). |
| 2 | Completion condition | Model issues a valid `finish` after required tool calls succeeded, or the run stops with a typed non-success status. |
| 3 | System boundary | In-memory copy of `data/sample_data.json`, reloaded fresh for every run. No real files, network access (other than the model API), shell or credentials. |
| 4 | Observations | User task, bounded chat history, untrusted external notes, tool results. |
| 5 | Actions / tools | `list_files`, `inspect_file`, `find_duplicates`, `move_file`; control actions `clarify`, `finish`, `blocked`, `approval_required`. |
| 6 | State | `AgentState`: goal, sandbox files, observations, actions taken, clarification count, retry count. Per-run and in memory. |
| 7 | Autonomy boundary | See section 4. Sandbox moves are automatic; deletion, execution, email and real-filesystem access do not exist as tools. |
| 8 | Primary risks | Ambiguous requests, prompt injection through notes, malformed model output, tool failure, runaway loops, claiming work that was not done. |
| 9 | Evaluation criteria | Correct status and tool choice on public cases, valid structured output, bounded steps, honest final report. |

## 3. Architecture

```
User / Arena request
        |
        v
  FastAPI (api.py)  --->  arena.py: timeout, size bound, footer, logging
        |
        v
  agent.py  bounded loop (max_steps)
   1. build prompt (prompts.py): SYSTEM | goal | state | history | untrusted notes | observations
   2. model call (Gemini) -> raw JSON
   3. Pydantic validation -> semantic validation -> one bounded repair attempt
   4. decision handling:
        clarify / blocked / approval_required / finish  -> stop with typed status
        tool action -> tools.py (path checks, retries, fault gateway)
   5. tool result validated (ToolResult) -> observation -> back to step 1
```

The system, not the model, owns validation, retries and stopping.

### Folder layout

```
student-agent/
  app/
    main.py        FastAPI composition, static files
    api.py         routes, model selection, chat sessions
    arena.py       common boundary: timeout, size limit, footer, logging
    agent.py       the bounded loop, validation, recovery, fault injection
    models.py      typed contracts (decision, state, tool I/O, responses)
    prompts.py     system policy and dynamic prompt assembly
    tools.py       sandbox tools and path safety
    memory.py      bounded in-memory chat history (LangChain messages)
    config.py      environment configuration
    static/        minimal chat interface
  data/sample_data.json     synthetic sandbox files
  evaluation/               public cases, runners, results
  tests/                    scripted-model unit tests
  arena_manifest.json
  render.yaml, Dockerfile, requirements.txt, .env.example, run.py
```

## 4. Autonomy boundary

| Allowed automatically | Blocked / requires approval |
|---|---|
| Listing, inspecting and finding duplicates in the sandbox | Permanent deletion |
| Moving files between **sandbox** folders (in-memory only) | Executing programs |
| Asking clarifying questions | Moving files outside `/sandbox`, real paths such as `C:\...` |
| | Sending email or any external action |

Defence in depth: there is **no delete, execute or send tool**, so even a compromised model cannot perform those actions. An unknown action fails schema validation and never executes. Tools reject relative or `..` paths, and `move_file` rejects a file path given as a destination. The model returns `blocked` or `approval_required` for out-of-scope requests, and the system reports the true number of moves in every reply (`[Sandbox moves performed: N]`).

## 5. Prompt and context design

Persistent policy is kept separate from the changing context. `prompts.py` assembles the prompt dynamically from these layers:

| Layer | Content |
|---|---|
| SYSTEM | Role, allowed actions, safety policy, output contract (sent as the model's system instruction) |
| USER | Current goal |
| STATE / RUNTIME | Serialized `AgentState` |
| HISTORY | Bounded recent chat messages |
| EXTERNAL / UNTRUSTED | Notes supplied with the request, always labelled `trust: untrusted` |
| TOOL OBSERVATION | Last eight tool results |

**Trust rule:** external text is data, never instructions. The system prompt states this explicitly, the prompt repeats it next to the external block, and the agent has no destructive tool to be tricked into using.

## 6. Message memory policy

- Each chat has a stable `session_id`; history is stored as LangChain `HumanMessage` / `AIMessage` objects.
- At most **six recent turns (12 messages)**, a **24,000-character** ceiling and **100 sessions**; older messages are dropped.
- "New chat" clears server history. Arena runs (`/arena/run`) are independent and have no memory.
- Memory is **in memory only** and is lost on restart. The deployment runs **one worker**.
- Multi-turn example: "Clean up my files" → the agent asks how to organize → "file type" continues the same task (covered by a unit test and manual testing).

## 7. Structured output and contract

```python
class AgentDecision(BaseModel):          # extra fields forbidden
    action: Literal["list_files", "inspect_file", "find_duplicates", "move_file",
                    "clarify", "finish", "blocked", "approval_required"]
    path: str | None
    source: str | None
    destination: str | None
    question: str | None        # max 1500 chars
    reason: str | None          # max 1500 chars
    final_response: str | None  # max 1500 chars
```

Responses use `ArenaResponse` (`status`, `final_response`, `steps`, `stop_reason`, `tool_calls`, `errors`, `events`, `metrics`).

### Validation layers

1. **Schema:** Pydantic, required fields, allowed enum values, unknown fields rejected.
2. **Semantic:** each action needs its own fields (for example `move_file` needs `source` and `destination`; `blocked` needs a `reason`); `..` path segments rejected.
3. **Tool-level:** paths must start with `/` and contain no `..`; the source file must exist; the destination must be a folder and must not collide with an existing file.
4. **Tool output:** every tool result is validated as `ToolResult`.
5. **Recovery:** one repair attempt with the validation error fed back to the model. A repair counts against the step budget. If it fails, the run returns `contract_error`.

## 8. Limits and stopping conditions

| Limit | Value |
|---|---|
| `MAX_STEPS` | 6 (model decisions, repairs included) |
| `MAX_TOOL_RETRIES` | 2 per tool call |
| `MAX_OUTPUT_TOKENS` | 512 (configurable) |
| Run timeout | 40 s; per model HTTP call 12 s |
| Model HTTP retries | one retry on 500/503; a 429 or 503 may fall back to the other Lite model (logged as `model_fallback`) |
| History | 6 turns, 24,000 characters |

| Status | `stop_reason` examples |
|---|---|
| `completed` | `task_completed` |
| `needs_clarification` | `clarification_required` |
| `blocked` | `autonomy_boundary` |
| `approval_required` | `approval_required` |
| `tool_error` | `tool_retry_limit`, `malformed_tool_retry_limit` |
| `contract_error` | `decision_contract_failed`, `repair_failed` |
| `budget_exceeded` | `max_steps_reached`, `time_budget_reached` |
| `failed` | `model_error`, `internal_error`, `response_too_large` |

Token usage is recorded when the provider reports it. Cost is reported as `null` (not a fabricated zero).

## 9. Model selection experiment

Both models ran the same ten inputs through the production code path (`evaluation/compare_models.py`) with fallback disabled. Raw results: `evaluation/model_comparison.json`.

| Model | Task success | Valid structure | Correct action | Avg latency (s) | Input tokens | Output tokens | Est. cost |
|---|---|---|---|---|---|---|---|
| gemini-3.5-flash-lite | 10/10 | 9/10 | 10/10 | 3.74 | 32,838 | 1,033 | n/a (free tier) |
| gemini-3.1-flash-lite | 7/10 | 9/10 | 9/10 | 6.51 | 27,005 | 1,343 | n/a (free tier) |

`gemini-3.1-flash-lite` used the wrong tool on one input and asked unnecessary clarification questions on two (a missing-file inspection and a delete request).

**Selection:** `gemini-3.5-flash-lite` is the deployed default. It was more accurate and faster, and its calls fit comfortably inside the 40 s run budget. Its tasks are narrow (choose one of a few tools), so a larger model was not expected to help.

**Full Flash models were ruled out on availability.** `gemini-3.5-flash`, `3.6-flash` and `3.8-flash` are limited to 5 requests per minute and 20 per day on the free tier (see `limits.png`), and in testing returned 503 errors and responses of up to 47 s. Cost was $0 on the free tier; token counts are recorded so paid cost can be estimated.

## 10. Testing evidence

- `tests/test_file_janitor.py` — scripted-model tests (no API calls): ambiguity, injection, invalid and unrepairable decisions, unknown action, tool timeout, malformed tool output, budget termination, autonomy block, multi-turn clarification, destination guard.
- `evaluation/public_cases.json` — eight live cases run against the server with `evaluation/run_public_tests.py`.
- Manual behaviour tests of the chat interface and `/docs` covered all six stress categories.
- Deployed health and Arena endpoint checks: see `SUBMISSION.md`.

## 11. Limitations

- Chat history is in memory and is lost on restart or sleep; single worker only.
- A task needing more than six steps ends with `budget_exceeded`. For example, sorting all seven sandbox files by type needs eight steps. The stop is explicit and the footer reports how many moves happened. A batch move tool would raise this ceiling.
- Prompt-injection resistance relies on the prompt plus the absence of dangerous tools; the model can still be misled into choosing a harmless but wrong sandbox action.
- Free-tier model quotas can cause 429 errors under bursts; the fallback to the second Lite model reduces but does not remove this.
- Free hosting may sleep, causing a slow first request.
- Cost is not estimated.

## 12. Run locally (Windows)

```powershell
py -3.12 -m venv .venv          # or your installed Python 3.12+
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env     # then edit .env and add your key
.\.venv\Scripts\python.exe run.py
```

Open `http://127.0.0.1:8000/` (interface) or `/docs` (API). Environment variables: `HOST`, `PORT`, `MODEL_PROVIDER`, `MODEL_NAME`, `GEMINI_API_KEY`, `MAX_STEPS`, `MAX_TOOL_RETRIES`, `MAX_OUTPUT_TOKENS`, `RUN_TIMEOUT_SECONDS`.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe evaluation/run_public_tests.py --url http://127.0.0.1:8000
.\.venv\Scripts\python.exe evaluation/compare_models.py
```

## 13. Deployment

Deployed as a web service with one worker. Set the secret `GEMINI_API_KEY`, plus `MODEL_PROVIDER=gemini` and `MODEL_NAME=gemini-3.5-flash-lite`, in the host's environment settings. Never commit keys.

- Build: `pip install -r requirements.txt`
- Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`
- Health check: `/health`

Endpoints: `GET /health`, `GET /arena/manifest`, `POST /arena/run`, `POST /chat` (interface), `GET /docs`.
