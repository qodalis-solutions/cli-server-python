from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import pty
import select
import signal
import struct
import fcntl
import termios

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class CliShellSessionManager:
    async def handle_session(
        self,
        websocket: WebSocket,
        cols: int,
        rows: int,
        command: str | None = None,
    ) -> None:
        shell, args = self._get_shell_info(command)
        master_fd: int | None = None
        child_pid: int | None = None

        try:
            child_pid, master_fd = pty.fork()

            if child_pid == 0:
                # Child process
                os.environ["TERM"] = "xterm-256color"
                if command:
                    os.execvp(shell, [shell] + args)
                else:
                    os.execvp(shell, [shell])
                # execvp never returns

            # Parent process
            self._set_pty_size(master_fd, cols, rows)

            detected_os = (
                "darwin" if platform.system() == "Darwin"
                else "win32" if platform.system() == "Windows"
                else "linux"
            )

            await websocket.send_text(json.dumps({
                "type": "ready",
                "shell": os.path.basename(shell),
                "os": detected_os,
            }))

            read_task = asyncio.ensure_future(
                self._read_pty_output(master_fd, websocket)
            )
            input_task = asyncio.ensure_future(
                self._read_websocket_input(websocket, master_fd)
            )
            exit_task = asyncio.ensure_future(
                self._wait_for_exit(child_pid, websocket)
            )

            done, pending = await asyncio.wait(
                [read_task, input_task, exit_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        except Exception as exc:
            try:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": str(exc),
                }))
            except Exception:
                pass
        finally:
            if master_fd is not None:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
            if child_pid is not None and child_pid > 0:
                try:
                    os.kill(child_pid, signal.SIGTERM)
                except OSError:
                    pass
                try:
                    os.waitpid(child_pid, os.WNOHANG)
                except ChildProcessError:
                    pass

    async def _read_pty_output(
        self, master_fd: int, websocket: WebSocket
    ) -> None:
        loop = asyncio.get_event_loop()
        try:
            while True:
                data = await loop.run_in_executor(
                    None, self._blocking_read, master_fd
                )
                if not data:
                    break
                await websocket.send_text(json.dumps({
                    "type": "stdout",
                    "data": data,
                }))
        except (OSError, asyncio.CancelledError):
            pass

    def _blocking_read(self, fd: int) -> str | None:
        try:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                data = os.read(fd, 4096)
                if not data:
                    return None
                return data.decode("utf-8", errors="replace")
            return ""
        except OSError:
            return None

    async def _read_websocket_input(
        self, websocket: WebSocket, master_fd: int
    ) -> None:
        try:
            while True:
                raw = await websocket.receive_text()
                msg = json.loads(raw)
                msg_type = msg.get("type")

                if msg_type == "stdin":
                    data = msg.get("data", "")
                    os.write(master_fd, data.encode("utf-8"))
                elif msg_type == "resize":
                    new_cols = msg.get("cols", 80)
                    new_rows = msg.get("rows", 24)
                    self._set_pty_size(master_fd, new_cols, new_rows)
        except Exception:
            pass

    async def _wait_for_exit(
        self, child_pid: int, websocket: WebSocket
    ) -> None:
        loop = asyncio.get_event_loop()
        try:
            _, status = await loop.run_in_executor(
                None, os.waitpid, child_pid, 0
            )
            exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
            await websocket.send_text(json.dumps({
                "type": "exit",
                "code": exit_code,
            }))
        except (ChildProcessError, asyncio.CancelledError):
            pass

    @staticmethod
    def _set_pty_size(fd: int, cols: int, rows: int) -> None:
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    @staticmethod
    def _get_shell_info(
        command: str | None,
    ) -> tuple[str, list[str]]:
        if platform.system() == "Windows":
            shell = "powershell.exe"
            return (shell, ["-Command", command]) if command else (shell, [])

        shell = "/bin/bash"
        return (shell, ["-c", command]) if command else (shell, [])
