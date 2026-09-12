"""``mcc-apps``: the Desktop apps cards, without a browser.

    mcc-apps list
    mcc-apps status <app>
    mcc-apps configure <app> [--preview] [--default-model]
    mcc-apps undo <app> [--restore]

**Why it calls the server rather than the engine.** Every card's Configure
writes a model catalogue, and resolving one means walking the capability ladder
-- which needs a ``RequestRuntimePort`` a separate process does not have. So
this command talks to the same four admin routes the page does, exactly as the
``mcc-<agent>`` launchers already read ``GET /admin/api/catalogue-models``.
Headless parity is then structural rather than promised: there is one
implementation and two callers, and a card and a command cannot disagree about
what would be written.

It is deliberately **not** named ``mcc-desktop``. That name is already MCC's
own tray application; a second meaning for it -- "configure *other people's*
desktop apps" -- would be the third sense of one word in a namespace whose
collisions the docs already spend paragraphs on.

Nothing here launches an application, and nothing sets an environment variable
on the user's behalf. ``configure`` prints what to export and the next
``status`` reports whether it took.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from my_claude_code.config.proxy_auth import proxy_auth_token
from my_claude_code.config.server_urls import local_proxy_root_url
from my_claude_code.config.settings import get_settings

DESKTOP_APPS_PATH = "/admin/api/desktop-apps"

#: Long enough for the server to walk the resolution ladder once, which is what
#: a plan costs. Sized against the same measurement
#: ``cli/harnesses/catalogue_client`` documents -- 1.8-4.0 s on a 292-model
#: install -- and deliberately not the health-check budget, which is milliseconds.
REQUEST_TIMEOUT_SECONDS = 30.0

#: How each probe state reads on one line of a terminal. The words are the
#: card's badges, so a user reading both sees one vocabulary.
STATE_LABELS: dict[str, str] = {
    "not_installed": "not installed",
    "not_routable": "not routable",
    "installed": "installed, not configured",
    "configured": "configured by MCC",
    "drifted": "configured but drifted",
    "unreadable": "config file will not parse",
    "credential_unresolved": "written, but the credential cannot resolve",
    "removed_by_app": "the application removed MCC's configuration",
}


class AppsCommandError(RuntimeError):
    """Something the user can act on: a bad app id, or no server running."""


def _call(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    settings = get_settings()
    root = local_proxy_root_url(settings)
    url = f"{root.rstrip('/')}{path}"
    headers = {
        # The admin API is loopback-and-local-Origin only. This command *is*
        # local, so it states an Origin matching the server it is calling
        # rather than being waved through -- the guard stays one rule with no
        # exemption for MCC's own callers.
        "Origin": root.rstrip("/"),
        "Content-Type": "application/json",
    }
    if token := proxy_auth_token(settings.anthropic_auth_token).strip():
        headers["Authorization"] = f"Bearer {token}"

    body = json.dumps(payload or {}).encode("utf-8") if method == "POST" else None
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
        except ValueError, OSError:
            detail = exc.reason if isinstance(exc.reason, str) else ""
        raise AppsCommandError(detail or f"{exc.code} from {url}") from exc
    except URLError as exc:
        raise AppsCommandError(
            f"cannot reach the MCC server at {root}. Start it with mcc-server, "
            f"then try again. ({exc.reason})"
        ) from exc


def _apps() -> list[dict[str, Any]]:
    payload = _call("GET", DESKTOP_APPS_PATH)
    apps = payload.get("apps") if isinstance(payload, dict) else None
    if not isinstance(apps, list):
        raise AppsCommandError("the server returned no desktop-app list")
    return apps


def _find(app_id: str) -> dict[str, Any]:
    apps = _apps()
    for entry in apps:
        if entry.get("id") == app_id:
            return entry
    known = ", ".join(sorted(str(entry.get("id")) for entry in apps))
    raise AppsCommandError(f"unknown app: {app_id}. Known: {known}")


def _state_label(entry: dict[str, Any]) -> str:
    probe = entry.get("probe") or {}
    state = str(probe.get("state", ""))
    return STATE_LABELS.get(state, state or "unknown")


def _print_list() -> None:
    apps = _apps()
    width = max((len(str(entry.get("id", ""))) for entry in apps), default=0)
    for entry in apps:
        print(f"{entry.get('id', '')!s:<{width}}  {_state_label(entry)}")
    print()
    print("mcc-apps status <app> for the details, configure <app> to write it.")


def _print_status(app_id: str) -> None:
    entry = _find(app_id)
    probe = entry.get("probe") or {}
    print(f"{entry.get('display_name')} ({entry.get('id')})")
    print(f"  state         {_state_label(entry)}")
    if entry.get("status") == "not_routable":
        print(f"  why not       {entry.get('unavailable_reason')}")
        return
    if path := probe.get("document_path"):
        print(f"  config file   {path}")
    if owned := entry.get("owned_key"):
        print(f"  MCC owns      {owned}")
    if overwrites := entry.get("overwrites"):
        print(f"  overwrites    {', '.join(overwrites)}")
    if sidecar := entry.get("sidecar_path"):
        print(f"  MCC-owned     {sidecar}")
    print(f"  base URL      {entry.get('base_url')}")
    if reference := entry.get("token_reference"):
        print(f"  token         {reference}")
    if variable := entry.get("token_env_var"):
        exported = "exported" if probe.get("token_env_present") else "NOT exported"
        print(f"  {variable:<13} {exported} in this shell")
    if command := entry.get("open_command"):
        print(f"  open with     {command}")
    if entry.get("status") == "instructions_only":
        if reason := entry.get("instructions_reason"):
            print(f"  no button     {reason}")
        print("  set these by hand:")
        for field in entry.get("instruction_fields", []):
            print(f"    {field.get('label'):<16} {field.get('value')}")
    for note in entry.get("notes", []):
        print(f"  note          {note}")
    if probe.get("restorable"):
        print("  undo          --restore can put back what MCC replaced")


def _print_plan(plan: dict[str, Any]) -> None:
    if plan.get("no_op"):
        print("Already exactly what MCC would write. Nothing to do.")
        return
    print(f"--- {plan.get('document_path')}")
    print(plan.get("diff") or "(no change to this document)")
    if plan.get("sidecar_diff"):
        print(f"--- {plan.get('sidecar_path')}  (a file MCC owns outright)")
        print(plan["sidecar_diff"])
    if overwritten := plan.get("overwritten_keys"):
        print(f"Replaces existing values at: {', '.join(overwritten)}")
        print("`mcc-apps undo <app> --restore` puts those back.")


def _configure(app_id: str, *, preview: bool, default_model: bool) -> None:
    entry = _find(app_id)
    if entry.get("status") != "servable":
        raise AppsCommandError(
            f"{entry.get('display_name')} is not configured by writing a file. "
            f"Run `mcc-apps status {app_id}` for what to do instead."
        )

    payload = {"set_default_model": default_model}
    plan = _call("POST", f"{DESKTOP_APPS_PATH}/{app_id}/plan", payload)
    _print_plan(plan)
    if preview:
        return

    result = _call("POST", f"{DESKTOP_APPS_PATH}/{app_id}/configure", payload)
    if not result.get("changed"):
        print("No change: the file already said this.")
    else:
        print(f"Wrote {result.get('document_path')}")
        if backup := result.get("backup_path"):
            print(f"Backed up your original to {backup}")
    for action in plan.get("actions", []):
        print(f"  -> {action}")


def _undo(app_id: str, *, restore: bool) -> None:
    entry = _find(app_id)
    if entry.get("status") != "servable":
        raise AppsCommandError(
            f"{entry.get('display_name')} has no MCC keys in a file to remove."
        )
    mode = "restore" if restore else "keys_only"
    result = _call("POST", f"{DESKTOP_APPS_PATH}/{app_id}/undo", {"mode": mode})
    if not result.get("changed"):
        print("Nothing of MCC's was in that file.")
        return
    print(f"Removed MCC's keys from {result.get('document_path')}")
    if result.get("removed_sidecar"):
        print("Deleted the file MCC owned outright.")
    if restored := result.get("restored_keys"):
        print(f"Restored your original values at: {', '.join(restored)}")
    elif restore:
        print(
            "Nothing had to be restored: MCC created its keys rather than "
            "replacing yours."
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcc-apps",
        description="Point desktop applications at this MCC server, and take it back out.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("list", help="every desktop app and its state")

    status = subcommands.add_parser("status", help="one app in detail")
    status.add_argument("app")

    configure = subcommands.add_parser("configure", help="write MCC's keys")
    configure.add_argument("app")
    configure.add_argument(
        "--preview",
        action="store_true",
        help="show the diff and write nothing",
    )
    configure.add_argument(
        "--default-model",
        action="store_true",
        help="also set the app's default model to mcc/best",
    )

    undo = subcommands.add_parser("undo", help="remove MCC's keys")
    undo.add_argument("app")
    undo.add_argument(
        "--restore",
        action="store_true",
        help="also put back the values MCC replaced",
    )
    return parser


def apps_command(argv: Sequence[str] | None = None) -> None:
    """Dispatch ``mcc-apps`` subcommands."""

    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        match args.command:
            case "list":
                _print_list()
            case "status":
                _print_status(args.app)
            case "configure":
                _configure(
                    args.app,
                    preview=args.preview,
                    default_model=args.default_model,
                )
            case "undo":
                _undo(args.app, restore=args.restore)
    except AppsCommandError as exc:
        print(f"mcc-apps: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
