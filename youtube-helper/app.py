import logging

from flask import Flask, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

SHORT_THRESHOLD = 60  # seconds
TRANSCRIPT_MAX_CHARS = 10_000


def _get_duration(video_id: str) -> int | None:
    """Return video duration in seconds using yt-dlp, or None on failure."""
    try:
        import yt_dlp

        opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
            return int(info.get("duration") or 0)
    except Exception as e:
        log.warning("yt-dlp failed for %s: %s", video_id, e)
        return None


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

    duration = _get_duration(video_id)
    is_short = duration is not None and duration < SHORT_THRESHOLD

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


@app.route("/health")
def health():
    return jsonify({"status": "ok"})
