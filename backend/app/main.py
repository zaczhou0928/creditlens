from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routes import router
from .config import BASE_DIR
from .demo_data import seed_demo_data


@asynccontextmanager
async def lifespan(_: FastAPI):
    database_dir = BASE_DIR / "data" / "databases" / "credit_ops"
    if not database_dir.exists() or not any(database_dir.glob("*.csv")):
        seed_demo_data(database_dir=database_dir)
    yield


app = FastAPI(
    title="CreditLens 零售信贷 Agent API",
    version="1.1.0",
    description="基于Schema RAG、模型Text2SQL、LangGraph和MCP的零售信贷开放问数与可信来源分析服务",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
