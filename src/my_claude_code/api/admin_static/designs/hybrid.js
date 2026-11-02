/* ══════════════════════════════════════════════════════════════════════════
   HYBRID — the design's own chrome.  (registry id: `hybrid`)

   Ported from `design-previews/option-19-hybrid/direction.js` (2 KB, the round-4
   rebuild on base 8.0.2), which ran against the preview kit. The kit gave the
   preview four things this module has to do for itself, because THE PRODUCT
   SHIPS NO KIT (spec §13.5: "The preview kit is not shipped"):

     kit                                   here
     -----------------------------------   ------------------------------------
     `MCCKit.configure({...})`             the loader's `start(api)` argument
     `MCCKit.start()` calling onStart      the loader calls `start(api)` for us
     `K.configure({attrs})` writing        the registry row's `attrs`, written
       `data-sections`/`data-fields`/…     by `syncDesignAttributes()` in core
     `K.openPalette()` / `K.goTo()`        `openPalette()` / `api.setActiveView`
     the kit's `nav` module (numbers,       `decorateRail()` below — the rail's
       group headings)                     numbers and headings, built here
     the kit's shortcuts module            `installShortcuts()` below
     the kit's `MCCKit.el()` DOM helper    small helpers at the top of this file
     the kit's overlay for the `?` sheet   `#reqDetailModal`? no — a dialog this
                                           module owns (`hy-shortcut-sheet`),
                                           torn down with everything else.

   THE CONTRACT THIS FILE OBEYS (see `loadDesignShell` in admin.js, which is
   where it is enforced):

     - it is an ES MODULE and it publishes ONE factory:
           window.MCCDesignShells["designs/hybrid.js"] = { start }
     - it reaches the page ONLY through the `api` it is handed. It never touches
       `window.state`, `window.setActiveView` or any other admin.js binding —
       those are classic-script globals a module cannot see anyway, which is the
       structural reason the rule holds.
     - everything it attaches goes through `api.mount` / `api.listen` /
       `api.classes` / `api.onTeardown`, so the loader can take it all away
       again. The ONE exception is deliberate and is undone by hand: the version
       indicator is MOVED into the masthead, so its original parent and next
       sibling are recorded and restored on teardown.

   WHAT IT BUILDS, in the order the page sees it:

     1. a masthead (`header.hy-masthead`) carrying the brand, "Server control",
        the palette's door, the version indicator and a keyboard-hints button;
     2. the numbered, grouped rail — numbers and group headings added to the
        product's own `#sectionNav`, which keeps every link and every listener
        admin.js gave it (this design adds decoration; it does not re-implement
        navigation);
     3. a Ctrl/⌘K command palette over the pages, every page's own sections
        (read from the shipped `SECTION_NAVIGATOR` through the DOM, so it stays
        one table) and every setting on the page;
     4. `g`-key shortcuts and `/` to focus the page's own search, `?` for the
        sheet that lists them;
     5. a right DRAWER for the request detail — the CSS comes from the
        foundation's `data-detail="drawer"`; this file adds Escape-to-close
        ownership so a drawer opened from the palette is closed by the same key
        that closes the palette.
   ══════════════════════════════════════════════════════════════════════════ */

