// Fragment never reaches access logs. Remove it before any async work, and do
// not store capabilities in localStorage/sessionStorage or persistent JS state.
window.inkSightEntry = (async () => {
  let entry = new URLSearchParams(location.hash.slice(1)).get("entry");
  if (location.hash) history.replaceState(null, "", location.pathname);
  if (!entry) return;
  const response = await fetch("/api/desktop/browser-session", {
    method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ticket: entry}),
  });
  entry = null;
  if (!response.ok) throw new Error("配置入口已过期，请从 InkSight 菜单重新打开");
})();
// Attach immediately; manager.js awaits and displays the actionable error.
window.inkSightEntry.catch(() => {});
