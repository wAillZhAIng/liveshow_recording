"""Page-local media discovery, including WebRTC audio rendered via Web Audio.

The hook is installed only in a dedicated room page before navigation. It does
not access network media URLs, another tab, microphone, or system audio.
"""

from ..live_identity import ROOM_IDENTITY_JS

ROOM_CAPTURE_INIT_SCRIPT = r"""(() => {
  if (window !== window.top || window.__whatnotRoomRemoteTracks) return;
  const tracks = new Set();
  const connections = new Set();
  const decodedAudio = new Set();
  Object.defineProperty(window, '__whatnotRoomRemoteTracks', {value: tracks});
  Object.defineProperty(window, '__whatnotRoomPeerConnections', {value: connections});
  Object.defineProperty(window, '__whatnotRoomDecodedAudioTracks', {value: decodedAudio});
""" + ROOM_IDENTITY_JS + r"""
  window.navigation?.addEventListener('navigate', (event) => {
    if (!event.cancelable || event.userInitiated || event.downloadRequest !== null) return;
    const capture = window.__whatnotBrowserAVCapture || window.__whatnotAudioCapture;
    const original = showId(new URL(location.href));
    const destination = showId(new URL(event.destination.url));
    // An automatic handoff can destroy the recorders before their last chunks
    // flush. Keep the original document so the normal end checks can still
    // verify the original show. The handoff itself is not proof of an end.
    if (original && capture?.showId === original && destination && destination !== original) {
      event.preventDefault();
    }
  });
  const Original = window.RTCPeerConnection;
  if (typeof Original !== 'function') return;
  window.RTCPeerConnection = new Proxy(Original, {
    construct(target, args, newTarget) {
      const connection = Reflect.construct(target, args, newTarget);
      connections.add(connection);
      let statsTimer = null;
      let readingStats = false;
      const updateAudioStats = async () => {
        if (readingStats || connection.connectionState === 'closed') return;
        readingStats = true;
        try {
          const stats = await connection.getStats();
          for (const report of stats.values()) {
            if (report.type !== 'inbound-rtp' || report.kind !== 'audio' ||
              !(report.totalSamplesReceived > 0)) continue;
            for (const {track} of connection.getReceivers()) {
              if (track?.kind === 'audio' && (!report.trackIdentifier ||
                report.trackIdentifier === track.id)) decodedAudio.add(track);
            }
          }
        } catch (_) { /* Closing connections are not a readiness signal. */ }
        finally { readingStats = false; }
      };
      connection.addEventListener('track', ({track}) => {
        tracks.add(track);
        track.addEventListener('ended', () => {
          tracks.delete(track); decodedAudio.delete(track);
        }, {once: true});
        if (track.kind === 'audio' && statsTimer === null) {
          statsTimer = setInterval(updateAudioStats, 500);
          void updateAudioStats();
        }
      });
      connection.addEventListener('connectionstatechange', () => {
        if (connection.connectionState === 'closed') {
          connections.delete(connection);
          clearInterval(statsTimer);
          for (const receiver of connection.getReceivers()) {
            tracks.delete(receiver.track); decodedAudio.delete(receiver.track);
          }
        }
      });
      return connection;
    }
  });
})();"""

# Used by both recorders. Direct WebRTC inputs MUST be cloned: recorder cleanup
# must never stop the player's original receiver tracks. Prefer rendered HTML
# sources, and use the same room's remote audio only when HTML lacks audio.
CAPTURE_SOURCES_JS = """
    const captureRoomSources = (elements, capture) => {
        const sources = [];
        for (const element of elements) {
            try { sources.push({element, stream: capture.call(element)}); } catch (_) {}
        }
        const hasAudio = sources.some(({stream}) => stream.getAudioTracks().some(
            track => track.readyState === 'live'
        ));
        if (!hasAudio) {
            const remote = [...(window.__whatnotRoomRemoteTracks || [])].find(
                track => track.kind === 'audio' && track.readyState === 'live'
                    && track.enabled && !track.muted
            );
            if (remote) sources.push({
                element: {tagName: 'WEBRTC'},
                stream: new MediaStream([remote.clone()])
            });
        }
        return sources;
    };
"""