(function () {
  "use strict";

  /* ----------------------------------------------------------- helpers ---- */

  /** The smallest DOM builder worth having. Text only — never `innerHTML`, so
   *  a provider name or a setting label can never become markup. */
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs) {
      Object.entries(attrs).forEach(([name, value]) => {
        if (value === undefined || value === null || value === false) return;
        if (name === "class") node.className = value;
        else if (name === "text") node.textContent = String(value);
        else node.setAttribute(name, String(value));
      });
    }
    children.flat().forEach((child) => {
      if (child === null || child === undefined || child === false) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  const flat = (node) =>
    node ? String(node.textContent || "").replace(/\s+/g, " ").trim() : "";

  /** The page labels from the product's own rail, in the product's own order.
   *  Reading them rather than restating them is what makes a page added in a
   *  later release appear in the palette and the shortcut list for free. */
  function pageEntries() {
    return Array.from(document.querySelectorAll("#sectionNav .nav-link")).map(
      (button) => ({
        view: button.dataset.view,
        label: flat(button.querySelector(".hy-nav-text") || button),
        node: button,
      }),
    );
  }

  /** The sections of one page, from the shipped per-page navigator.
   *
   * 8.1.0 ships `SECTION_NAVIGATOR` as one table and renders a strip of buttons
   * per page. This reads the STRIP rather than the table: the table is an
   * admin.js binding a module cannot see, and the strip is the same data after
   * it has been resolved against this page's markup — so a selector that does
   * not exist in this build cannot reach the palette either. */
  function sectionEntries(viewId) {
    const nav = document.querySelector(
      `.admin-view[data-view="${viewId}"] > .section-navigator`,
    );
    if (!nav) return [];
    return Array.from(nav.querySelectorAll("button[data-section-target]")).map(
      (button) => ({
        label: flat(button),
        selector: button.dataset.sectionTarget,
      }),
    );
  }

  /** Every setting on one page, as { label, key, node }.
   *
   * A field is rendered into `#field-<KEY>`; its label is the first span inside
   * its `<label>`, with the control's own id as the fallback, because a field
   * whose label is missing is still reachable by its key. */
  function fieldEntries(viewId) {
    const view = document.querySelector(`.admin-view[data-view="${viewId}"]`);
    if (!view) return [];
    return Array.from(view.querySelectorAll("[id^='field-']"))
      .filter((node) => node.closest(".field") || node.closest(".field-grid"))
      .map((node) => {
        const host = node.closest(".field") || node;
        /* The label's FIRST span, and only a span that is a DIRECT child of
         * the `<label>` — `label > span` rather than `label span` — because a
         * field's own meta row ("default value · Use default") and its
         * description live inside later nodes, and a descendant query picks the
         * first of those up on a control the renderer wraps differently. The
         * key is a perfectly good label when the markup surprises us: it is
         * what the reader typed into `.env` and it is unique. */
        const labelNode =
          host.querySelector("label > span") || host.querySelector("label");
        const key = node.id.replace(/^field-/, "").replace(/-model-config$/, "");
        return {
          key,
          label: flat(labelNode) || key,
          node,
        };
      });
  }

  const viewLabel = (viewId) => {
    const entry = pageEntries().find((page) => page.view === viewId);
    return entry ? entry.label : viewId;
  };

  /* ------------------------------------------------------------- factory -- */

  const shell = {
    start(api) {
      const root = document.documentElement;
      const palette = { box: null, returnFocus: null, items: [], shown: [], selected: 0 };
      const sheet = { box: null, returnFocus: null };
      let pendingGo = false;
      let goTimer = 0;

      /* ================================================== 1. the masthead == */

      const searchButton = el(
        "button",
        {
          type: "button",
          class: "hy-search",
          "aria-label": "Search pages, sections and settings",
          "aria-haspopup": "dialog",
          "aria-expanded": "false",
          "aria-controls": "hyPalette",
        },
        el("span", null, "Jump to anything…"),
        el(
          "span",
          { class: "hy-keys", "aria-hidden": "true" },
          el("span", { class: "hy-kbd" }, "Ctrl"),
          el("span", { class: "hy-kbd" }, "K"),
        ),
      );
      const hintsButton = el(
        "button",
        {
          type: "button",
          class: "hy-search hy-hints",
          "aria-label": "Keyboard shortcuts",
          "aria-haspopup": "dialog",
          "aria-expanded": "false",
        },
        el("span", { "aria-hidden": "true" }, "?"),
        el("span", null, "Shortcuts"),
      );
      const tools = el("span", { class: "hy-tools" });
      const masthead = el(
        "header",
        { class: "hy-masthead" },
        el("span", { class: "hy-brand" }, "My Claude Code"),
        el("span", { class: "hy-sub" }, "Server control"),
        searchButton,
        el("span", { class: "hy-spacer" }),
        tools,
        hintsButton,
      );

      /* THE VERSION INDICATOR MOVES. It lives in the sidebar's brand block,
         which this design hides down to its text; leaving it there would put
         the one piece of live server state in the small print of a hidden
         block. It is the ONE node this module re-parents, so its original
         parent and next sibling are recorded and put back on teardown — which
         is what makes the round trip byte-identical rather than merely
         visually equivalent. */
      const indicator = document.getElementById("versionIndicator");
      const indicatorHome = indicator
        ? { parent: indicator.parentNode, next: indicator.nextSibling }
        : null;
      if (indicator && indicatorHome) tools.appendChild(indicator);

      api.mount(masthead);
      api.classes.onBody("hy-mode");

      /* ================================================ 2. the numbered rail */

      /* Decoration this shell owns, so teardown can find it without guessing:
         one element per group heading, keyed by label, plus the observer. */
      const rail = { headings: new Map(), mutation: null };
      /* THE FIVE GROUPS THE USER APPROVED (spec "LEFT RAIL": SETUP · AGENTS ·
         ROUTING · OBSERVE · SYSTEM), and the marker page each one opens with.
         These are the same five markers round 4's preview used, and they lie in
         the PRODUCT'S OWN ORDER — the rail is never reordered, so every page
         stays where the product put it and the grouping is a set of headings
         over it. A marker that does not exist in this build (a marketplace with
         no key) simply is not drawn; the heading never appears without items. */
      const GROUPS = [
        ["Setup", "get_started"],
        ["Agents", "claude"],
        ["Routing", "model_config"],
        ["Observe", "requests"],
        ["System", "limits"],
      ];

      function decorateRail() {
        const nav = document.getElementById("sectionNav");
        if (!nav) return;
        const links = Array.from(nav.querySelectorAll(".nav-link")).filter(
          (link) => !link.hidden,
        );
        links.forEach((link, index) => {
          let number = link.querySelector(".hy-nav-num");
          if (!number) {
            number = el("span", { class: "hy-nav-num", "aria-hidden": "true" });
            link.prepend(number);
          }
          number.textContent = String(index + 1).padStart(2, "0");
          // The link's own text becomes a span so the number and the label are
          // both addressable; the text node itself is reused, never copied, so
          // the button's accessible name is unchanged.
          if (!link.querySelector(".hy-nav-text")) {
            const textNode = Array.from(link.childNodes).find(
              (node) => node.nodeType === 3 && node.textContent.trim(),
            );
            if (textNode) {
              const span = el("span", { class: "hy-nav-text" }, textNode.textContent.trim());
              link.replaceChild(span, textNode);
            }
          }
        });
        /* One heading per marker, MOVED to sit before its marker on every pass
         * rather than only created once. `renderNav()` rebuilds the rail
         * wholesale, and `refreshMarketplaceNav()` then UNHIDES a link after
         * that — so a heading created while its marker was hidden (or created
         * against a different neighbour) has to be re-placed, not merely
         * re-confirmed. Keeping one element per marker in a map is what makes
         * "re-place" idempotent: the rail can never end up with two "Routing"s.
         */
        GROUPS.forEach(([label, marker]) => {
          let heading = rail.headings.get(label);
          const target = nav.querySelector(`.nav-link[data-view="${marker}"]`);
          if (!heading || !heading.isConnected) {
            heading = el("p", { class: "hy-nav-group", "aria-hidden": "true" }, label);
            rail.headings.set(label, heading);
          }
          if (!target || target.hidden) {
            if (heading.isConnected) heading.remove();
            return;
          }
          if (heading.nextElementSibling !== target) {
            nav.insertBefore(heading, target);
          }
        });
        // No link may show a number the visible order disagrees with: an index
        // is a position, and a stale one is worse than none.
        links.forEach((link, index) => {
          const number = link.querySelector(".hy-nav-num");
          if (number) number.textContent = String(index + 1).padStart(2, "0");
        });
      }

      decorateRail();
      // `renderNav()` rebuilds the rail wholesale (on every settings reload and
      // every marketplace-key change), so the decoration has to be re-applied.
      // A MutationObserver on childList is the one signal that covers every
      // caller without this module knowing any of them.
      const nav = document.getElementById("sectionNav");
      if (nav) {
        rail.mutation = new window.MutationObserver(() => {
          // The observer is disconnected while decorating so the module's own
          // insertions cannot re-trigger it: a decoration pass that schedules a
          // decoration pass is an infinite loop in a microtask.
          rail.mutation.disconnect();
          decorateRail();
          rail.mutation.observe(nav, { childList: true });
        });
        rail.mutation.observe(nav, { childList: true });
        api.onTeardown(() => rail.mutation.disconnect());
      }

      /* ================================================= 3. the palette ==== */

      function paletteItems(query) {
        const items = [];
        const pages = pageEntries();
        pages.forEach((page) => {
          items.push({
            label: page.label,
            hint: "page",
            terms: page.label,
            run: () => api.setActiveView(page.view),
          });
        });
        pages.forEach((page) => {
          sectionEntries(page.view).forEach((section) => {
            items.push({
              label: section.label,
              hint: `section · ${page.label}`,
              terms: `${section.label} ${page.label}`,
              run: () => {
                api.setActiveView(page.view);
                // The section exists only once its page is on screen; the
                // product's own navigator is the thing that knows how to land
                // on it, so its button is pressed rather than re-implemented.
                window.setTimeout(() => {
                  const strip = document.querySelector(
                    `.admin-view[data-view="${page.view}"] > .section-navigator`,
                  );
                  const button =
                    strip &&
                    strip.querySelector(
                      `button[data-section-target="${cssEscape(section.selector)}"]`,
                    );
                  if (button) button.click();
                }, 60);
              },
            });
          });
        });
        pages.forEach((page) => {
          fieldEntries(page.view).forEach((field) => {
            items.push({
              label: field.label,
              code: field.key,
              hint: `setting · ${page.label}`,
              terms: `${field.label} ${field.key} ${page.label}`,
              run: () => {
                api.setActiveView(page.view);
                window.setTimeout(() => {
                  const target = document.getElementById(`field-${cssEscape(field.key)}`);
                  if (!target) return;
                  target.scrollIntoView({ block: "center", behavior: "auto" });
                  if (typeof target.focus === "function") target.focus({ preventScroll: true });
                }, 60);
              },
            });
          });
        });
        const normalise = (value) =>
          String(value || "")
            .toLowerCase()
            .replace(/[-_/]+/g, " ");
        const words = normalise(query).split(/\s+/).filter(Boolean);
        const matched = words.length
          ? items.filter((item) => {
              const hay = normalise(`${item.terms} ${item.hint}`);
              return words.every((word) => hay.includes(word));
            })
          : items.filter((item) => item.hint === "page");
        // A page is one screenful of results; anything more is a scroll with no
        // signal. The cap is what makes the arrow keys usable.
        return matched.slice(0, 60);
      }

      function paintPalette(query) {
        if (!palette.box) return;
        const list = palette.box.querySelector(".hy-palette-list");
        palette.shown = paletteItems(query);
        palette.selected = 0;
        if (!palette.shown.length) {
          list.replaceChildren(el("li", { class: "hy-palette-empty" }, "Nothing matches that."));
          return;
        }
        list.replaceChildren(
          ...palette.shown.map((item, index) =>
            el(
              "li",
              { role: "option", "aria-selected": String(index === 0) },
              el("span", null, item.label, item.code ? el("code", null, `  ${item.code}`) : null),
              el("span", null, item.hint),
            ),
          ),
        );
      }

      function movePaletteSelection(delta) {
        if (!palette.box || !palette.shown.length) return;
        const count = palette.shown.length;
        palette.selected = (palette.selected + delta + count) % count;
        const list = palette.box.querySelector(".hy-palette-list");
        Array.from(list.children).forEach((node, index) =>
          node.setAttribute("aria-selected", String(index === palette.selected)),
        );
        const current = list.children[palette.selected];
        if (current && typeof current.scrollIntoView === "function") {
          current.scrollIntoView({ block: "nearest" });
        }
      }

      function runPaletteSelection() {
        const item = palette.shown[palette.selected];
        if (!item) return;
        closePalette(true);
        item.run();
      }

      function openPalette() {
        if (palette.box) return;
        palette.returnFocus = document.activeElement;
        const input = el("input", {
          type: "search",
          role: "combobox",
          "aria-expanded": "true",
          "aria-controls": "hyPaletteList",
          "aria-label": "Search pages, sections and settings",
          placeholder: "Jump to a page, a section or any setting",
          autocomplete: "off",
        });
        const list = el("ul", {
          class: "hy-palette-list",
          id: "hyPaletteList",
          role: "listbox",
          "aria-label": "Results",
        });
        const box = el(
          "div",
          {
            class: "hy-palette",
            id: "hyPalette",
            role: "dialog",
            "aria-modal": "true",
            "aria-label": "Command palette",
          },
          el("div", { class: "hy-palette-box" }, input, list),
        );
        palette.box = box;
        searchButton.setAttribute("aria-expanded", "true");
        api.listen(input, "input", () => paintPalette(input.value));
        api.listen(input, "keydown", (event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            movePaletteSelection(event.key === "ArrowDown" ? 1 : -1);
          } else if (event.key === "Enter") {
            event.preventDefault();
            runPaletteSelection();
          } else if (event.key === "Escape") {
            event.preventDefault();
            closePalette();
          } else if (event.key === "Tab") {
            // One control inside the dialog: Tab would leave it, and a modal
            // whose only control can be tabbed past is a trap with a hole.
            event.preventDefault();
          }
        });
        api.listen(list, "click", (event) => {
          const item = event.target.closest("li[role='option']");
          if (!item) return;
          const index = Array.from(list.children).indexOf(item);
          palette.selected = index;
          runPaletteSelection();
        });
        api.listen(box, "click", (event) => {
          if (event.target === box) closePalette();
        });
        api.mount(box);
        paintPalette("");
        input.focus();
      }

      function closePalette(restoreFocus) {
        if (!palette.box) return;
        const box = palette.box;
        const returnTo = palette.returnFocus;
        palette.box = null;
        palette.shown = [];
        palette.returnFocus = null;
        box.remove();
        searchButton.setAttribute("aria-expanded", "false");
        /* Focus goes back to whatever had it when the palette opened — the
         * masthead's button when it was clicked, the field when Ctrl+K was
         * pressed from one. `restoreFocus === false` is the teardown path: the
         * whole document is being put back, and moving focus then would fight
         * whatever the reader did next.
         *
         * THE TIMING IS THE WHOLE TRICK. Removing the focused node's subtree
         * makes the browser (and jsdom) drop focus to `<body>`, and that
         * happens DURING the removal — so a `focus()` issued before the box is
         * out of the document is undone by the removal, and one issued
         * synchronously after it can still be racing the same blur. It is
         * therefore issued on the next task, from a callback that is not this
         * function's caller's stack: a microtask is soon enough to be invisible
         * to a reader and late enough to win.
         *
         * `preventScroll` because a palette opened halfway down a long page
         * must not scroll the reader back to the masthead. */
        if (restoreFocus === false) return;
        const target =
          returnTo && typeof returnTo.focus === "function" && returnTo.isConnected
            ? returnTo
            : searchButton;
        const restore = () => {
          if (target.isConnected) target.focus({ preventScroll: true });
        };
        window.setTimeout(restore, 0);
      }

      api.listen(searchButton, "click", () => openPalette());

      /* ============================================ 4. g-keys and the sheet */

      const SHORTCUTS = {
        s: "get_started",
        p: "providers",
        x: "proxying",
        c: "claude",
        a: "coding_agents",
        m: "model_config",
        o: "models",
        e: "messaging",
        r: "requests",
        t: "optimizer",
        w: "web_search",
        l: "limits",
        h: "guide",
        d: "docs",
      };

      const isTyping = (target) =>
        Boolean(
          target &&
            (target.matches?.("input, select, textarea, [contenteditable='true']") ||
              target.isContentEditable),
        );

      function openSheet() {
        if (sheet.box) return;
        sheet.returnFocus = document.activeElement;
        const rows = Object.entries(SHORTCUTS).map(([key, viewId]) =>
          el(
            "li",
            null,
            el(
              "span",
              null,
              el("span", { class: "hy-kbd" }, "g"),
              " ",
              el("span", { class: "hy-kbd" }, key),
            ),
            el("span", null, viewLabel(viewId)),
          ),
        );
        rows.push(
          el("li", null, el("span", null, el("span", { class: "hy-kbd" }, "/")), el("span", null, "Search this page")),
          el(
            "li",
            null,
            el(
              "span",
              null,
              el("span", { class: "hy-kbd" }, "Ctrl"),
              " ",
              el("span", { class: "hy-kbd" }, "K"),
            ),
            el("span", null, "Jump to any page, section or setting"),
          ),
          el("li", null, el("span", null, el("span", { class: "hy-kbd" }, "Esc")), el("span", null, "Close this sheet or the palette")),
        );
        const closeButton = el("button", { type: "button", class: "secondary-button" }, "Close");
        const box = el(
          "div",
          {
            class: "hy-palette",
            id: "hyShortcutSheet",
            role: "dialog",
            "aria-modal": "true",
            "aria-labelledby": "hyShortcutTitle",
          },
          el(
            "div",
            { class: "hy-palette-box" },
            el(
              "div",
              { class: "hy-palette-head" },
              el("strong", { id: "hyShortcutTitle" }, "Keyboard"),
              closeButton,
            ),
            el("ul", { class: "hy-shortcut-rows" }, rows),
          ),
        );
        sheet.box = box;
        hintsButton.setAttribute("aria-expanded", "true");
        api.listen(closeButton, "click", () => closeSheet());
        api.listen(box, "click", (event) => {
          if (event.target === box) closeSheet();
        });
        api.mount(box);
        closeButton.focus();
      }

      function closeSheet() {
        if (!sheet.box) return;
        const box = sheet.box;
        const returnTo = sheet.returnFocus;
        sheet.box = null;
        sheet.returnFocus = null;
        box.remove();
        hintsButton.setAttribute("aria-expanded", "false");
        // Same ordering rule as the palette: out of the document, then focus,
        // one task later so the removal's own blur cannot undo it.
        const target =
          returnTo && typeof returnTo.focus === "function" && returnTo.isConnected
            ? returnTo
            : hintsButton;
        window.setTimeout(() => {
          if (target.isConnected) target.focus({ preventScroll: true });
        }, 0);
      }

      api.listen(hintsButton, "click", () => openSheet());

      api.listen(document, "keydown", (event) => {
        // A dialog this design owns answers Escape first, so a palette opened
        // over a page never also closes the page's own modal.
        if (event.key === "Escape") {
          if (sheet.box) {
            event.preventDefault();
            closeSheet();
            return;
          }
          if (palette.box) {
            event.preventDefault();
            /* `closePalette()` puts focus back on its opener one task later;
               see the note there for why it cannot be synchronous. */
            closePalette();
            return;
          }
        }
        // The palette's shortcut is the one chord that works while typing: it
        // does not consume a character, so a reader in a search box can still
        // jump. Every other chord is suppressed inside a field.
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
          event.preventDefault();
          if (palette.box) closePalette();
          else openPalette();
          return;
        }
        if (isTyping(event.target)) return;
        if (event.ctrlKey || event.metaKey || event.altKey) return;
        if (event.key === "/") {
          const page = document.querySelector(".admin-view:not([hidden])");
          const field =
            page &&
            page.querySelector(
              'input[type="search"], input[placeholder*="ilter" i], input[placeholder*="earch" i]',
            );
          if (field) {
            event.preventDefault();
            field.focus();
          }
          return;
        }
        if (event.key === "?") {
          event.preventDefault();
          openSheet();
          return;
        }
        if (event.key === "g") {
          pendingGo = true;
          window.clearTimeout(goTimer);
          goTimer = window.setTimeout(() => {
            pendingGo = false;
          }, 900);
          return;
        }
        if (pendingGo && Object.prototype.hasOwnProperty.call(SHORTCUTS, event.key)) {
          pendingGo = false;
          window.clearTimeout(goTimer);
          event.preventDefault();
          api.setActiveView(SHORTCUTS[event.key]);
        }
      });

      /* ========================================= 5. the drawer's own Escape */

      /* The foundation's `data-detail="drawer"` moves the product's own
         `#reqDetailModal` to the right edge; the product's close button and
         backdrop click still work. This adds NOTHING to the modal's behaviour —
         the request detail already closes on Escape through admin.js's own
         handler. What this module does is make sure the drawer's scroll
         position starts at the top when it is opened from the palette, so a
         reader who asked for a number sees the number rather than the previous
         request's scrolled-down raw boxes. */
      api.onViewChange(() => {
        const modal = document.getElementById("reqDetailModal");
        if (modal) modal.scrollTop = 0;
      });

      /* ============================================== 6. tidy up after itself */

      api.onTeardown(() => {
        window.clearTimeout(goTimer);
        pendingGo = false;
        closePalette(false);
        closeSheet();
        // The re-parented indicator goes home. Recorded as (parent, next) so it
        // lands in the same document position it left, not merely somewhere in
        // the same block.
        if (indicator && indicatorHome && indicatorHome.parent) {
          indicatorHome.parent.insertBefore(indicator, indicatorHome.next || null);
        }
        // The rail's decoration is this module's markup too: a number span and
        // a group heading left behind on Today's page would be exactly the leak
        // the round-trip proof exists to catch.
        document.querySelectorAll(".hy-nav-num").forEach((node) => node.remove());
        document.querySelectorAll(".hy-nav-group").forEach((node) => node.remove());
        // And the label spans go back to a bare text node, which is what the
        // product's own renderer produced.
        document.querySelectorAll("#sectionNav .nav-link .hy-nav-text").forEach((span) => {
          const text = document.createTextNode(span.textContent);
          span.replaceWith(text);
        });
      });

      void root;
    },
  };

  /** `CSS.escape` with a fallback, for a selector fragment this file writes. */
  function cssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === "function") {
      return window.CSS.escape(String(value));
    }
    return String(value).replace(/["\\]/g, "\\$&");
  }

  /* PUBLISH THE FACTORY, IDEMPOTENTLY.
   *
   * The loader evaluates this module at most once per document — `import()`
   * caches by URL — but a module can legitimately be evaluated again: a
   * test/harness that feeds the source to the page (this design's jsdom
   * scenario does exactly that, because jsdom has no server to import from),
   * or a future cache-busted reload. Re-registering is harmless and is the
   * right behaviour: the LAST registration is the one that runs. What must not
   * happen is the module deciding, from the presence of an earlier
   * registration, that it has nothing to do — a stale factory from a previous
   * evaluation would then be handed a fresh `api`, and its `start()` would run
   * against closures from a page state that no longer exists.
   *
   * So: always overwrite, and never short-circuit. */
  window.MCCDesignShells = window.MCCDesignShells || {};
  window.MCCDesignShells["designs/hybrid.js"] = shell;
})();
