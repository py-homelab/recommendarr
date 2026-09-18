"""Endpoints and secrets. Secrets are read from the environment or decrypted in memory;
nothing here ever writes one to disk."""

import os
import subprocess
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("RECOMMENDARR_DATA", ROOT / "data"))
REPORTS_DIR = ROOT / "reports"
DB_PATH = DATA_DIR / "harness.db"

TAUTULLI_URL = os.environ.get("TAUTULLI_URL", "http://192.168.1.119:8181/api/v2")
SEERR_URL = os.environ.get("SEERR_URL", "http://192.168.1.119:5055/api/v1")
TMDB_URL = "https://api.themoviedb.org/3"

HOMELAB_STACKS = Path(os.environ.get("HOMELAB_STACKS", Path.home() / "Code/homelab-stacks"))

# secret name -> sops file inside homelab-stacks
SOPS_FILES = {
    "TAUTULLI_API_KEY": "truenas/monitoring/secrets.sops.env",
    "SEERR_PICKS_API_KEY": "truenas/media/secrets.sops.env",
}

# Users with fewer completed plays than this are not evaluated.
MIN_COMPLETED_PLAYS = 100


@cache
def _sops_env(rel_path: str) -> dict[str, str]:
    out = subprocess.run(
        ["sops", "-d", rel_path],
        cwd=HOMELAB_STACKS,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    env = {}
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.lstrip().startswith("#"):
            env[key.strip()] = value.strip().strip("\"'")
    return env


@cache
def _dotenv() -> dict[str, str]:
    """Gitignored `.env` / `.envrc` in the project root, for keys with no sops home (TMDb)."""
    env = {}
    for name in (".env", ".envrc"):
        path = ROOT / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and not key.startswith("#"):
                env.setdefault(key.strip(), value.strip().strip("\"'"))
    return env


def secret(name: str) -> str:
    if value := os.environ.get(name) or _dotenv().get(name):
        return value
    if rel_path := SOPS_FILES.get(name):
        if value := _sops_env(rel_path).get(name):
            return value
    raise SystemExit(f"{name} is not set. Export it in the shell before running this command.")
