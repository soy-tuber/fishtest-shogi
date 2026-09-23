"""ASGI app for the bookforge coordinator.

Development::

    BOOKFORGE_AUTH=stub uv run uvicorn bookforge.app:app --port 8000

Environment:

* ``BOOKFORGE_DB`` MongoDB database name (default ``bookforge``)
* ``BOOKFORGE_BOOKS_DIR`` directory of ``<book_id>.db`` files (default ``./books``)
* ``BOOKFORGE_AUTH`` ``stub`` | ``access`` (see ``bookforge.auth``)
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from bookforge.api import router as api_router
from bookforge.auth import PrincipalMiddleware, auth_mode
from bookforge.service import Bookforge


def create_app(db=None, books_dir=None, schedule=True):
    """``db``/``books_dir`` can be injected (tests); otherwise they come from
    the environment and a local MongoDB."""
    mode = auth_mode()

    @asynccontextmanager
    async def lifespan(app):
        nonlocal db
        if db is None:
            from pymongo import MongoClient

            db = MongoClient("localhost")[os.environ.get("BOOKFORGE_DB", "bookforge")]
        service = await run_in_threadpool(
            Bookforge, db, books_dir or os.environ.get("BOOKFORGE_BOOKS_DIR", "books")
        )
        app.state.bookforge = service
        if schedule:
            service.schedule_tasks()
        try:
            yield
        finally:
            await run_in_threadpool(service.shutdown)

    app = FastAPI(lifespan=lifespan, openapi_url=os.environ.get("OPENAPI_URL"))
    app.add_middleware(PrincipalMiddleware, mode=mode)
    app.include_router(api_router)
    return app


def __getattr__(name):
    # ``uvicorn bookforge.app:app`` without building an app at import time
    # (tests import this module without any environment set up).
    if name == "app":
        return create_app()
    raise AttributeError(name)
