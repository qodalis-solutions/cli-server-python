"""Standalone CLI server entry point."""

from __future__ import annotations

import os

import uvicorn

from .create_cli_server import CliServerOptions, create_cli_server
from .processors import (
    CliEchoCommandProcessor,
    CliStatusCommandProcessor,
    CliSystemCommandProcessor,
    CliHttpCommandProcessor,
    CliHashCommandProcessor,
    CliBase64CommandProcessor,
    CliUuidCommandProcessor,
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
                .add_processor(CliSystemCommandProcessor())
                .add_processor(CliHttpCommandProcessor())
                .add_processor(CliHashCommandProcessor())
                .add_processor(CliBase64CommandProcessor())
                .add_processor(CliUuidCommandProcessor())
            ),
        )
    )

    uvicorn.run(result.app, host=host, port=port)


if __name__ == "__main__":
    main()
