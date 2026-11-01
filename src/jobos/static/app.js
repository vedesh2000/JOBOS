// Minimal progressive enhancement: theme memory and honest submit feedback.
(function () {
  "use strict";

  var KEY = "jobos-theme";

  function apply(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    var button = document.getElementById("theme-toggle");
    if (button) {
      button.textContent = theme === "dark" ? "Light" : "Dark";
      button.setAttribute("aria-label", "Switch to " + (theme === "dark" ? "light" : "dark") + " mode");
    }
  }

  function stored() {
    try {
      return localStorage.getItem(KEY);
    } catch (error) {
      return null;
    }
  }

  function escapeHtml(value) {
    var div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  // Watch a running crawl and refresh the page when it finishes, instead of asking
  // the user to reload. Progressive enhancement: without JS the form still posts and
  // redirects, and the server-rendered status message still shows.
  function initCrawlWatcher() {
    var region = document.getElementById("crawl-live");
    if (!region) {
      return;
    }
    var statusUrl = region.getAttribute("data-status-url") || "/api/crawl";
    var form = document.getElementById("crawl-form");
    var button = form ? form.querySelector('button[type="submit"]') : null;
    var wavesButton = document.getElementById("crawl-waves-btn");
    var polling = false;
    // Waves we have already shown a notification for, so each fires exactly once.
    var notifiedWaves = {};

    function flash(kind, html) {
      region.innerHTML = '<div class="flash ' + kind + '">' + html + "</div>";
    }

    // Fire a browser notification (if the user granted permission) as each wave
    // finishes. Portable: the browser handles OS integration, no server-side OS code.
    // Degrades silently to the on-page flash when notifications are unavailable/denied.
    function notifyWaveProgress(status) {
      var done = status.completed_waves || [];
      for (var i = 0; i < done.length; i++) {
        var wave = done[i];
        if (notifiedWaves[wave]) {
          continue;
        }
        notifiedWaves[wave] = true;
        var s = status.summary || {};
        var body =
          wave === "fast"
            ? "Fast wave done — you can start applying now. Medium and slow keep running."
            : wave.charAt(0).toUpperCase() + wave.slice(1) + " wave complete.";
        if (
          "Notification" in window &&
          Notification.permission === "granted"
        ) {
          try {
            new Notification("JobOS: " + wave + " crawl done", { body: body });
          } catch (e) {
            /* notifications best-effort */
          }
        }
      }
    }

    function spinner(text) {
      return '<span class="spinner" aria-hidden="true"></span> ' + text;
    }

    // Counts grow as each company is stored, so a long crawl shows real movement
    // rather than an indefinite "this can take a minute".
    function progressText(status) {
      var s = status.summary;
      if (!s || !s.companies_requested) {
        return "starting…";
      }
      var done = (s.companies_succeeded || 0) + Object.keys(s.errors || {}).length;
      var text = done + " of " + s.companies_requested + " boards";
      if (s.jobs_seen) {
        text += ", " + s.jobs_seen + " roles";
      }
      return text;
    }

    function poll() {
      fetch(statusUrl, { headers: { Accept: "application/json" } })
        .then(function (response) {
          return response.json();
        })
        .then(function (status) {
          // Notify on any wave that finished since the last poll (wave crawls only).
          notifyWaveProgress(status);
          if (status.running) {
            var waveNote = status.current_wave
              ? " [" + status.current_wave + " wave]"
              : "";
            flash(
              "info",
              spinner("Crawling " + escapeHtml(status.label) + waveNote + "… " + progressText(status))
            );
            window.setTimeout(poll, 2000);
            return;
          }
          polling = false;
          if (status.error) {
            flash("error", "Crawl failed: " + escapeHtml(status.error));
            if (button) {
              button.disabled = false;
            }
            return;
          }
          var summary = status.summary;
          if (summary) {
            flash(
              "info",
              spinner(
                "Done — " +
                  summary.jobs_added +
                  " added, " +
                  summary.jobs_updated +
                  " updated. Refreshing…"
              )
            );
          }
          window.setTimeout(function () {
            window.location.reload();
          }, 800);
        })
        .catch(function () {
          // Transient hiccup while the server is busy; back off and retry.
          window.setTimeout(poll, 4000);
        });
    }

    function watch() {
      if (polling) {
        return;
      }
      polling = true;
      poll();
    }

    // The page was opened while a crawl was already in flight.
    if (region.getAttribute("data-running") === "true") {
      watch();
    }

    if (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        if (button) {
          button.textContent = "Starting…";
          button.disabled = true;
        }
        flash("info", spinner("Starting crawl…"));
        fetch("/crawl", {
          method: "POST",
          headers: { Accept: "application/json" },
          body: new FormData(form),
        })
          .then(function (response) {
            if (!response.ok) {
              return response
                .json()
                .catch(function () {
                  return {};
                })
                .then(function (data) {
                  throw new Error(data.detail || "Could not start the crawl.");
                });
            }
            return response.json();
          })
          .then(function () {
            watch();
          })
          .catch(function (error) {
            if (button) {
              button.disabled = false;
            }
            flash("error", escapeHtml(error.message || "Could not start the crawl."));
          });
      });
    }

    if (wavesButton) {
      wavesButton.addEventListener("click", function () {
        // Ask once so the per-wave "done" notifications can appear. Declining is fine:
        // the on-page flash still shows wave progress.
        if ("Notification" in window && Notification.permission === "default") {
          Notification.requestPermission();
        }
        notifiedWaves = {};
        wavesButton.disabled = true;
        if (button) {
          button.disabled = true;
        }
        flash("info", spinner("Starting wave crawl (fast → medium → slow)…"));
        fetch("/crawl-waves", {
          method: "POST",
          headers: { Accept: "application/json" },
        })
          .then(function (response) {
            if (!response.ok) {
              return response
                .json()
                .catch(function () {
                  return {};
                })
                .then(function (data) {
                  throw new Error(data.detail || "Could not start the wave crawl.");
                });
            }
            return response.json();
          })
          .then(function () {
            watch();
          })
          .catch(function (error) {
            wavesButton.disabled = false;
            if (button) {
              button.disabled = false;
            }
            flash("error", escapeHtml(error.message || "Could not start the wave crawl."));
          });
      });
    }
  }

  var initial = stored();
  if (!initial) {
    initial = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  }
  // Coach chat: submit without a reload, append the user's line and the reply.
  // Progressive enhancement — without JS the form still POSTs and redirects back.
  function initCoach() {
    var form = document.getElementById("coach-form");
    var log = document.getElementById("coach-log");
    var input = document.getElementById("coach-input");
    if (!form || !log || !input) {
      return;
    }
    function bubble(role, text) {
      var empty = document.getElementById("coach-empty");
      if (empty) {
        empty.remove();
      }
      var wrap = document.createElement("div");
      wrap.className = "coach-msg " + role;
      wrap.innerHTML =
        '<div class="coach-bubble-wrap"><span class="coach-who">' +
        (role === "user" ? "You" : "Coach") +
        '</span><div class="coach-bubble">' +
        escapeHtml(text) +
        "</div></div>";
      log.appendChild(wrap);
      wrap.scrollIntoView({ block: "nearest" });
      return wrap;
    }
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var text = input.value.trim();
      if (!text) {
        return;
      }
      bubble("user", text);
      input.value = "";
      input.disabled = true;
      var pending = bubble("assistant", "…");
      var body = new FormData();
      body.append("message", text);
      fetch("/coach/message", {
        method: "POST",
        headers: { Accept: "application/json" },
        body: body,
      })
        .then(function (response) {
          return response.json().then(function (data) {
            if (!response.ok) {
              throw new Error(data.detail || "The coach could not reply.");
            }
            return data;
          });
        })
        .then(function (data) {
          pending.querySelector(".coach-bubble").textContent = data.reply;
        })
        .catch(function (error) {
          pending.querySelector(".coach-bubble").textContent = error.message;
        })
        .finally(function () {
          input.disabled = false;
          input.focus();
        });
    });
  }

  // Insights "Ask about your data": stateless grounded Q&A with suggested questions.
  function initInsightsAsk() {
    var form = document.getElementById("ask-form");
    var input = document.getElementById("ask-input");
    var answer = document.getElementById("ask-answer");
    if (!form || !input || !answer) {
      return;
    }
    var suggestions = document.querySelectorAll(".ask-suggestion");
    for (var i = 0; i < suggestions.length; i++) {
      suggestions[i].addEventListener("click", function (event) {
        input.value = event.target.textContent.trim();
        form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit"));
      });
    }
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var q = input.value.trim();
      if (!q) {
        return;
      }
      answer.hidden = false;
      answer.textContent = "Thinking…";
      input.disabled = true;
      var body = new FormData();
      body.append("question", q);
      fetch("/insights/ask", {
        method: "POST",
        headers: { Accept: "application/json" },
        body: body,
      })
        .then(function (response) {
          return response.json().then(function (data) {
            if (!response.ok) {
              throw new Error(data.detail || "Could not answer that.");
            }
            return data;
          });
        })
        .then(function (data) {
          answer.textContent = data.reply;
        })
        .catch(function (error) {
          answer.textContent = error.message;
        })
        .finally(function () {
          input.disabled = false;
        });
    });
  }

  // Inline status dropdowns on the Jobs list: submit on change so triage is one click.
  function initStatusSelects() {
    var selects = document.querySelectorAll(".status-select");
    for (var i = 0; i < selects.length; i++) {
      selects[i].addEventListener("change", function (event) {
        var form = event.target.closest("form");
        if (form) {
          form.submit();
        }
      });
    }
  }

  // Organizations: instant client-side filter over the configured-boards table.
  // Client-side because the full list is already on the page and 1k+ rows filter
  // faster in the browser than a round-trip.
  function initBoardFilter() {
    var input = document.getElementById("board-filter");
    var table = document.getElementById("board-table");
    var count = document.getElementById("board-filter-count");
    if (!input || !table) {
      return;
    }
    var rows = Array.prototype.slice.call(table.querySelectorAll("tbody tr"));
    var total = rows.length;
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      var shown = 0;
      for (var i = 0; i < rows.length; i++) {
        var match = !q || (rows[i].getAttribute("data-search") || "").indexOf(q) !== -1;
        rows[i].style.display = match ? "" : "none";
        if (match) {
          shown++;
        }
      }
      if (count) {
        count.textContent = q ? shown + " of " + total + " boards" : total + " boards";
      }
    });
  }

  apply(initial);

  document.addEventListener("DOMContentLoaded", function () {
    apply(document.documentElement.getAttribute("data-theme") || initial);

    var toggle = document.getElementById("theme-toggle");
    if (toggle) {
      toggle.addEventListener("click", function () {
        var next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
        try {
          localStorage.setItem(KEY, next);
        } catch (error) {
          /* private mode: theme simply will not persist */
        }
        apply(next);
      });
    }

    // Confirm destructive actions before they submit (e.g. hard-deleting jobs).
    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (!window.confirm(form.getAttribute("data-confirm"))) {
          event.preventDefault();
        }
      });
    });

    // Local models take time. Say so instead of looking frozen.
    document.querySelectorAll("form[data-busy]").forEach(function (form) {
      form.addEventListener("submit", function () {
        var button = form.querySelector('button[type="submit"], button:not([type])');
        if (button && !button.disabled) {
          button.dataset.idleLabel = button.textContent;
          button.textContent = form.getAttribute("data-busy") || "Working...";
          button.disabled = true;
        }
      });
    });

    initCrawlWatcher();
    initCoach();
    initInsightsAsk();
    initStatusSelects();
    initBoardFilter();
  });
})();
