"""``jobs``: the history retention defaults, changeable at runtime."""

from __future__ import annotations

from typing import Any, Callable, Optional

from qodalis_cli_server_abstractions.config import (
    CliConfigSectionDefinition,
    CliConfigSettingDefinition,
    CliConfigValidation,
    CliStoredConfigProvider,
)
from qodalis_cli_server_abstractions.jobs import JobHistoryRetention
from qodalis_cli_server_abstractions.jobs.job_history_retention import format_duration, parse_duration

from .cli_job_scheduler import CliJobScheduler

_FIELDS = {
    "maxAge": "max_age",
    "maxRunsPerJob": "max_runs_per_job",
    "failedMaxAge": "failed_max_age",
    "failedMaxRunsPerJob": "failed_max_runs_per_job",
}


def _as_duration(value: Any) -> Optional[str]:
    try:
        return format_duration(parse_duration(value))
    except ValueError:
        return None


def _duration_rule(value: Any) -> Optional[str]:
    try:
        parse_duration(value)
        return None
    except ValueError:
        return "must be a duration in s, m, h or d (e.g. '30d')"


def _count_rule(value: Any) -> Optional[str]:
    return None if isinstance(value, int) or (isinstance(value, float) and value.is_integer()) else "must be a whole number"


def create_jobs_config_provider(scheduler: CliJobScheduler) -> CliStoredConfigProvider:
    """``jobs``: the history retention every job gets (unless it sets its
    own), changeable at runtime; the next ``jobs:prune-history`` run applies
    it.  The prune interval is set in code."""
    initial = scheduler.history_retention or JobHistoryRetention()

    def apply(key: str) -> Callable[[Any], None]:
        def run(value: Any) -> None:
            current = scheduler.history_retention
            limits = {f: getattr(current, f) if current is not None else None for f in _FIELDS.values()}
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            limits[_FIELDS[key]] = value
            retention = JobHistoryRetention(**limits)
            scheduler.set_history_defaults(retention if retention.is_configured else None)
        return run

    def duration(key: str, label: str, description: str) -> CliConfigSettingDefinition:
        return CliConfigSettingDefinition(
            key=key, label=label, description=description, type="duration",
            default=_as_duration(getattr(initial, _FIELDS[key])),
            validation=CliConfigValidation(min=1000), validate=_duration_rule, apply=apply(key),
        )

    def count(key: str, label: str, description: str) -> CliConfigSettingDefinition:
        return CliConfigSettingDefinition(
            key=key, label=label, description=description, type="number",
            default=getattr(initial, _FIELDS[key]),
            validation=CliConfigValidation(min=1), validate=_count_rule, apply=apply(key),
        )

    return CliStoredConfigProvider(
        id="jobs",
        title="Jobs",
        description="Defaults of the background jobs",
        order=60,
        sections=[CliConfigSectionDefinition(
            id="history",
            title="Run history retention",
            description=(
                "Applied to every job without its own history options by the jobs:prune-history job; "
                "empty keeps every run"
            ),
            settings=[
                duration("maxAge", "Keep runs for", "Delete runs that started longer ago than this"),
                count("maxRunsPerJob", "Runs kept per job", "Keep at most this many runs per job, the newest"),
                duration(
                    "failedMaxAge", "Keep failed runs for",
                    "Delete failed runs that started longer ago than this (default: Keep runs for)",
                ),
                count(
                    "failedMaxRunsPerJob", "Failed runs kept per job",
                    "Keep at most this many failed runs per job, counted on their own",
                ),
                CliConfigSettingDefinition(
                    key="pruneInterval", label="Prune every",
                    description="How often jobs:prune-history runs (set in code)",
                    type="duration", mutable=False, requires_restart=True,
                    default=scheduler.history_prune_interval,
                ),
            ],
        )],
    )
