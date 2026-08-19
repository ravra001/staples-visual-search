"""
Voice input — streaming speech-to-text for the Staples AI chat's mic button.

Google Cloud Speech-to-Text (speech.googleapis.com), same GCP project and
credentials as Vertex AI (agent.py's Gemini calls, embeddings.py's vertex
backend) -- a separate API to enable, not a separate project to configure.
Deliberately NOT a locally-bundled model the way CLIP is: unlike CLIP (an
embedding, needed on every search, worth the offline/zero-marginal-cost
tradeoff), speech-to-text only runs on an explicit mic click, so the
per-call cost and network dependency are much less consequential, and Cloud
Speech-to-Text's accuracy on real speech is a genuine step up over a small
locally-bundled model for a live demo.

STREAMING, not one-shot: text appears as the user talks (interim results),
not only after they stop -- see main.py's /ws/agent/transcribe. Text stops
here either way: transcripts fill the existing chat input box, NOT
auto-submitted -- transcription errors on shopping-specific terms (SKUs,
brand names) are common enough that a quick glance before sending is worth
the extra click.

AUDIO FORMAT: raw LINEAR16 PCM, not whatever container/codec
MediaRecorder happens to produce. MediaRecorder's actual output format is
browser-dependent (Chrome/Firefox default to webm/opus, Safari to mp4/aac,
and Safari's webm support for MediaRecorder is unreliable even where it
exists) -- picking one and hoping is exactly the kind of "works on my
browser" bug this app has avoided elsewhere. The frontend instead captures
raw samples via the Web Audio API (AudioContext + AudioWorklet, universally
supported), converts to 16-bit PCM client-side, and reports its own actual
sample rate over the WebSocket -- so this module never has to guess a
codec or a rate; it's told both, always correctly, regardless of browser.
"""
import queue
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

# Sentinel pushed onto a chunk queue to signal "no more audio is coming" --
# distinct from an empty bytes chunk, which is a legal (if useless) frame.
_END_OF_STREAM = object()


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


def _request_generator(speech_mod, sample_rate_hertz: int, chunk_queue: "queue.Queue"):
    """First yielded request carries the streaming config (required by the
    API to be the very first message on the stream); every request after
    that carries one audio chunk. Blocks on chunk_queue.get() between
    chunks -- this generator runs the underlying gRPC streaming call's pace,
    so blocking here is exactly "wait for the next bit of audio to arrive
    from the browser," not a busy-loop."""
    rec_config = speech_mod.RecognitionConfig(
        encoding=speech_mod.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=sample_rate_hertz,
        language_code=config.VOICE_LANGUAGE,
        model="command_and_search",   # short spoken queries, not dictation/phone-call audio
        enable_automatic_punctuation=False,
    )
    streaming_config = speech_mod.StreamingRecognitionConfig(
        config=rec_config,
        interim_results=True,   # the whole point -- partial text before the user stops talking
    )
    yield speech_mod.StreamingRecognizeRequest(streaming_config=streaming_config)
    while True:
        chunk = chunk_queue.get()
        if chunk is _END_OF_STREAM:
            return
        yield speech_mod.StreamingRecognizeRequest(audio_content=chunk)


def streaming_transcribe(sample_rate_hertz: int, chunk_queue: "queue.Queue", on_result):
    """Runs Cloud Speech-to-Text's bidirectional streaming call to completion.
    BLOCKING and synchronous (the underlying gRPC client is) -- callers on
    an asyncio event loop (main.py's WebSocket handler) must run this in a
    worker thread, not await it directly.

    chunk_queue: caller pushes raw LINEAR16 PCM bytes onto this as they
        arrive from the browser, and _END_OF_STREAM exactly once when the
        client stops recording (or disconnects) -- this function returns
        once that sentinel has been consumed and Google's stream closes.
    on_result(text, is_final): called for every partial/final transcript
        Google returns, in real time as they arrive -- NOT batched until
        the end. The caller (main.py) is expected to push each call
        straight out over the WebSocket to the browser.
    """
    state = _get_speech_client()
    client, speech_mod = state["client"], state["speech"]

    requests = _request_generator(speech_mod, sample_rate_hertz, chunk_queue)
    try:
        responses = client.streaming_recognize(requests=requests)
        for response in responses:
            for result in response.results:
                if not result.alternatives:
                    continue
                on_result(result.alternatives[0].transcript.strip(), result.is_final)
    except Exception:
        print(f"[speech] streaming_transcribe failed:\n{traceback.format_exc()}", file=sys.stderr)
        raise
