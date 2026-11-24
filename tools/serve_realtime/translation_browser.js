// Browser peer for the recorded source clip. Observe audio only through the
// remote MediaStreamTrack and retain subtitles only from the SCTP channel.
async ({ grant, clip }) => {
  const pc = new RTCPeerConnection();
  const audio = new AudioContext({sampleRate: 24000});
  const messages = [], frames = [];
  const playback = new Audio();
  playback.autoplay = true;
  let count = 0, peak = 0, streamCount = 0, capture, input, audioSeen = false;
  const deadline = performance.now() + 90000;
  async function until(test, label) {
    while (!test()) {
      const error = messages.find(event => event.type === 'error');
      if (error) throw new Error(error.error.code);
      if (performance.now() > deadline || pc.connectionState === 'failed')
        throw new Error(label + ' / ' + pc.connectionState + ' / ' +
          messages.map(event => event.type).slice(-6).join(','));
      await new Promise(resolve => setTimeout(resolve, 20));
    }
  }
  try {
    await audio.resume();
    const source = audio.createMediaStreamDestination();
    pc.addTrack(source.stream.getAudioTracks()[0], source.stream);
    pc.ontrack = ({streams, track}) => {
      streamCount = streams.length;
      if (!streamCount || track.kind !== 'audio') return;
      playback.srcObject = streams[0];
      input = audio.createMediaStreamSource(streams[0]);
      // Capture the actual decoded peer output without a microphone or speaker.
      capture = audio.createScriptProcessor(1024, 1, 1);
      capture.onaudioprocess = event => {
        if (count >= 24000 * 95) return;
        const samples = Int16Array.from(event.inputBuffer.getChannelData(0),
          x => Math.max(-32768, Math.min(32767, Math.round(x * 32768))));
        for (const x of samples) peak = Math.max(peak, Math.abs(x));
        count += samples.length;
        frames.push(samples);
      };
      input.connect(capture).connect(audio.destination);
    };
    const channel = pc.createDataChannel('oai-events');
    channel.onmessage = ({data}) => messages.push(JSON.parse(data));
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    const response = await fetch('/v1/realtime/translations/calls', {
      method:'POST', headers:{Authorization:'Bearer ' + grant, 'Content-Type':'application/sdp'},
      body:offer.sdp,
    });
    if (response.status !== 201 || response.headers.get('content-type') !== 'application/sdp')
      throw new Error('SDP ' + response.status + ' ' + (await response.text()).slice(0,400));
    await pc.setRemoteDescription({type:'answer', sdp: await response.text()});
    const location = response.headers.get('location');
    await until(() => messages[0]?.type === 'session.created', 'translation session');
    const bytes = Uint8Array.fromBase64(clip);
    const speech = audio.createBuffer(1, bytes.byteLength / 2, 24000);
    const samples = new Int16Array(bytes.buffer);
    speech.copyToChannel(Float32Array.from(samples, x => x / 32768), 0);
    const recorded = audio.createBufferSource();
    recorded.buffer = speech;
    recorded.connect(source);
    recorded.start(audio.currentTime + 0.3);
    await until(() => peak > 500 && messages.some(e => e.type === 'session.output_transcript.delta'),
                'native remote speech');
    audioSeen = true;
    channel.send(JSON.stringify({type:'session.close'}));
    await until(() => messages.some(e => e.type === 'session.closed'), 'ordered close');
    // Let already received RTP pass through Chromium's playback jitter buffer.
    await new Promise(resolve => setTimeout(resolve, 500));
    if (!audioSeen || !streamCount || messages.some(e => e.type === 'session.output_audio.delta'))
      throw new Error('remote track and data-channel separation');
    const pcm = new Int16Array(count);
    let i = 0;
    for (const chunk of frames) {pcm.set(chunk, i); i += chunk.length;}
    const begin = Math.max(0, pcm.findIndex(x => Math.abs(x) > 48) - 7200);
    const end = Math.min(count, pcm.findLastIndex(x => Math.abs(x) > 48) + 7201);
    const reports = [...(await pc.getStats()).values()];
    const rtp = reports.find(s => s.type === 'inbound-rtp' && s.kind === 'audio');
    if (!rtp?.packetsReceived || !rtp.totalSamplesReceived || !rtp.totalAudioEnergy)
      throw new Error('empty browser RTP playback statistics');
    return {location, messages, inbound:{packets:rtp.packetsReceived,
            decodedSamples:rtp.totalSamplesReceived, energy:rtp.totalAudioEnergy},
            pcm:new Uint8Array(pcm.slice(begin,end).buffer).toBase64(),
            peak, streamCount, receivedBeforeClose:audioSeen, inputOfferCandidates:
              offer.sdp.split('\n').filter(line => line.startsWith('a=candidate:')).length};
  } finally {
    playback.pause();
    playback.srcObject = null;
    capture?.disconnect();
    input?.disconnect();
    pc.close();
    await audio.close();
  }
}
