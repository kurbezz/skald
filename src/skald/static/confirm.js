// Shared form helpers: confirmation prompts and the TV scope editor behaviour.
(function () {
  "use strict";

  document.addEventListener("submit", function (event) {
    if (event.defaultPrevented) return;
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.dataset.pending === "1") {
      event.preventDefault();
      return;
    }
    var message = form.dataset.confirm;
    if (message && !window.confirm(message)) {
      event.preventDefault();
      return;
    }
    var emptyMessage = form.dataset.confirmEmpty;
    if (emptyMessage && !form.querySelector("input[type=checkbox]:checked")) {
      if (!window.confirm(emptyMessage)) {
        event.preventDefault();
        return;
      }
    }
    form.dataset.submitting = "1";
    var button = event.submitter || form.querySelector("button[data-pending-label]");
    if (button && button.dataset.pendingLabel) {
      // Wait until submission data is collected; never disable the submitter early.
      window.setTimeout(function () {
        if (event.defaultPrevented) return;
        form.dataset.pending = "1";
        form.setAttribute("aria-busy", "true");
        button.dataset.idleLabel = button.textContent;
        button.textContent = button.dataset.pendingLabel;
        button.disabled = true;
      }, 0);
    }
  });

  // Back/forward cache restores the previous DOM, including disabled controls.
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("form[data-pending]").forEach(function (form) {
      delete form.dataset.pending;
      delete form.dataset.submitting;
      form.removeAttribute("aria-busy");
      form.querySelectorAll("button[data-idle-label]").forEach(function (button) {
        button.textContent = button.dataset.idleLabel;
        button.disabled = false;
        delete button.dataset.idleLabel;
      });
    });
  });

  function updateSeason(details) {
    var seasonBox = details.querySelector("input[name=season_ids]");
    var episodes = details.querySelectorAll("input[name=episode_ids]");
    var included = !!(seasonBox && seasonBox.checked);
    var count = 0;
    episodes.forEach(function (box) {
      if (included) {
        box.dataset.wasChecked = box.dataset.wasChecked || (box.checked ? "1" : "0");
        box.checked = true;
        box.disabled = true;
      } else {
        if (box.disabled && box.dataset.wasChecked !== undefined) {
          box.checked = box.dataset.wasChecked === "1";
        }
        box.disabled = false;
        delete box.dataset.wasChecked;
      }
      var note = box.closest("label").querySelector(".tv-included-note");
      if (note) {
        note.hidden = !included;
        if (included) box.setAttribute("aria-describedby", note.id);
        else box.removeAttribute("aria-describedby");
      }
      if (box.checked) count += 1;
    });
    var counter = details.querySelector("[data-selected-count]");
    if (counter) {
      // Preserve the server's saved count until this season's episodes exist.
      if (details.dataset.seasonLoaded === "0") return;
      counter.textContent = count ? count + " of " + episodes.length + " selected" : "";
    }
  }

  // Fetch a season's episode list the first time its <details> is opened.
  function loadSeason(details) {
    if (details.dataset.seasonLoaded !== "0" || details.dataset.seasonLoading === "1") return;
    var url = details.dataset.seasonUrl;
    var list = details.querySelector("[data-episode-list]");
    var status = details.querySelector("[data-episode-status]");
    if (!url || !list || !status || !window.fetch) return;
    details.dataset.seasonLoading = "1";
    list.setAttribute("aria-busy", "true");
    status.dataset.state = "loading";
    status.textContent = "Loading episodes…";
    fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(function (response) {
        return response.json().then(function (data) {
          if (!response.ok) throw new Error(data && data.error ? data.error : "Request failed");
          return data;
        });
      })
      .then(function (data) {
        if (!Array.isArray(data.episodes)) throw new Error("Invalid episode list");
        list.replaceChildren();
        data.episodes.forEach(function (episode) {
          var label = document.createElement("label");
          label.className = "tv-check tv-check--episode";
          var input = document.createElement("input");
          input.type = "checkbox";
          input.name = "episode_ids";
          input.value = String(episode.tmdb_id);
          var box = document.createElement("span");
          box.className = "tv-check-box";
          box.setAttribute("aria-hidden", "true");
          var num = document.createElement("span");
          num.className = "tv-episode-number";
          num.textContent = "E" + (episode.episode_number < 10 ? "0" : "") + episode.episode_number;
          var title = document.createElement("span");
          title.className = "tv-episode-title";
          title.textContent = episode.name || "Episode " + episode.episode_number;
          var note = document.createElement("small");
          note.className = "tv-included-note";
          note.id = "included-" + data.tmdb_id + "-" + episode.tmdb_id;
          note.textContent = "Included by season";
          note.hidden = true;
          label.append(input, box, num, title, note);
          list.appendChild(label);
        });
        // Mark the season as fully loaded so saving treats its picks as authoritative.
        var marker = document.createElement("input");
        marker.type = "hidden";
        marker.name = "loaded_season_ids";
        marker.value = String(data.tmdb_id);
        details.querySelector(".tv-season-body").prepend(marker);
        list.hidden = data.episodes.length === 0;
        status.dataset.state = "ready";
        status.textContent = data.episodes.length ? "" : "Episodes for this season are not available yet.";
        status.hidden = data.episodes.length > 0;
        details.dataset.seasonLoaded = "1";
        var count = details.querySelector("[data-total-count]");
        if (count) {
          var n = data.episodes.length;
          count.textContent = n + " episode" + (n === 1 ? "" : "s");
        }
        updateSeason(details);
      })
      .catch(function () {
        status.dataset.state = "error";
        status.textContent = "Could not load episodes. Your selection is unchanged. ";
        var retry = document.createElement("button");
        retry.type = "button";
        retry.className = "btn-link";
        retry.textContent = "Try again";
        retry.addEventListener("click", function () { loadSeason(details); });
        status.appendChild(retry);
      })
      .then(function () {
        delete details.dataset.seasonLoading;
        list.removeAttribute("aria-busy");
      });
  }

  document.querySelectorAll("form[data-scope-form]").forEach(function (form) {
    var dirty = false;
    form.querySelectorAll("details.tv-season").forEach(function (details) {
      updateSeason(details);
      details.addEventListener("toggle", function () {
        if (details.open) loadSeason(details);
      });
      var loadLink = details.querySelector("[data-load-episodes]");
      if (loadLink && window.fetch) loadLink.addEventListener("click", function (event) {
        event.preventDefault();
        loadSeason(details);
      });
      if (details.open) loadSeason(details);
    });
    form.addEventListener("change", function (event) {
      dirty = true;
      var details = event.target.closest && event.target.closest("details.tv-season");
      if (details) updateSeason(details);
    });
    window.addEventListener("beforeunload", function (event) {
      if (dirty && !form.dataset.submitting) {
        event.preventDefault();
        event.returnValue = "";
      }
    });
  });
})();
