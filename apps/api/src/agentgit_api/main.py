from fastapi import FastAPI

from agentgit_api.routes import router
from agentgit_core import __version__

app = FastAPI(title="AgentGit API", version=__version__)
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe: returns 200 if the process is up."""
    return {"status": "ok", "version": __version__}
