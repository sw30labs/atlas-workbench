"""Configuration for the Contingency Atlas pipeline.

All settings come from environment variables with sensible defaults.
Paths resolve to absolute paths under the project root (parent of contingency_atlas/)
unless overridden by env vars.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


def package_root() -> Path:
    """Absolute path to the installed or source package directory."""
    return Path(__file__).resolve().parent


def project_root() -> Path:
    """Return the checkout root, or the current directory when installed.

    A wheel must never default runtime writes into ``site-packages``. Source
    checkouts use their repository root; regular installations use the
    operator's current working directory unless an explicit path is supplied.
    """
    checkout = package_root().parent
    if (checkout / "pyproject.toml").is_file():
        return checkout
    return Path.cwd().resolve()


def _abs_under_root(path: Path | str, root: Path | None = None) -> Path:
    """Resolve path to absolute; relative paths are relative to project root."""
    root = root or project_root()
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = root / p
    return p.resolve()


def yaml_config_path() -> Path:
    """Path to the YAML config file.

    ``ATLAS_CONFIG`` wins; otherwise a config file inside the active data
    directory (``ATLAS_HOME/config.yaml``) is used when present, so a
    self-contained workspace carries its own settings.  A source checkout
    falls back to its repository-root ``contingency-atlas.yaml``.
    """
    if v := os.environ.get("ATLAS_CONFIG"):
        return Path(v).expanduser()
    if home := os.environ.get("ATLAS_HOME"):
        candidate = Path(home).expanduser() / "config.yaml"
        if candidate.is_file():
            return candidate
    return project_root() / "contingency-atlas.yaml"


# YAML omlx: key → (Settings field, type coercion). The API key is deliberately
# absent — secrets belong in the OMLX_API_KEY env var, not in a config file.
_OMLX_YAML_KEYS: dict[str, tuple[str, type]] = {
    "base_url": ("omlx_base_url", str),
    "model": ("omlx_model", str),
    "model_judge": ("omlx_model_judge", str),
    "model_final_judge": ("omlx_model_final_judge", str),
    "timeout": ("omlx_timeout", float),
    "max_tokens": ("omlx_max_tokens", int),
    "use_responses_api": ("omlx_use_responses_api", bool),
}

# YAML dgx: same shape as omlx, plus enable_thinking. Key stays out of the
# DGX_API_KEY / VLLM_API_KEY env vars for the same reason as above.
_DGX_YAML_KEYS: dict[str, tuple[str, type]] = {
    "base_url": ("dgx_base_url", str),
    "model": ("dgx_model", str),
    "model_judge": ("dgx_model_judge", str),
    "model_final_judge": ("dgx_model_final_judge", str),
    "timeout": ("dgx_timeout", float),
    "max_tokens": ("dgx_max_tokens", int),
    "enable_thinking": ("dgx_enable_thinking", bool),
}

# Supported LLM backends. "omlx" is the Mac MLX server; "dgx" is the
# NVIDIA DGX vLLM server. The aliases match their underlying runtimes.
_LLM_PROVIDERS: tuple[str, ...] = ("omlx", "dgx")
_PROVIDER_ALIASES: dict[str, str] = {"mlx": "omlx", "vllm": "dgx"}


def normalize_llm_provider(value: Any) -> str:
    """Normalize a provider name, raising ValueError on anything unknown."""
    p = str(value).strip().lower()
    p = _PROVIDER_ALIASES.get(p, p)
    if p not in _LLM_PROVIDERS:
        raise ValueError(
            f"llm_provider must be one of {sorted(_LLM_PROVIDERS)} "
            f"(aliases: {sorted(_PROVIDER_ALIASES)}), got {value!r}"
        )
    return p


def _coerce_bool(v: Any) -> bool:
    """Strict bool coercion for config files — bool('false') is True."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
    raise ValueError(f"must be a boolean, got {v!r}")


