// Pure state transitions shared by QML and the Node regression tests.
// A resolved API target is not a started PTY; PTY bytes are not an attach ack.
function initial() {
    return { attempt: 0, roomId: "", phase: "idle", target: null,
        started: false, outputObserved: false, error: "" };
}

function begin(previous, roomId) {
    var next = initial();
    next.attempt = previous.attempt + 1;
    next.roomId = roomId;
    next.phase = "resolving";
    return next;
}

function deactivate(previous) {
    var next = initial();
    next.attempt = previous.attempt + 1;
    return next;
}

function matches(state, attempt, roomId) {
    return state.attempt === attempt && state.roomId === roomId;
}

function resolve(state, attempt, roomId, response) {
    if (!matches(state, attempt, roomId) || state.phase !== "resolving") return state;
    response = response || {};
    var tty = response.tty || {};
    if (response.ok === false || tty.agentError)
        return Object.assign({}, state, { phase: "failed", error: String(tty.agentError || response.error || "Terminal unavailable") });
    if (tty.awaitingBinding === true)
        return Object.assign({}, state, { phase: "unbound" });
    var target = {
        sessionName: String(tty.sessionName || "ares"),
        agentName: String(tty.agentName || ""),
        sshTarget: String(tty.sshTarget || tty.remoteTarget || ""),
        remoteBin: String(tty.remoteBin || ""),
        remoteSocket: String(tty.remoteSocket || ""),
        readOnly: tty.readOnly === true, transport: String(tty.transport || "")
    };
    var peer = target.sessionName === "peer-observe";
    if (peer) {
        if (target.agentName !== roomId || !target.readOnly || target.transport !== "peer" || tty.agentStarted !== true)
            return Object.assign({}, state, { phase: "failed", error: "Verified peer observation unavailable" });
        return Object.assign({}, state, { phase: "resolved", target: target });
    }
    var unified = target.sessionName === "terminal";
    var remote = target.sessionName === "remote";
    var valid = target.agentName !== "" && (unified || remote || target.sessionName === "ares" || target.sessionName === "default");
    if (unified || remote) valid = valid && target.remoteBin.charAt(0) === "/" && target.remoteSocket.charAt(0) === "/";
    if (remote) valid = valid && target.sshTarget !== "";
    if (!unified) valid = valid && tty.agentStarted === true;
    if (!valid) return Object.assign({}, state, { phase: "failed", error: "No usable terminal target. Choose an existing terminal." });
    return Object.assign({}, state, { phase: "resolved", target: target });
}

function loadable(state) {
    return state.target !== null && state.phase !== "failed";
}

// The caller must supply a separately verified exclusive-writer grant. PTY output is not one.
function interactive(state, previewReadOnly, verifiedWriter) {
    return verifiedWriter === true && loadable(state) && state.started && state.outputObserved
        && !previewReadOnly && !state.target.readOnly && state.target.sessionName !== "peer-observe";
}

function event(state, attempt, roomId, kind) {
    if (!matches(state, attempt, roomId) || state.phase === "failed" || state.phase === "idle") return state;
    if (kind === "timeout") {
        if (state.phase === "output" || state.phase === "unbound") return state;
        return Object.assign({}, state, { phase: "failed", error:
            "Terminal startup timed out. Retry or choose an existing terminal." });
    }
    if (!loadable(state)) return state;
    if (kind === "finished") return Object.assign({}, state, { phase: "failed",
        error: "Terminal client disconnected. Retry or choose an existing terminal." });
    if (kind === "started") return Object.assign({}, state, { started: true,
        phase: state.outputObserved ? "output" : "waiting-output" });
    if (kind === "output") return Object.assign({}, state, { outputObserved: true,
        phase: state.started ? "output" : state.phase });
    return state;
}

function ownsInput(state) {
    // Only a positive unbound response permits the legacy first-message path.
    return state.phase !== "unbound";
}

function status(state) {
    if (state.error) return state.error;
    if (state.phase === "output") return state.target && state.target.sessionName === "peer-observe"
        ? "Read-only peer output · observed (not interactive)" : "Terminal output · attachment not verified";
    if (state.phase === "waiting-output") return "Client started · waiting for terminal output";
    if (state.phase === "resolved") return "Target resolved · waiting for terminal size";
    if (state.phase === "unbound") return "No terminal binding · choose an existing terminal";
    return "Resolving terminal…";
}

function argumentsFor(target) {
    if (target.sessionName === "peer-observe") return [target.agentName];
    if (target.sessionName === "terminal" || target.sessionName === "remote")
        return [target.sessionName, target.agentName, target.sshTarget, target.remoteBin, target.remoteSocket];
    return [target.sessionName, target.agentName];
}
