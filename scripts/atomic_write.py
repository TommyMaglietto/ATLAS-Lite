#!/usr/bin/env python3
"""
Atomic file writing utility for ATLAS Lite.
Writes to a temporary file, then renames to target (atomic operation on POSIX systems).
Ensures state files are never corrupted mid-write.
"""

import json
import os
import tempfile
import sys
import time
import msvcrt
from contextlib import contextmanager
from pathlib import Path

# ---------------------------------------------------------------------------
# File-locking constants
# ---------------------------------------------------------------------------
_LOCK_RETRIES = 10
_LOCK_RETRY_DELAY = 0.5        # seconds between retries
_LOCK_STALE_SECONDS = 60       # force-remove locks older than this


def atomic_write_json(filepath, data):
    """
    Atomically write JSON data to a file.

    Args:
        filepath (str): Target file path
        data (dict): Python dictionary to serialize as JSON

    Returns:
        bool: True on success, False on failure

    Example:
        atomic_write_json('state/positions.json', {'positions': []})
    """
    try:
        filepath = Path(filepath)
        # Ensure directory exists
        filepath.parent.mkdir(parents=True, exist_ok=True)

        # Write to temporary file
        with tempfile.NamedTemporaryFile(
            mode='w',
            dir=filepath.parent,
            delete=False,
            suffix='.tmp',
            encoding='utf-8'
        ) as tmp:
            json.dump(data, tmp, indent=2, ensure_ascii=False)
            tmp_name = tmp.name

        # Atomic rename (overwrites target)
        os.replace(tmp_name, str(filepath))
        return True

    except Exception as e:
        print(f"ERROR: atomic_write_json failed for {filepath}: {e}", file=sys.stderr)
        # Clean up temp file if it exists
        try:
            if 'tmp_name' in locals():
                os.remove(tmp_name)
        except:
            pass
        return False


def atomic_write_text(filepath, content):
    """
    Atomically write text content to a file.

    Args:
        filepath (str): Target file path
        content (str): Text to write

    Returns:
        bool: True on success, False on failure
    """
    try:
        filepath = Path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            mode='w',
            dir=filepath.parent,
            delete=False,
            suffix='.tmp',
            encoding='utf-8'
        ) as tmp:
            tmp.write(content)
            tmp_name = tmp.name

        os.replace(tmp_name, str(filepath))
        return True

    except Exception as e:
        print(f"ERROR: atomic_write_text failed for {filepath}: {e}", file=sys.stderr)
        try:
            if 'tmp_name' in locals():
                os.remove(tmp_name)
        except:
            pass
        return False


