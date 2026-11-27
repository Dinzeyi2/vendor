"""Fixture: a FastAPI service with one real endpoint (plus a health check)."""
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Reverser")


class Text(BaseModel):
    text: str = "example"


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/reverse", summary="Reverse a piece of text")
def reverse(body: Text):
    return {"reversed": body.text[::-1], "length": len(body.text)}
