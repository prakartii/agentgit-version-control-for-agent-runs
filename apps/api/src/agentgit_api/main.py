import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agentgit_api.routes import router
from agentgit_core import __version__

app = FastAPI(title="AgentGit API", version=__version__)
# The UI runs on another origin in development. The API is read-only, so GET is
# all that is allowed; set AGENTGIT_CORS_ORIGINS (comma separated) to change it.
_origins = os.environ.get("AGENTGIT_CORS_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_methods=["GET"])
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe: returns 200 if the process is up."""
    return {"status": "ok", "version": __version__}
