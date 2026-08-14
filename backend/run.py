"""
Entry point — launches the app using host/port/reload from config.yaml.

    python run.py            # or: uv run python run.py

Everything else (embedding backend, catalog, classifier, …) also comes from
config.yaml, so there are no environment variables to remember.
"""
import uvicorn

import config


def main():
    uvicorn.run(
        "main:app",
        host=config.SERVER_HOST,
        port=config.SERVER_PORT,
        reload=config.SERVER_RELOAD,
    )


if __name__ == "__main__":
    main()
