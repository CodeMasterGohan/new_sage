# FastAPI 0.115.14

## Request body

Declare the body as a Pydantic BaseModel parameter: def create_item(item: Item): — FastAPI reads the request body as JSON, validates it against Item, and returns 422 with details when validation fails.

```python
from fastapi import FastAPI
from pydantic import BaseModel

class Item(BaseModel):
    name: str
    price: float
    in_stock: bool = True

app = FastAPI()

@app.post("/items/")
async def create_item(item: Item):
    return {"name": item.name, "price": item.price}
```

## BackgroundTasks

The signature is add_task(func, *args, **kwargs). FastAPI schedules func to run in the background after the response is sent, passing *args and **kwargs through to it.

```python
from fastapi import BackgroundTasks, FastAPI

app = FastAPI()

def write_log(message: str):
    with open("log.txt", "a") as f:
        f.write(message)

@app.post("/send-notification/{email}")
async def send_notification(email: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(write_log, f"notified {email}\n")
    return {"message": "Notification sent in the background"}
```

## Header parameters

Declare header parameters like query parameters: async def read_items(user_agent: Annotated[str | None, Header()] = None). FastAPI converts underscores to hyphens, so user_agent matches the User-Agent header.

## HTTP status codes

Set the response status code on the decorator: @app.post("/items/", status_code=201). Use status from fastapi for readable constants, e.g. status_code=status.HTTP_201_CREATED.
