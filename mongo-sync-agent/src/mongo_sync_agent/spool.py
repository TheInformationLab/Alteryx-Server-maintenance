import shutil
from pathlib import Path

from loguru import logger


def setup_spool(spool_base: str, run_id: str) -> Path:
    """Create {spool_base}/{run_id}/ and return it as a Path."""
    spool_dir = Path(spool_base) / run_id
    spool_dir.mkdir(parents=True, exist_ok=True)
    logger.debug("Spool directory created: {}", spool_dir)
    return spool_dir


def sweep_orphans(spool_base: str, current_run_id: str) -> int:
    """Delete all subdirectories of spool_base whose name != current_run_id.
    Returns count of directories removed. Ignores errors on individual removes.
    Called at agent startup before processing begins."""
    spool_base_path = Path(spool_base)
    if not spool_base_path.exists():
        return 0

    count = 0
    for item in spool_base_path.iterdir():
        if item.is_dir() and item.name != current_run_id:
            logger.warning("Sweeping orphaned spool directory: {}", item)
            shutil.rmtree(item, ignore_errors=True)
            count += 1
    if count:
        logger.info("Swept {} orphaned spool director{}", count, "y" if count == 1 else "ies")
    return count


def spool_path(spool_dir: Path, unit: str, ext: str) -> Path:
    """Return {spool_dir}/{unit}.{ext} — the path for a unit's spool file."""
    return spool_dir / f"{unit}.{ext}"
