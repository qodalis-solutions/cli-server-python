"""Server plugins in tests: the fixture plugin (``tests/fixtures/server_plugin_hello``) as an installed package or a
wheel, a fake pip that installs wheels by unzipping them, and the fake marketplace and PyPI of the server
marketplace (no network, no pip)."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import sys
import zipfile
from typing import Any, Optional

from qodalis_cli_admin import MarketplaceResponse, PipRunResult

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "server_plugin_hello")
HELLO_PACKAGE = "qcli-hello-plugin"
HELLO_MODULE = "qcli_hello_plugin"
HELLO_VERSION = "1.2.0"
MARKET = "https://market.test"
API = f"{MARKET}/api/v1/qcli/marketplace"
PYPI = "https://pypi.test"
FILES = "https://files.test"
SERVER_VERSION = "1.0.0b31"


def metadata_text(version: str = HELLO_VERSION, requires: Optional[list[str]] = None, name: str = HELLO_PACKAGE) -> str:
    """The fixture's METADATA with another version or other requirements."""
    lines = [
        "Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}",
        "Summary: A tiny server plugin installed at runtime (loader tests)",
    ]
    reqs = ["qodalis-cli-server-abstractions>=1.0.0b7", "fastapi>=0.100"] if requires is None else requires
    lines += [f"Requires-Dist: {r}" for r in reqs]
    return "\n".join(lines) + "\n"


def entry_points_text() -> str:
    with open(os.path.join(FIXTURE, "entry_points.txt"), encoding="utf-8") as f:
        return f.read()


def install_fixture(plugins_dir: str, version: str = HELLO_VERSION, entry_points: Optional[str] = None) -> None:
    """The fixture package as ``pip install --target`` leaves it: the module and its ``.dist-info`` (with RECORD)."""
    os.makedirs(plugins_dir, exist_ok=True)
    target = os.path.join(plugins_dir, HELLO_MODULE)
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(os.path.join(FIXTURE, HELLO_MODULE), target, ignore=shutil.ignore_patterns("__pycache__"))
    info = os.path.join(plugins_dir, f"{HELLO_MODULE}-{version}.dist-info")
    os.makedirs(info, exist_ok=True)
    files = {
        "METADATA": metadata_text(version),
        "entry_points.txt": entry_points_text() if entry_points is None else entry_points,
    }
    for name, text in files.items():
        with open(os.path.join(info, name), "w", encoding="utf-8") as f:
            f.write(text)
    record = [f"{HELLO_MODULE}/{n},," for n in os.listdir(target) if n.endswith(".py")]
    record += [f"{HELLO_MODULE}-{version}.dist-info/{n},," for n in (*files, "RECORD")]
    with open(os.path.join(info, "RECORD"), "w", encoding="utf-8") as f:
        f.write("\n".join(record) + "\n")


def write_installed(plugins_dir: str, records: list[dict[str, Any]]) -> None:
    os.makedirs(plugins_dir, exist_ok=True)
    with open(os.path.join(plugins_dir, "installed.json"), "w", encoding="utf-8") as f:
        json.dump({"plugins": records}, f)


def read_installed(plugins_dir: str) -> list[dict[str, Any]]:
    with open(os.path.join(plugins_dir, "installed.json"), encoding="utf-8") as f:
        return json.load(f)["plugins"]


def hello_record(**extra: Any) -> dict[str, Any]:
    return {"name": "hello", "package": HELLO_PACKAGE, "version": HELLO_VERSION, "integrity": "sha256-x", **extra}


def forget_fixture_modules() -> None:
    """Drops the fixture's imported modules and the plugins directories from ``sys.path`` (each test has its own)."""
    for name in [m for m in sys.modules if m == HELLO_MODULE or m.startswith(HELLO_MODULE + ".")]:
        del sys.modules[name]
    sys.path[:] = [p for p in sys.path if not p.endswith("server-plugins")]


def make_wheel(
    version: str = HELLO_VERSION, *, requires: Optional[list[str]] = None, entry_points: Optional[str] = None,
    name: str = HELLO_PACKAGE,
) -> bytes:
    """A wheel of the fixture plugin."""
    out = io.BytesIO()
    info = f"{HELLO_MODULE}-{version}.dist-info"
    with zipfile.ZipFile(out, "w") as z:
        module_dir = os.path.join(FIXTURE, HELLO_MODULE)
        names: list[str] = []
        for n in sorted(os.listdir(module_dir)):
            if n.endswith(".py"):
                with open(os.path.join(module_dir, n), encoding="utf-8") as f:
                    z.writestr(f"{HELLO_MODULE}/{n}", f.read())
                names.append(f"{HELLO_MODULE}/{n}")
        z.writestr(f"{info}/METADATA", metadata_text(version, requires, name))
        z.writestr(f"{info}/entry_points.txt", entry_points_text() if entry_points is None else entry_points)
        z.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nGenerator: tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        names += [f"{info}/METADATA", f"{info}/entry_points.txt", f"{info}/WHEEL", f"{info}/RECORD"]
        z.writestr(f"{info}/RECORD", "\n".join(f"{n},," for n in names) + "\n")
    return out.getvalue()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sri(data: bytes) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(data).digest()).decode()


def response(status: int, body: Any) -> MarketplaceResponse:
    data = body if isinstance(body, bytes) else body.encode() if isinstance(body, str) else json.dumps(body).encode()
    return MarketplaceResponse(status, data, {"content-length": str(len(data))})


