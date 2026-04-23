import logging

from flask import Flask, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

SHORT_THRESHOLD = 60  # seconds
TRANSCRIPT_MAX_CHARS = 10_000


def _get_video_metadata(video_id: str) -> dict | None:
    """Return video metadata dict using yt-dlp, or None on failure."""
    try:
        import yt_dlp

        opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
            return {
                "duration": int(info.get("duration") or 0),
                "webpage_url": info.get("webpage_url", ""),
                "original_url": info.get("original_url", ""),
            }
    except Exception as e:
        log.warning("yt-dlp failed for %s: %s", video_id, e)
        return None


def _is_short(metadata: dict) -> bool:
    """Determine if a video is a YouTube Short from yt-dlp metadata."""
    # Primary: yt-dlp resolves the real URL which contains /shorts/ for Shorts
    for url_field in ("webpage_url", "original_url"):
        if "/shorts/" in metadata.get(url_field, ""):
            return True
    # Fallback: duration-based heuristic
    duration = metadata.get("duration")
    return duration is not None and duration < SHORT_THRESHOLD


def _get_transcript(video_id: str) -> tuple[str | None, str | None]:
    """Return (transcript_text, language) or (None, None) on failure."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        ytt_api = YouTubeTranscriptApi()
        transcript = ytt_api.fetch(video_id)
        text = " ".join(snippet.text for snippet in transcript.snippets)
        language = transcript.language
        return text[:TRANSCRIPT_MAX_CHARS], language
    except Exception as e:
        log.warning("Transcript unavailable for %s: %s", video_id, e)
        return None, None


@app.route("/video-info")
def video_info():
    video_id = request.args.get("v", "").strip()
    if not video_id:
        return jsonify({"error": "Missing ?v= parameter"}), 400

    metadata = _get_video_metadata(video_id)
    duration = metadata["duration"] if metadata else None
    is_short = _is_short(metadata) if metadata else False

    transcript = None
    language = None
    if not is_short:
        transcript, language = _get_transcript(video_id)

    return jsonify(
        {
            "video_id": video_id,
            "duration": duration,
            "is_short": is_short,
            "transcript": transcript,
            "language": language,
            "error": None,
        }
    )


@app.route("/test-short")
def test_short():
    """Diagnostic endpoint showing how Shorts detection works for a video."""
    video_id = request.args.get("v", "").strip()
    if not video_id:
        return jsonify({"error": "Missing ?v= parameter"}), 400

    metadata = _get_video_metadata(video_id)
    if not metadata:
        return jsonify({"video_id": video_id, "error": "yt-dlp metadata fetch failed"})

    url_match = any(
        "/shorts/" in metadata.get(f, "") for f in ("webpage_url", "original_url")
    )
    duration_match = (
        metadata["duration"] is not None and metadata["duration"] < SHORT_THRESHOLD
    )

    return jsonify(
        {
            "video_id": video_id,
            "is_short": _is_short(metadata),
            "detection": {
                "url_match": url_match,
                "duration_match": duration_match,
                "webpage_url": metadata.get("webpage_url", ""),
                "original_url": metadata.get("original_url", ""),
                "duration": metadata["duration"],
                "threshold": SHORT_THRESHOLD,
            },
        }
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok"})
