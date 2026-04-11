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
from pathlib import Path


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
