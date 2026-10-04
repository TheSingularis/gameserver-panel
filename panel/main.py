import os

import uvicorn

from .app import Settings, create_app

if __name__ == "__main__":
    uvicorn.run(create_app(Settings.from_env()), host="0.0.0.0",
                port=int(os.environ.get("PANEL_PORT", "8080")), log_level="info")
