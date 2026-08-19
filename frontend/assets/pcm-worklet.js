// Runs on the audio rendering thread (not the main thread) -- converts
// each block of Float32 samples the browser captures into 16-bit PCM
// (LINEAR16), the format backend/speech.py's streaming_transcribe expects.
// A separate module file because AudioWorkletProcessor must be registered
// in a worklet's own global scope, loaded via audioContext.audioWorklet.
// addModule() -- it cannot live inline in app.js.
class PCMProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length) {
      const pcm16 = new Int16Array(channel.length);
      for (let i = 0; i < channel.length; i++) {
        const s = Math.max(-1, Math.min(1, channel[i]));
        pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      // Transfers ownership of the buffer instead of copying it -- cheap,
      // and safe since pcm16 isn't touched again after this.
      this.port.postMessage(pcm16.buffer, [pcm16.buffer]);
    }
    return true;   // keep the processor alive for the next block
  }
}
registerProcessor("pcm-processor", PCMProcessor);
