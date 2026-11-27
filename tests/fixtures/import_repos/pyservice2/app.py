"""Fixture: a FastAPI service with two plausible endpoints (needs a choice)."""
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class Text(BaseModel):
    text: str


@app.post("/upper")
def upper(body: Text):
    return {"text": body.text.upper()}


@app.post("/lower")
def lower(body: Text):
    return {"text": body.text.lower()}
