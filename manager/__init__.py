"""Reusable local Machine Manager runtime."""

from .machine_manager import MachineManager
from .dispatcher import DispatchOutcome, DispatchResult, WorkDispatcher
from .research_worker import (
    BoundedSourceFetcher,
    LocalOllamaResearchSummarizer,
    OllamaResearchHandler,
    PublicResearchHandler,
    ResearchRetryableError,
    ResearchTaskError,
)
from .scheduler import WorkItem, WorkScheduler
from .state_store import StateStore
from .supervisor import HealthSignals, JobState, WorkerSpec, WorkerSupervisor
from .workstreams import WorkstreamRegistry

__all__ = [
    "HealthSignals",
    "JobState",
    "MachineManager",
    "DispatchOutcome",
    "DispatchResult",
    "BoundedSourceFetcher",
    "LocalOllamaResearchSummarizer",
    "OllamaResearchHandler",
    "PublicResearchHandler",
    "ResearchRetryableError",
    "ResearchTaskError",
    "StateStore",
    "WorkItem",
    "WorkDispatcher",
    "WorkScheduler",
    "WorkerSpec",
    "WorkerSupervisor",
    "WorkstreamRegistry",
]
