"""Writes the agent's secrets into .env (git-ignored), from PayFlow's local secrets: its PayFlow
client secret, and, when PayFlow sends its alerts to the agent, the token those alerts carry
(secrets/incident-agent-token, made by PayFlow's scripts/generate-local-secrets.sh --agent).

    .venv\\Scripts\\python.exe scripts\\write_env.py                    # PayFlow at D:\\Payments-Engine
    .venv\\Scripts\\python.exe scripts\\write_env.py C:\\code\\payflow

Run it once, and again if PayFlow's local secrets are regenerated. Other lines of .env are kept.
No secret is ever printed.
"""
import sys
from pathlib import Path

KEY_IN_PAYFLOW = "incident-agent-client-secret"
KEY_IN_ENV = "PAYFLOW_CLIENT_SECRET"
TOKEN_FILE = "incident-agent-token"
TOKEN_IN_ENV = "AGENT_TOKEN"


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

    values = {KEY_IN_ENV: secret}
    token_file = source.parent / TOKEN_FILE
    if token_file.is_file():
        values[TOKEN_IN_ENV] = token_file.read_text(encoding="utf-8").strip()

    target = Path(__file__).resolve().parent.parent / ".env"
    kept = [line for line in (target.read_text(encoding="utf-8").splitlines() if target.is_file() else [])
            if line.partition("=")[0] not in values]
    target.write_text("\n".join([*kept, *(f"{key}={value}" for key, value in values.items())]) + "\n", encoding="utf-8")
    print(f"wrote {', '.join(values)} to {target}")
    if TOKEN_IN_ENV not in values:
        print(f"(no {token_file}: PayFlow does not send its alerts to the agent; see its generate-local-secrets.sh --agent)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
