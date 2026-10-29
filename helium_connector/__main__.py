import logging
import os

import uvicorn

from helium_connector.app import create_app
from helium_connector.settings import Settings

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    port = int(os.environ.get("PORT", "8001"))
    uvicorn.run(create_app(Settings.from_env()), host="127.0.0.1", port=port)
