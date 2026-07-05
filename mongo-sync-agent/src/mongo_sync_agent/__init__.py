"""Alteryx MongoDB → S3 / Snowflake incremental sync agent."""
from ._version import __version__

def run(config_path: str, only: set[str] | None = None, dry_run: bool = False) -> int:
    """Run one sync cycle. Returns exit code: 0=ok, 1=partial failure, 2=fatal."""
    from pathlib import Path
    from .config import load_config
    from .runner import run_cycle
    cfg = load_config(Path(config_path))
    return run_cycle(cfg, only=only, dry_run=dry_run)
