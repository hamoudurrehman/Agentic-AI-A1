import json

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import FileResponse

from app.config import ROOT, settings
from app.models import ArenaRequest, ArenaResponse, ChatRequest
from app.arena import execute


router = APIRouter()


@router.get("/")
def index():
    return FileResponse(
        ROOT / "app/static/index.html"
    )


@router.get("/health")
def health():
    return {
        "status": "ok",
        "implementation": "file_janitor",
    }


@router.get("/arena/manifest")
def manifest():
    return json.loads(
        (ROOT / "arena_manifest.json").read_text()
    )


@router.get("/models")
def models():

    available = []

    if settings.gemini_api_key:
        available = [
            "gemini-3.1-flash-lite",
            "gemini-3.5-flash-lite",
        ]

    return {
        "models": available
    }


@router.post(
    "/arena/run",
    response_model=ArenaResponse,
)
async def arena_run(payload: ArenaRequest):

    model = (
        settings.model_name
        or "gemini-2.5-flash"
    )

    return await execute(
        payload,
        model=model,
    )


@router.post(
    "/chat",
    response_model=ArenaResponse,
)
async def chat(
    payload: ChatRequest,
    request: Request,
):

    available_models = models()["models"]

    selected_model = payload.model

    if selected_model == "unconfigured":
        selected_model = (
            settings.model_name
            or (
                available_models[0]
                if available_models
                else "unconfigured"
            )
        )

    if selected_model not in available_models:
        if selected_model != "unconfigured":
            raise HTTPException(
                400,
                "Model is not enabled",
            )

    busy = request.app.state.busy

    if payload.session_id in busy:
        raise HTTPException(
            409,
            "This chat is already running",
        )

    busy.add(payload.session_id)

    memory = request.app.state.memory

    try:

        result = await execute(
            payload,
            memory.get(payload.session_id),
            selected_model,
        )

        memory.add(
            payload.session_id,
            payload.task,
            result.final_response,
        )

        return result

    finally:
        busy.discard(payload.session_id)


@router.delete("/chat/{session_id}")
def reset(
    session_id: str,
    request: Request,
):

    if session_id in request.app.state.busy:
        raise HTTPException(
            409,
            "Chat is running",
        )

    request.app.state.memory.clear(
        session_id
    )

    return {
        "status": "cleared"
    }