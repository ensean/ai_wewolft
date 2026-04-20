import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.sse import SSEManager
from backend.api.routes import create_router

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)

app = FastAPI(title="AI 狼人杀", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Shared singletons
sse_manager = SSEManager()
engine_holder: dict = {}  # {"engine": GameEngine, "task": asyncio.Task}

app.include_router(create_router(sse_manager, engine_holder))
