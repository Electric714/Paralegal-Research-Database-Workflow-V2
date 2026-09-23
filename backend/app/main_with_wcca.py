from __future__ import annotations

from .main import app
from .run_control_api import router as run_control_router
from .wcca_api import router as wcca_router

# app.main mounts the built React frontend at "/" as its final route. Move that
# catch-all mount behind the additional API routes so they remain reachable in
# the packaged one-process launcher.
frontend_mounts = [route for route in app.router.routes if getattr(route, "name", None) == "frontend"]
for route in frontend_mounts:
    app.router.routes.remove(route)

app.include_router(wcca_router)
app.include_router(run_control_router)
app.router.routes.extend(frontend_mounts)
