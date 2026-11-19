// Synthesised notification sounds (WebAudio) – no audio files needed.
(function () {
  let ctx = null;
  function audio() {
    if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (ctx.state === "suspended") ctx.resume();
    return ctx;
  }
  // Browsers only allow audio after a user gesture: unlock on first interaction.
  ["pointerdown", "keydown"].forEach((ev) =>
    window.addEventListener(ev, () => { try { audio(); } catch (e) {} }, { once: true, capture: true }));

  function tone(freq, start, dur, type, vol) {
    const c = audio();
    const o = c.createOscillator();
    const g = c.createGain();
    o.type = type || "sine";
    o.frequency.setValueAtTime(freq, c.currentTime + start);
    g.gain.setValueAtTime(0.0001, c.currentTime + start);
    g.gain.exponentialRampToValueAtTime(Math.max(vol, 0.0002), c.currentTime + start + 0.015);
    g.gain.exponentialRampToValueAtTime(0.0001, c.currentTime + start + dur);
    o.connect(g).connect(c.destination);
    o.start(c.currentTime + start);
    o.stop(c.currentTime + start + dur + 0.05);
  }
  const N = { C3: 130.8, E3: 164.8, A3: 220, C4: 261.6, E4: 329.6, G4: 392, A4: 440, C5: 523.3, E5: 659.3, G5: 784, C6: 1046.5 };
  const PATTERNS = {
    // [note, start, duration, waveform]
    buy:         [[N.C5, 0, 0.14, "sine"], [N.G5, 0.12, 0.22, "sine"]],
    sell_profit: [[N.C5, 0, 0.12, "triangle"], [N.E5, 0.1, 0.12, "triangle"], [N.G5, 0.2, 0.28, "triangle"]],
    sell_loss:   [[N.E4, 0, 0.18, "triangle"], [N.C4, 0.16, 0.32, "triangle"]],
    take_profit: [[N.C5, 0, 0.1, "sine"], [N.E5, 0.08, 0.1, "sine"], [N.G5, 0.16, 0.1, "sine"], [N.C6, 0.24, 0.35, "sine"]],
    stop_loss:   [[N.A3, 0, 0.2, "sawtooth"], [N.E3, 0.22, 0.35, "sawtooth"]],
    error:       [[N.C3, 0, 0.18, "square"], [N.C3, 0.25, 0.18, "square"], [N.C3, 0.5, 0.3, "square"]],
    info:        [[N.A4, 0, 0.12, "sine"]],
  };
  window.tbSound = function (kind, volume) {
    try {
      const p = PATTERNS[kind] || PATTERNS.info;
      const v = (volume === undefined ? 0.6 : volume) * (kind === "stop_loss" || kind === "error" ? 0.18 : 0.3);
      p.forEach(([f, s, d, t]) => tone(f, s, d, t, v));
    } catch (e) { console.warn("tbSound", e); }
  };
})();
