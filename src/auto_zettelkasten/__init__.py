"""Public package metadata for Auto-Zettelkasten."""

from .models import (
    ArtifactManifest,
    ExpansionCandidate,
    ExpansionDecision,
    ExpansionReport,
    ExpansionRequest,
    MapRequest,
    RunReport,
    StatusReport,
)

ENGINE_VERSION = "0.2.0"
ARTIFACT_SCHEMA_VERSION = "1.1"

__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "ENGINE_VERSION",
    "ArtifactManifest",
    "ExpansionCandidate",
    "ExpansionDecision",
    "ExpansionReport",
    "ExpansionRequest",
    "MapRequest",
    "RunReport",
    "StatusReport",
]
