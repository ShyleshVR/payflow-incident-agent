"""Writes the agent's PayFlow secret into .env (git-ignored), from PayFlow's local secrets.

    .venv\\Scripts\\python.exe scripts\\write_env.py                    # PayFlow at D:\\Payments-Engine
    .venv\\Scripts\\python.exe scripts\\write_env.py C:\\code\\payflow

Run it once, and again if PayFlow's local secrets are regenerated. Other lines of .env are kept.
The secret is never printed.
"""
import sys
from pathlib import Path

KEY_IN_PAYFLOW = "incident-agent-client-secret"
KEY_IN_ENV = "PAYFLOW_CLIENT_SECRET"


def main() -> int:
    payflow = Path(sys.argv[1] if len(sys.argv) > 1 else r"D:\Payments-Engine")
    source = payflow / "k8s" / "overlays" / "local" / "secrets" / "auth.env"
    if not source.is_file():
        print(f"{source} not found. Run scripts/generate-local-secrets.sh in PayFlow first.")
        return 1
    secret = next((line.partition("=")[2].strip() for line in source.read_text(encoding="utf-8").splitlines()
                   if line.startswith(KEY_IN_PAYFLOW + "=")), "")
    if not secret:
        print(f"{source} has no {KEY_IN_PAYFLOW}. PayFlow needs the incident-agent client "
              "(branch feature/incident-agent-access), then scripts/generate-local-secrets.sh.")
        return 1

    target = Path(__file__).resolve().parent.parent / ".env"
    kept = [line for line in (target.read_text(encoding="utf-8").splitlines() if target.is_file() else [])
            if not line.startswith(KEY_IN_ENV + "=")]
    target.write_text("\n".join([*kept, f"{KEY_IN_ENV}={secret}"]) + "\n", encoding="utf-8")
    print(f"wrote {KEY_IN_ENV} to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
