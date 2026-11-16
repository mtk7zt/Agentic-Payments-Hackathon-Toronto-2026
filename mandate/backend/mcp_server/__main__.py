"""Run the local Streamable HTTP endpoint."""
import uvicorn

from .server import Config, create_app


def main() -> None:
    config = Config.from_env()
    uvicorn.run(create_app(config), host=config.host, port=config.port, access_log=False)


if __name__ == "__main__":
    main()
