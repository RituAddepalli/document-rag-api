import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import auth, chat, documents, health
from app.core.config import settings
from app.core.logging_config import configure_logging
from app.db.init_db import init_db
from app.middleware.error_logging import ErrorLoggingMiddleware

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up %s (%s)", settings.APP_NAME, settings.ENVIRONMENT)
    await init_db()
    yield
    logger.info("Shutting down %s", settings.APP_NAME)


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    description="FastAPI backend with JWT auth and a Retrieval-Augmented Generation pipeline.",
    lifespan=lifespan,
)

# Note on ordering: Starlette treats the LAST middleware added as the
# OUTERMOST layer. We add ErrorLoggingMiddleware first (inner) and
# CORSMiddleware second (outer) so that CORS headers are still attached
# even to the JSON error responses produced by ErrorLoggingMiddleware.
app.add_middleware(ErrorLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(documents.router)
app.include_router(chat.router)
