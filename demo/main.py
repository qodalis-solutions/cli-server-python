"""Demo CLI server with sample processors."""

from __future__ import annotations

import os

import uvicorn

from qodalis_cli import (
    CliServerOptions,
    create_cli_server,
    CliSystemCommandProcessor,
    CliHttpCommandProcessor,
    CliHashCommandProcessor,
    CliBase64CommandProcessor,
    CliUuidCommandProcessor,
)

from processors import (
    CliEchoCommandProcessor,
    CliHelloCommandProcessor,
    CliMathCommandProcessor,
    CliStatusCommandProcessor,
    CliTimeCommandProcessor,
)


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
            ),
        )
    )

    print(f"Qodalis CLI Demo Server (Python) running on http://{host}:{port}")
    print(f"  API: http://{host}:{port}/api/cli")
    print(f"  WebSocket: ws://{host}:{port}/ws/cli/events")

    uvicorn.run(result.app, host=host, port=port)


if __name__ == "__main__":
    main()
