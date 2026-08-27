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
  });
})();
