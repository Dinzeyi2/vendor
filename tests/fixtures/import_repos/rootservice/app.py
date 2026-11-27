"""Fixture: a Flask-style service whose only page is the root (FastAPI here, no extra dependencies)."""
from fastapi import FastAPI

app = FastAPI()


@app.get("/")
def index():
    return {"message": "Hello from the root"}