def _parse_llm_section(
    path: Path, data: dict[str, Any], section: str, keys: dict[str, tuple[str, type]]
) -> dict[str, Any]:
    """Parse one YAML LLM section (omlx:/dgx:) into {Settings field: value}."""
    raw = data.get(section) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: '{section}' must be a mapping")
    unknown = set(raw) - set(keys)
    if unknown:
        raise ValueError(
            f"{path}: unknown {section} key(s) {sorted(unknown)} "
            f"(supported: {sorted(keys)})"
        )
    out: dict[str, Any] = {}
    for key, (field_name, coerce) in keys.items():
        if key not in raw:
            continue
        v = raw[key]
        try:
            if coerce is str:
                out[field_name] = "" if v is None else str(v)
            elif coerce is bool:
                out[field_name] = _coerce_bool(v)
            else:
                out[field_name] = coerce(v)
        except (TypeError, ValueError):
            raise ValueError(
                f"{path}: {section}.{key} must be {coerce.__name__}, got {v!r}"
            ) from None
    return out


def load_yaml_settings(path: Path | None = None) -> dict[str, Any]:
    """Read the YAML config file into {Settings field: value}.

    Missing file → {}. Invalid YAML, unknown sections/keys, or wrong types
    raise ValueError — a config file with a silent typo is worse than none.
    """
    path = path or yaml_config_path()
    if not path.is_file():
        return {}
    try:
        import yaml
    except ImportError as e:
        raise ValueError(f"PyYAML is required to read {path} (pip install pyyaml)") from e
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping, got {type(data).__name__}")
    unknown_top = set(data) - {"provider", "omlx", "dgx", "organization_context", "bulk_ingest"}
    if unknown_top:
        raise ValueError(
            f"{path}: unknown section(s) {sorted(unknown_top)} "
            f"(supported: 'provider', 'omlx', 'dgx', 'organization_context', 'bulk_ingest')"
        )
    out: dict[str, Any] = {}
    if "provider" in data:
        try:
            out["llm_provider"] = normalize_llm_provider(data["provider"])
        except ValueError as e:
            raise ValueError(f"{path}: provider: {e}") from None
    out.update(_parse_llm_section(path, data, "omlx", _OMLX_YAML_KEYS))
    out.update(_parse_llm_section(path, data, "dgx", _DGX_YAML_KEYS))
    out.update(_parse_llm_section(path, data, "organization_context", {
        "enabled": ("context_enabled", bool),
        "db_path": ("context_db_path", str),
    }))
    out.update(_parse_bulk_ingest_section(path, data))
    return out


