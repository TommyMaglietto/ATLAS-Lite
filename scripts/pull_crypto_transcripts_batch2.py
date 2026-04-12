"""
Batch 2: More crypto trading strategy video transcripts.
"""

import sys
import json
import time
from datetime import datetime

sys.path.insert(0, r'C:\Users\magli\Desktop\TransD')
from transcript_utils import fetch_transcript, TranscriptError
from youtube_utils import extract_video_id, fetch_video_title

# More video candidates - popular algo trading / crypto strategy videos
VIDEOS = [
    # Algovibes - Algorithmic Trading Strategy in Python
    "https://www.youtube.com/watch?v=SEQbb8w7VTw",
    # Freqtrade - Building a Crypto Trading Bot
    "https://www.youtube.com/watch?v=s-RGiI4Xplo",
    # Sentdex - Python for Finance crypto trading
    "https://www.youtube.com/watch?v=2BrpKpWwT2A",
    # Bybit grid bot strategy tutorial
    "https://www.youtube.com/watch?v=kplxWD0Ceuw",
    # Konstantin Borimechkov - I Coded a Trading Bot
    "https://www.youtube.com/watch?v=c_MjMbQK0Zk",
    # Crypto Trading Strategy That Actually Works 2024
    "https://www.youtube.com/watch?v=lZs0m2m2F7Y",
    # Siraj Raval - Build a ChatGPT Trading Bot
    "https://www.youtube.com/watch?v=Itz-bVj25WQ",
    # CryptoJebb - RSI Divergence Bitcoin Strategy
    "https://www.youtube.com/watch?v=VZ-b_T4PMCQ",
    # Benjamin Cowen - Risk Metric Crypto Strategy
    "https://www.youtube.com/watch?v=xLYN6B3HUAY",
    # DataDash - Crypto DCA Strategy
    "https://www.youtube.com/watch?v=gPW0jhg_DhM",
    # The Art of Trading - Mean Reversion Strategy
    "https://www.youtube.com/watch?v=s9eCH6j2gBo",
    # Bob Sharpe - Python Crypto Trading Bot from Scratch
    "https://www.youtube.com/watch?v=ndlkCMmPOcM",
    # Coding Trading Bot - Bollinger Band Squeeze Strategy
    "https://www.youtube.com/watch?v=F1YBiAJYI_0",
    # MACD + RSI Strategy Backtest
    "https://www.youtube.com/watch?v=7RjOvVj_5kA",
    # Grid Bot Crypto Strategy Tutorial
    "https://www.youtube.com/watch?v=1PEiddAVjgU",
]

results = []
failed = []

for url in VIDEOS:
    vid = extract_video_id(url)
    if not vid:
        print(f"SKIP - Could not extract video ID from: {url}")
        failed.append({"url": url, "error": "Could not extract video ID"})
        continue

    try:
        title = fetch_video_title(vid)
    except Exception:
        title = vid

    print(f"\nFetching: {title} ({vid})")

    try:
        transcript = fetch_transcript(vid)
        word_count = len(transcript.split())
        print(f"  SUCCESS - {word_count} words")
        results.append({
            "video_id": vid,
            "title": title,
            "url": url,
            "transcript": transcript,
            "word_count": word_count
        })
    except TranscriptError as e:
        err_short = str(e).split('\n')[0][:80]
        print(f"  FAILED - {err_short}")
        failed.append({"url": url, "video_id": vid, "title": title, "error": str(e)})
    except Exception as e:
        print(f"  ERROR - {e}")
        failed.append({"url": url, "video_id": vid, "title": title, "error": str(e)})

    time.sleep(1)

print(f"\nBatch 2 RESULTS: {len(results)} succeeded, {len(failed)} failed")
for r in results:
    print(f"  OK: {r['title']} ({r['word_count']} words)")

# Load batch 1 results and merge
batch1_path = r'C:\Users\magli\Desktop\TradeEngine\docs\transcripts_raw.json'
try:
    with open(batch1_path, 'r', encoding='utf-8') as fh:
        batch1 = json.load(fh)
    all_results = batch1.get("results", []) + results
    all_failed = batch1.get("failed", []) + failed
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

with open(batch1_path, 'w', encoding='utf-8') as fh:
    json.dump(output, fh, indent=2, ensure_ascii=False)
print(f"\nMerged total: {len(all_results)} transcripts saved to {batch1_path}")
