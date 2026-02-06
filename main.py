from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from routers import marketplace_router

app = FastAPI(title="SAFE STAKE")

app.include_router(marketplace_router)
app.mount("/static", StaticFiles(directory="static"), name="static")

