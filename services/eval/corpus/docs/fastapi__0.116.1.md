# FastAPI 0.116.1

## Dependencies with Depends

Declare a dependency with a function parameter using Depends: def read_items(commons: Annotated[dict, Depends(common_parameters)]): — FastAPI calls common_parameters() for each request and passes the returned value into your path operation.

```python
from typing import Annotated
from fastapi import Depends, FastAPI

app = FastAPI()

async def common_parameters(q: str | None = None, skip: int = 0, limit: int = 100):
    return {"q": q, "skip": skip, "limit": limit}

@app.get("/items/")
async def read_items(commons: Annotated[dict, Depends(common_parameters)]):
    return commons
```

## Lifespan events

The @app.on_event('startup') and @app.on_event('shutdown') decorators are deprecated since FastAPI 0.93.0. Use the lifespan async context manager instead: pass lifespan=lifespan to FastAPI() and yield between startup and shutdown code.

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    yield
    # shutdown

app = FastAPI(lifespan=lifespan)
```

## Query parameters

The signature is Query(default=Undefined, *, alias=None, title=None, description=None, deprecated=None, include_in_schema=True, max_length=None, min_length=None, pattern=None). In practice you write q: Annotated[str | None, Query(max_length=50)] = None.

## Path parameters

Declare path parameters with the same syntax as Python format strings: @app.get("/items/{item_id}"). The value is passed to your function as item_id, and you can declare its type (int, str, UUID) for automatic validation.

## Response model

Declare a response_model on the decorator to filter and document the output: @app.post("/items/", response_model=Item). FastAPI validates the returned data against the model and generates the OpenAPI schema from it.
