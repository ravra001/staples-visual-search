"""
Voice input — speech-to-text for the Staples AI chat's mic button.

Google Cloud Speech-to-Text (speech.googleapis.com), same GCP project and
credentials as Vertex AI (agent.py's Gemini calls, embeddings.py's vertex
backend) -- a separate API to enable, not a separate project to configure.
Deliberately NOT a locally-bundled model the way CLIP is: unlike CLIP (an
embedding, needed on every search, worth the offline/zero-marginal-cost
tradeoff), speech-to-text only runs on an explicit mic click, so the
per-call cost and network dependency are much less consequential, and Cloud
Speech-to-Text's accuracy on real speech is a genuine step up over a small
locally-bundled model for a live demo.

Text stops here: the transcript is handed back to the browser to fill the
existing chat input box, NOT auto-submitted -- transcription errors on
shopping-specific terms (SKUs, brand names) are common enough that a quick
glance before sending is worth the extra click.
"""
import io
import sys
import threading
import traceback

import config

# Same reasoning as embeddings.py's identical block: on TLS-intercepting
# networks (corporate proxy / AV), Python's bundled CA set won't trust the
# interception cert, so the gRPC/HTTPS calls Cloud Speech-to-Text's client
# makes fail with CERTIFICATE_VERIFY_FAILED. truststore makes Python trust
# the OS cert store instead. Optional + best-effort, and safe to call twice
# (idempotent) if embeddings.py already did it in the same process.
try:  # pragma: no cover
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

_speech_state = {}
_speech_load_lock = threading.Lock()


def _get_speech_client():
    """Lazy-load the Cloud Speech-to-Text client once per process -- same
    lock + double-check pattern as embeddings.py's _get_clip() and
    agent.py's _get_agent_model(), for the same reason (two concurrent
    first-requests under Cloud Run's --concurrency shouldn't both init)."""
    if _speech_state:
        return _speech_state
    with _speech_load_lock:
        if _speech_state:
            return _speech_state
        try:
            from google.cloud import speech
        except ImportError as e:  # pragma: no cover - depends on optional install
            raise RuntimeError(
                "Voice input requires google-cloud-speech. Install it with: "
                "pip install -r requirements-ml.txt"
            ) from e
        if not config.GCP_PROJECT:
            raise RuntimeError(
                "Voice input requires embedding.vertex.project in config.yaml (or GCP_PROJECT) "
                "-- same project used for Vertex, just a different Google Cloud API."
            )
        _speech_state.update(client=speech.SpeechClient(), speech=speech)
        return _speech_state


def transcribe(audio_bytes: bytes, mime_type: str = "") -> str:
    """One recorded clip -> best transcript, or "" if nothing recognizable
    was said. Cloud Speech-to-Text's synchronous recognize() call is used
    (not streaming) -- a mic-button clip from the browser is a few seconds
    at most, well under the ~1 minute sync-call limit, so the extra
    complexity of a streaming session buys nothing here.

    ENCODING IS NOT SPECIFIED: the browser's MediaRecorder typically
    produces webm/opus (Chrome/Edge) -- Cloud Speech-to-Text auto-detects
    the container/codec from the audio bytes themselves when encoding is
    left unset, which is more robust than hardcoding one and having a
    different browser's actual output silently mismatch it."""
    state = _get_speech_client()
    client, speech = state["client"], state["speech"]

    audio = speech.RecognitionAudio(content=audio_bytes)
    rec_config = speech.RecognitionConfig(
        language_code=config.VOICE_LANGUAGE,
        # Shopping queries are short, spoken sentences, not phone-call audio
        # or dictation -- this model is tuned for that, per Google's docs.
        model="command_and_search",
        enable_automatic_punctuation=False,
    )
    try:
        response = client.recognize(config=rec_config, audio=audio)
    except Exception:
        print(f"[speech] transcribe failed:\n{traceback.format_exc()}", file=sys.stderr)
        raise

    if not response.results:
        return ""
    # Each result is one detected utterance; alternatives[0] is the top
    # guess. Join in case the clip contained a pause splitting it into more
    # than one result -- rare for a short mic-button clip, but cheap to handle.
    return " ".join(r.alternatives[0].transcript.strip() for r in response.results if r.alternatives).strip()
