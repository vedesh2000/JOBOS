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
    var polling = false;

    function flash(kind, html) {
      region.innerHTML = '<div class="flash ' + kind + '">' + html + "</div>";
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
          if (status.running) {
            flash("info", spinner("Crawling " + escapeHtml(status.label) + "… " + progressText(status)));
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
  }

  var initial = stored();
  if (!initial) {
    initial = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
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
  });
})();