class FakePip:
    """``pip install --target <dir> --no-deps <wheel>`` unzips the wheel there; other installs are recorded (and
    leave a ``.dist-info`` per requirement)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail: Optional[str] = None
        #: The requirements file of the last hash-checked install.
        self.requirements: Optional[str] = None
        #: Extra distributions a dependency install brings (``name``), as pip resolving a tree would.
        self.brings: list[str] = []

    async def __call__(self, args: list[str]) -> PipRunResult:
        self.calls.append(list(args))
        if self.fail:
            return PipRunResult(1, "", f"ERROR: {self.fail}")
        target = args[args.index("--target") + 1]
        os.makedirs(target, exist_ok=True)
        wheels = [a for a in args if a.endswith(".whl")]
        if wheels:
            with zipfile.ZipFile(wheels[0]) as z:
                z.extractall(target)
            return PipRunResult(0, "", "")
        reqs = [a for a in args[args.index("--target") + 2:] if not a.startswith("-") and a != ":all:"]
        if "-r" in args:
            path = args[args.index("-r") + 1]
            with open(path, encoding="utf-8") as f:
                self.requirements = f.read()
            reqs = [line.split(" ")[0].replace("==", ">=") for line in self.requirements.splitlines() if line.strip()]
        for req in reqs:
            dist = req.split(">")[0].split("=")[0].split("<")[0].split("[")[0].strip().replace("-", "_")
            os.makedirs(os.path.join(target, f"{dist}-9.9.9.dist-info"), exist_ok=True)
            with open(os.path.join(target, f"{dist}-9.9.9.dist-info", "METADATA"), "w") as f:
                f.write(f"Metadata-Version: 2.1\nName: {dist}\nVersion: 9.9.9\n")
            with open(os.path.join(target, f"{dist}-9.9.9.dist-info", "RECORD"), "w") as f:
                f.write(f"{dist}-9.9.9.dist-info/METADATA,,\n")
        for dist in self.brings:
            os.makedirs(os.path.join(target, f"{dist}-9.9.9.dist-info"), exist_ok=True)
            with open(os.path.join(target, f"{dist}-9.9.9.dist-info", "RECORD"), "w") as f:
                f.write(f"{dist}-9.9.9.dist-info/RECORD,,\n")
        return PipRunResult(0, "", "")


class FakeServerNet:
    """The marketplace's server plugins, PyPI's JSON API and its files, in memory."""

    def __init__(self) -> None:
        self.plugins: dict[str, dict[str, Any]] = {}
        self.releases: dict[tuple[str, str], dict[str, Any]] = {}
        self.files: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.marketplace_down = False

    def publish(
        self, name: str = "hello", *, versions: Optional[list[str]] = None, package: str = HELLO_PACKAGE,
        verified: bool = True, platforms: Optional[list[str]] = None, server_range: Optional[str] = ">=1.0.0b7",
        wheel: Optional[Any] = None, marketplace_integrity: Any = "same", auto_install: Optional[bool] = None,
        needs_configuration: Optional[list[str]] = None, dependencies: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        """A plugin with a Python package (and others listed); each version's wheel on PyPI (``wheel(version)``)."""
        versions = versions or [HELLO_VERSION]
        platforms = platforms or ["node", "python"]
        item: dict[str, Any] = {
            "name": name, "kind": "server", "title": name.title(), "description": f"The {name} plugin", "icon": "👋",
            "publisher": {"name": "acme", "verified": verified},
            "platforms": {
                p: {"package": package if p == "python" else f"@acme/{name}", "registry": p, "latest": versions[-1]}
                for p in platforms
            },
            "versions": [],
        }
        for v in versions:
            data = wheel(v) if wheel else make_wheel(v)
            filename = f"{HELLO_MODULE}-{v}-py3-none-any.whl"
            url = f"{FILES}/{filename}"
            self.files[url] = data
            self.releases[(package, v)] = {"info": {"name": package, "version": v}, "urls": [{
                "filename": filename, "url": url, "packagetype": "bdist_wheel", "digests": {"sha256": sha256_hex(data)},
            }]}
            for p in platforms:
                record: dict[str, Any] = {"version": v, "platform": p, "status": "listed", "serverRange": server_range}
                if p == "python":
                    record["integrity"] = sri(data) if marketplace_integrity == "same" else marketplace_integrity
                    if auto_install is not None:
                        record["autoInstall"] = auto_install
                        if not auto_install:
                            record["autoInstallReason"] = "needs the host's code"
                    if needs_configuration:
                        record["needsConfiguration"] = needs_configuration
                    if dependencies is not None:
                        record["dependencies"] = dependencies
                item["versions"].append(record)
        self.plugins[name] = item

    async def fetch(self, url: str, headers: Any, timeout: float) -> MarketplaceResponse:
        self.calls.append(url)
        if url.startswith(MARKET):
            if self.marketplace_down:
                return response(503, "down")
            from urllib.parse import parse_qs, unquote, urlsplit

            parts = urlsplit(url)
            path = parts.path[len("/api/v1/qcli/marketplace"):]
            if path == "/plugins":
                q = parse_qs(parts.query).get("query", [""])[0]
                items = [
                    {k: v for k, v in p.items() if k != "versions"}
                    for p in self.plugins.values() if not q or q in p["name"]
                ]
                return response(200, {"items": items, "total": len(items), "page": 1, "pageSize": 50})
            if path.startswith("/plugins/"):
                plugin = self.plugins.get(unquote(path[len("/plugins/"):]))
                return response(200, plugin) if plugin else response(404, {"error": "not found"})
            return response(404, {"error": "not found"})
        if url.startswith(PYPI):
            parts = url[len(PYPI) + len("/pypi/"):].split("/")
            release = self.releases.get((parts[0], parts[1]))
            return response(200, release) if release else response(404, {"message": "Not Found"})
        if url in self.files:
            return response(200, self.files[url])
        return response(404, "not found")
