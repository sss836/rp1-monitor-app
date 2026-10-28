"""Computer-only command entry point; no robot collector or demo launcher."""

from .cli import main as run_cli

PC_COMMANDS = {
    "server",
    "relay",
    "desktop",
    "station",
    "archive",
    "backfill-key-windows",
    "sql",
    "admin-init",
    "provision",
    "profile-init",
    "doctor",
    "diagnostics",
    "ask",
}


def main():
    run_cli(allowed_commands=PC_COMMANDS)


if __name__ == "__main__":
    main()
