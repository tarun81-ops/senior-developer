"""Loads ``config/*.yaml`` into typed objects.

This is the only module that knows the config file format. Everything else asks
the registry questions like "which models should the *coder* agent try, in what
order?" That keeps the model-assignment policy out of the code, which was an
explicit requirement.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from backend.core.config import Settings, read_yaml
from backend.core.errors import ConfigError, MissingApiKey
from backend.core.local_settings import LocalOverrides, apply_overrides, read_overrides
from backend.core.provider.schemas import ModelSpec, ProviderSpec
from backend.core.secrets import get_api_key

logger = logging.getLogger(__name__)


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RetryConfig(_Base):
    max_attempts_per_provider: int = 3
    base_delay_seconds: float = 1.5
    max_delay_seconds: float = 30.0
    jitter: bool = True


class CooldownConfig(_Base):
    on_rate_limit_seconds: int = 60
    on_auth_error_seconds: int = 900
    on_credits_seconds: int = 3600
    on_server_error_seconds: int = 30
    on_network_error_seconds: int = 20
    #: HTTP 200 with an empty completion (thinking models burn the budget on
    #: reasoning). Short cooldown: it usually works on the next try or the next
    #: request, and we would rather fail over than wait long.
    on_empty_seconds: int = 45


class BudgetConfig(_Base):
    max_calls_per_run: int = 60
    max_tokens_per_run: int = 400_000


class RequestConfig(_Base):
    timeout_seconds: float = 120.0
    temperature_default: float = 0.2
    stream: bool = False


class ExecutionConfig(_Base):
    """Phase 3: running the generated project's commands (see config/limits.yaml)."""

    enabled: bool = True
    #: executables we are willing to start; anything else is refused
    allow: list[str] = Field(
        default_factory=lambda: ["python", "py", "pytest", "npm", "node", "npx"]
    )
    timeout_seconds: float = 300.0
    max_output_bytes: int = 20_000
    env: dict[str, str] = Field(default_factory=dict)


