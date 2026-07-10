"""Public package metadata for Auto-Zettelkasten."""

from .models import ArtifactManifest, MapRequest, RunReport, StatusReport

ENGINE_VERSION = "0.1.0"
ARTIFACT_SCHEMA_VERSION = "1.0"

__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "ENGINE_VERSION",
    "ArtifactManifest",
    "MapRequest",
    "RunReport",
    "StatusReport",
]
