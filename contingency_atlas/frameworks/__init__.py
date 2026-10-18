"""I expose versioned external-framework catalogs for Contingency Atlas.

I keep these catalogs separate from the project-native risk taxonomy.  The
public functions in this module are deliberately small so callers do not need
to depend on the MITRE YAML representation or on SQLite implementation details.
"""

from .mitre_atlas import (
    DEFAULT_MAX_BYTES,
    MITRE_ATLAS,
    FrameworkImportError,
    ImportSizeError,
    ReleaseCollisionError,
    import_mitre_atlas,
)
from .store import (
    MAX_SEARCH_LIMIT,
    activate_release,
    framework_status,
    get_object,
    migrate_frameworks,
    resolve_technique,
    search_techniques,
    technique_context,
)

__all__ = [
    "DEFAULT_MAX_BYTES",
    "MAX_SEARCH_LIMIT",
    "MITRE_ATLAS",
    "FrameworkImportError",
    "ImportSizeError",
    "ReleaseCollisionError",
    "activate_release",
    "framework_status",
    "get_object",
    "import_mitre_atlas",
    "migrate_frameworks",
    "resolve_technique",
    "search_techniques",
    "technique_context",
]
