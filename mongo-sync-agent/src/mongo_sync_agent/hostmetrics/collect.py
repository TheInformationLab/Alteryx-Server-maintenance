"""psutil-based host metrics collection."""

import ctypes
import sys
from datetime import datetime, timezone

import psutil
from loguru import logger

# --- Windows drive-letter -> physical-drive mapping ------------------------
#
# On Windows (the target platform), psutil.disk_io_counters(perdisk=True) keys
# its result by *physical drive* ("PhysicalDrive0", "PhysicalDrive1", ...), not
# by the drive letters ("C:", "D:") the agent is configured with. There is no
# psutil API to translate one to the other, and a single volume can span more
# than one physical disk, so we resolve the mapping ourselves via the Win32
# IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS control code. This uses only the stdlib
# ``ctypes`` module -- no extra dependency (important for the PyInstaller
# one-file exe) and no Administrator rights (the volume handle is opened with
# zero access, which is enough to query its disk extents).

_IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x560000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_WRITE = 0x2
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
# A volume spanning this many physical disks is implausible; the buffer is
# sized generously and DeviceIoControl reports the true count regardless.
_MAX_EXTENTS = 32


def _windows_volume_disk_numbers(disk: str) -> list[int]:
    """Return the physical disk numbers backing a Windows volume.

    ``disk`` is a drive letter or mount point such as ``"C:"`` / ``"C:\\"``.
    A volume can span multiple physical disks (spanned/striped volumes), so the
    returned list may hold more than one number. Raises ``OSError`` if the
    volume can't be opened or queried.
    """
    from ctypes import wintypes

    class DISK_EXTENT(ctypes.Structure):
        _fields_ = [
            ("DiskNumber", wintypes.DWORD),
            ("StartingOffset", wintypes.LARGE_INTEGER),
            ("ExtentLength", wintypes.LARGE_INTEGER),
        ]

    class VOLUME_DISK_EXTENTS(ctypes.Structure):
        _fields_ = [
            ("NumberOfDiskExtents", wintypes.DWORD),
            ("Extents", DISK_EXTENT * _MAX_EXTENTS),
        ]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = wintypes.HANDLE
    k.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    k.DeviceIoControl.restype = wintypes.BOOL
    k.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    k.CloseHandle.argtypes = [wintypes.HANDLE]

    # Normalise "C:" / "C:\\" / "C:/" to the volume device path r"\\.\C:".
    letter = disk.rstrip("\\/").rstrip(":")
    volume_path = "\\\\.\\" + letter + ":"

    handle = k.CreateFileW(
        volume_path, 0, _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None, _OPEN_EXISTING, 0, None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), f"could not open volume {volume_path}")
    try:
        extents = VOLUME_DISK_EXTENTS()
        returned = wintypes.DWORD(0)
        ok = k.DeviceIoControl(
            handle, _IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, None, 0,
            ctypes.byref(extents), ctypes.sizeof(extents), ctypes.byref(returned), None,
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), f"could not query disk extents for {volume_path}")
        count = min(extents.NumberOfDiskExtents, _MAX_EXTENTS)
        return [extents.Extents[i].DiskNumber for i in range(count)]
    finally:
        k.CloseHandle(handle)


def _resolve_perdisk_keys(disk: str) -> list[str] | None:
    """Map a configured disk to the psutil ``perdisk`` keys backing it.

    Returns a list of ``"PhysicalDriveN"`` keys on Windows, or ``None`` when the
    mapping can't be resolved (non-Windows platform, or the volume can't be
    queried) so the caller can fall back to the graceful zero+warning record.
    """
    if sys.platform != "win32":
        return None
    try:
        numbers = _windows_volume_disk_numbers(disk)
    except Exception as e:
        logger.debug("Could not map disk {} to a physical drive: {}", disk, e)
        return None
    if not numbers:
        return None
    return [f"PhysicalDrive{n}" for n in numbers]


def _sum_disk_io(keys: list[str], disk_io_counters: dict) -> dict | None:
    """Sum the psutil IO counters for ``keys`` (a volume may span several disks).

    Returns a dict of summed read/write byte and operation counts, or ``None``
    if none of the keys are present in ``disk_io_counters``.
    """
    present = [key for key in keys if key in disk_io_counters]
    if not present:
        return None
    read_bytes = write_bytes = read_count = write_count = 0
    for key in present:
        counters = disk_io_counters[key]
        read_bytes += counters.read_bytes
        write_bytes += counters.write_bytes
        read_count += counters.read_count
        write_count += counters.write_count
    return {
        "read_bytes": read_bytes,
        "write_bytes": write_bytes,
        "read_count": read_count,
        "write_count": write_count,
    }


