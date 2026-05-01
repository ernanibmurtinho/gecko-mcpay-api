"""Ingestion: discovery → extraction → chunking → embedding → store.

Public surface kept small: `discover`, `ingest`, plus the result/candidate
types used by the approval flow and the workflow layer.
"""

from gecko_core.models import SourceCandidate

from .discovery import discover
from .pipeline import dispatch_providers, ingest, url_hash
from .providers import ProviderHealth, ProviderKind, SourceChunk, SourceProvider
from .types import IngestionResult, SourceOutcome

__all__ = [
    "IngestionResult",
    "ProviderHealth",
    "ProviderKind",
    "SourceCandidate",
    "SourceChunk",
    "SourceOutcome",
    "SourceProvider",
    "discover",
    "dispatch_providers",
    "ingest",
    "url_hash",
]
