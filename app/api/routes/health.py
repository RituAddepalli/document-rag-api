from fastapi import APIRouter

from app.core.config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> dict:
    return {"status": "ok"}


if settings.DEBUG:

    @router.get("/debug/error", include_in_schema=False)
    async def trigger_error() -> dict:
        """Deliberately raises, for exercising the error-logging middleware."""
        raise RuntimeError("This is a deliberate test error for the logging middleware.")
