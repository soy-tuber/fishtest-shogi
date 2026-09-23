"""Worker API (design doc §7). All endpoints are JSON POSTs; the endpoint
names are the fishtest ones."""

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from bookforge.service import BookforgeError

router = APIRouter(tags=["worker-api"])


async def _call(request: Request, method_name):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "request is not json encoded"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "request must be a json object"}, status_code=400)
    service = request.app.state.bookforge
    principal = request.state.principal
    try:
        result = await run_in_threadpool(getattr(service, method_name), principal, body)
    except BookforgeError as e:
        return JSONResponse(
            {"error": f"{request.url.path}: {e}"}, status_code=e.status_code
        )
    return JSONResponse(result)


@router.post("/api/request_version")
async def api_request_version(request: Request):
    return await _call(request, "register_worker")


@router.post("/api/request_task")
async def api_request_task(request: Request):
    return await _call(request, "request_task")


@router.post("/api/beat")
async def api_beat(request: Request):
    return await _call(request, "beat")


@router.post("/api/update_task")
async def api_update_task(request: Request):
    return await _call(request, "update_task")


@router.post("/api/failed_task")
async def api_failed_task(request: Request):
    return await _call(request, "failed_task")


@router.post("/api/worker_log")
async def api_worker_log(request: Request):
    return await _call(request, "worker_log")