def collect(disks: list[str], host: str = "") -> list[dict]:
    """Return a list of metric records (one per type). Each record is a plain dict
    suitable for JSON serialisation. All timestamps are ISO-8601 UTC strings.

    Every record carries the ``host`` identifier so metrics from multiple
    Server hosts landing in the same S3/Snowflake can be attributed correctly.

    Include (``host`` is added to every record just before return):
    - {"metric": "cpu_percent_1s", "value": psutil.cpu_percent(interval=1), "ts": ...}
    - {"metric": "memory", "total": ..., "available": ..., "used": ..., "percent": ..., "ts": ...}
      from psutil.virtual_memory()
    - For each disk in disks that exists (skip with warning if not found):
      {"metric": "disk_usage", "path": disk, "total": ..., "used": ..., "free": ..., "percent": ..., "ts": ...}
      {"metric": "disk_io", "path": disk, "read_bytes": ..., "write_bytes": ...,
       "read_count": ..., "write_count": ..., "ts": ...}
      disk_io from psutil.disk_io_counters(perdisk=True). On Windows those
      counters are keyed by physical drive, so the configured drive letter is
      mapped to its backing physical drive(s) (see _resolve_perdisk_keys) and
      their counters summed. If the mapping genuinely can't be resolved, emit a
      record with all zeros and a "warning" key.
    """
    logger.debug("Collecting host metrics (host={} disks={})", host, disks)
    records = []

    # CPU metrics
    try:
        cpu_percent = psutil.cpu_percent(interval=1)
        records.append({
            "metric": "cpu_percent_1s",
            "value": cpu_percent,
            "ts": datetime.now(timezone.utc).isoformat()
        })
        logger.debug("CPU: {}%", cpu_percent)
    except Exception as e:
        logger.warning("Failed to collect CPU metrics: {}", e)

    # Memory metrics
    try:
        mem = psutil.virtual_memory()
        records.append({
            "metric": "memory",
            "total": mem.total,
            "available": mem.available,
            "used": mem.used,
            "percent": mem.percent,
            "ts": datetime.now(timezone.utc).isoformat()
        })
        logger.debug("Memory: {}% used ({} MB avail)", mem.percent, mem.available // 1_048_576)
    except Exception as e:
        logger.warning("Failed to collect memory metrics: {}", e)

    # Get disk IO counters once (for all disks)
    disk_io_counters = {}
    try:
        disk_io_counters = psutil.disk_io_counters(perdisk=True) or {}
    except Exception as e:
        logger.warning("Failed to collect disk IO counters: {}", e)

    # Disk metrics
    for disk in disks:
        try:
            usage = psutil.disk_usage(disk)
        except Exception as e:
            logger.warning("Disk {} not found or inaccessible: {}", disk, e)
            continue

        records.append({
            "metric": "disk_usage",
            "path": disk,
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": usage.percent,
            "ts": datetime.now(timezone.utc).isoformat()
        })
        logger.debug("Disk {}: {}% used", disk, usage.percent)

        try:
            # psutil keys perdisk counters by physical drive on Windows, so a
            # direct drive-letter lookup misses. Try a direct match first (e.g.
            # a Linux device name, or a key that already is a physical drive),
            # then fall back to mapping the volume to its physical drive(s).
            if disk in disk_io_counters:
                io_keys = [disk]
            else:
                io_keys = _resolve_perdisk_keys(disk)

            io_data = _sum_disk_io(io_keys, disk_io_counters) if io_keys else None
            if io_data is not None:
                records.append({
                    "metric": "disk_io",
                    "path": disk,
                    "read_bytes": io_data["read_bytes"],
                    "write_bytes": io_data["write_bytes"],
                    "read_count": io_data["read_count"],
                    "write_count": io_data["write_count"],
                    "ts": datetime.now(timezone.utc).isoformat()
                })
            else:
                logger.warning("Disk IO data not found for {}; emitting zeros", disk)
                records.append({
                    "metric": "disk_io",
                    "path": disk,
                    "read_bytes": 0,
                    "write_bytes": 0,
                    "read_count": 0,
                    "write_count": 0,
                    "warning": f"Disk IO data not found for {disk}",
                    "ts": datetime.now(timezone.utc).isoformat()
                })
        except Exception as e:
            logger.warning("Failed to collect disk IO for {}: {}", disk, e)

    # Stamp the host onto every record in one place so the guarantee holds
    # structurally: any metric added above cannot forget to carry the host.
    for record in records:
        record["host"] = host

    logger.debug("Host metrics collected: {} record(s)", len(records))
    return records
