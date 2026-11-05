// Captures the microphone in the browser's audio thread for voice.js: gathers the 128-sample
// frames it's given into larger pieces (about 21 ms at 48 kHz) and posts each to the page,
// which resamples it to 24 kHz and sends it to Cora.

const PIECE = 1024;

class MicCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.piece = new Float32Array(PIECE);
    this.filled = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) {
      let offset = 0;
      while (offset < channel.length) {
        const count = Math.min(channel.length - offset, PIECE - this.filled);
        this.piece.set(channel.subarray(offset, offset + count), this.filled);
        this.filled += count;
        offset += count;
        if (this.filled === PIECE) {
          this.port.postMessage(this.piece);
          this.piece = new Float32Array(PIECE);
          this.filled = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("mic-capture", MicCapture);
