"""OpenAI API coordinator for the shared CTFd event loop."""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic_ai import Agent, RunContext

from backend.agents.coordinator_core import (
    do_broadcast,
    do_bump_agent,
    do_check_swarm_status,
    do_fetch_challenges,
    do_get_solve_status,
    do_kill_swarm,
    do_read_solver_trace,
    do_spawn_swarm,
    do_submit_flag,
)
from backend.agents.coordinator_loop import build_deps, run_event_loop
from backend.config import Settings
from backend.deps import CoordinatorDeps
from backend.models import resolve_model, resolve_model_settings

logger = logging.getLogger(__name__)

COORDINATOR_PROMPT = """You are the coordinator for a live CTF competition.
Use tools to fetch challenges, start solvers, inspect their traces, and give precise
technical guidance when they are stuck. Prioritize unsolved challenges with many solves.
The event loop automatically starts new challenges. Do not spawn duplicate swarms.
Keep active solvers running until a flag is confirmed, unless the operator asks to stop.
Use the tools for all state changes. Keep responses short.
"""


def build_coordinator_agent(settings: Settings) -> Agent[CoordinatorDeps, str]:
    """Build a tool-limited coordinator that calls the same core as legacy backends."""
    model = resolve_model("openai/gpt-5.6-sol", settings)
    agent = Agent(
        model,
        deps_type=CoordinatorDeps,
        system_prompt=COORDINATOR_PROMPT,
        model_settings=resolve_model_settings("openai/gpt-5.6-sol/medium"),
    )

    @agent.tool
    async def fetch_challenges(ctx: RunContext[CoordinatorDeps]) -> str:
        """List all CTFd challenges with category, points, solve count and status."""
        return await do_fetch_challenges(ctx.deps)

    @agent.tool
    async def get_solve_status(ctx: RunContext[CoordinatorDeps]) -> str:
        """Check solved challenges and the progress of active swarms."""
        return await do_get_solve_status(ctx.deps)

    @agent.tool
    async def spawn_swarm(ctx: RunContext[CoordinatorDeps], challenge_name: str) -> str:
        """Start solvers on an unsolved challenge by its exact CTFd name."""
        return await do_spawn_swarm(ctx.deps, challenge_name)

    @agent.tool
    async def check_swarm_status(ctx: RunContext[CoordinatorDeps], challenge_name: str) -> str:
        """Get the state of each solver for one challenge."""
        return await do_check_swarm_status(ctx.deps, challenge_name)

    @agent.tool
    async def submit_flag(ctx: RunContext[CoordinatorDeps], challenge_name: str, flag: str) -> str:
        """Verify a discovered flag with CTFd, unless dry run is enabled."""
        return await do_submit_flag(ctx.deps, challenge_name, flag)

    @agent.tool
    async def kill_swarm(ctx: RunContext[CoordinatorDeps], challenge_name: str) -> str:
        """Stop a challenge swarm when the operator explicitly asks."""
        return await do_kill_swarm(ctx.deps, challenge_name)

    @agent.tool
    async def bump_agent(ctx: RunContext[CoordinatorDeps], challenge_name: str, model_spec: str, insights: str) -> str:
        """Give a specific new lead to one stuck solver."""
        return await do_bump_agent(ctx.deps, challenge_name, model_spec, insights)

    @agent.tool
    async def broadcast(ctx: RunContext[CoordinatorDeps], challenge_name: str, message: str) -> str:
        """Share a finding with every solver working on a challenge."""
        return await do_broadcast(ctx.deps, challenge_name, message)

    @agent.tool
    async def read_solver_trace(ctx: RunContext[CoordinatorDeps], challenge_name: str, model_spec: str, last_n: int = 20) -> str:
        """Read recent tool calls and results for one solver before suggesting a new lead."""
        return await do_read_solver_trace(ctx.deps, challenge_name, model_spec, last_n)

    return agent


async def run_api_coordinator(
    settings: Settings,
    model_specs: list[str] | None = None,
    challenges_root: str = "challenges",
    no_submit: bool = False,
    coordinator_model: str | None = None,
    msg_port: int = 0,
) -> dict[str, Any]:
    """Coordinate through the OpenAI API while preserving conversation history."""
    if coordinator_model and coordinator_model != "gpt-5.6-sol":
        raise ValueError("The API coordinator currently supports gpt-5.6-sol only")

    ctfd, cost_tracker, deps = build_deps(settings, model_specs, challenges_root, no_submit)
    deps.msg_port = msg_port
    agent = build_coordinator_agent(settings)
    history: list = []

    async def turn_fn(message: str) -> None:
        nonlocal history
        started = time.monotonic()
        result = await agent.run(message, deps=deps, message_history=history or None)
        history = result.all_messages()
        cost_tracker.record(
            "coordinator/gpt-5.6-sol",
            result.usage(),
            "gpt-5.6-sol",
            provider_spec="openai",
            duration_seconds=time.monotonic() - started,
        )
        logger.info("Coordinator: %s", str(result.output)[:300])

    return await run_event_loop(deps, ctfd, cost_tracker, turn_fn)
