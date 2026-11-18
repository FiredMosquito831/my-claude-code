/* Progressive enhancement only. The site reads and navigates without this file. */
(function () {
  "use strict";

  // Mobile navigation toggle.
  var toggle = document.querySelector(".nav-toggle");
  var nav = document.getElementById("site-nav");
  if (toggle && nav) {
    toggle.addEventListener("click", function () {
      var open = nav.classList.toggle("is-open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  // Mark the current table-of-contents section while scrolling.
  var tocLinks = Array.prototype.slice.call(document.querySelectorAll(".toc a[href^='#']"));
  if (tocLinks.length === 0 || !("IntersectionObserver" in window)) {
    return;
  }
  var byId = {};
  var targets = [];
  tocLinks.forEach(function (link) {
    var id = link.getAttribute("href").slice(1);
    var el = document.getElementById(id);
    if (el) {
      byId[id] = link;
      targets.push(el);
    }
  });
  var current = null;
  function activate(link) {
    if (!link || link === current) { return; }
    if (current) { current.removeAttribute("aria-current"); }
    link.setAttribute("aria-current", "true");
    current = link;
  }
  var observer = new IntersectionObserver(function (entries) {
    var visible = entries.filter(function (entry) { return entry.isIntersecting; });
    if (visible.length === 0) { return; }
    visible.sort(function (a, b) { return a.boundingClientRect.top - b.boundingClientRect.top; });
    activate(byId[visible[0].target.id]);
  }, { rootMargin: "-20% 0px -70% 0px", threshold: 0 });
  targets.forEach(function (t) { observer.observe(t); });
})();
