"""Account endpoints for the website backend (server-to-server only).

Auth: ``Authorization: Bearer <WEBSITE_BACKEND_SECRET>``. The signed-in customer is named by
``X-Customer-Id``, which the website backend must take from its own verified session. The
customer ID is accepted only alongside the server credential -- never from a URL or body --
and every query is scoped to it. Customer keys and the operator API_KEY cannot call these.
"""
from __future__ import annotations

import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app import accounts, db

SECRET_ENV = "WEBSITE_BACKEND_SECRET"
MIN_SECRET_LEN = 32
CUSTOMER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:|@+\-]{0,127}$")

router = APIRouter(prefix="/v1/account", tags=["account (website backend only)"])


def signed_in_customer(authorization: Optional[str] = Header(default=None),
                       x_customer_id: Optional[str] = Header(default=None)) -> str:
    expected = os.environ.get(SECRET_ENV, "")
    if len(expected) < MIN_SECRET_LEN:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "account API not configured on server")
    scheme, _, presented = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(presented.strip().encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid server credential",
                            headers={"WWW-Authenticate": "Bearer"})
    if not x_customer_id or not CUSTOMER_ID_RE.fullmatch(x_customer_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "X-Customer-Id header is required: the signed-in customer's ID from your session "
                            "(1-128 chars: letters, digits, _ . : | @ + -)")
    if not db.configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "account storage not configured on server")
    return x_customer_id


def _db_call(fn, *args):
    try:
        return fn(*args)
    except db.DatabaseUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None


# -- models ------------------------------------------------------------------
class CreateKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # e.g. a smuggled "customer_id" is rejected (422)
    name: str = Field(min_length=1, max_length=100)


class KeyInfo(BaseModel):
    id: str
    name: str
    prefix: str
    status: str
    created_at: datetime
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


class CreatedKey(KeyInfo):
    secret: str = Field(description="The full API key. Returned only in this response; it cannot be retrieved later.")


class KeyList(BaseModel):
    keys: List[KeyInfo]


# -- endpoints ---------------------------------------------------------------
@router.post("/keys", response_model=CreatedKey, status_code=status.HTTP_201_CREATED)
def create_key(body: CreateKeyRequest, customer_id: str = Depends(signed_in_customer)):
    try:
        return _db_call(accounts.create_key, customer_id, body.name.strip() or "key")
    except accounts.TooManyKeys as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@router.get("/keys", response_model=KeyList)
def list_keys(customer_id: str = Depends(signed_in_customer)):
    return {"keys": _db_call(accounts.list_keys, customer_id)}


@router.post("/keys/{key_id}/revoke", response_model=KeyInfo)
def revoke_key(key_id: uuid.UUID, customer_id: str = Depends(signed_in_customer)):
    key = _db_call(accounts.revoke_key, customer_id, key_id)
    if key is None:  # not found, or another customer's key: same answer either way
        raise HTTPException(status.HTTP_404_NOT_FOUND, "key not found")
    return key


@router.delete("/keys/{key_id}", response_model=KeyInfo, include_in_schema=False)
def revoke_key_delete(key_id: uuid.UUID, customer_id: str = Depends(signed_in_customer)):
    return revoke_key(key_id, customer_id)


@router.get("/usage")
def usage(customer_id: str = Depends(signed_in_customer),
          start: Optional[datetime] = Query(default=None, description="ISO 8601; default 30 days before end"),
          end: Optional[datetime] = Query(default=None, description="ISO 8601, exclusive; default now"),
          key_id: Optional[uuid.UUID] = Query(default=None, description="limit to one of this customer's keys"),
          recent: int = Query(default=20, ge=0, le=100, description="how many recent requests to itemize")):
    end = _aware(end) if end else datetime.now(timezone.utc)
    start = _aware(start) if start else end - timedelta(days=30)
    if start >= end:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "start must be before end")
    if end - start > timedelta(days=366):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "range can be at most 366 days")
    if key_id and not any(k["id"] == str(key_id) for k in _db_call(accounts.list_keys, customer_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "key not found")
    return _db_call(accounts.usage_report, customer_id, start, end, key_id, recent)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
