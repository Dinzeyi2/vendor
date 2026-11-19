"""HTTP endpoints for prepaid credits.

- GET  /v1/account/credits            website backend (same auth + customer scoping as keys)
- POST /v1/account/credits/checkout   website backend: {"pack_id": "..."} -> Stripe Checkout URL
- POST /v1/stripe/webhook             Stripe only: signature verified against the raw body
- POST /v1/admin/credits/adjust       operator API_KEY only: support grants and corrections
"""
from __future__ import annotations

import os
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app import billing, db
from app.account_api import CUSTOMER_ID_RE, signed_in_customer

router = APIRouter()


def error_response(exc: billing.BillingError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"error": exc.code, "message": str(exc), **exc.extra})


def _call(fn, *args):
    try:
        return fn(*args)
    except billing.BillingError as exc:
        return error_response(exc)
    except db.DatabaseUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None


class CheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pack_id: str = Field(min_length=1, max_length=64)


@router.get("/v1/account/credits", tags=["account (website backend only)"])
def get_credits(customer_id: str = Depends(signed_in_customer)):
    return _call(billing.balance_summary, customer_id)


@router.post("/v1/account/credits/checkout", status_code=201, tags=["account (website backend only)"])
def create_checkout(body: CheckoutRequest, customer_id: str = Depends(signed_in_customer)):
    return _call(billing.create_checkout, customer_id, body.pack_id)


@router.post("/v1/stripe/webhook", include_in_schema=False)
async def stripe_webhook(request: Request, stripe_signature: Optional[str] = Header(default=None)):
    payload = await request.body()  # raw bytes: the signature covers the exact body Stripe sent
    if not db.configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "account storage not configured on server")
    try:  # blocking Postgres work runs in a thread
        return await run_in_threadpool(billing.handle_webhook, payload, stripe_signature)
    except billing.BillingError as exc:
        return error_response(exc)
    except db.DatabaseUnavailable as exc:  # 5xx makes Stripe retry later
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None


class AdjustRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: str = Field(pattern=CUSTOMER_ID_RE.pattern)
    delta: int = Field(ge=-1_000_000_000, le=1_000_000_000)
    note: str = Field(min_length=1, max_length=200)


def require_operator(x_api_key: Optional[str] = Header(default=None)) -> None:
    expected = os.environ.get("API_KEY", "")
    if not expected or not x_api_key or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "operator key required")


@router.post("/v1/admin/credits/adjust", dependencies=[Depends(require_operator)], include_in_schema=False)
def adjust_credits(body: AdjustRequest):
    if body.delta == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "delta must not be 0")
    if not db.configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "account storage not configured on server")
    result = _call(billing.adjust, body.customer_id, body.delta, body.note)
    return result if isinstance(result, JSONResponse) else {"customer_id": body.customer_id, "balance": result}
