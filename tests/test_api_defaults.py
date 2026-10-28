"""Offline checks for the new API-first path."""

from subprocess import CompletedProcess
from typing import cast

import pytest
from click.testing import CliRunner
from pydantic_ai.models.openai import OpenAIResponsesModelSettings

from backend.agents.api_coordinator import build_coordinator_agent
from backend.agents.coordinator_loop import _auto_spawn_unsolved
from backend.cli import main
from backend.config import Settings
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.deps import CoordinatorDeps
from backend.models import DEFAULT_MODELS, resolve_model, resolve_model_settings
from backend.poller import CTFdPoller


def _settings() -> Settings:
    return Settings(
        ctfd_url="https://ctfd.test",
        ctfd_token="test-token",
        openai_api_key="test-openai-key",
        anthropic_api_key="test-anthropic-key",
    )


def test_api_defaults_and_models() -> None:
    settings = _settings()
    assert DEFAULT_MODELS == ["openai/gpt-5.6-sol/medium"]
    assert type(resolve_model(DEFAULT_MODELS[0], settings)).__name__ == "OpenAIResponsesModel"
    model_settings = cast(OpenAIResponsesModelSettings, resolve_model_settings(DEFAULT_MODELS[0]))
    assert model_settings["openai_reasoning_effort"] == "medium"
    assert type(resolve_model("anthropic/claude-opus-4-8", settings)).__name__ == "AnthropicModel"
    assert len(build_coordinator_agent(settings)._function_toolset.tools) == 9


def test_check_reports_missing_image(monkeypatch) -> None:
    monkeypatch.setattr("backend.cli.Settings", lambda **kwargs: _settings())
    monkeypatch.setattr("backend.cli.shutil.which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        "backend.cli.subprocess.run",
        lambda *args, **kwargs: CompletedProcess(args[0], 1, "", "image missing"),
    )
    result = CliRunner().invoke(main, ["--check"])
    assert result.exit_code != 0
    assert "Sandbox image 'ctf-sandbox' is missing" in result.output


def test_check_passes_without_starting_agents(monkeypatch) -> None:
    monkeypatch.setattr("backend.cli.Settings", lambda **kwargs: _settings())
    monkeypatch.setattr("backend.cli.shutil.which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        "backend.cli.subprocess.run",
        lambda *args, **kwargs: CompletedProcess(args[0], 0, "image", ""),
    )
    result = CliRunner().invoke(main, ["--check"])
    assert result.exit_code == 0
    assert "Local checks passed" in result.output


@pytest.mark.asyncio
async def test_pending_challenges_fill_freed_slot(monkeypatch) -> None:
    settings = _settings()
    ctfd = CTFdClient(base_url=settings.ctfd_url, token=settings.ctfd_token)
    deps = CoordinatorDeps(ctfd=ctfd, cost_tracker=CostTracker(), settings=settings, max_concurrent_challenges=1)
    poller = CTFdPoller(ctfd=ctfd)
    poller._known_challenges = {"alpha", "beta"}
    attempted: set[str] = set()

    async def start_one(deps: CoordinatorDeps, name: str) -> bool:
        if len(deps.swarms) >= deps.max_concurrent_challenges:
            return False
        deps.swarms[name] = object()
        return True

    monkeypatch.setattr("backend.agents.coordinator_loop._auto_spawn_one", start_one)
    await _auto_spawn_unsolved(deps, poller, attempted)
    assert attempted == {"alpha"}

    deps.swarms.pop("alpha")
    await _auto_spawn_unsolved(deps, poller, attempted)
    assert attempted == {"alpha", "beta"}
    assert set(deps.swarms) == {"beta"}
