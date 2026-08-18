"""
Detect the Claude runtime environment and report constraints/recommendations.

Why this exists: Odoo Sidekick can be invoked from different Claude surfaces
that have very different runtime semantics (sandboxed container vs. user's real
filesystem). The same skill code runs everywhere, but the profile config does
not travel between surfaces. This script makes those constraints visible so
onboarding and operational decisions can adapt.

Surfaces:
  - headless      : autonomous agent / scheduler run, no human at the keyboard
                    (declared via ODOO_SIDEKICK_HEADLESS=1 — takes precedence)
  - claude.ai     : web/mobile chat, sandboxed Linux container, ephemeral fs
  - cowork        : desktop agent on user's real filesystem
  - claude_code   : CLI dev environment, local fs + shell
  - local         : running on a real machine but not via a recognizable Claude
                    surface (the catch-all — detection always resolves)

Usage:
    python3 -m scripts.detect_env             # human-readable
    python3 -m scripts.detect_env --json      # machine-readable
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from pathlib import Path


SANDBOX_SIGNALS = (
    "/mnt/skills/public",
    "/mnt/skills/private",
    "/mnt/skills/examples",
    "/mnt/user-data/uploads",
    "/mnt/user-data/outputs",
)


def detect() -> dict:
    home = Path.home()
    state_dir = Path(
        os.environ.get("ODOO_SIDEKICK_STATE_DIR") or home / ".config" / "odoo-sidekick"
    ).expanduser()
    config_path = Path(
        os.environ.get("ODOO_PROFILES_PATH") or state_dir / "profiles.yaml"
    ).expanduser()
    user_profile_path = state_dir / "user_profile.yaml"

    info: dict = {
        "environment": "unknown",
        "is_sandboxed": False,
        "filesystem_persistent": True,
        "platform": platform.system(),
        "platform_release": platform.release(),
        "python_version": sys.version.split()[0],
        "user_home": str(home),
        "config_path": str(config_path),
        "config_exists": config_path.exists(),
        "user_profile_path": str(user_profile_path),
        "user_profile_exists": user_profile_path.exists(),
        "headless": False,
        "caller": os.environ.get("ODOO_SIDEKICK_CALLER") or None,
        "notes": [],
        "recommendations": [],
        "best_for": [],
    }

    # Headless / autonomous-agent mode — declared explicitly, takes precedence
    # over surface sniffing because the same host can serve both interactive
    # and scheduled runs.
    if os.environ.get("ODOO_SIDEKICK_HEADLESS", "").strip().lower() in ("1", "true", "yes"):
        info["environment"] = "headless"
        info["headless"] = True
        info["notes"].extend([
            "Headless run declared via ODOO_SIDEKICK_HEADLESS — no human is "
            "assumed to be present at call time.",
            "Skip onboarding, welcome-back chrome, and conversational prompts; "
            "emit machine-parseable output (--json flags).",
            "Chat-level write confirmation is unavailable here. Writes are gated "
            "by the profile instead: confirm_cmd (external approval hook), "
            "require_auth_ref (ticket reference per write), write_policy "
            "(model/method allowlist).",
        ])
        info["recommendations"].extend([
            "Set ODOO_SIDEKICK_CALLER=<agent-or-run-id> so the shared call log "
            "is a usable audit trail.",
            "Give each agent its own ODOO_SIDEKICK_STATE_DIR (or share one "
            "deliberately, with caller set).",
            "Run scripts/verify_profile.py at deploy time — client-side "
            "read-only is not a security boundary.",
            "Pre-seed user context with `python3 -m scripts.user_profile seed "
            "--file <seed>` (see assets/user_profile.example.yaml) — the "
            "tailoring questions never run headless.",
        ])
        info["best_for"].extend([
            "Scheduled heartbeats and cron-driven reports.",
            "Multi-agent orchestration platforms.",
            "CI pipelines and unattended batch work.",
        ])
        return info

    # Claude.ai sandbox — look for the mounted skill / user-data paths.
    if any(Path(p).exists() for p in SANDBOX_SIGNALS):
        info["environment"] = "claude.ai"
        info["is_sandboxed"] = True
        info["filesystem_persistent"] = False
        info["notes"].extend([
            "This is a Claude.ai sandbox — filesystem is ephemeral.",
            "Profile config written here will NOT persist to your real machine "
            "or across separate conversations.",
            "API keys pasted in chat persist in conversation history regardless "
            "of surface or session. Treat chat history as long-lived.",
        ])
        info["recommendations"].extend([
            "Use Path A (.env download) over Path B (chat paste). Even in this "
            "ephemeral environment, the chat history is not ephemeral.",
            "For recurring or production-style work, set up the skill in "
            "Cowork or Claude Code on your local machine instead.",
            "If you do paste a key in chat (Path B), rotate it in Odoo once "
            "your work is done.",
        ])
        info["best_for"].extend([
            "One-off analytical queries.",
            "Exploratory \"show me X\" requests.",
            "Mobile use.",
            "Demonstrations.",
        ])
        return info

    # Try to refine the local case from env hints.
    env = os.environ
    claude_code_signals = any(
        env.get(k) for k in ("CLAUDE_CODE", "CLAUDECODE")
    )
    cowork_signals = any(
        env.get(k) for k in ("COWORK", "CLAUDE_COWORK")
    ) or "cowork" in env.get("CLAUDE_CLIENT", "").lower()

    if claude_code_signals:
        info["environment"] = "claude_code"
        info["notes"].append(
            "Running in Claude Code — full local filesystem + shell access."
        )
        info["recommendations"].extend([
            "Config persists at the path above. Source your .env from your "
            "shell rc or from a project-local file.",
            "Ideal surface for batch operations and skill iteration.",
        ])
        info["best_for"].extend([
            "Skill development and iteration.",
            "Batch operations and automation.",
            "Git workflows around the skill itself.",
            "Repeatable scripted analyses.",
        ])
    elif cowork_signals:
        info["environment"] = "cowork"
        info["notes"].append(
            "Running in Cowork — Claude is operating on your real filesystem."
        )
        info["recommendations"].extend([
            "Path A (.env on disk, sourced once) is strictly better here. "
            "Clean separation between key and config.",
            "Make sure Python 3 plus pyyaml (and duckdb if you want caching) "
            "are installed locally. Cowork won't auto-install deps.",
        ])
        info["best_for"].extend([
            "Recurring reports.",
            "Work that integrates with local files (Excel, PDFs, Word).",
            "Scheduled or semi-automated tasks via your OS scheduler.",
            "Multi-tool workflows that chain Odoo data with other local apps.",
        ])
    else:
        info["environment"] = "local"
        info["notes"].append(
            "Local environment detected (no Claude surface markers). Config "
            "and cache files will persist normally."
        )
        info["recommendations"].append(
            "Treat as Claude Code / Cowork: Path A for the API key, "
            "persistent config at the path above."
        )

    return info


def print_human(info: dict) -> None:
    env = info["environment"]
    label = {
        "headless":   "Headless (autonomous agent / scheduler, no human present)",
        "claude.ai":  "Claude.ai (web/mobile sandbox)",
        "cowork":     "Cowork (desktop agent on your real filesystem)",
        "claude_code":"Claude Code (local CLI)",
        "local":      "Local environment",
        "unknown":    "Unknown",
    }.get(env, env)
    sandbox = "yes" if info["is_sandboxed"] else "no"
    persist = "no (ephemeral)" if not info["filesystem_persistent"] else "yes"

    print(f"Environment:        {label}")
    print(f"Sandboxed:          {sandbox}")
    print(f"Config persists:    {persist}")
    print(f"Platform:           {info['platform']} ({info['platform_release']})")
    print(f"Python:             {info['python_version']}")
    print(f"Config path:        {info['config_path']}")
    print(f"Config exists:      {'yes' if info['config_exists'] else 'no'}")
    print(f"User profile:       "
          f"{info['user_profile_path'] if info['user_profile_exists'] else 'none (tailoring not captured)'}")

    if info["notes"]:
        print("\nWhat this means:")
        for n in info["notes"]:
            print(f"  - {n}")

    if info["recommendations"]:
        print("\nRecommendations:")
        for r in info["recommendations"]:
            print(f"  - {r}")

    if info["best_for"]:
        print("\nBest for:")
        for b in info["best_for"]:
            print(f"  - {b}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="detect_env", description=__doc__.split("\n\n")[0])
    p.add_argument("--json", action="store_true", help="Output as JSON instead of human-readable text")
    args = p.parse_args(argv)
    info = detect()
    if args.json:
        json.dump(info, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print_human(info)
    return 0


if __name__ == "__main__":
    sys.exit(main())
