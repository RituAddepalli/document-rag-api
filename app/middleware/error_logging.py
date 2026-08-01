import logging
import traceback
import uuid

from jose import JWTError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.security import decode_access_token
from app.db.session import AsyncSessionLocal
from app.models.error_log import ErrorLog

logger = logging.getLogger(__name__)


def _extract_user_id(request: Request) -> uuid.UUID | None:
    """
    Best-effort extraction of the authenticated user id from the request's
    bearer token, without raising if the token is missing/invalid — this
    middleware must never itself become a source of unhandled errors.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header.split(" ", 1)[1].strip()
    try:
        payload = decode_access_token(token)
        user_id_raw = payload.get("sub")
        return uuid.UUID(user_id_raw) if user_id_raw else None
    except (JWTError, ValueError):
        return None


class ErrorLoggingMiddleware(BaseHTTPMiddleware):
    """
    Wraps every request. Any exception that isn't already handled further
    down the stack is caught here, persisted to the `error_logs` table with
    full context (timestamp, endpoint, method, message, stack trace,
    authenticated user id if any), and turned into a clean JSON 500
    response — the client never sees a raw traceback.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        request_id = str(uuid.uuid4())
        try:
            response = await call_next(request)
            return response
        except Exception as exc:  # noqa: BLE001
            stack_trace = traceback.format_exc()
            endpoint = request.url.path
            method = request.method
            user_id = _extract_user_id(request)

            logger.error(
                "Unhandled exception on %s %s (request_id=%s): %s",
                method,
                endpoint,
                request_id,
                exc,
            )

            try:
                async with AsyncSessionLocal() as db:
                    db.add(
                        ErrorLog(
                            endpoint=endpoint,
                            method=method,
                            error_message=str(exc)[:4000],
                            stack_trace=stack_trace[:8000],
                            user_id=user_id,
                            request_id=request_id,
                        )
                    )
                    await db.commit()
            except Exception:  # noqa: BLE001
                # If we can't even log the error (e.g. DB is down), fall
                # back to stdout logging so we don't lose the request cycle.
                logger.exception("Failed to persist error log to the database")

            return JSONResponse(
                status_code=500,
                content={
                    "error": "internal_server_error",
                    "message": "An unexpected error occurred. Our team has been notified.",
                    "request_id": request_id,
                },
            )
