# Security Policy

My Claude Code runs on your machine, holds your provider credentials, and opens
a network listener. That combination deserves a written policy rather than an
assumption, so this file says what is supported, how to report a problem
privately, what is a vulnerability here and what is a documented design
decision, and what to do first if a key of yours is exposed.

## Supported versions

| Version | Supported |
| --- | --- |
| The [latest release](https://github.com/FiredMosquito831/my-claude-code/releases/latest) | Yes |
| Anything older | No |

MCC ships frequently and updates in place, so there is no long-term support
branch: fixes land in the next release rather than being backported. If you are
behind, update first — the dashboard's **Update** button, the install one-liner,
or `npm install -g @firedmosquito831/my-claude-code` — and re-check whether the
problem still reproduces.

## Reporting a vulnerability

**Use GitHub's private vulnerability reporting, not a public issue:**
<https://github.com/FiredMosquito831/my-claude-code/security/advisories/new>

That opens a private advisory visible only to you and the maintainer. It is the
right channel for anything that could expose a credential, reach the proxy from
another machine, run code the operator did not choose, or corrupt an install.

Please include, as far as you can establish it:

- the MCC version (`mcc-server --version`) and your OS;
- what an attacker gains, and what access they need to start;
- the minimal reproduction — a request, a config shape, or a sequence of steps;
- whether you saw it on a real install or only in the code.

**Do not include real credentials, tokens, `.env` contents, OAuth files or raw
request bodies** — a masked label (`sk-a…f9c2`), a shape, or a redacted excerpt
is always enough to act on. See *If you have exposed a credential* below.

You will get an acknowledgement, and then either a fix, a request for more
detail, or an explanation of why the behaviour is intended. This is a
single-maintainer project: there is no paid support contract and no guaranteed
response time, and honest notice of that is better than a number nobody can
hold to.

## Scope

**In scope** — the parts of MCC that hold secrets, accept input, or change the
machine:

- the proxy listener and its authentication (`ANTHROPIC_AUTH_TOKEN`, `HOST`);
- the admin dashboard and its API, which are loopback-restricted by design;
- credential storage and handling in the config directory (`~/.mcc`), including
  the OAuth credential stores;
- the request log — anything that lands a raw secret in the database, an export,
  a log line or an HTTP response;
- the coding-agent and desktop-app configuration writers, which edit files
  outside MCC's own directory;
- the installers, the update path, and the desktop shell, including anything
  that runs code from a downloaded artifact without verifying its digest;
- the outbound provider path, where a malicious upstream response reaches the
  parser.

**Out of scope** — real, but not this project's defect:

- vulnerabilities in the upstream model providers, or in the coding agents MCC
  launches;
- an operator's own configuration choices that MCC documents and warns about
  (binding to `0.0.0.0` *with* a token set, enabling
  `WEB_FETCH_ALLOW_PRIVATE_NETWORKS`, or granting MCC a key with broader
  permissions than it needs);
- anything that requires an attacker to already have interactive access to the
  account MCC runs as. At that point they can read `~/.mcc` directly, and no
  proxy-level control changes that.

## Design decisions that are not vulnerabilities

These are deliberate, documented and tested. Reporting one is welcome as a
discussion, but it will not be treated as an advisory:

- **Credentials are identified by a masked label** — `first4…last4` plus a pool
  index. The raw key never reaches the database, a log line, or an HTTP
  response. Request and response bodies are redacted at write time, before
  storage, and the export path adds no redaction of its own because it cannot
  reach an unredacted value.
- **The dashboard and its API are loopback-only.** A request from another
  machine is refused even with a valid proxy token.
- **The server refuses to start** when `HOST` accepts outside connections and
  `ANTHROPIC_AUTH_TOKEN` is empty, rather than serving an open proxy that spends
  your provider credits. A loopback bind with no token stays allowed: that is a
  single-machine setup, not an accident. A fresh install writes `HOST=127.0.0.1`
  and a generated token.
- **Secrets stay on the machine.** MCC has no telemetry of its own and phones
  home to nothing; its only outbound traffic is to the model providers you
  configure, the catalogue sources it reads, and GitHub for updates. Where MCC
  writes another agent's configuration it turns *that* agent's usage reporting
  off — Gemini CLI's `privacy.usageStatisticsEnabled`, Claude Desktop's
  telemetry keys and RTK's opt-out are all set for you.
- **Downloaded artifacts are digest-verified** — the wheel against the digest
  GitHub publishes for the release asset, and the desktop shell against both an
  in-source pin and the release's own `SHA256SUMS-desktop-shell.txt` — before
  anything is executed.

## If you have exposed a credential

Rotate first, report second. Revoking a leaked key at the provider takes
seconds; an advisory does not stop it being used in the meantime.

1. Revoke or rotate the key in the provider's own console.
2. Replace it in MCC — **Providers** page, or the entry in `~/.mcc/.env`.
3. Then, if MCC caused the exposure rather than a screenshot or a paste, open a
   private advisory with the masked label and the surface it leaked through.

If you believe a raw secret reached the request log, the safe order is: stop the
server, rotate the key, and only then look — and say so in the report rather
than attaching the database.

## Credit

Reporters are credited in the advisory and the release notes unless they ask not
to be. There is no bug bounty; this is an independent open-source project, not a
funded program, and pretending otherwise would waste your time.
