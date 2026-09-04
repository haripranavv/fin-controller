from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes_auth import router as auth_router
from app.api.routes_batches import router as batches_router
from app.api.routes_cases import router as cases_router
from app.api.routes_exceptions import router as exceptions_router
from app.api.routes_import import router as import_router
from app.api.routes_overview import router as overview_router
from app.api.routes_runs import router as runs_router

app = FastAPI(title="AI Finance Controller", version="0.1.0")

# Local dev only: the Vite dev server (frontend/) runs on a different
# origin. No auth/roles in scope (PROJECT_SPEC.md section 19), so this is
# intentionally permissive for local operator-console use, not a public API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:5174", "http://127.0.0.1:5174",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(overview_router)
app.include_router(batches_router)
app.include_router(cases_router)
app.include_router(exceptions_router)
app.include_router(runs_router)
app.include_router(auth_router)
app.include_router(import_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
