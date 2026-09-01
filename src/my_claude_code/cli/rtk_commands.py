"""``mcc-rtk`` / ``fcc-rtk`` command: manage the RTK token optimizer."""

import sys
from collections.abc import Mapping, Sequence
from typing import Any

from my_claude_code.config.harnesses import rtk_capable_ids
from my_claude_code.config.rtk import (
    RtkError,
    RtkState,
    apply_rtk_state,
    load_rtk_state,
    rtk_status,
    save_rtk_state,
)

# Derived, never restated: an agent this command accepts and an agent the
# registry marks RTK-capable are the same list by construction.
_ALL_AGENTS = rtk_capable_ids()
_AGENT_LIST = ", ".join(_ALL_AGENTS)


def rtk_command(argv: Sequence[str] | None = None) -> None:
    """Dispatch ``mcc-rtk`` / ``fcc-rtk`` subcommands."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        _print_usage()
        raise SystemExit(1)

    subcommand = args[0]
    if subcommand in ("--help", "-h", "help"):
        _print_usage()
        return

    try:
        if subcommand == "status":
            _require_no_extra(args[1:])
            _print_status(rtk_status())
        elif subcommand == "enable":
            _reconcile(_parse_agents(args[1:]), enabled=True)
        elif subcommand == "disable":
            _reconcile(_parse_agents(args[1:]), enabled=False)
        elif subcommand == "uninstall":
            _require_no_extra(args[1:])
            _reconcile(set(_ALL_AGENTS), enabled=False, uninstall=True)
        elif subcommand == "apply":
            _require_no_extra(args[1:])
            apply_rtk_state(load_rtk_state())
        else:
            print(f"error: unknown subcommand: {subcommand}", file=sys.stderr)
            _print_usage()
            raise SystemExit(1)
    except (RtkError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _require_no_extra(args: Sequence[str]) -> None:
    if args:
        raise ValueError(f"unexpected arguments: {' '.join(args)}")


def _parse_agents(args: Sequence[str]) -> set[str]:
    if not args:
        raise ValueError(f"specify one or more agents: {_AGENT_LIST}")

    agents: set[str] = set()
    for argument in args:
        for token in argument.split(","):
            agent = token.strip().lower()
            if agent not in _ALL_AGENTS:
                raise ValueError(f"unknown agent: {token}")
            agents.add(agent)
    if not agents:
        raise ValueError(f"specify one or more agents: {_AGENT_LIST}")
    return agents


def _reconcile(
    agents: set[str],
    *,
    enabled: bool,
    uninstall: bool = False,
) -> None:
    values = load_rtk_state().as_dict()
    for agent in agents:
        values[agent] = enabled

    state = RtkState(values)
    save_rtk_state(state)
    apply_rtk_state(state, uninstall=uninstall)


def _print_status(status: Mapping[str, Any]) -> None:
    lines = [f"installed:   {status['installed']}"]
    lines.extend(f"{agent:<11} {status[agent]}" for agent in _ALL_AGENTS)
    lines.append(f"binary_path: {status['binary_path']}")
    lines.append(f"version:     {status['version']}")
    print("\n".join(lines))


def _print_usage() -> None:
    print(
        f"""Usage: mcc-rtk <subcommand> [agents]

Manage the RTK token optimizer across coding agents.

Subcommands:
  status                  Print RTK install and agent state.
  enable <agents>         Enable RTK for comma-separated agents ({_AGENT_LIST}).
  disable <agents>        Disable RTK for comma-separated agents ({_AGENT_LIST}).
  uninstall               Disable all agents and remove the RTK binary.
  apply                   Re-reconcile the machine from the stored state.
  help                    Show this help text.

The legacy fcc-rtk command is an alias and behaves identically."""
    )
