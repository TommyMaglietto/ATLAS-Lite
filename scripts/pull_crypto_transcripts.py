"""
Pull YouTube transcripts for crypto AI trading strategy videos.
Uses TransD tools.
"""

import sys
import json
import time
from datetime import datetime

sys.path.insert(0, r'C:\Users\magli\Desktop\TransD')
from transcript_utils import fetch_transcript, TranscriptError
from youtube_utils import extract_video_id, fetch_video_title

# Curated list of crypto trading strategy videos from well-known channels.
# These cover: RSI strategies, Bollinger Bands, mean reversion, grid trading,
# AI/ML bots, backtested results, and automated crypto strategies.
VIDEOS = [
    # Trading Rush - "I Tested Bollinger Bands + RSI Trading Strategy 100 Times"
    "https://www.youtube.com/watch?v=KV_JIcuToK4",
    # Trading Rush - "I Tested RSI + MACD Trading Strategy 100 Times"
    "https://www.youtube.com/watch?v=4FWCSzbnYJE",
    # Trading Rush - "I Tested RSI + SuperTrend Trading Strategy 100 Times"
    "https://www.youtube.com/watch?v=DkbH4gErzE8",
    # Coding Jesus - "I Coded A Trading Bot and Gave It $1000 to Trade"
    "https://www.youtube.com/watch?v=GdlFhF6gjKo",
    # Moon Dev - "I Coded an AI Crypto Trading Bot"
    "https://www.youtube.com/watch?v=c9OjEThuJjY",
    # The Moving Average - "The Only RSI Trading Strategy You Will Ever Need"
    "https://www.youtube.com/watch?v=LlvDBSGnkHU",
    # Nicholas Renotte - "Build an AI Crypto Trading Bot"
    "https://www.youtube.com/watch?v=GFQaEYi1bps",
    # Part Time Larry - Crypto Bot w/ SuperTrend strategy
    "https://www.youtube.com/watch?v=fpqzXgZjSqM",
    # Algovibes - "Simple but Profitable Crypto Trading Strategy"
    "https://www.youtube.com/watch?v=o3Nli0bKByI",
    # Investopedia - "Bollinger Bands Trading Strategy"
    "https://www.youtube.com/watch?v=tkJRl6qO7aQ",
    # Trading 212 - "How to Use RSI Indicator for Day Trading"
    "https://www.youtube.com/watch?v=TnWPfb2kiac",
    # Rayner Teo - "Bollinger Bands Trading Strategy"
    "https://www.youtube.com/watch?v=oMse58GfMmI",
]

results = []
failed = []

for url in VIDEOS:
    vid = extract_video_id(url)
    if not vid:
        print(f"SKIP - Could not extract video ID from: {url}")
        failed.append({"url": url, "error": "Could not extract video ID"})
        continue

    # Get title
    try:
        title = fetch_video_title(vid)
    except Exception:
        title = vid

    print(f"\n{'='*60}")
    print(f"Fetching: {title} ({vid})")
    print(f"{'='*60}")

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
        print(f"  FAILED - {e}")
        failed.append({"url": url, "video_id": vid, "title": title, "error": str(e)})
    except Exception as e:
        print(f"  ERROR - {e}")
        failed.append({"url": url, "video_id": vid, "title": title, "error": str(e)})

    # Small delay to avoid rate limiting
    time.sleep(1)

print(f"\n\n{'='*60}")
print(f"RESULTS: {len(results)} succeeded, {len(failed)} failed")
print(f"{'='*60}")

for r in results:
    print(f"  OK: {r['title']} ({r['word_count']} words)")
for f in failed:
    print(f"  FAIL: {f.get('title', f['url'])} - {f['error']}")

# Save raw results as JSON for further processing
output_path = r'C:\Users\magli\Desktop\TradeEngine\docs\transcripts_raw.json'
with open(output_path, 'w', encoding='utf-8') as fh:
    json.dump({"fetched": datetime.now().isoformat(), "results": results, "failed": failed}, fh, indent=2, ensure_ascii=False)
print(f"\nRaw transcripts saved to: {output_path}")
