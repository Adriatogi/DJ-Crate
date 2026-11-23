// Show the latest release version next to the download button.
// On any failure (offline, rate limit, no releases yet) the label stays empty.
(function () {
  var el = document.getElementById("version");
  if (!el || !window.fetch) return;
  fetch("https://api.github.com/repos/Adriatogi/DJ-Crate/releases/latest", {
    headers: { Accept: "application/vnd.github+json" },
  })
    .then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    })
    .then(function (data) {
      var tag = data && typeof data.tag_name === "string" ? data.tag_name.trim() : "";
      if (!tag) return;
      el.textContent = tag.charAt(0) === "v" ? tag : "v" + tag;
    })
    .catch(function () {});
})();
