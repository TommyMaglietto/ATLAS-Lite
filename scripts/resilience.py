#!/usr/bin/env python3
"""
ATLAS Lite - Resilience Hardening Utilities (Phase 3)

Provides retry logic, timeout configuration, spread validation,
PID-file singleton locks, safe state writes, and minimum quantity checks
for robust trading operations.
"""

import atexit
import functools
import inspect
import os
import random
import shutil
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Project root (TradeEngine/)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Add scripts/ to path so we can import atomic_write
# ---------------------------------------------------------------------------
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from atomic_write import locked_read_modify_write


# ===========================================================================
# 3A: retry_api decorator
# ===========================================================================

def retry_api(max_retries=3, base_delay=1.0, max_delay=30.0, retryable_exceptions=(Exception,)):
    """Decorator for Alpaca API calls with exponential backoff + jitter.

    Args:
        max_retries: Maximum number of retry attempts (default 3).
        base_delay: Base delay in seconds before first retry (default 1.0).
        max_delay: Maximum delay cap in seconds (default 30.0).
        retryable_exceptions: Tuple of exception types that trigger a retry.

    Returns:
        Decorated function that retries on specified exceptions.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return fn(*args, **kwargs)
                except retryable_exceptions as exc:
                    last_exc = exc
                    if attempt < max_retries:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        jitter = random.uniform(0, delay * 0.10)
                        total_delay = delay + jitter
                        print(
                            f"  RETRY {attempt + 1}/{max_retries}: "
                            f"{fn.__name__}() failed with {type(exc).__name__}: {exc} "
                            f"-- retrying in {total_delay:.1f}s"
                        )
                        time.sleep(total_delay)
                    else:
                        # Final attempt failed -- re-raise
                        raise last_exc
        return wrapper
    return decorator


# ===========================================================================
# 3B: configure_client_timeouts
# ===========================================================================

def configure_client_timeouts(client, connect=10.0, read=30.0, write=10.0, pool=10.0):
    """Set socket timeouts on Alpaca SDK clients.

    Tries to find the underlying httpx client and apply a Timeout object.
    Silently warns if it fails (different SDK versions may have different
    internals).

    Args:
        client: An Alpaca TradingClient or DataClient instance.
        connect: Connection timeout in seconds (default 10.0).
        read: Read timeout in seconds (default 30.0).
        write: Write timeout in seconds (default 10.0).
        pool: Connection pool timeout in seconds (default 10.0).
    """
    try:
        import httpx
        timeout = httpx.Timeout(connect=connect, read=read, write=write, pool=pool)

        # Try common internal attribute names across SDK versions
        for attr_name in ("_session", "_http_client", "_client"):
            http_client = getattr(client, attr_name, None)
            if http_client is not None and hasattr(http_client, "timeout"):
                http_client.timeout = timeout
                return

        print(
            f"  WARNING: configure_client_timeouts -- "
            f"could not find httpx client on {type(client).__name__}; "
            f"timeouts not applied"
        )
    except ImportError:
        print("  WARNING: configure_client_timeouts -- httpx not installed; timeouts not applied")
    except Exception as e:
        print(f"  WARNING: configure_client_timeouts failed: {e}")


# ===========================================================================
# 3C: validate_spread
# ===========================================================================

def validate_spread(data_client, symbol, max_spread_pct=0.5):
    """Check bid-ask spread before market order.

    Args:
        data_client: An Alpaca CryptoHistoricalDataClient instance.
        symbol: Crypto symbol (e.g. 'BTC/USD').
        max_spread_pct: Maximum acceptable spread percentage (default 0.5).

    Returns:
        Tuple of (ok, spread_pct, midpoint):
            ok: True if spread is within tolerance.
            spread_pct: Computed bid-ask spread percentage.
            midpoint: Midpoint price between bid and ask.
    """
    try:
        from alpaca.data.requests import CryptoLatestQuoteRequest

        req = CryptoLatestQuoteRequest(symbol_or_symbols=[symbol])
        quotes = data_client.get_crypto_latest_quote(req)
        quote = quotes.get(symbol)

        if quote is None:
            # Fail open -- don't block trades on quote failures
            return True, 0.0, 0.0

        bid = float(quote.bid_price) if quote.bid_price else 0.0
        ask = float(quote.ask_price) if quote.ask_price else 0.0

        if bid <= 0 or ask <= 0:
            # Fail open -- incomplete quote data
            return True, 0.0, 0.0

        spread_pct = (ask - bid) / bid * 100
        midpoint = (bid + ask) / 2.0

        return (spread_pct <= max_spread_pct, spread_pct, midpoint)

    except Exception as e:
        # Fail open -- don't block trades on quote failures
        print(f"  WARNING: validate_spread({symbol}) failed: {e}")
        return True, 0.0, 0.0


# ===========================================================================
# 3D: acquire_pid_lock
# ===========================================================================

# Track PID file paths so the atexit handler can clean them up
_active_pid_files = []


def _cleanup_pid_file(pid_file):
    """atexit handler: remove PID file if it belongs to this process."""
    try:
        if os.path.exists(pid_file):
            with open(pid_file, "r") as f:
                stored_pid = int(f.read().strip())
            if stored_pid == os.getpid():
                os.remove(pid_file)
    except Exception:
        pass


def acquire_pid_lock(script_name=None):
    """PID-file singleton lock. Returns True if acquired, False if another instance running.

    PID files are stored in state/{script_name}.pid.

    Args:
        script_name: Name for the PID file (without .pid extension).
            If None, auto-detects from the calling script's filename.

    Returns:
        True if the lock was acquired (this is the only running instance).
        False if another instance is already running.
    """
    if script_name is None:
        # Auto-detect from the caller's filename
        frame = inspect.stack()[1]
        caller_file = frame.filename
        script_name = Path(caller_file).stem

    pid_dir = PROJECT_ROOT / "state"
    pid_dir.mkdir(parents=True, exist_ok=True)
    pid_file = str(pid_dir / f"{script_name}.pid")

    if os.path.exists(pid_file):
        try:
            with open(pid_file, "r") as f:
                old_pid = int(f.read().strip())

            # Check if the old process is still alive
            os.kill(old_pid, 0)
            # If we get here, the process is alive
            print(f"  PID LOCK: Another instance ({script_name}, pid={old_pid}) is running. Exiting.")
            return False

        except (ProcessLookupError, PermissionError):
            # ProcessLookupError: process is dead (stale PID file)
            # PermissionError on Windows: process exists but we lack permissions
            #   -- treat PermissionError as "alive" on Windows for safety
            if sys.platform == "win32":
                try:
                    os.kill(old_pid, 0)
                except ProcessLookupError:
                    pass  # Dead process, proceed to overwrite
                except PermissionError:
                    # On Windows, PermissionError from os.kill means process IS alive
                    print(f"  PID LOCK: Another instance ({script_name}, pid={old_pid}) is running. Exiting.")
                    return False
            # On non-Windows, ProcessLookupError means dead -- fall through
        except (ValueError, OSError):
            # Corrupt PID file or other OS error -- overwrite it
            pass

    # Write our PID
    try:
        with open(pid_file, "w") as f:
            f.write(str(os.getpid()))
    except Exception as e:
        print(f"  WARNING: Could not write PID file {pid_file}: {e}")
        return True  # Fail open -- don't block the script

    # Register atexit cleanup
    _active_pid_files.append(pid_file)
    atexit.register(_cleanup_pid_file, pid_file)

    return True


# ===========================================================================
# 3E: safe_state_write
# ===========================================================================

def safe_state_write(filepath, modifier_fn):
    """Disk-space check + locked_read_modify_write with error handling.

    Checks for at least 50MB free disk space before performing the write.
    On failure, prints a CRITICAL warning and returns None.

    Args:
        filepath: Path to the JSON state file.
        modifier_fn: Callable that receives a dict and returns the modified dict.

    Returns:
        The updated dict on success, or None on failure.
    """
    # Check disk space (minimum 50 MB free)
    try:
        usage = shutil.disk_usage(Path(filepath).parent)
        free_mb = usage.free / (1024 * 1024)
        if free_mb < 50:
            print(
                f"  CRITICAL: Low disk space ({free_mb:.1f} MB free) -- "
                f"skipping write to {filepath}",
                file=sys.stderr,
            )
            return None
    except Exception as e:
        # Can't check disk space -- proceed anyway
        print(f"  WARNING: Could not check disk space: {e}")

    try:
        result = locked_read_modify_write(filepath, modifier_fn)
        return result
    except (IOError, OSError) as e:
        print(
            f"  CRITICAL: safe_state_write failed for {filepath}: {e}",
            file=sys.stderr,
        )
        return None


# ===========================================================================
# 3F: validate_min_qty
# ===========================================================================

# Per-asset minimum quantities for crypto
_CRYPTO_MIN_QTY = {
    "BTC/USD": 0.00001,
    "ETH/USD": 0.0001,
    "SOL/USD": 0.01,
    "DOGE/USD": 1.0,
    "AVAX/USD": 0.01,
    "LINK/USD": 0.01,
}
_CRYPTO_DEFAULT_MIN = 0.001
_EQUITY_MIN_QTY = 1


def validate_min_qty(symbol, qty, asset_class="crypto"):
    """Per-asset minimum quantity check.

    Args:
        symbol: Asset symbol (e.g. 'BTC/USD', 'AAPL').
        qty: Quantity to validate.
        asset_class: 'crypto' or 'equity' (default 'crypto').

    Returns:
        Tuple of (ok, min_qty):
            ok: True if qty meets the minimum requirement.
            min_qty: The minimum quantity for this asset.
    """
    if asset_class == "crypto":
        min_q = _CRYPTO_MIN_QTY.get(symbol, _CRYPTO_DEFAULT_MIN)
    else:
        min_q = _EQUITY_MIN_QTY

    return (qty >= min_q, min_q)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=== resilience.py self-test ===")

    # Test retry_api decorator
    _counter = [0]

    @retry_api(max_retries=2, base_delay=0.1, max_delay=1.0)
    def flaky_func():
        _counter[0] += 1
        if _counter[0] < 3:
            raise ConnectionError("simulated failure")
        return "success"

    result = flaky_func()
    assert result == "success", f"Expected 'success', got {result}"
    assert _counter[0] == 3, f"Expected 3 calls, got {_counter[0]}"
    print("  retry_api: OK")

    # Test validate_min_qty
    ok, min_q = validate_min_qty("BTC/USD", 0.001)
    assert ok is True
    ok, min_q = validate_min_qty("BTC/USD", 0.000001)
    assert ok is False
    ok, min_q = validate_min_qty("DOGE/USD", 0.5)
    assert ok is False and min_q == 1.0
    ok, min_q = validate_min_qty("AAPL", 1, asset_class="equity")
    assert ok is True
    ok, min_q = validate_min_qty("AAPL", 0.5, asset_class="equity")
    assert ok is False
    print("  validate_min_qty: OK")

    # Test acquire_pid_lock
    got_lock = acquire_pid_lock("resilience_test")
    assert got_lock is True
    # Second call with same name should also succeed (same process)
    # because os.kill(our_pid, 0) succeeds but we ARE the same process
    # Clean up test PID file
    test_pid = str(PROJECT_ROOT / "state" / "resilience_test.pid")
    if os.path.exists(test_pid):
        os.remove(test_pid)
    print("  acquire_pid_lock: OK")

    # Test validate_spread (no client, just error path)
    ok, spread, mid = validate_spread(None, "BTC/USD")
    assert ok is True  # Fail open
    print("  validate_spread (fail-open): OK")

    print("\nAll resilience.py self-tests passed.")
