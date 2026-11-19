"""Unified algorithm API."""
from __future__ import annotations

import os
import secrets
from typing import Any, Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from app.operations import OPERATIONS, OperationError

app = FastAPI(title="Unified Algorithm API", version="0.1.0")

API_KEY_ENV = "API_KEY"
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(provided: Optional[str] = Security(_api_key_header)) -> None:
    """Require the X-API-Key header to match the API_KEY environment variable.

    Fails closed: if API_KEY is not configured, the endpoint is unavailable.
    """
    expected = os.environ.get(API_KEY_ENV)
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="API key not configured on server")
    if not provided or not secrets.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid or missing API key",
                            headers={"WWW-Authenticate": "APIKey"})


class Task(BaseModel):
    id: str
    operation: str
    inputs: Dict[str, Any] = Field(default_factory=dict)


class ExecuteRequest(BaseModel):
    tasks: List[Task] = Field(min_length=1)


class TaskResult(BaseModel):
    id: str
    operation: str
    status: Literal["ok", "error"]
    output: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class ExecuteResponse(BaseModel):
    results: List[TaskResult]


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/execute", response_model=ExecuteResponse, dependencies=[Depends(require_api_key)])
def execute(request: ExecuteRequest) -> ExecuteResponse:
    results: List[TaskResult] = []
    for task in request.tasks:
        handler = OPERATIONS.get(task.operation)
        if handler is None:
            results.append(TaskResult(id=task.id, operation=task.operation, status="error",
                                      error=f"unknown operation '{task.operation}'"))
            continue
        try:
            output = handler(task.inputs)
        except OperationError as exc:
            results.append(TaskResult(id=task.id, operation=task.operation, status="error", error=str(exc)))
            continue
        results.append(TaskResult(id=task.id, operation=task.operation, status="ok", output=output))
    return ExecuteResponse(results=results)
