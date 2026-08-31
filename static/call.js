/*
Me Talk 通話クライアント。

ブラウザ同士を直接つなぐ WebRTC を使う。参加者どうしが総当たりで
接続する方式なので、人数が増えるほど各端末の負担が増える。

音質は Opus の設定を SDP に書き込んで確保する。
ノイズ抑制とエコー除去はブラウザ内蔵の処理を有効にする。

Dev yuzuki_akrdev.ofc
*/

const Call = (() => {
    const state = {
        call: null,
        localStream: null,
        screenStream: null,
        peers: new Map(),
        iceServers: [],
        hasTurn: false,
        muted: false,
        deafened: false,
        videoOn: false,
        screenOn: false,
        listenOnly: false,
        highQuality: false,
        devices: { mic: "", camera: "", speaker: "" },
        screenQuality: { height: 1080, frameRate: 30 },
        stats: new Map(),
        statsTimer: null,
        popout: null,
        mode: "mesh",
        sfu: { pc: null, sessionId: "", pulled: new Set(), streams: new Map(), negotiating: false },
    };

    const AUDIO_CONSTRAINTS = {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
        sampleRate: 48000,
        sampleSize: 16,
    };

    const VIDEO_CONSTRAINTS = {
        width: { ideal: 1280 },
        height: { ideal: 720 },
        frameRate: { ideal: 30, max: 30 },
    };

    function myId() {
        return (window.currentUser && window.currentUser.id) || 0;
    }

    function emit(name, detail) {
        window.dispatchEvent(new CustomEvent(name, { detail }));
    }

    function send(kind, to, payload) {
        if (!window.ws || window.ws.readyState !== 1 || !state.call) return;
        window.ws.send(JSON.stringify({
            type: "call.signal",
            data: { call_id: state.call.id, to, kind, payload },
        }));
    }

    // Opus の設定を書き換えて、音楽や声がつぶれないようにする
    function tuneAudio(sdp) {
        const bitrate = state.highQuality ? 128000 : 48000;
        const stereo = state.highQuality ? 1 : 0;
        return sdp.replace(/a=fmtp:(\d+) ([^\r\n]*minptime=[^\r\n]*)/g, (m, pt, params) => {
            let out = params;
            if (!/stereo=/.test(out)) out += ";stereo=" + stereo;
            else out = out.replace(/stereo=\d/, "stereo=" + stereo);
            if (!/maxaveragebitrate=/.test(out)) out += ";maxaveragebitrate=" + bitrate;
            else out = out.replace(/maxaveragebitrate=\d+/, "maxaveragebitrate=" + bitrate);
            if (!/useinbandfec=/.test(out)) out += ";useinbandfec=1";
            return `a=fmtp:${pt} ${out}`;
        });
    }

    async function ensureLocalStream(withVideo) {
        if (state.listenOnly) return null;
        const audio = Object.assign({}, AUDIO_CONSTRAINTS);
        if (state.devices.mic) audio.deviceId = { exact: state.devices.mic };
        const video = withVideo ? Object.assign({}, VIDEO_CONSTRAINTS) : false;
        if (video && state.devices.camera) video.deviceId = { exact: state.devices.camera };
        const stream = await navigator.mediaDevices.getUserMedia({ audio, video });
        if (state.localStream) {
            state.localStream.getTracks().forEach((t) => t.stop());
        }
        state.localStream = stream;
        stream.getAudioTracks().forEach((t) => { t.enabled = !state.muted; });
        emit("call:local", { stream });
        return stream;
    }

    function createPeer(remoteId) {
        const pc = new RTCPeerConnection({
            iceServers: state.iceServers,
            bundlePolicy: "max-bundle",
            rtcpMuxPolicy: "require",
        });
        const entry = {
            pc,
            polite: myId() > remoteId,
            makingOffer: false,
            ignoreOffer: false,
            stream: new MediaStream(),
            senders: {},
        };
        state.peers.set(remoteId, entry);

        if (state.localStream) {
            state.localStream.getTracks().forEach((track) => {
                entry.senders[track.kind] = pc.addTrack(track, state.localStream);
            });
        } else {
            pc.addTransceiver("audio", { direction: "recvonly" });
            pc.addTransceiver("video", { direction: "recvonly" });
        }

        pc.ontrack = (ev) => {
            ev.streams[0].getTracks().forEach((t) => {
                if (!entry.stream.getTracks().some((x) => x.id === t.id)) entry.stream.addTrack(t);
            });
            emit("call:remote", { userId: remoteId, stream: entry.stream });
        };
        pc.onicecandidate = (ev) => {
            if (ev.candidate) send("ice", remoteId, ev.candidate.toJSON());
        };
        pc.onconnectionstatechange = () => {
            emit("call:peerstate", { userId: remoteId, state: pc.connectionState });
            if (pc.connectionState === "failed") {
                try { pc.restartIce(); } catch (e) {}
            }
        };
        pc.onnegotiationneeded = async () => {
            try {
                entry.makingOffer = true;
                await pc.setLocalDescription();
                const desc = pc.localDescription;
                send("desc", remoteId, { type: desc.type, sdp: tuneAudio(desc.sdp) });
            } catch (e) {
                console.error("negotiation", e);
            } finally {
                entry.makingOffer = false;
            }
        };
        return entry;
    }

    async function handleSignal(data) {
        if (!state.call || data.call_id !== state.call.id) return;
        const remoteId = data.from;
        let entry = state.peers.get(remoteId);
        if (!entry) entry = createPeer(remoteId);
        const pc = entry.pc;

        try {
            if (data.kind === "desc") {
                const desc = data.payload;
                const offerCollision = desc.type === "offer"
                    && (entry.makingOffer || pc.signalingState !== "stable");
                entry.ignoreOffer = !entry.polite && offerCollision;
                if (entry.ignoreOffer) return;
                await pc.setRemoteDescription(desc);
                if (desc.type === "offer") {
                    await pc.setLocalDescription();
                    const local = pc.localDescription;
                    send("desc", remoteId, { type: local.type, sdp: tuneAudio(local.sdp) });
                }
            } else if (data.kind === "ice") {
                try { await pc.addIceCandidate(data.payload); }
                catch (e) { if (!entry.ignoreOffer) throw e; }
            } else if (data.kind === "bye") {
                closePeer(remoteId);
            }
        } catch (e) {
            console.error("signal", e);
        }
    }

    function closePeer(remoteId) {
        const entry = state.peers.get(remoteId);
        if (!entry) return;
        try { entry.pc.close(); } catch (e) {}
        state.peers.delete(remoteId);
        state.stats.delete(remoteId);
        emit("call:peergone", { userId: remoteId });
    }

    function syncPeers() {
        if (!state.call) return;
        const ids = state.call.participants.map((p) => p.user.id).filter((id) => id !== myId());
        ids.forEach((id) => {
            if (!state.peers.has(id) && myId() < id) createPeer(id);
            else if (!state.peers.has(id)) createPeer(id);
        });
        Array.from(state.peers.keys()).forEach((id) => {
            if (!ids.includes(id)) closePeer(id);
        });
    }

    async function replaceTrack(kind, track) {
        if (state.mode === "sfu" && state.sfu.pc) {
            const sender = state.sfu.pc.getSenders().find((s) => s.track && s.track.kind === kind);
            if (sender) await sender.replaceTrack(track);
            return;
        }
        for (const entry of state.peers.values()) {
            const sender = entry.pc.getSenders().find((s) => s.track && s.track.kind === kind);
            if (sender) await sender.replaceTrack(track);
            else if (track) entry.pc.addTrack(track, state.localStream || new MediaStream([track]));
        }
    }

    // ここから下が中継(SFU)方式。各自がCloudflareと1本だけつなぐ。
    function sfuPeer() {
        if (state.sfu.pc) return state.sfu.pc;
        const pc = new RTCPeerConnection({
            iceServers: state.iceServers,
            bundlePolicy: "max-bundle",
            rtcpMuxPolicy: "require",
        });
        pc.ontrack = (ev) => {
            const mid = ev.transceiver && ev.transceiver.mid;
            const owner = state.sfu.streams.get(mid);
            if (!owner) return;
            let stream = state.sfu.streams.get("user:" + owner);
            if (!stream) {
                stream = new MediaStream();
                state.sfu.streams.set("user:" + owner, stream);
            }
            if (!stream.getTracks().some((t) => t.id === ev.track.id)) stream.addTrack(ev.track);
            emit("call:remote", { userId: owner, stream });
        };
        pc.onconnectionstatechange = () => {
            emit("call:peerstate", { userId: 0, state: pc.connectionState });
            if (pc.connectionState === "failed") {
                try { pc.restartIce(); } catch (e) {}
            }
        };
        state.sfu.pc = pc;
        return pc;
    }

    async function sfuPublish() {
        const pc = sfuPeer();
        const senders = [];
        if (state.localStream) {
            state.localStream.getTracks().forEach((track) => {
                senders.push({ sender: pc.addTransceiver(track, { direction: "sendonly" }), track });
            });
        } else {
            pc.addTransceiver("audio", { direction: "sendonly" });
        }

        // 1回目: 接続を開いて応答を受け取る
        await pc.setLocalDescription();
        const opened = await window.api(`/api/calls/${state.call.id}/sfu/session`, {
            method: "POST",
            body: JSON.stringify({ sdp: tuneAudio(pc.localDescription.sdp) }),
        });
        state.sfu.sessionId = opened.session_id;
        if (opened.answer) await pc.setRemoteDescription(opened.answer);

        if (!senders.length) return;

        // 2回目: 応答でコーデックが確定したので、最新の状態を伝えてトラック名を登録する
        const tracks = senders.map(({ sender, track }) => ({
            location: "local",
            mid: sender.mid,
            trackName: track.id,
        }));
        await pc.setLocalDescription();
        const res = await window.api(`/api/calls/${state.call.id}/sfu/publish`, {
            method: "POST",
            body: JSON.stringify({ sdp: tuneAudio(pc.localDescription.sdp), tracks }),
        });
        if (res.answer) await pc.setRemoteDescription(res.answer);
    }

    async function sfuSync() {
        if (!state.call || state.mode !== "sfu" || !state.sfu.sessionId) return;
        if (state.sfu.negotiating) return;
        const wanted = [];
        state.call.participants.forEach((p) => {
            if (p.user.id === myId() || !p.sfu_session_id) return;
            [p.audio_track, p.video_track].forEach((name) => {
                if (!name) return;
                const key = p.sfu_session_id + "/" + name;
                if (state.sfu.pulled.has(key)) return;
                wanted.push({ sessionId: p.sfu_session_id, trackName: name, owner: p.user.id, key });
            });
        });
        if (!wanted.length) return;

        state.sfu.negotiating = true;
        try {
            const res = await window.api(`/api/calls/${state.call.id}/sfu/subscribe`, {
                method: "POST",
                body: JSON.stringify({ tracks: wanted.map((w) => ({ sessionId: w.sessionId, trackName: w.trackName })) }),
            });
            (res.tracks || []).forEach((t, i) => {
                const source = wanted[i];
                if (t.mid != null && source) state.sfu.streams.set(String(t.mid), source.owner);
            });
            wanted.forEach((w) => state.sfu.pulled.add(w.key));
            if (res.offer) {
                const pc = sfuPeer();
                await pc.setRemoteDescription(res.offer);
                await pc.setLocalDescription();
                await window.api(`/api/calls/${state.call.id}/sfu/renegotiate`, {
                    method: "PUT",
                    body: JSON.stringify({ sdp: pc.localDescription.sdp }),
                });
            }
        } catch (e) {
            console.error("sfu subscribe", e);
        } finally {
            state.sfu.negotiating = false;
        }
    }

    function closeSfu() {
        if (state.sfu.pc) {
            try { state.sfu.pc.close(); } catch (e) {}
        }
        state.sfu = { pc: null, sessionId: "", pulled: new Set(), streams: new Map(), negotiating: false };
    }

    async function start(scope, targetId, options) {
        const opts = options || {};
        state.listenOnly = !!opts.listenOnly;
        state.videoOn = !!opts.video && !state.listenOnly;

        const cfg = await window.api("/api/calls/config");
        state.iceServers = cfg.ice_servers || [];
        state.hasTurn = !!cfg.has_turn;

        await ensureLocalStream(state.videoOn);

        const res = await window.api("/api/calls/start", {
            method: "POST",
            body: JSON.stringify({
                scope, target_id: targetId,
                video: state.videoOn, listen_only: state.listenOnly,
            }),
        });
        state.call = res;
        state.mode = res.mode || "mesh";
        emit("call:joined", { call: res });
        if (state.mode === "sfu") {
            await sfuPublish();
            await sfuSync();
        } else {
            syncPeers();
        }
        startStats();
        return res;
    }

    function onRoomUpdate(call) {
        if (!state.call || call.id !== state.call.id) return;
        state.call = call;
        state.mode = call.mode || state.mode;
        emit("call:update", { call });
        if (state.mode === "sfu") sfuSync();
        else syncPeers();
    }

    async function leave() {
        if (!state.call) return;
        const id = state.call.id;
        Array.from(state.peers.keys()).forEach((remoteId) => {
            send("bye", remoteId, {});
            closePeer(remoteId);
        });
        closeSfu();
        if (state.localStream) state.localStream.getTracks().forEach((t) => t.stop());
        if (state.screenStream) state.screenStream.getTracks().forEach((t) => t.stop());
        state.localStream = null;
        state.screenStream = null;
        stopStats();
        closePopout();
        state.call = null;
        state.muted = false; state.deafened = false;
        state.videoOn = false; state.screenOn = false; state.listenOnly = false;
        try { await window.api(`/api/calls/${id}/leave`, { method: "POST" }); } catch (e) {}
        emit("call:left", {});
    }

    async function pushState(patch) {
        if (!state.call) return;
        try {
            const res = await window.api(`/api/calls/${state.call.id}/state`, {
                method: "PATCH", body: JSON.stringify(patch),
            });
            state.call = res;
            emit("call:update", { call: res });
        } catch (e) {}
    }

    function toggleMute() {
        if (state.listenOnly) return true;
        state.muted = !state.muted;
        if (state.localStream) {
            state.localStream.getAudioTracks().forEach((t) => { t.enabled = !state.muted; });
        }
        pushState({ muted: state.muted });
        emit("call:self", {});
        return state.muted;
    }

    function toggleDeafen() {
        state.deafened = !state.deafened;
        if (state.deafened && !state.muted && !state.listenOnly) {
            state.muted = true;
            if (state.localStream) state.localStream.getAudioTracks().forEach((t) => { t.enabled = false; });
        }
        emit("call:deafen", { deafened: state.deafened });
        pushState({ deafened: state.deafened, muted: state.muted });
        emit("call:self", {});
        return state.deafened;
    }

    async function toggleVideo() {
        if (state.listenOnly) return false;
        state.videoOn = !state.videoOn;
        if (state.videoOn) {
            const video = Object.assign({}, VIDEO_CONSTRAINTS);
            if (state.devices.camera) video.deviceId = { exact: state.devices.camera };
            const stream = await navigator.mediaDevices.getUserMedia({ video });
            const track = stream.getVideoTracks()[0];
            if (state.localStream) state.localStream.addTrack(track);
            else state.localStream = stream;
            await replaceTrack("video", track);
            emit("call:local", { stream: state.localStream });
        } else {
            const tracks = state.localStream ? state.localStream.getVideoTracks() : [];
            tracks.forEach((t) => { t.stop(); state.localStream.removeTrack(t); });
            await replaceTrack("video", null);
            emit("call:local", { stream: state.localStream });
        }
        pushState({ video_on: state.videoOn });
        emit("call:self", {});
        return state.videoOn;
    }

    async function switchCamera(deviceId) {
        state.devices.camera = deviceId;
        if (!state.videoOn) return;
        const stream = await navigator.mediaDevices.getUserMedia({
            video: Object.assign({ deviceId: { exact: deviceId } }, VIDEO_CONSTRAINTS),
        });
        const track = stream.getVideoTracks()[0];
        const old = state.localStream ? state.localStream.getVideoTracks()[0] : null;
        if (old) { old.stop(); state.localStream.removeTrack(old); }
        if (state.localStream) state.localStream.addTrack(track);
        await replaceTrack("video", track);
        emit("call:local", { stream: state.localStream });
    }

    async function switchMic(deviceId) {
        state.devices.mic = deviceId;
        if (state.listenOnly) return;
        const audio = Object.assign({ deviceId: { exact: deviceId } }, AUDIO_CONSTRAINTS);
        const stream = await navigator.mediaDevices.getUserMedia({ audio });
        const track = stream.getAudioTracks()[0];
        track.enabled = !state.muted;
        const old = state.localStream ? state.localStream.getAudioTracks()[0] : null;
        if (old) { old.stop(); state.localStream.removeTrack(old); }
        if (state.localStream) state.localStream.addTrack(track);
        else state.localStream = stream;
        await replaceTrack("audio", track);
    }

    async function switchSpeaker(deviceId) {
        state.devices.speaker = deviceId;
        const elements = document.querySelectorAll("audio.call-audio, video.call-video");
        for (const el of elements) {
            if (el.setSinkId) { try { await el.setSinkId(deviceId); } catch (e) {} }
        }
    }

    function speakerSwitchSupported() {
        return typeof HTMLMediaElement !== "undefined"
            && typeof HTMLMediaElement.prototype.setSinkId === "function";
    }

    async function listDevices() {
        try {
            const all = await navigator.mediaDevices.enumerateDevices();
            return {
                mics: all.filter((d) => d.kind === "audioinput"),
                cameras: all.filter((d) => d.kind === "videoinput"),
                speakers: all.filter((d) => d.kind === "audiooutput"),
            };
        } catch (e) {
            return { mics: [], cameras: [], speakers: [] };
        }
    }

    async function startScreen(quality) {
        if (quality) state.screenQuality = quality;
        const q = state.screenQuality;
        const stream = await navigator.mediaDevices.getDisplayMedia({
            video: {
                height: { ideal: q.height, max: q.height },
                frameRate: { ideal: q.frameRate, max: q.frameRate },
            },
            audio: true,
        });
        state.screenStream = stream;
        const track = stream.getVideoTracks()[0];
        // ゲーム中でも重くならないよう、動きの滑らかさより解像度を落とす方を優先する
        try {
            track.contentHint = q.frameRate >= 45 ? "motion" : "detail";
        } catch (e) {}
        await replaceTrack("video", track);
        for (const entry of state.peers.values()) {
            const sender = entry.pc.getSenders().find((s) => s.track && s.track.kind === "video");
            if (!sender) continue;
            const params = sender.getParameters();
            if (!params.encodings || !params.encodings.length) params.encodings = [{}];
            params.encodings[0].maxBitrate = q.frameRate >= 45 ? 4000000 : 2500000;
            params.encodings[0].maxFramerate = q.frameRate;
            params.degradationPreference = q.frameRate >= 45 ? "maintain-framerate" : "maintain-resolution";
            try { await sender.setParameters(params); } catch (e) {}
        }
        track.onended = () => stopScreen();
        state.screenOn = true;
        pushState({ screen_on: true });
        emit("call:self", {});
        return true;
    }

    async function stopScreen() {
        if (state.screenStream) {
            state.screenStream.getTracks().forEach((t) => t.stop());
            state.screenStream = null;
        }
        state.screenOn = false;
        const camTrack = state.localStream ? state.localStream.getVideoTracks()[0] : null;
        await replaceTrack("video", state.videoOn ? camTrack : null);
        pushState({ screen_on: false });
        emit("call:self", {});
    }

    async function setScreenQuality(quality) {
        state.screenQuality = quality;
        if (!state.screenOn || !state.screenStream) return;
        const track = state.screenStream.getVideoTracks()[0];
        if (!track) return;
        try {
            await track.applyConstraints({
                height: { ideal: quality.height, max: quality.height },
                frameRate: { ideal: quality.frameRate, max: quality.frameRate },
            });
        } catch (e) {}
        for (const entry of state.peers.values()) {
            const sender = entry.pc.getSenders().find((s) => s.track && s.track.kind === "video");
            if (!sender) continue;
            const params = sender.getParameters();
            if (!params.encodings || !params.encodings.length) params.encodings = [{}];
            params.encodings[0].maxBitrate = quality.frameRate >= 45 ? 4000000 : 2500000;
            params.encodings[0].maxFramerate = quality.frameRate;
            params.degradationPreference = quality.frameRate >= 45 ? "maintain-framerate" : "maintain-resolution";
            try { await sender.setParameters(params); } catch (e) {}
        }
    }

    async function setHighQualityAudio(on) {
        state.highQuality = !!on;
        for (const entry of state.peers.values()) {
            try {
                await entry.pc.setLocalDescription();
                const desc = entry.pc.localDescription;
                const remoteId = Array.from(state.peers.entries())
                    .find(([, v]) => v === entry)[0];
                send("desc", remoteId, { type: desc.type, sdp: tuneAudio(desc.sdp) });
            } catch (e) {}
        }
    }

    function startStats() {
        stopStats();
        state.statsTimer = setInterval(async () => {
            if (state.mode === "sfu") {
                if (!state.sfu.pc) return;
                try {
                    const report = await state.sfu.pc.getStats();
                    let rtt = null, loss = null;
                    report.forEach((s) => {
                        if (s.type === "candidate-pair" && s.state === "succeeded" && s.currentRoundTripTime != null) {
                            rtt = Math.round(s.currentRoundTripTime * 1000);
                        }
                        if (s.type === "inbound-rtp" && s.kind === "audio" && s.packetsLost != null && s.packetsReceived) {
                            loss = Math.round((s.packetsLost / (s.packetsLost + s.packetsReceived)) * 100);
                        }
                    });
                    state.call.participants.forEach((p) => {
                        if (p.user.id !== myId()) state.stats.set(p.user.id, { rtt, loss });
                    });
                } catch (e) {}
                emit("call:stats", { stats: state.stats });
                return;
            }
            for (const [id, entry] of state.peers.entries()) {
                try {
                    const report = await entry.pc.getStats();
                    let rtt = null, loss = null, bitrate = null;
                    report.forEach((s) => {
                        if (s.type === "candidate-pair" && s.state === "succeeded" && s.currentRoundTripTime != null) {
                            rtt = Math.round(s.currentRoundTripTime * 1000);
                        }
                        if (s.type === "inbound-rtp" && s.kind === "audio") {
                            if (s.packetsLost != null && s.packetsReceived) {
                                loss = Math.round((s.packetsLost / (s.packetsLost + s.packetsReceived)) * 100);
                            }
                        }
                    });
                    state.stats.set(id, { rtt, loss, bitrate });
                } catch (e) {}
            }
            emit("call:stats", { stats: state.stats });
        }, 3000);
    }

    function stopStats() {
        if (state.statsTimer) clearInterval(state.statsTimer);
        state.statsTimer = null;
    }

    function popoutSupported() {
        return !!(window.documentPictureInPicture && window.matchMedia("(min-width: 900px)").matches);
    }

    async function openPopout(buildContent) {
        if (!popoutSupported()) return false;
        try {
            const win = await window.documentPictureInPicture.requestWindow({ width: 420, height: 300 });
            state.popout = win;
            document.querySelectorAll("style").forEach((node) => {
                win.document.head.appendChild(node.cloneNode(true));
            });
            const root = win.document.createElement("div");
            root.id = "popout-root";
            win.document.body.appendChild(root);
            win.document.documentElement.setAttribute("data-theme",
                document.documentElement.getAttribute("data-theme") || "midnight");
            buildContent(root, win);
            win.addEventListener("pagehide", () => { state.popout = null; emit("call:popout", { open: false }); });
            emit("call:popout", { open: true });
            return true;
        } catch (e) {
            return false;
        }
    }

    function closePopout() {
        if (state.popout) {
            try { state.popout.close(); } catch (e) {}
            state.popout = null;
            emit("call:popout", { open: false });
        }
    }

    return {
        state, start, leave, handleSignal, onRoomUpdate,
        toggleMute, toggleDeafen, toggleVideo,
        switchCamera, switchMic, switchSpeaker, speakerSwitchSupported, listDevices,
        startScreen, stopScreen, setScreenQuality, setHighQualityAudio,
        popoutSupported, openPopout, closePopout,
        isActive: () => !!state.call,
        mode: () => state.mode,
    };
})();
