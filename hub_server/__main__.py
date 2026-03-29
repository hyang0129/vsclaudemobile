"""Run the hub server with uvicorn."""

import uvicorn


def main():
    uvicorn.run(
        "hub_server.app:app",
        host="0.0.0.0",
        port=8420,
        log_level="info",
    )


if __name__ == "__main__":
    main()
