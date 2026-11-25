"""``host``: the server options and the environment variables (read-only, secrets masked)."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from qodalis_cli_server_abstractions.config import CliConfigEntry, CliConfigSection, ICliConfigProvider


#: The environment variables the ``host`` provider shows by default: the
#: server's own (``QCLI_*``) and a few harmless ones.  Everything else (PATH,
#: session ids, socket paths, cloud credentials, ...) stays out unless allowed.
DEFAULT_ENVIRONMENT_ALLOW: tuple[str, ...] = ("QCLI_*", "PYTHON_ENV", "ENVIRONMENT", "PORT", "HOST", "TZ", "LANG")


@dataclass
class CliHostEnvironmentOptions:
    """Which environment variables the ``host`` provider shows
    (``CliServerOptions.config.environment``)."""

    #: Further names to show, besides :data:`DEFAULT_ENVIRONMENT_ALLOW`; ``*`` matches any run of characters.
    allow: list[str] = field(default_factory=list)
    #: Names to show instead of the default list (and ``allow``): replaces it
    #: entirely, so ``["*"]`` shows every variable.  Values that look secret
    #: are masked either way.
    include: Optional[list[str]] = None
    #: Names to leave out, applied after the names shown (default none).
    exclude: list[str] = field(default_factory=list)
    #: Further names whose values are secrets (masked), besides the built-in rule.
    secrets: list[str] = field(default_factory=list)


@dataclass
class CliHostOption:
    """A server option the ``host`` provider lists."""

    key: str
    value: Any
    label: Optional[str] = None
    description: Optional[str] = None


#: Names treated as secrets: anything with SECRET, PASS (PASSWORD, PASSWD,
#: PASSPHRASE), PWD_, TOKEN, KEY, SESSION, CREDENTIAL, AUTH, PRIVATE, CONN
#: (CONNECTION, CONNSTR), DSN, COOKIE, SALT, SIGNATURE or CERT in it, anywhere
#: and in any case.  Broad on purpose: a harmless variable masked costs less
#: than a secret shown.
_SECRET_NAME = re.compile(
    r"(SECRET|PASS|PWD_|TOKEN|KEY|SESSION|CREDENTIAL|AUTH|PRIVATE|CONN|DSN|COOKIE|SALT|SIGNATURE|CERT)",
    re.IGNORECASE,
)

#: ``scheme://user:password@host``: a URL carrying credentials.
_URL_WITH_CREDENTIALS = re.compile(r"^[a-z][a-z0-9+.-]*://[^/\s@]*:[^/\s@]*@", re.IGNORECASE)


def _glob(pattern: str, name: str) -> bool:
    return fnmatch.fnmatchcase(name.lower(), pattern.lower())


def is_secret_environment_variable(name: str, value: Optional[str], extra: Sequence[str] = ()) -> bool:
    """True when an environment variable holds a secret: by its name, a configured name, or a URL with credentials."""
    if _SECRET_NAME.search(name):
        return True
    if any(_glob(pattern, name) for pattern in extra):
        return True
    return isinstance(value, str) and _URL_WITH_CREDENTIALS.match(value) is not None


def _type_of(value: Any) -> str:
    if isinstance(value, list):
        return "string[]"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "string"


class CliHostConfigProvider(ICliConfigProvider):
    """``host`` (read-only): the host's configuration.  Section ``options``:
    the options the server was created with; section ``environment``: the
    allowed environment variables (:data:`DEFAULT_ENVIRONMENT_ALLOW` plus
    ``allow``, or ``include``), secrets masked (``value: null``,
    ``isSet: true``; see :func:`is_secret_environment_variable`)."""

    id = "host"
    title = "Host"
    description = "Server options and environment variables (read-only, secrets masked)"
    order = 20

    def __init__(
        self,
        options: Callable[[], Sequence[CliHostOption]],
        environment: Optional[CliHostEnvironmentOptions] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._options = options
        environment = environment or CliHostEnvironmentOptions()
        self._include = (
            list(environment.include) if environment.include is not None
            else [*DEFAULT_ENVIRONMENT_ALLOW, *environment.allow]
        )
        self._exclude = list(environment.exclude)
        self._secrets = list(environment.secrets)
        self._env = env

    def get_sections(self, context: object = None) -> list[CliConfigSection]:
        env = self._env if self._env is not None else os.environ
        names = sorted(
            (n for n in env if any(_glob(p, n) for p in self._include) and not any(_glob(p, n) for p in self._exclude)),
            key=str.lower,
        )
        environment: list[CliConfigEntry] = []
        for name in names:
            value = env.get(name)
            if is_secret_environment_variable(name, value, self._secrets):
                environment.append(CliConfigEntry(
                    key=name, type="secret", value=None, is_set=bool(value), sensitive=True, source="environment",
                ))
            else:
                environment.append(CliConfigEntry(key=name, type="string", value=value or "", source="environment"))
        options = [
            CliConfigEntry(key=o.key, label=o.label, description=o.description, type=_type_of(o.value), value=o.value)
            for o in self._options()
        ]
        return [
            CliConfigSection(
                id="options", title="Server options",
                description="The options the server was created with", entries=options,
            ),
            CliConfigSection(
                id="environment", title="Environment variables",
                description="Secrets are masked", entries=environment,
            ),
        ]
