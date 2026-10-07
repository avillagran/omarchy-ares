// Pure UI gates: discovery is never trust and a changed identity invalidates consent.
function selectedPeer(peers, id) {
  if (!id) return null
  return (peers || []).find(function(peer) { return String(peer.id || "") === String(id) }) || null
}
function canPair(peer, fingerprint, confirmed, code, busy) {
  var pin = String(peer && peer.fingerprint || "")
  var secret = String(code || "").trim()
  return !!peer && peer.paired !== true && peer.online === true && !busy
    && String(peer.id || "") !== "" && /^[0-9a-fA-F]{64}$/.test(pin)
    && pin === String(fingerprint)
    && confirmed === true && /^[0-9]{8}$/.test(secret)
}
function canRevoke(peer, confirmed, busy) {
  return !!peer && peer.paired === true && String(peer.alias || "") !== ""
    && confirmed === true && !busy
}
function sessionSummary(row) {
  if (!row || typeof row !== "object") return "Session · Unassigned"
  var title = String(row.title || row.id || "Session").replace(/[\u0000-\u001f\u007f]/g, " ").slice(0, 120)
  var profile = String(row.profile || "unknown").slice(0, 64)
  var path = String(row.git_repo_root || row.cwd || "")
  var folder = path ? path.split("/").filter(Boolean).pop() : "Unassigned"
  return title + " · " + profile + " · " + String(folder || "Unassigned").slice(0, 80)
}
if (typeof module !== "undefined") module.exports = { selectedPeer, canPair, canRevoke, sessionSummary }
