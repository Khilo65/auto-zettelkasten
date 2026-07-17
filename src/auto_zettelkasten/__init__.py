"""Public package metadata and collection-mapping contracts."""

ENGINE_VERSION = "0.5.0"
ARTIFACT_SCHEMA_VERSION = "1.4"
__version__ = ENGINE_VERSION

from .models import (  # noqa: E402 - version constants must exist before api imports this module
    ArtifactManifest,
    ClusterProposal,
    ClusterSynthesis,
    EvidenceFinding,
    EvidenceProfile,
    GapAnchor,
    GapRationale,
    GapStudyDesign,
    GapValueAssessment,
    LiteratureMapReport,
    LiteratureMapRequest,
    LiteratureMappingPolicy,
    MapRequest,
    ProcessingPolicy,
    RunReport,
    StatusReport,
)
from .api import run_literature_map  # noqa: E402 - depends on the constants above
from .ports import (  # noqa: E402 - public protocols depend on the models above
    ClusterSynthesisReasoner,
    ExternalDiscoveryProvider,
    LiteratureReasoner,
)

__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "ENGINE_VERSION",
    "__version__",
    "ArtifactManifest",
    "ClusterProposal",
    "ClusterSynthesis",
    "ClusterSynthesisReasoner",
    "EvidenceFinding",
    "EvidenceProfile",
    "ExternalDiscoveryProvider",
    "GapAnchor",
    "GapRationale",
    "GapStudyDesign",
    "GapValueAssessment",
    "LiteratureMapReport",
    "LiteratureMapRequest",
    "LiteratureMappingPolicy",
    "LiteratureReasoner",
    "MapRequest",
    "ProcessingPolicy",
    "RunReport",
    "StatusReport",
    "run_literature_map",
]
