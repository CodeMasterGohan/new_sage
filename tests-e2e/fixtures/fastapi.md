# FastAPI {VERSION}

FastAPI {VERSION} is a modern, fast (high-performance) web framework for
building APIs with Python 3.8+ based on standard Python type hints.

## Getting Started

Install FastAPI {VERSION} and start building your first API in minutes.
The interactive documentation is generated automatically from your code.

```python
from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def read_root():
    return {"hello": "world", "version": "{VERSION}"}
```

## Path Parameters

In FastAPI {VERSION} you declare path parameters with the same syntax as
Python format strings. They are validated and documented automatically.

```python
@app.get("/items/{item_id}")
def read_item(item_id: int):
    return {"item_id": item_id, "version": "{VERSION}"}
```