def _parse_bulk_ingest_section(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    """YAML ``bulk_ingest: {roots: [dir, ...]}``: folders the dashboard may ingest."""
    raw = data.get("bulk_ingest") or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: 'bulk_ingest' must be a mapping")
    unknown = set(raw) - {"roots"}
    if unknown:
        raise ValueError(f"{path}: unknown bulk_ingest key(s) {sorted(unknown)} (supported: ['roots'])")
    if "roots" not in raw:
        return {}
    roots = raw["roots"]
    if roots is None:
        roots = []
    if not isinstance(roots, list) or not all(isinstance(r, str) and r.strip() for r in roots):
        raise ValueError(f"{path}: bulk_ingest.roots must be a list of directory paths")
    return {"bulk_ingest_roots": [Path(r.strip()) for r in roots]}


class Settings(BaseModel):
    """Pipeline configuration loaded from environment / defaults."""

    context_enabled: bool = Field(default=False, description="Explicit opt-in to the private organization workspace")
    context_db_path: Path = Field(default=Path("organization-context.db"))
    # Folders the dashboard's Bulk import may read. None means the defaults:
    # <workspace>/inbox and the checkout's examples/ directory. Relative
    # entries resolve under the project root. An empty list disables the screen.
    bulk_ingest_roots: list[Path] | None = Field(
        default=None,
        description="Allowed parent folders for dashboard bulk ingest (ATLAS_BULK_INGEST_ROOTS)",
    )

    # ── LLM (OMLX) ──
    omlx_base_url: str = Field(
        default="http://127.0.0.1:8000/v1",
        description="OMLX OpenAI-compatible endpoint",
    )
    omlx_api_key: str = Field(default="test", repr=False)
    omlx_model: str = Field(
        default="DeepSeek-V4-Flash-0731-MXFP4-MLX",
        description="Default model for generation tasks",
    )
    omlx_model_judge: str = Field(
        default="DeepSeek-V4-Flash-0731-MXFP4-MLX",
        description="Model for LLM-as-a-Judge evaluation",
    )
    omlx_model_final_judge: str = Field(
        default="",
        description="Independent model for the final pass/fail verdict. "
                    "Empty string disables the independent final judge.",
    )
    omlx_timeout: float = Field(
        default=600.0,
        description="HTTP timeout in seconds (OMLX can be slow; "
                    "DeepSeek-V4-Flash MXFP4 long jobs often exceed 3 minutes)",
    )
    omlx_max_tokens: int = Field(default=16384)
    omlx_use_responses_api: bool = Field(
        default=False,
        description="If True, OMLX calls POST /v1/responses instead of "
                    "/v1/chat/completions. Off by default; requires an OMLX "
                    "build that exposes the Responses API.",
    )

    # ── LLM backend select ──
    llm_provider: str = Field(
        default="omlx",
        description="Active LLM backend: 'omlx' (Mac MLX server) or 'dgx' "
                    "(NVIDIA DGX vLLM server). Aliases: mlx, vllm.",
    )

    # ── LLM (DGX vLLM) ──
    # The default expects one loopback vLLM server. Operators may override the
    # endpoint and model through project-scoped environment variables.
    dgx_base_url: str = Field(
        default="http://127.0.0.1:8000/v1",
        description="DGX vLLM OpenAI-compatible endpoint",
    )
    dgx_api_key: str = Field(
        default="test",
        repr=False,
        description="Bearer key for the DGX server (vLLM ignores it but "
                    "requires a non-empty value)",
    )
    dgx_model: str = Field(
        default="Qwen/Qwen3.6-27B-FP8",
        description="Default model for generation tasks (must match /v1/models)",
    )
    dgx_model_judge: str = Field(
        default="Qwen/Qwen3.6-27B-FP8",
        description="Model for LLM-as-a-Judge evaluation",
    )
    dgx_model_final_judge: str = Field(
        default="",
        description="Independent model for the final pass/fail verdict. "
                    "Empty string disables the independent final judge.",
    )
    dgx_timeout: float = Field(
        default=600.0,
        description="HTTP timeout in seconds (DGX Spark bandwidth is slow)",
    )
    dgx_max_tokens: int = Field(default=4096)
    dgx_enable_thinking: bool = Field(
        default=False,
        description="Let Qwen3.x reasoning models think (chat_template_kwargs "
                    "enable_thinking). Off by default: thinking can burn the "
                    "whole token budget in reasoning_content.",
    )

    # ── Data paths (absolute after validation) ──
    workspace: Path = Field(default_factory=project_root)
    scenario_db_path: Path = Field(default_factory=lambda: project_root() / "atlas.db")
    evidence_db_path: Path = Field(default_factory=lambda: project_root() / "event_evidence.db")
    emerging_risks_path: Path = Field(
        default_factory=lambda: project_root() / "emerging_risks.json"
    )
    scenario_export_path: Path = Field(
        default_factory=lambda: project_root() / "scenario_export.json"
    )

    # ── DriftLab ──
    driftlab_enabled: bool = Field(
        default=False,
        description="Enable the optional DriftLab trace and baseline integration",
    )
    driftlab_data_dir: Path = Field(default_factory=lambda: Path.home() / ".driftlab")
    driftlab_agent_name: str = Field(default="contingency-atlas-pipeline")
    driftlab_baseline_path: Path = Field(
        default_factory=lambda: (
            package_root()
            / "baselines"
            / "contingency-atlas-pipeline-baseline.json"
        ),
        description="OSCAL-style DriftLab baseline profile for this pipeline",
    )

    # ── Pipeline ──
    max_judge_iterations: int = Field(
        default=3,
        description="Max Ralph Loop iterations for QA (LLM-as-a-Judge)",
    )
    judge_score_threshold: float = Field(
        default=0.75,
        description="Minimum judge score to accept a scenario (0-1)",
    )
    # Safety: never apply merge/retire actions unless ATLAS_AUTO_RETIRE=1
    auto_retire: bool = Field(
        default=False,
        description="If True, Phase 6 may apply merge and retire actions "
                    "(env ATLAS_AUTO_RETIRE=1)",
    )

    # ── Causal analysis (Phase 5) ──
    causal_confidence_threshold: float = Field(default=0.6)
    causal_min_samples: int = Field(default=5)
    causal_max_lag_days: int = Field(default=180)
    temporal_lag_days: int = Field(default=180)
    temporal_min_samples: int = Field(default=5)
    temporal_n_permutations: int = Field(default=1000)

    @field_validator("llm_provider", mode="before")
    @classmethod
    def _normalize_provider(cls, v: Any) -> str:
        return normalize_llm_provider(v)

    @field_validator(
        "workspace",
        "scenario_db_path",
        "evidence_db_path",
        "emerging_risks_path",
        "scenario_export_path",
        "driftlab_data_dir",
        "driftlab_baseline_path",
        "context_db_path",
        mode="before",
    )
    @classmethod
    def _coerce_path(cls, v: Any) -> Path:
        if v is None:
            return project_root()
        return Path(v)

    @model_validator(mode="after")
    def _resolve_absolute_and_env(self) -> Settings:
        root = project_root()
        # Overrides apply only to fields NOT explicitly passed to the
        # constructor — an explicit Settings(scenario_db_path=...) must win,
        # otherwise "isolated" runs get silently redirected by ambient env.
        explicit = self.model_fields_set

        # YAML config file: above built-in defaults, below env vars.
        for attr, val in load_yaml_settings().items():
            if attr not in explicit:
                object.__setattr__(self, attr, val)
        env_map = {
            "ATLAS_WORKSPACE": "workspace",
            "ATLAS_DB": "scenario_db_path",
            "ATLAS_EVIDENCE_DB": "evidence_db_path",
            "ATLAS_EMERGING_RISKS": "emerging_risks_path",
            "ATLAS_SCENARIO_EXPORT": "scenario_export_path",
            "ATLAS_CONTEXT_DB": "context_db_path",
            "DRIFTLAB_DATA_DIR": "driftlab_data_dir",
            "ATLAS_DRIFTLAB_BASELINE": "driftlab_baseline_path",
        }
        for env_key, attr in env_map.items():
            if attr in explicit:
                continue
            if v := os.environ.get(env_key):
                object.__setattr__(self, attr, Path(v))

        # ATLAS_HOME: one directory for every runtime store.  Explicit
        # arguments and the individual ATLAS_* variables above still win, so
        # existing deployments keep their layout.
        if home_env := os.environ.get("ATLAS_HOME"):
            home_path = Path(home_env).expanduser()
            if not home_path.is_absolute():
                home_path = root / home_path
            home_path = home_path.resolve()
            if "workspace" not in explicit and not os.environ.get("ATLAS_WORKSPACE"):
                object.__setattr__(self, "workspace", home_path)
            env_for_attr = {attr: key for key, attr in env_map.items()}
            for attr, filename in (
                ("scenario_db_path", "atlas.db"),
                ("evidence_db_path", "event_evidence.db"),
                ("emerging_risks_path", "emerging_risks.json"),
                ("scenario_export_path", "scenario_export.json"),
                ("context_db_path", "organization-context.db"),
            ):
                if attr in explicit or os.environ.get(env_for_attr[attr]):
                    continue
                object.__setattr__(self, attr, home_path / filename)

        # Absolute paths: relative → under project root
        for attr in (
            "workspace",
            "scenario_db_path",
            "evidence_db_path",
            "emerging_risks_path",
            "scenario_export_path",
            "driftlab_baseline_path",
            "context_db_path",
        ):
            object.__setattr__(self, attr, _abs_under_root(getattr(self, attr), root))

        if "context_enabled" not in explicit and (v := os.environ.get("ATLAS_CONTEXT_ENABLED")):
            object.__setattr__(self, "context_enabled", _coerce_bool(v))

        if "bulk_ingest_roots" not in explicit and (v := os.environ.get("ATLAS_BULK_INGEST_ROOTS")) is not None:
            object.__setattr__(self, "bulk_ingest_roots", [Path(p) for p in v.split(os.pathsep) if p.strip()])
        if self.bulk_ingest_roots is None:
            object.__setattr__(self, "bulk_ingest_roots", [self.workspace / "inbox", root / "examples"])
        object.__setattr__(self, "bulk_ingest_roots", [
            _abs_under_root(Path(p).expanduser(), root) for p in self.bulk_ingest_roots
        ])

        # driftlab_data_dir: expanduser + absolute (not forced under project)
        dd = Path(self.driftlab_data_dir).expanduser()
        if not dd.is_absolute():
            dd = (root / dd).resolve()
        else:
            dd = dd.resolve()
        object.__setattr__(self, "driftlab_data_dir", dd)

        # LLM env overrides
        if "llm_provider" not in explicit:
            pv = os.environ.get("ATLAS_LLM_PROVIDER") or os.environ.get("LLM_PROVIDER")
            if pv:
                object.__setattr__(self, "llm_provider", normalize_llm_provider(pv))
        if "omlx_base_url" not in explicit and (v := os.environ.get("OMLX_BASE_URL")):
            object.__setattr__(self, "omlx_base_url", v)
        if "omlx_api_key" not in explicit and (v := os.environ.get("OMLX_API_KEY")):
            object.__setattr__(self, "omlx_api_key", v)
        if "omlx_model" not in explicit and (v := os.environ.get("OMLX_MODEL")):
            object.__setattr__(self, "omlx_model", v)
        if "omlx_model_judge" not in explicit and (v := os.environ.get("OMLX_MODEL_JUDGE")):
            object.__setattr__(self, "omlx_model_judge", v)
        if "omlx_model_final_judge" not in explicit and (v := os.environ.get("OMLX_MODEL_FINAL_JUDGE")):
            object.__setattr__(self, "omlx_model_final_judge", v)
        if "omlx_use_responses_api" not in explicit and (
            v := os.environ.get("OMLX_USE_RESPONSES_API")
        ):
            object.__setattr__(self, "omlx_use_responses_api", _coerce_bool(v))

        # DGX env overrides. DGX_* win; standard VLLM_* names are fallbacks.
        def _dgx_env(*names: str) -> str | None:
            for name in names:
                if v := os.environ.get(name):
                    return v
            return None

        if "dgx_base_url" not in explicit and (v := _dgx_env("DGX_BASE_URL", "VLLM_BASE_URL")):
            object.__setattr__(self, "dgx_base_url", v)
        if "dgx_api_key" not in explicit and (v := _dgx_env("DGX_API_KEY", "VLLM_API_KEY")):
            object.__setattr__(self, "dgx_api_key", v)
        if "dgx_model" not in explicit and (v := _dgx_env("DGX_MODEL", "VLLM_MODEL")):
            object.__setattr__(self, "dgx_model", v)
        if "dgx_model_judge" not in explicit and (v := _dgx_env("DGX_MODEL_JUDGE")):
            object.__setattr__(self, "dgx_model_judge", v)
        if "dgx_model_final_judge" not in explicit and (v := _dgx_env("DGX_MODEL_FINAL_JUDGE")):
            object.__setattr__(self, "dgx_model_final_judge", v)
        if "dgx_timeout" not in explicit and (v := _dgx_env("DGX_TIMEOUT", "VLLM_TIMEOUT_S")):
            object.__setattr__(self, "dgx_timeout", float(v))
        if "dgx_max_tokens" not in explicit and (v := _dgx_env("DGX_MAX_TOKENS", "VLLM_MAX_TOKENS")):
            object.__setattr__(self, "dgx_max_tokens", int(v))
        if "dgx_enable_thinking" not in explicit and (
            v := _dgx_env("DGX_ENABLE_THINKING", "VLLM_ENABLE_THINKING")
        ):
            object.__setattr__(self, "dgx_enable_thinking", _coerce_bool(v))

        # Optional runtime-governance integration
        if "driftlab_enabled" not in explicit:
            driftlab_flag = os.environ.get(
                "ATLAS_DRIFTLAB_ENABLED", ""
            ).strip().lower()
            if driftlab_flag in ("1", "true", "yes", "on"):
                object.__setattr__(self, "driftlab_enabled", True)
            elif driftlab_flag in ("0", "false", "no", "off"):
                object.__setattr__(self, "driftlab_enabled", False)

        # Auto-retire safety switch
        if "auto_retire" not in explicit:
            ar = os.environ.get("ATLAS_AUTO_RETIRE", "").strip().lower()
            if ar in ("1", "true", "yes", "on"):
                object.__setattr__(self, "auto_retire", True)
            elif ar in ("0", "false", "no", "off"):
                object.__setattr__(self, "auto_retire", False)

        return self

    # ── Effective LLM accessors ──
    # Everything that talks to the LLM reads these, not the backend-specific
    # fields, so switching llm_provider reroutes the whole pipeline.
    @property
    def is_dgx(self) -> bool:
        return self.llm_provider == "dgx"

    @property
    def llm_base_url(self) -> str:
        return self.dgx_base_url if self.is_dgx else self.omlx_base_url

    @property
    def llm_api_key(self) -> str:
        return self.dgx_api_key if self.is_dgx else self.omlx_api_key

    @property
    def llm_model(self) -> str:
        return self.dgx_model if self.is_dgx else self.omlx_model

    @property
    def llm_model_judge(self) -> str:
        return self.dgx_model_judge if self.is_dgx else self.omlx_model_judge

    @property
    def llm_model_final_judge(self) -> str:
        return self.dgx_model_final_judge if self.is_dgx else self.omlx_model_final_judge

    @property
    def llm_timeout(self) -> float:
        return self.dgx_timeout if self.is_dgx else self.omlx_timeout

    @property
    def llm_max_tokens(self) -> int:
        return self.dgx_max_tokens if self.is_dgx else self.omlx_max_tokens

    @property
    def llm_enable_thinking(self) -> bool:
        # Thinking-mode kwargs are a vLLM/Qwen3.x knob; OMLX gets nothing.
        return self.is_dgx and self.dgx_enable_thinking

    @property
    def llm_use_responses_api(self) -> bool:
        """Responses API is an OMLX option. DGX stays on chat completions."""
        return (not self.is_dgx) and self.omlx_use_responses_api

    def to_safe_dict(self) -> dict[str, Any]:
        """Return a serializable dict with secrets redacted.

        Suitable for LangGraph state / logs. The API key is intentionally
        omitted so it is never carried through the graph state.
        """
        return {
            "workspace": str(self.workspace),
            "scenario_db_path": str(self.scenario_db_path),
            "evidence_db_path": str(self.evidence_db_path),
            "emerging_risks_path": str(self.emerging_risks_path),
            "scenario_export_path": str(self.scenario_export_path),
            "omlx_base_url": self.omlx_base_url,
            "omlx_model": self.omlx_model,
            "omlx_model_judge": self.omlx_model_judge,
            "omlx_model_final_judge": self.omlx_model_final_judge,
            "omlx_timeout": self.omlx_timeout,
            "omlx_max_tokens": self.omlx_max_tokens,
            "omlx_use_responses_api": self.omlx_use_responses_api,
            "llm_provider": self.llm_provider,
            "dgx_base_url": self.dgx_base_url,
            "dgx_model": self.dgx_model,
            "dgx_model_judge": self.dgx_model_judge,
            "dgx_model_final_judge": self.dgx_model_final_judge,
            "dgx_timeout": self.dgx_timeout,
            "dgx_max_tokens": self.dgx_max_tokens,
            "dgx_enable_thinking": self.dgx_enable_thinking,
            "max_judge_iterations": self.max_judge_iterations,
            "judge_score_threshold": self.judge_score_threshold,
            "driftlab_enabled": self.driftlab_enabled,
            "driftlab_baseline_path": str(self.driftlab_baseline_path),
            "auto_retire": self.auto_retire,
            "causal_confidence_threshold": self.causal_confidence_threshold,
            "causal_min_samples": self.causal_min_samples,
            "causal_max_lag_days": self.causal_max_lag_days,
            "temporal_lag_days": self.temporal_lag_days,
            "temporal_min_samples": self.temporal_min_samples,
            "temporal_n_permutations": self.temporal_n_permutations,
        }


def get_settings() -> Settings:
    """Return a Settings instance (not cached — tests may mutate env)."""
    return Settings()


def auto_retire_enabled(settings: Settings | None = None) -> bool:
    """Whether Phase 6 may apply merges/retirements (ATLAS_AUTO_RETIRE=1)."""
    if settings is not None:
        return bool(settings.auto_retire)
    ar = os.environ.get("ATLAS_AUTO_RETIRE", "").strip().lower()
    return ar in ("1", "true", "yes", "on")
