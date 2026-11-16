import hashlib
import json
from dataclasses import asdict, dataclass


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class QueryPolicy:
    version: str = "v1"
    k: int = 3
    daily_queries: int = 100
    max_candidates: int = 10000
    max_events: int = 100000
    max_window_days: int = 366
    max_sequence: int = 8
    max_groups: int = 100
    max_group_dimensions: int = 2
    filter_fields: tuple = ("region", "age_band", "plan")
    group_fields: tuple = ("region", "age_band", "plan")

    def __post_init__(self):
        if not isinstance(self.version, str) or not self.version: raise ValueError("policy version is required")
        for key in ("k", "daily_queries", "max_candidates", "max_events", "max_window_days", "max_sequence", "max_groups", "max_group_dimensions"):
            if type(getattr(self, key)) is not int or getattr(self, key) < 1: raise ValueError(f"invalid policy limit: {key}")
        if self.k < 2: raise ValueError("minimum cohort size must be at least two")
        for key in ("filter_fields", "group_fields"):
            value = tuple(getattr(self, key))
            if len(set(value)) != len(value) or any(not isinstance(f, str) or not f for f in value):
                raise ValueError("invalid policy fields")
            object.__setattr__(self, key, value)

    @property
    def digest(self):
        return hashlib.sha256(canonical(asdict(self)).encode()).hexdigest()
