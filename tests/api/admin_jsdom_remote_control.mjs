/**
 * Remote Control (/rc) Admin Dashboard scenario (v8.53.0, PR 16),
 * run when MCC_JSDOM_SCENARIO=remote_control.
 */

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function prepareRemoteControlScenario({ window, ROUTES }) {
  ROUTES["/admin/api/rc/status"] = {
    enabled: true,
    permission_mode: "ask",
    allowed_dirs: ["/tmp/workspace"],
    max_sessions: 8,
    active_sessions: 1,
    total_sessions: 1,
    tunnel_mode: "tailscale_serve",
    active_tunnel_mode: "off",
    device_count: 1,
    tunnels: {
      configured_mode: "tailscale_serve",
      active_mode: "off",
      local_url: "http://127.0.0.1:8082/rc",
      recommended_url: "https://my-host.ts.net/rc",
      tailscale: {
        installed: true,
        running: true,
        serve_active: false,
        serve_url: "https://my-host.ts.net/rc",
        local_url: "http://100.64.0.1:8082/rc",
      },
      cloudflare: {
        installed: false,
        running: false,
        mode: "quick",
        public_url: "",
        install_hint: "cloudflared is not installed on PATH.",
      },
    },
  };

  const devices = [
    {
      device_id: "dev_abc123",
      label: "<img src=x onerror='window.__rcXss=1'>Pixel",
      last_ip: "127.0.0.1",
      revoked: false,
    },
  ];
  ROUTES["/admin/api/rc/devices"] = { devices };
  ROUTES["/admin/api/rc/sessions"] = {
    sessions: [
      {
        id: "rc_sess_1",
        title: "claude — workspace",
        harness: "claude",
        driver_tier: "tier_1b_claude",
        status: "idle",
        permission_mode: "ask",
        cwd: "/tmp/workspace",
      },
    ],
  };

  const posts = [];
  const tunnelPosts = [];
  const realFetch = window.fetch;
  window.fetch = (url, options = {}) => {
    const path = String(url).split("?")[0];
    if (path === "/admin/api/rc/pair-token") {
      posts.push(path);
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        json: async () => ({
          token: "tok_test",
          label: "Mobile Device",
          pair_url: "https://my-host.ts.net/rc?pair=tok_test",
          local_pair_url: "http://127.0.0.1:8082/rc?pair=tok_test",
          expires_in_seconds: 60,
          qr_svg:
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 21 21'><rect width='21' height='21' fill='#fff'/></svg>",
        }),
      });
    }
    if (path === "/admin/api/rc/devices/dev_abc123/revoke") {
      posts.push(path);
      ROUTES["/admin/api/rc/devices"] = { devices: [] };
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        json: async () => ({ revoked: true, device_id: "dev_abc123" }),
      });
    }
    if (path === "/admin/api/rc/tunnel/start") {
      let body = null;
      try {
        body = options.body ? JSON.parse(String(options.body)) : null;
      } catch (_err) {
        body = null;
      }
      tunnelPosts.push({ path, body });
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        json: async () => ({ ok: true, mode: body?.mode || "" }),
      });
    }
    return realFetch(url, options);
  };

  const doc = window.document;
  const waitFor = async (predicate, timeoutMs = 20000) => {
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      if (predicate()) return true;
      await wait(50);
    }
    return false;
  };

  return {
    async run({ scriptErrors, consoleErrors }) {
      if (!(await waitFor(() => doc.querySelector('.nav-link[data-view="remote_control"]')))) {
        return { scenarioError: "Missing remote_control nav link", scriptErrors, consoleErrors };
      }
      doc.querySelector('.nav-link[data-view="remote_control"]').click();
      if (!(await waitFor(() => doc.querySelectorAll("#rcTunnelCards .rc-tunnel-card").length === 4))) {
        return { scenarioError: "Tunnel cards did not render", scriptErrors, consoleErrors };
      }

      const view = doc.getElementById("view-remote_control");
      const renderedKeys = Array.from(
        view.querySelectorAll("[data-key], [id^='field-RC_']"),
      ).map((el) => el.dataset.key || el.id.replace(/^field-/, ""));

      const pairBtn = doc.getElementById("rcGeneratePairBtn");
      pairBtn.click();
      await waitFor(() => (doc.getElementById("rcPairUrlText")?.textContent || "").includes("tok_test"));

      const qrImg = doc.getElementById("rcPairQrImg");
      const pairUrlText = doc.getElementById("rcPairUrlText")?.textContent || "";
      const xssFlag = window.__rcXss || "";
      const xssImgs = doc.querySelectorAll("#rcDevicesList img").length;

      const revokeBtn = doc.querySelector("#rcDevicesList button");
      if (revokeBtn) revokeBtn.click();
      await waitFor(() => (doc.getElementById("rcDevicesList")?.textContent || "").includes("No paired remote devices"));

      const afterRevokeText = doc.getElementById("rcDevicesList")?.textContent || "";

      const modal = doc.getElementById("rcTunnelModal");
      const closeBtn = doc.getElementById("rcTunnelClose");
      const titleEl = doc.getElementById("rcTunnelTitle");
      const bodyEl = doc.getElementById("rcTunnelBody");
      const cardModes = Array.from(
        doc.querySelectorAll("#rcTunnelCards .rc-tunnel-card"),
      ).map((card) => ({
        mode: card.dataset.rcTunnelMode || "",
        role: card.getAttribute("role") || "",
        tabIndex: card.tabIndex,
      }));

      const tsServeCard = doc.querySelector(
        '#rcTunnelCards .rc-tunnel-card[data-rc-tunnel-mode="tailscale_serve"]',
      );
      tsServeCard.click();
      await waitFor(() => modal && !modal.hidden);
      const openedOnClick = Boolean(modal && !modal.hidden);
      const tailscaleServeTitle = titleEl?.textContent || "";
      const tailscaleServeBodyText = bodyEl?.textContent || "";
      const dataKeysInModal = modal
        ? modal.querySelectorAll("[data-key]").length
        : -1;
      const scrollLockedDuringOpen = doc.body.style.overflow;
      const closeButtonFocused = doc.activeElement === closeBtn;

      closeBtn.click();
      await waitFor(() => modal && modal.hidden);
      const closedOnCloseBtn = Boolean(modal && modal.hidden);
      const scrollUnlockedAfterClose = doc.body.style.overflow;
      const focusReturnedToServeCard = doc.activeElement === tsServeCard;

      const cfCard = doc.querySelector(
        '#rcTunnelCards .rc-tunnel-card[data-rc-tunnel-mode="cloudflare"]',
      );
      cfCard.focus();
      cfCard.dispatchEvent(
        new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
      );
      await waitFor(() => modal && !modal.hidden);
      const openedOnEnter = Boolean(modal && !modal.hidden);
      const cloudflareTitle = titleEl?.textContent || "";
      const cloudflareDataKeys = modal
        ? modal.querySelectorAll("[data-key]").length
        : -1;

      doc.dispatchEvent(
        new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
      );
      await waitFor(() => modal && modal.hidden);
      const closedOnEscape = Boolean(modal && modal.hidden);
      const scrollUnlockedAfterEscape = doc.body.style.overflow;

      const directBtn = Array.from(
        doc.querySelectorAll("#rcTunnelCards .rc-tunnel-card button"),
      ).find((b) => (b.textContent || "").trim() === "Use Tailnet Direct");
      if (directBtn) directBtn.click();
      await waitFor(() => tunnelPosts.length >= 1);
      await wait(120);
      const modalStayedClosedOnControl = Boolean(modal && modal.hidden);

      return {
        scenarioError: null,
        scriptErrors,
        consoleErrors,
        viewVisible: !view.hidden,
        renderedKeysCount: new Set(renderedKeys).size,
        hasQrDataUri: (qrImg?.src || "").startsWith("data:image/svg+xml"),
        pairUrlText,
        tunnelCardCount: doc.querySelectorAll("#rcTunnelCards .rc-tunnel-card").length,
        xssFlag,
        xssImgs,
        afterRevokeText,
        posts,
        tunnelModal: {
          cardModes,
          openedOnClick,
          tailscaleServeTitle,
          tailscaleServeBodyLen: tailscaleServeBodyText.trim().length,
          hasLoopbackNote: tailscaleServeBodyText.includes("/admin"),
          hasTailscaleSettingName: tailscaleServeBodyText.includes(
            "RC_TAILSCALE_HOSTNAME",
          ),
          dataKeysInModal,
          scrollLockedDuringOpen,
          closeButtonFocused,
          closedOnCloseBtn,
          scrollUnlockedAfterClose,
          focusReturnedToServeCard,
          openedOnEnter,
          cloudflareTitle,
          cloudflareDataKeys,
          closedOnEscape,
          scrollUnlockedAfterEscape,
          modalStayedClosedOnControl,
          tunnelPosts,
        },
      };
    },
  };
}
