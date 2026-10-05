"""ONIONTRACE synthetic farm generator.

Builds a deterministic, self-hosted test bed of synthetic onion services, operator
clusters, injected opsec flaws, and authored personas -- together with the ground
truth needed to score every metric in Part E of the project plan.

SCOPE LIMIT (read this before using any metric that comes out of here):
the farm validates *rules, correlation, and robustness*. It does NOT validate human
authorship attribution. Personas are generated with known parameters, so a model
that recovers them has recovered the generator, not a property of human writing.
Everything measured over personas is reported as **synthetic persona retrieval**.

Everything the farm generates is authored by us from benign vocabulary and is safe
to redistribute (Part B4: "Synthetic farm -- ours -- this is what ships on GitHub").
"""

from farm.model import (
    AbstentionCase,
    Cluster,
    Endpoint,
    FarmConfig,
    FarmTruth,
    Flaw,
    FlawScope,
    HardNegative,
    Persona,
    PersonaRole,
    Post,
    RuleClass,
    Service,
    StyleRegime,
    deterministic_hex,
    deterministic_onion_address,
)

__all__ = [
    "AbstentionCase",
    "Cluster",
    "Endpoint",
    "FarmConfig",
    "FarmTruth",
    "Flaw",
    "FlawScope",
    "HardNegative",
    "Persona",
    "PersonaRole",
    "Post",
    "RuleClass",
    "Service",
    "StyleRegime",
    "deterministic_hex",
    "deterministic_onion_address",
]

__version__ = "0.1.0"
