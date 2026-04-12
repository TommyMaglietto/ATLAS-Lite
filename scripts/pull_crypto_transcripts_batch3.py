"""
Batch 3: Pull transcripts from verified YouTube video IDs found via live search.
"""

import sys
import io
import json
import time
from datetime import datetime

# Fix Windows console encoding for emoji in titles
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

sys.path.insert(0, r'C:\Users\magli\Desktop\TransD')
from transcript_utils import fetch_transcript, TranscriptError
from youtube_utils import extract_video_id, fetch_video_title

# Video IDs extracted from live YouTube search results
VIDEOS = {
    # Search 1: RSI / Bollinger Bands / Backtest strategies
    "pCmJ8wsAS_w": "Bollinger Band + RSI Trading Strategy That Actually Works",
    "j2ESnjhT2no": "I Tested RSI + Bollinger Bands Strategy in 2025: Crypto, Stocks, Futures, Forex",
    "c9-SIpy3dEw": "Mean Reversion Trading Strategy Explained and Backtested - 179% Profit",
    "RbQaARxEW9o": "Python Backtest of A Scalping Strategy with VWAP, Bollinger Bands and RSI",
    "TgJgKp_DRhk": "The Bollinger band and RSI strategy - Is it crypto trading bot strategy profitable",
    "N7uP9V0Iktc": "The ONLY 2 Indicators I use to make $4351/Day Trading",
    # Search 2: AI / mean reversion / grid / DCA bot strategies
    "5I2vtNovJcQ": "GPT Mean Reversion strategy in Python makes 813%",
    "B0OPP-OVZhw": "How to Build a Trading Bot in Python Full Algorithmic Trading Tutorial",
    "fdUHWEpkAvQ": "Building Mean Reversion Bots From Scratch - The Full Pipeline",
    "YDhncCUbXm4": "How To Make Money With Crypto Grid Bots In 2025",
    "ruhou5iXxvY": "Beginners Guide To AI Crypto Trading Bots In 2025 Pionex",
    "_bse0xQgXXo": "EASIEST Bollinger Bands Crypto Trading Strategy",
}

# Load previous results to skip already-fetched videos
raw_path = r'C:\Users\magli\Desktop\TradeEngine\docs\transcripts_raw.json'
try:
    with open(raw_path, 'r', encoding='utf-8') as fh:
        prev_data = json.load(fh)
    already_fetched = {r['video_id'] for r in prev_data.get('results', [])}
except Exception:
    prev_data = None
    already_fetched = set()

results = []
failed = []

for vid, fallback_title in VIDEOS.items():
    if vid in already_fetched:
        print(f"SKIP (already have): {fallback_title} ({vid})")
        continue
    try:
        title = fetch_video_title(vid)
    except Exception:
        title = fallback_title

    print(f"\nFetching: {title} ({vid})")

    try:
        transcript = fetch_transcript(vid)
        word_count = len(transcript.split())
        print(f"  SUCCESS - {word_count} words")
        results.append({
            "video_id": vid,
            "title": title,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "transcript": transcript,
            "word_count": word_count
        })
    except TranscriptError as e:
        err_short = str(e).split('\n')[0][:80]
        print(f"  FAILED - {err_short}")
        failed.append({"video_id": vid, "title": title, "error": str(e)[:120]})
    except Exception as e:
        print(f"  ERROR - {e}")
        failed.append({"video_id": vid, "title": title, "error": str(e)[:120]})

    time.sleep(1.5)

print(f"\nBatch 3 RESULTS: {len(results)} succeeded, {len(failed)} failed")
for r in results:
    print(f"  OK: {r['title']} ({r['word_count']} words)")
for f in failed:
    print(f"  FAIL: {f['title']}")

# Load previous results and merge
raw_path = r'C:\Users\magli\Desktop\TradeEngine\docs\transcripts_raw.json'
try:
    with open(raw_path, 'r', encoding='utf-8') as fh:
        prev = json.load(fh)
    # Deduplicate by video_id
    existing_ids = {r['video_id'] for r in prev.get('results', [])}
    new_results = [r for r in results if r['video_id'] not in existing_ids]
    all_results = prev.get('results', []) + new_results
    all_failed = prev.get('failed', []) + failed
except Exception:
    all_results = results
    all_failed = failed

output = {
    "fetched": datetime.now().isoformat(),
    "total_succeeded": len(all_results),
    "total_failed": len(all_failed),
    "results": all_results,
    "failed": all_failed
}

with open(raw_path, 'w', encoding='utf-8') as fh:
    json.dump(output, fh, indent=2, ensure_ascii=False)
print(f"\nMerged total: {len(all_results)} transcripts saved to {raw_path}")