class LimitsConfig(_Base):
    retry: RetryConfig = Field(default_factory=RetryConfig)
    cooldown: CooldownConfig = Field(default_factory=CooldownConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    request: RequestConfig = Field(default_factory=RequestConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)


class RouteCandidate(_Base):
    """One step of an agent's fallback chain."""

    provider: str
    model: str


class AgentConfig(_Base):
    name: str
    role: str = ""
    prompt_file: str = ""
    temperature: float = 0.2
    max_output_tokens: int = 4096
    routing: list[RouteCandidate] = Field(default_factory=list)

    def prompt_path(self, root: Path) -> Path:
        if not self.prompt_file:
            raise ConfigError(f"Agent '{self.name}' has no prompt_file configured")
        path = Path(self.prompt_file)
        return path if path.is_absolute() else (root / path)


#: Phase 2 default pipeline. Only used when agents.yaml has no ``pipeline:`` section.
DEFAULT_PIPELINE_STAGES = ["planner", "architect", "coder", "tester", "reviewer", "devops", "docs"]


class PipelineConfig(_Base):
    """Stage order and review policy for the orchestrator (config, not code)."""

    stages: list[str] = Field(default_factory=lambda: list(DEFAULT_PIPELINE_STAGES))
    max_fix_iterations: int = 1
    #: Phase 3: materialise the stages' `files` into workspace/<project>/
    apply_workspace: bool = True
    #: Phase 3: run the tester's command and feed the result back into the fix loop
    run_tests: bool = True


class Registry:
    """In-memory view of all configuration."""

    def __init__(
        self,
        *,
        providers: dict[str, ProviderSpec],
        agents: dict[str, AgentConfig],
        limits: LimitsConfig,
        root: Path,
        pipeline: PipelineConfig | None = None,
    ) -> None:
        self.providers = providers
        self.agents = agents
        self.limits = limits
        self.root = Path(root)
        self.pipeline = pipeline or PipelineConfig()

    # -- construction -------------------------------------------------------
    @classmethod
    def load(cls, settings: Settings, *, overrides: LocalOverrides | None = None) -> Registry:
        """Tracked ``config/*.yaml`` with the local overrides layered on top (D22).

        ``overrides=None`` reads them from ``data/settings.local.yaml``, falling
        back to the tracked defaults if that file is stale or corrupt (see
        :meth:`load_effective`). The settings API passes a candidate instead, to
        validate it before saving; a bad candidate always raises.
        """
        if overrides is not None:
            return cls._load(settings, overrides)
        return cls.load_effective(settings)[0]

    @classmethod
    def load_effective(cls, settings: Settings) -> tuple[Registry, LocalOverrides]:
        """The registry plus the overrides actually applied to it.

        A bad ``data/settings.local.yaml`` (unparsable, or naming an agent,
        provider or model that no longer exists) must not take down every run
        and the CLI, so it is ignored with a loud warning naming the file, and
        the tracked defaults apply. A broken *tracked* config still raises: the
        defaults are loaded before the warning, so it never blames the wrong file.
        """
        path = settings.local_settings_path
        try:
            overrides = read_overrides(path)
            return cls._load(settings, overrides), overrides
        except ConfigError as exc:
            if not path.exists():
                raise
            defaults = LocalOverrides()
            registry = cls._load(settings, defaults)
            logger.warning(
                "IGNORING %s and using the tracked config/ defaults: %s. "
                "Fix it with PUT /api/settings {} or delete the file.",
                path,
                exc,
            )
            return registry, defaults

    @classmethod
    def _load(cls, settings: Settings, overrides: LocalOverrides) -> Registry:
        files = settings.config_files
        limits_path = files["limits"]
        providers_raw = read_yaml(files["providers"])
        return cls.from_dict(
            providers_raw=providers_raw,
            agents_raw=apply_overrides(
                read_yaml(files["agents"]),
                overrides,
                providers=set(providers_raw.get("providers") or {}),
            ),
            limits_raw=read_yaml(limits_path) if limits_path.exists() else {},
            root=settings.root,
        )

    @classmethod
    def from_dict(
        cls,
        *,
        providers_raw: dict,
        agents_raw: dict,
        limits_raw: dict | None = None,
        root: Path | str,
    ) -> Registry:
        providers: dict[str, ProviderSpec] = {}
        for name, raw in (providers_raw.get("providers") or {}).items():
            payload = dict(raw or {})
            payload["name"] = name
            spec = ProviderSpec.model_validate(payload)
            if not spec.base_url:
                raise ConfigError(f"Provider '{name}' has no base_url")
            # The key in the YAML map IS the model id; make that explicit on the object.
            spec.models = {
                model_id: (
                    model
                    if model.id
                    else model.model_copy(update={"id": model_id})
                )
                for model_id, model in spec.models.items()
            }
            providers[name] = spec

        agents: dict[str, AgentConfig] = {}
        for name, raw in (agents_raw.get("agents") or {}).items():
            payload = dict(raw or {})
            payload["name"] = name
            agent = AgentConfig.model_validate(payload)
            for candidate in agent.routing:
                if candidate.provider not in providers:
                    raise ConfigError(
                        f"Agent '{name}' routes to unknown provider '{candidate.provider}'"
                    )
                model_ids = providers[candidate.provider].models
                if candidate.model not in model_ids:
                    raise ConfigError(
                        f"Agent '{name}' routes to model '{candidate.model}' which is not "
                        f"listed under provider '{candidate.provider}' in providers.yaml"
                    )
            agents[name] = agent

        limits = LimitsConfig.model_validate(limits_raw or {})
        pipeline = PipelineConfig.model_validate(agents_raw.get("pipeline") or {})
        return cls(
            providers=providers,
            agents=agents,
            limits=limits,
            root=Path(root),
            pipeline=pipeline,
        )

    # -- lookups ------------------------------------------------------------
    def provider(self, name: str) -> ProviderSpec:
        try:
            return self.providers[name]
        except KeyError as exc:
            known = ", ".join(sorted(self.providers)) or "(none)"
            raise ConfigError(f"Unknown provider '{name}'. Known providers: {known}") from exc

    def agent(self, name: str) -> AgentConfig:
        try:
            return self.agents[name]
        except KeyError as exc:
            known = ", ".join(sorted(self.agents)) or "(none)"
            raise ConfigError(f"Unknown agent '{name}'. Known agents: {known}") from exc

    def resolve(self, provider_name: str, model_id: str) -> tuple[ProviderSpec, ModelSpec]:
        spec = self.provider(provider_name)
        return spec, spec.model(model_id)

    def candidate_chain(self, agent_name: str) -> list[tuple[ProviderSpec, ModelSpec]]:
        """Full routing chain for an agent, in priority order."""
        agent = self.agent(agent_name)
        return [self.resolve(c.provider, c.model) for c in agent.routing]

    def api_key_for(self, spec: ProviderSpec, *, required: bool = False) -> str | None:
        key = get_api_key(spec.api_key_env, provider=spec.name)
        if key is None and required and spec.requires_key:
            raise MissingApiKey(spec.name, spec.api_key_env)
        return key

    def provider_status(self) -> list[dict]:
        """Rows for the ``providers`` CLI command and the settings screen."""
        rows = []
        for name, spec in self.providers.items():
            key = self.api_key_for(spec)
            rows.append(
                {
                    "provider": name,
                    "label": spec.label or name,
                    "kind": spec.kind,
                    "requires_key": spec.requires_key,
                    "key_present": key is not None,
                    "api_key_env": spec.api_key_env,
                    "models": sorted(spec.models),
                }
            )
        return rows

