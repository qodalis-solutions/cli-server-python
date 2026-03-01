"""Demo CLI server with sample processors."""

from __future__ import annotations

import os
import sys

import uvicorn

# Allow importing from the project root so ``plugins`` is accessible.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from qodalis_cli import (
    CliServerOptions,
    create_cli_server,
    CliSystemCommandProcessor,
    CliHttpCommandProcessor,
    CliHashCommandProcessor,
    CliBase64CommandProcessor,
    CliUuidCommandProcessor,
    FileSystemOptions,
)

from processors import (
    CliEchoCommandProcessor,
    CliHelloCommandProcessor,
    CliMathCommandProcessor,
    CliStatusCommandProcessor,
    CliTimeCommandProcessor,
)

from plugins.weather import WeatherModule


def main() -> None:
    port = int(os.environ.get("PORT", "8048"))
    host = os.environ.get("HOST", "0.0.0.0")

    result = create_cli_server(
        CliServerOptions(
            configure=lambda builder: (
                builder
                .add_processor(CliEchoCommandProcessor())
                .add_processor(CliStatusCommandProcessor())
                .add_processor(CliTimeCommandProcessor())
                .add_processor(CliHelloCommandProcessor())
                .add_processor(CliMathCommandProcessor())
                .add_processor(CliSystemCommandProcessor())
                .add_processor(CliHttpCommandProcessor())
                .add_processor(CliHashCommandProcessor())
                .add_processor(CliBase64CommandProcessor())
                .add_processor(CliUuidCommandProcessor())
                .add_module(WeatherModule())
                .add_filesystem(FileSystemOptions(allowed_paths=["/tmp", "/app", "/home"]))
            ),
        )
    )

    print(f"Qodalis CLI Demo Server (Python) running on http://{host}:{port}")
    print(f"  API: http://{host}:{port}/api/cli")
    print(f"  WebSocket: ws://{host}:{port}/ws/cli/events")

    uvicorn.run(result.app, host=host, port=port)


if __name__ == "__main__":
    main()
