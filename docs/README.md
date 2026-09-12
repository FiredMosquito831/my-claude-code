# Documentation

The [README](../README.md) is the map: what My Claude Code is, how to install it, and one row per capability. These pages are the territory.

| Page | What is in it |
| --- | --- |
| [Usage Guide](./USAGE.md) | The manual. Install per platform, first run, connecting each coding agent and desktop app, providers and keys, tiers and routing, web search, analytics, rotation, limits, updating, security, troubleshooting. |
| [Clients](./CLIENTS.md) | Editor integrations and any OpenAI-, Anthropic- or Gemini-shaped client that is not one of the launchers. |
| [OAuth providers](./OAUTH-PROVIDERS.md) | Sign-in-based providers: a Claude subscription, ChatGPT, Kimi For Coding. |
| [Messaging](./MESSAGING.md) | Running sessions over Discord or Telegram, with voice-note transcription. |
| [Routing reference](./ROUTING-REFERENCE.md) | Key rotation, fallback chains, output budgets, limits and resilience, and the token optimizer, as reference rather than tutorial. |
| [Web search](./WEB-SEARCH.md) | The `web_search` server tool, fulfilled at the proxy by 14 search providers instead of by Anthropic. |
| [Claude Code config](./CLAUDE-CODE-CONFIG.md) | Pointing Claude Code at this proxy, per session or permanently. |
| [Anthropic subscription](./ANTHROPIC-SUBSCRIPTION.md) | What signing in with a Claude subscription costs you, and why Anthropic does not permit it. |
| [Brand](./BRAND.md) | Naming, what the 7.0.0 rename retired, and what may not change. |
| [Release checklist](./RELEASE-CHECKLIST.md) | What a release has to satisfy before it is published. |
| [Architecture](../ARCHITECTURE.md) | How a request travels through the proxy, and who owns what. |
| [Contributing](../CONTRIBUTING.md) | Local checks, versioning rules, and how changes get merged. |
| [Decision records](./adr/) | Why the load-bearing choices were made. |

The dashboard renders this index and every page above it that is written for whoever *runs* MCC — the README, this page, the Usage Guide, Clients, OAuth providers, Messaging, the routing reference, web search, the Claude Code config page, the Anthropic subscription page, Architecture and Contributing — on its own **Docs** page, offline, for the version you actually have installed. Brand, the release checklist and the decision records are written for whoever *builds* it and stay on GitHub.
