import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.events = defaultdict(deque)

    async def dispatch(self, request, call_next):
        path = request.url.path
        limits = {
            "/admin/login": (5, 60),
            "/oauth/token": (20, 60),
            "/oauth/register": (10, 60),
            "/oauth/authorize": (20, 60),
        }
        if path in limits:
            limit, window = limits[path]
            ip = (
                request.headers.get("cf-connecting-ip")
                or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                or (request.client.host if request.client else "unknown")
            )
            key = (ip, path)
            now = time.monotonic()
            bucket = self.events[key]
            while bucket and bucket[0] <= now - window:
                bucket.popleft()
            if len(bucket) >= limit:
                return JSONResponse(
                    {"error": "rate_limited"}, 429, headers={"Retry-After": str(window)}
                )
            bucket.append(now)
        return await call_next(request)