def atomic_read_json(filepath):
    """
    Safely read JSON file.

    Args:
        filepath (str): File path to read

    Returns:
        dict: Parsed JSON object, or None on failure
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"ERROR: atomic_read_json failed for {filepath}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# File-locking helpers (Windows / msvcrt)
# ---------------------------------------------------------------------------

class LockTimeout(Exception):
    """Raised when a file lock cannot be acquired within the retry limit."""


def _lock_path(filepath):
    """Return the .lock companion path for *filepath*."""
    return str(Path(filepath).with_suffix(Path(filepath).suffix + '.lock'))


def _remove_stale_lock(lock_file):
    """
    If *lock_file* exists and is older than _LOCK_STALE_SECONDS, delete it.
    This guards against orphaned locks left by crashes.
    """
    try:
        if os.path.exists(lock_file):
            age = time.time() - os.path.getmtime(lock_file)
            if age > _LOCK_STALE_SECONDS:
                os.remove(lock_file)
                print(
                    f"WARNING: removed stale lock {lock_file} (age={age:.0f}s)",
                    file=sys.stderr,
                )
    except OSError:
        # Another process may have already cleaned it up — that is fine.
        pass


@contextmanager
def file_lock(filepath):
    """
    Context manager that holds an exclusive lock backed by a .lock file.

    Uses msvcrt.locking (Windows) on the open lock-file handle so that the
    OS itself prevents concurrent access even across processes.

    Usage:
        with file_lock('state/trailing_stops.json'):
            data = atomic_read_json('state/trailing_stops.json')
            data['key'] = 'value'
            atomic_write_json('state/trailing_stops.json', data)

    Raises:
        LockTimeout: if the lock cannot be acquired after _LOCK_RETRIES attempts.
    """
    lock_file = _lock_path(filepath)
    Path(lock_file).parent.mkdir(parents=True, exist_ok=True)

    _remove_stale_lock(lock_file)

    fh = None
    acquired = False
    for attempt in range(1, _LOCK_RETRIES + 1):
        try:
            # Open (or create) the lock file in read/write mode.
            fh = open(lock_file, 'w')
            # msvcrt.locking operates on the file descriptor.
            # LK_NBLCK = non-blocking exclusive lock; raises OSError on failure.
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            acquired = True
            break
        except (OSError, IOError):
            # Could not lock — another process holds it.
            if fh is not None:
                fh.close()
                fh = None
            if attempt < _LOCK_RETRIES:
                time.sleep(_LOCK_RETRY_DELAY)

    if not acquired:
        raise LockTimeout(
            f"Could not acquire lock on {lock_file} after "
            f"{_LOCK_RETRIES} retries ({_LOCK_RETRIES * _LOCK_RETRY_DELAY:.1f}s)"
        )

    try:
        yield
    finally:
        # Release the lock and clean up.
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except (OSError, IOError):
            pass
        try:
            fh.close()
        except (OSError, IOError):
            pass
        try:
            os.remove(lock_file)
        except OSError:
            pass


def locked_read_modify_write(filepath, modifier_fn):
    """
    Atomically read-modify-write a JSON state file under an exclusive lock.

    1. Acquires a file lock (via file_lock).
    2. Reads the current JSON from *filepath* (or starts with {} if missing).
    3. Calls ``modifier_fn(data)`` which MUST return the modified dict.
    4. Atomically writes the result back via atomic_write_json.

    Args:
        filepath (str): Path to the JSON state file.
        modifier_fn (callable): ``fn(data) -> data``. Receives the current
            contents as a dict and must return the updated dict.

    Returns:
        dict: The data as written (i.e. the return value of modifier_fn).

    Raises:
        LockTimeout: if the lock cannot be acquired.
        TypeError: if modifier_fn does not return a dict.
        Exception: any error from reading or writing propagates up.

    Example:
        def bump_counter(data):
            data['counter'] = data.get('counter', 0) + 1
            return data

        new_data = locked_read_modify_write('state/trailing_stops.json', bump_counter)
    """
    filepath = str(filepath)

    with file_lock(filepath):
        # Read current data (or empty dict if file does not yet exist).
        data = atomic_read_json(filepath)
        if data is None:
            data = {}

        # Apply the caller's transformation.
        updated = modifier_fn(data)

        if not isinstance(updated, dict):
            raise TypeError(
                f"modifier_fn must return a dict, got {type(updated).__name__}"
            )

        # Write back atomically.
        success = atomic_write_json(filepath, updated)
        if not success:
            raise IOError(f"atomic_write_json failed for {filepath}")

        return updated


# ---------------------------------------------------------------------------
# Shared symbol normalization (used by trailing_stop_monitor, reconcile,
# performance_tracker — consolidated here to avoid duplication)
# ---------------------------------------------------------------------------
CRYPTO_BASES = (
    "BTC", "ETH", "SOL", "DOGE", "AVAX", "LINK",
    "AAVE", "UNI", "DOT", "MATIC", "SHIB", "ADA",
    "XRP", "ALGO", "ATOM", "FTM", "NEAR", "OP",
    "ARB", "APE",
)


def normalize_crypto_symbol(symbol: str) -> str:
    """
    Normalize crypto symbol formats for consistent matching.

    * ``BTCUSD``  -> ``BTC/USD``
    * ``ETHUSD``  -> ``ETH/USD``
    * ``BTC/USD`` -> ``BTC/USD``  (already slashed — returned as-is)
    * ``AAPL``    -> ``AAPL``     (equity — returned unchanged)

    Handles the known crypto bases: BTC, ETH, SOL, DOGE, AVAX, LINK,
    AAVE, UNI, DOT, MATIC, SHIB, ADA, XRP, ALGO, ATOM, FTM, NEAR,
    OP, ARB, APE.
    """
    if "/" in symbol:
        return symbol
    for base in CRYPTO_BASES:
        if symbol.startswith(base) and symbol.endswith("USD"):
            return f"{base}/USD"
    return symbol


if __name__ == '__main__':
    # Self-test
    test_data = {'test': 'value', 'number': 42}
    test_file = Path('/tmp/test_atomic_write.json')

    # Test write
    if atomic_write_json(str(test_file), test_data):
        print(f"Write succeeded: {test_file}")
    else:
        print(f"Write failed")
        sys.exit(1)

    # Test read
    read_data = atomic_read_json(str(test_file))
    if read_data == test_data:
        print(f"Read succeeded, data matches")
    else:
        print(f"Read failed or data mismatch")
        sys.exit(1)

    # Cleanup
    test_file.unlink()
    print("All tests passed")
