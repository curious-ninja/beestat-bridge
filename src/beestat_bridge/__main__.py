import logging

import uvicorn

from .main import create_app
from .settings import load_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

settings = load_settings()
uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port)
