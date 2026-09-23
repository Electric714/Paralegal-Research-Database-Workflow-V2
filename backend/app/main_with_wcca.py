from __future__ import annotations

from .main import app
from .research.run_control import recover_interrupted_runs
from .run_control_api import router as run_control_router
from .wcca_api import router as wcca_router

# app.main mounts the built React frontend at "/" as its final route. Move that
# catch-all mount behind the additional API routes so they remain reachable in the
# packaged one-process launcher.
frontend_mounts = [route for route in app.router.routes if getattr(route, "name", None) == "frontend"]
for route in frontend_mounts:
    app.router.routes.remove(route)

app.include_router(wcca_router)
app.include_router(run_control_router)
app.router.routes.extend(frontend_mounts)


@app.on_event("startup")
def recover_interrupted_research_runs() -> None:
    # app.main's startup handler initializes the database first. Any run still marked
    # active after a process restart cannot have a live executor thread, so make it
    # explicitly resumable/stopped instead of leaving a zombie "running" status.
    recover_interrupted_runs()
