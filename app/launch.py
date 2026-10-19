"""The command line for one Cursor or Claude Code run."""

from __future__ import annotations

from app.access import claude_model_args, effort_args, model_args


_CLAUDE_MODES = {
    "agent": "bypassPermissions",
    "ask": "dontAsk",
    "plan": "plan",
}


def run_argv(
    cli: str,
    *,
    binary: str,
    workspace: str,
    mode: str,
    model: str,
    session_id: str,
    prompt: str,
    effort: str = "",
) -> list[str]:
    if cli == "claude":
        cmd = [
            binary,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode",
            _CLAUDE_MODES.get(mode, "bypassPermissions"),
            "--permission-prompts",
            "none",
        ]
        cmd.extend(claude_model_args(model))
        cmd.extend(effort_args(effort))
        if session_id:
            cmd.extend(["--resume", session_id])
        cmd.append(prompt)
        return cmd
    cmd = [
        binary,
        "-p",
        "--force",
        "--trust",
        "--approve-mcps",
        "--sandbox",
        "disabled",
        "--output-format",
        "stream-json",
        "--stream-partial-output",
        "--workspace",
        workspace,
    ]
    if mode in {"ask", "plan"}:
        cmd.extend(["--mode", mode])
    cmd.extend(model_args(model))
    if session_id:
        cmd.extend(["--resume", session_id])
    cmd.append(prompt)
    return cmd
