import time
import logging
import uuid
import json
import base64
from collections import defaultdict
from contextvars import ContextVar
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from app.core.config import settings

logger = logging.getLogger("uvicorn.access")
logger.disabled = True

request_logger = logging.getLogger("request_logger")
request_logger.setLevel(logging.INFO)

# Context variables for request tracking
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")
user_id_var: ContextVar[str] = ContextVar("user_id", default="")


RATE_LIMIT_REQUESTS = 100
RATE_LIMIT_WINDOW = 60

rate_limit_storage = defaultdict(list)






def register_middleware(app: FastAPI):

    # Allow both localhost and 127.0.0.1 origins for frontend  
    allow_origins = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8080",  # Alternative frontend port
        "http://127.0.0.1:8080",  # Alternative frontend port
        "https://localhost:3000",  # HTTPS variants
        "https://127.0.0.1:3000",
    ]
    
    # ✅ OPTIMIZATION: Add GZIP compression middleware
    # Reduces response size by 60-80% for JSON responses
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        allow_headers=["*"],
        allow_credentials=True,  # Now safe since we have specific origins
    )

    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.ALLOWED_HOSTS,
    )
    
  
