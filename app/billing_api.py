"""HTTP endpoints for prepaid credits.

- POST /v1/credits/grant          website server only, after it confirms a payment:
                                  Authorization: Bearer <CREDITS_GRANT_SECRET>
                                  {"customer_id", "credits", "payment_id", optional "type": "paid"|"welcome",
                                   optional "purchased_at"}; exactly once per payment_id
- GET  /v1/admin/credits/expiry-report  operator: how legacy balances would be handled (read-only)
- GET  /v1/account/credits        website backend (same auth + customer scoping as keys)
- POST /v1/admin/credits/adjust   operator API_KEY only: support grants and corrections
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import billing, db
from app.account_api import CUSTOMER_ID_RE, MIN_SECRET_LEN, signed_in_customer

router = APIRouter()


def error_response(exc: billing.BillingError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"error": exc.code, "message": str(exc), **exc.extra})


def _call(fn, *args):
    try:
        return fn(*args)
    except billing.BillingError as exc:
        return error_response(exc)
    except db.DatabaseUnavailable as exc:  # 503: callers should retry later
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None


def jsonable(value):
    return jsonable_encoder(value)


def _require_db() -> None:
    if not db.configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "account storage not configured on server")


# -- website server: credit grants ------------------------------------------------------------
def require_grant_secret(authorization: Optional[str] = Header(default=None)) -> None:
    expected = os.environ.get(billing.GRANT_SECRET_ENV, "")
    others = {os.environ.get("WEBSITE_BACKEND_SECRET", ""), os.environ.get("API_KEY", "")}
    if len(expected) < MIN_SECRET_LEN or expected in others:  # must be its own, strong secret
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "credit grants not configured on server")
    scheme, _, presented = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(presented.strip().encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid grant credential",
                            headers={"WWW-Authenticate": "Bearer"})


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: str = Field(pattern=CUSTOMER_ID_RE.pattern,
                             description="The paying customer's ID (the same ID you send as X-Customer-Id)")
    credits: int = Field(gt=0, le=billing.MAX_GRANT)
    payment_id: str = Field(pattern=billing.PAYMENT_ID_RE.pattern,
                            description="Your unique ID for this payment, e.g. a Stripe Checkout session ID")
    type: Literal["paid", "welcome"] = Field(default="paid", description="paid (expires) or welcome (free)")
    purchased_at: Optional[datetime] = Field(
        default=None, description="Paid grants: the verified purchase time, ISO 8601 with a timezone. "
                                  "If omitted (older integrations), Railway's receive time is used.")

    @field_validator("purchased_at")
    @classmethod
    def must_have_timezone(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is not None and value.utcoffset() is None:
            raise ValueError("purchased_at needs a timezone, e.g. 2026-11-19T15:04:05Z")
        return value


@router.post("/v1/credits/grant", tags=["credits (website server only)"], dependencies=[Depends(require_grant_secret)],
             responses={200: {"description": "already_credited: this payment_id was granted before; nothing added"},
                        201: {"description": "credited"},
                        409: {"description": "payment_id_conflict: same payment_id, different customer or amount"}})
def grant_credits(body: GrantRequest):
    _require_db()
    try:
        result = billing.grant(body.customer_id, body.credits, body.payment_id, body.type, body.purchased_at)
    except billing.BillingError as exc:
        return error_response(exc)
    except db.DatabaseUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    return JSONResponse(status_code=201 if result["status"] == "credited" else 200, content=jsonable(result))


# -- website backend: balance ----------------------------------------------------------------
@router.get("/v1/account/credits", tags=["account (website backend only)"])
def get_credits(customer_id: str = Depends(signed_in_customer)):
    return _call(billing.balance_summary, customer_id)


# -- operator ----------------------------------------------------------------------------------
class AdjustRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: str = Field(pattern=CUSTOMER_ID_RE.pattern)
    delta: int = Field(ge=-billing.MAX_GRANT, le=billing.MAX_GRANT)
    note: str = Field(min_length=1, max_length=200)


def require_operator(x_api_key: Optional[str] = Header(default=None)) -> None:
    expected = os.environ.get("API_KEY", "")
    if not expected or not x_api_key or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "operator key required")


@router.get("/v1/admin/credits/expiry-report", dependencies=[Depends(require_operator)], include_in_schema=False)
def expiry_report():
    """Read-only: how pre-expiration (legacy) balances would be handled under the one-year rule."""
    _require_db()
    return _call(billing.expiry_report)


@router.post("/v1/admin/credits/adjust", dependencies=[Depends(require_operator)], include_in_schema=False)
def adjust_credits(body: AdjustRequest):
    if body.delta == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "delta must not be 0")
    _require_db()
    result = _call(billing.adjust, body.customer_id, body.delta, body.note)
    return result if isinstance(result, JSONResponse) else {"customer_id": body.customer_id, "balance": result}
