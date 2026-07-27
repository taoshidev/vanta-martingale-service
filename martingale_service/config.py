"""Service configuration.

The DETECTION PARAMETERS block below is the single source of truth for every number the
detector uses -- change values HERE only. They are passed explicitly all the way down
(nothing is defaulted inside any function), so a run's numbers are never ambiguous and any
calc discrepancy traces straight back to this block.

Runtime settings (endpoints, credentials, database) are NOT in this file: they come from
environment variables, with an optional git-ignored ``secrets.json`` fallback for local runs
(see ``secrets-example.json``). This is a public repository -- never commit real endpoints,
keys or DSNs.
"""
import json
import os
from dataclasses import dataclass

# ============================ DETECTION PARAMETERS ============================
ESCALATION_FACTOR = 1.5        # a level must be >= this x an earlier level to count (2.0 = doubling)
FLOOR_FRACTION    = 0.03       # orders below this fraction of the pair's max size can't be chain levels
CHAIN_LENGTH      = 4          # this many escalating levels within one underwater streak => trigger
LOOKBACK_DAYS     = 10         # trailing detection-window length, in days
FLOOR_MODE        = "exclude"  # below-floor orders (incl. closes) can never be chain levels; the
                               # alternative, "clip", raises them to the floor instead
GRACE_PERIOD_DAYS = 3          # an eliminate-eligible trigger must arrive at least this many days
                               # after the warning; earlier triggers are recorded but absorbed
# ==============================================================================


@dataclass(frozen=True)
class DetectionParams:
    """The detection knobs bundled so they travel together (values from the block above)."""
    escalation_factor: float
    floor_fraction: float
    chain_length: int
    lookback_days: int
    floor_mode: str
    grace_period_days: int


DETECTION = DetectionParams(
    escalation_factor=ESCALATION_FACTOR,
    floor_fraction=FLOOR_FRACTION,
    chain_length=CHAIN_LENGTH,
    lookback_days=LOOKBACK_DAYS,
    floor_mode=FLOOR_MODE,
    grace_period_days=GRACE_PERIOD_DAYS,
)


@dataclass(frozen=True)
class RuntimeConfig:
    """Deployment-specific settings; see ``load_runtime_config`` for the sources."""
    rest_base_url: str
    api_key: str
    database_url: str
    ws_url: str | None            # doorbell endpoint; None disables it (sweep-only mode)
    sweep_interval_s: float


def load_runtime_config(secrets_path="secrets.json") -> RuntimeConfig:
    """Environment variables first, then the git-ignored ``secrets_path`` file, else defaults.

    Required: ``VANTA_REST_BASE_URL``, ``VANTA_API_KEY``, ``DATABASE_URL``.
    Optional: ``VANTA_WS_URL`` (unset -> websocket doorbell disabled; the reconcile sweep alone
    is still fully correct, just slower to react) and ``SWEEP_INTERVAL_SECONDS`` (default 60).
    """
    file_values = {}
    if os.path.exists(secrets_path):
        with open(secrets_path) as f:
            file_values = json.load(f)

    def get(name, default=None):
        return os.environ.get(name) or file_values.get(name) or default

    missing = [n for n in ("VANTA_REST_BASE_URL", "VANTA_API_KEY", "DATABASE_URL") if not get(n)]
    if missing:
        raise SystemExit(f"missing required configuration: {', '.join(missing)} "
                         f"(set as environment variables or in {secrets_path})")
    return RuntimeConfig(
        rest_base_url=get("VANTA_REST_BASE_URL").rstrip("/"),
        api_key=get("VANTA_API_KEY"),
        database_url=get("DATABASE_URL"),
        ws_url=get("VANTA_WS_URL"),
        sweep_interval_s=float(get("SWEEP_INTERVAL_SECONDS", 60)),
    )
