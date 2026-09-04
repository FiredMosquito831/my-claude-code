# Pull request history

The 284 pull requests of this project, 280 of them merged, as they stood when the repository left the fork network on 2026-09-05.

GitHub pull requests are database objects rather than Git objects, so they cannot be recreated in a new repository. They are preserved here instead: one page each with the full conversation, the raw API payload under `json/`, and the original URL. Every commit they introduced is present in this repository's own history — each was squash-merged into `main`.

| # | Title | Author | Merged | Merge commit |
| --- | --- | --- | --- | --- |
| [1](pr-0001.md) | fix: repair ChatGPT OAuth and pin fork installer | @FiredMosquito831 | 2026-07-29 13:58:08 | `4d236b51b7ee` |
| [2](pr-0002.md) | Fix ChatGPT OAuth across WSL and remote sessions | @FiredMosquito831 | 2026-07-29 15:23:52 | `e41cffdc6d72` |
| [3](pr-0003.md) | feat: add route-aware observability and strict web search routing | @FiredMosquito831 | 2026-07-29 21:58:05 | `f7cec25cc873` |
| [4](pr-0004.md) | feat: capture web search input and output analytics | @FiredMosquito831 | 2026-07-30 08:51:02 | `ac043419b0d9` |
| [5](pr-0005.md) | perf(analytics): stop analytics from blocking the proxy, close sqlite connections, reclaim pruned pages | @FiredMosquito831 | 2026-07-30 11:10:56 | `05df58af4cc9` |
| [6](pr-0006.md) | fix(rotation): correct credential health defects and attribute requests to keys | @FiredMosquito831 | 2026-07-30 22:29:38 | `13158626d69d` |
| [7](pr-0007.md) | fix(rotation): fail over a rejected credential instead of failing the request | @FiredMosquito831 | 2026-07-30 22:43:18 | `9e5f1276b214` |
| [8](pr-0008.md) | chore: release 4.14.2 and point installers at the new wheel | @FiredMosquito831 | 2026-07-30 23:52:04 | `e789eaabd302` |
| [9](pr-0009.md) | feat(admin): show the running version and install updates from the dashboard | @FiredMosquito831 | 2026-07-31 00:27:03 | `b30c73333470` |
| [10](pr-0010.md) | docs: retake the version panel screenshot in a steady state | @FiredMosquito831 | 2026-07-31 00:36:34 | `2c4a47d0721b` |
| [11](pr-0011.md) | fix(install): never let an optional coding agent block the install | @FiredMosquito831 | 2026-07-31 01:16:39 | `494f42ffefa3` |
| [12](pr-0012.md) | feat(rotation): route around throttled and out-of-budget credentials | @FiredMosquito831 | 2026-07-31 01:41:31 | `34c7288bb9cd` |
| [13](pr-0013.md) | fix: remove invented request ceilings; install only the proxy, always latest | @FiredMosquito831 | 2026-07-31 02:25:15 | `71028693228b` |
| [14](pr-0014.md) | fix(install): restore the uv helpers dropped from install.ps1 | @FiredMosquito831 | 2026-07-31 02:31:36 | `118f921d244e` |
| [15](pr-0015.md) | fix(install): stop relying on script scope in the Windows installer | @FiredMosquito831 | 2026-07-31 02:38:22 | `fe3f41d56249` |
| [16](pr-0016.md) | docs: make WORKING-NOTES.md a first-class part of the agent contract | @FiredMosquito831 | 2026-07-31 10:16:29 | `1bf8be4feb5a` |
| [17](pr-0017.md) | fix(websearch): honour provider rate-limit resets, align retention, cover query text | @FiredMosquito831 | 2026-07-31 13:25:55 | `11bee5a1260a` |
| [18](pr-0018.md) | feat(websearch): forward extracted content, domain filters, and real error codes | @FiredMosquito831 | 2026-07-31 13:56:17 | `f12b042af883` |
| [19](pr-0019.md) | feat(websearch): correct adapter parameters and document every option | @FiredMosquito831 | 2026-07-31 15:21:38 | `b4586c7333f9` |
| [20](pr-0020.md) | feat(updates): show release notes in the update banner | @FiredMosquito831 | 2026-07-31 16:11:59 | `47632ab95839` |
| [21](pr-0021.md) | docs(websearch): document the new capabilities and close the .env.example drift | @FiredMosquito831 | 2026-07-31 16:49:37 | `5e6ff617ef83` |
| [22](pr-0022.md) | fix(updates): defer the Windows install until the server exits | @FiredMosquito831 | 2026-07-31 18:57:59 | `be1b8d9d3e63` |
| [23](pr-0023.md) | fix(updates): stop the deferred Windows helper dying on uv's stderr | @FiredMosquito831 | 2026-07-31 19:30:05 | `cff4a882207d` |
| [24](pr-0024.md) | fix(updates): use CREATE_NO_WINDOW so the Windows update helper actually runs | @FiredMosquito831 | 2026-07-31 19:44:22 | `47522de83209` |
| [25](pr-0025.md) | fix(updates): identify the parent by start time, and retry the deferred install | @FiredMosquito831 | 2026-07-31 23:17:56 | `cf5e85be2f93` |
| [26](pr-0026.md) | feat(analytics): split token columns per provider and report prompt caching | @FiredMosquito831 | 2026-07-31 23:37:32 | `9fdd81861635` |
| [27](pr-0027.md) | fix(analytics): read prompt cache hits from OpenAI-compatible providers | @FiredMosquito831 | 2026-08-01 10:31:19 | `1c13923cc748` |
| [28](pr-0028.md) | docs: add a complete usage guide, and distinguish unreported caching from 0% | @FiredMosquito831 | 2026-08-01 11:28:37 | `6eaeccc5ada5` |
| [29](pr-0029.md) | feat(admin): add an in-dashboard Guide page | @FiredMosquito831 | 2026-08-01 12:09:32 | `013789a7db5b` |
| [30](pr-0030.md) | docs: rewrite the usage guide with real tutorials, and correct three errors | @FiredMosquito831 | 2026-08-01 16:08:36 | `547f3c482618` |
| [31](pr-0031.md) | fix(analytics): capture cache tokens from message_delta; illustrate the Guide | @FiredMosquito831 | 2026-08-01 16:59:29 | `b28e89cfddde` |
| [32](pr-0032.md) | fix(analytics): capture reasoning and tool calls; make the guide readable | @FiredMosquito831 | 2026-08-02 18:33:12 | `2423b2cf7833` |
| [33](pr-0033.md) | fix(analytics): stop double-counting cached input tokens | @FiredMosquito831 | 2026-08-06 12:31:52 | `054fa40520cf` |
| [34](pr-0034.md) | feat(admin): configure Claude Code's settings.json from the dashboard | @FiredMosquito831 | 2026-08-06 13:51:06 | `4277aca35445` |
| [35](pr-0035.md) | feat(cli): opt into Claude Code's model picker with --discover-models | @FiredMosquito831 | 2026-08-07 01:06:37 | `0805d6deeca3` |
| [36](pr-0036.md) | feat(admin): detect every Claude settings file and warn when one outranks it | @FiredMosquito831 | _closed_ | `a17a094c5a22` |
| [37](pr-0037.md) | feat(admin): detect every Claude settings file and warn when one outranks it | @FiredMosquito831 | 2026-08-07 01:16:47 | `8339e5b1ff5c` |
| [38](pr-0038.md) | feat(admin): Get Started checklist that walks through the real pages | @FiredMosquito831 | 2026-08-07 02:18:26 | `29a8ea3426e7` |
| [39](pr-0039.md) | feat(providers): Nous Portal, Kilo AI Gateway and Cline (4.30.0) | @FiredMosquito831 | 2026-08-07 02:44:45 | `da21e3a21734` |
| [40](pr-0040.md) | feat: per-tier model fallback chains and a vision adapter model | @FiredMosquito831 | 2026-08-07 20:35:29 | `ae7c7e1fc915` |
| [41](pr-0041.md) | feat(admin): turn Get Started into a step-by-step walkthrough | @FiredMosquito831 | 2026-08-07 20:54:20 | `3c5955b68927` |
| [42](pr-0042.md) | perf(analytics): poll a cheap heartbeat instead of the whole dashboard | @FiredMosquito831 | 2026-08-07 21:15:46 | `c68984dcb016` |
| [43](pr-0043.md) | fix(analytics): a percentile that stops early is slower than one that does not | @FiredMosquito831 | 2026-08-07 21:28:15 | `6112dcb9699a` |
| [44](pr-0044.md) | fix(admin): let a completed Get Started step be opened again | @FiredMosquito831 | 2026-08-07 21:51:57 | `cd09dfdb9225` |
| [45](pr-0045.md) | feat(admin): Model Routing reads as a route, not a grid of fields | @FiredMosquito831 | 2026-08-07 22:12:55 | `4e43e825c1a7` |
| [46](pr-0046.md) | fix(admin): fallback chains never reached Apply | @FiredMosquito831 | 2026-08-07 22:24:43 | `9da0cccafcf7` |
| [47](pr-0047.md) | feat(analytics): show when a fallback model answered | @FiredMosquito831 | 2026-08-07 22:38:59 | `724ddbeb2051` |
| [48](pr-0048.md) | feat(admin): give Get Started a progress bar and the Guide a rail label | @FiredMosquito831 | 2026-08-07 22:47:30 | `f9d8165ff6a8` |
| [49](pr-0049.md) | fix(admin): restore the failover styles and stop this losing again | @FiredMosquito831 | 2026-08-07 23:01:45 | `b2b506a36afe` |
| [50](pr-0050.md) | feat(admin): make the Web Search view answer which route is live | @FiredMosquito831 | 2026-08-07 23:07:17 | `657132b9af5e` |
| [51](pr-0051.md) | feat(admin): frame the Guide around tasks and split Get Started by necessity | @FiredMosquito831 | 2026-08-08 00:27:24 | `b3342f22aa41` |
| [52](pr-0052.md) | feat(providers): Alibaba Coding Plan and Token Plan (international + China), and group the provider catalog | @FiredMosquito831 | 2026-08-08 07:56:17 | `790c23afc55b` |
| [53](pr-0053.md) | feat(admin): make 35 providers findable — grouped, searchable provider cards | @FiredMosquito831 | 2026-08-08 08:24:31 | `975e28fd62b1` |
| [54](pr-0054.md) | fix(providers): Alibaba plan/endpoint mismatch bills silently, say so | @FiredMosquito831 | 2026-08-08 08:35:04 | `9c0dc1a5c037` |
| [55](pr-0055.md) | feat(admin): providers back to a flat card grid, one card per provider | @FiredMosquito831 | 2026-08-08 10:44:58 | `281f3b0e130a` |
| [56](pr-0056.md) | fix(admin): a provider credential is a pool, so stop offering to replace it | @FiredMosquito831 | 2026-08-08 11:14:29 | `226fc4992b8c` |
| [57](pr-0057.md) | docs: the Guide and USAGE described a Providers page that no longer exists | @FiredMosquito831 | 2026-08-08 11:33:38 | `3312e5765b8a` |
| [58](pr-0058.md) | fix: custom providers 500'd, fallback chains could never fire, 5.6 models hidden | @FiredMosquito831 | 2026-08-08 20:48:45 | `35b7e6b37d95` |
| [59](pr-0059.md) | feat(analytics): record the whole route trace, and drop the bare gpt-5.6 | @FiredMosquito831 | 2026-08-08 21:28:22 | `892766f757b1` |
| [60](pr-0060.md) | feat(routing): show where images go, and give the vision adapter a chain | @FiredMosquito831 | 2026-08-08 22:15:00 | `fd986dfc12bb` |
| [61](pr-0061.md) | docs: fallback chains, route tracing and the vision adapter, as shipped | @FiredMosquito831 | 2026-08-08 22:34:03 | `bdea34835b6d` |
| [62](pr-0062.md) | feat(analytics): all-time totals that retention cannot prune, and uptime | @FiredMosquito831 | 2026-08-09 09:31:44 | `72310ac85c72` |
| [63](pr-0063.md) | feat(request-log): store bodies zstd-compressed in a side table | @FiredMosquito831 | 2026-08-09 10:05:38 | `6be0537210b5` |
| [64](pr-0064.md) | perf(request-log): reject non-matching bodies before decoding them | @FiredMosquito831 | 2026-08-09 10:38:39 | `a58fd6846c0f` |
| [65](pr-0065.md) | fix(analytics): search reasoning and tool calls, not just prompt and reply | @FiredMosquito831 | 2026-08-09 11:07:20 | `1445ae0a9a83` |
| [66](pr-0066.md) | fix(analytics): the card said "Total requests" but counted stored rows | @FiredMosquito831 | 2026-08-09 11:24:05 | `bca3df38cac6` |
| [67](pr-0067.md) | feat(request-log): compact existing history, and deduplicate bodies | @FiredMosquito831 | 2026-08-09 12:45:52 | `e44bfcd8f25b` |
| [68](pr-0068.md) | fix(request-log): stop claiming compaction widens the page size | @FiredMosquito831 | 2026-08-09 13:00:59 | `29130e66ab61` |
| [69](pr-0069.md) | feat(request-log): store the prompt in its own shared blob | @FiredMosquito831 | 2026-08-09 15:11:30 | `96458c36cc98` |
| [70](pr-0070.md) | fallback: deadline a stalled model so the chain can take over | @FiredMosquito831 | 2026-08-09 17:26:03 | `6870bffcec8c` |
| [71](pr-0071.md) | admin: make the limits editable, and stop Save deleting what it cannot see | @FiredMosquito831 | 2026-08-09 18:21:05 | `847eb2a0019d` |
| [72](pr-0072.md) | admin: give the Limits settings a page to live on | @FiredMosquito831 | 2026-08-09 18:32:25 | `ed9693ce8df7` |
| [73](pr-0073.md) | config: give every limit a usable range and a working fallback | @FiredMosquito831 | 2026-08-09 19:26:08 | `f5f30df7079e` |
| [74](pr-0074.md) | docs: describe the deadlines, ejection and the Limits tab | @FiredMosquito831 | 2026-08-09 19:45:20 | `cb0762b10635` |
| [75](pr-0075.md) | fix(providers): configurable shared credentials, real test errors, Azure OpenAI | @FiredMosquito831 | 2026-08-10 15:25:12 | `1187b64e9353` |
| [76](pr-0076.md) | fix(admin): rotation editable from either card of a shared credential | @FiredMosquito831 | 2026-08-10 15:52:15 | `b69b87159aa6` |
| [77](pr-0077.md) | fix(updater): restart automatically into the installed release | @FiredMosquito831 | 2026-08-10 18:28:36 | `bc64ed81ec71` |
| [78](pr-0078.md) | feat: add Command Code provider | @FiredMosquito831 | 2026-08-11 08:44:03 | `48b27bd994fd` |
| [79](pr-0079.md) | fix: harden native Messages compatibility | @FiredMosquito831 | 2026-08-11 09:02:00 | `c7c799508d24` |
| [80](pr-0080.md) | fix(updater): graceful automatic update/process handoff (#80) | @FiredMosquito831 | 2026-08-11 11:20:28 | `8c978ac04dfe` |
| [81](pr-0081.md) | feat: add My Claude Code command family alongside legacy aliases (#81) | @FiredMosquito831 | 2026-08-11 12:36:51 | `a88458c157b7` |
| [82](pr-0082.md) | feat: My Claude Code 5.0 — complete rebrand from Free Claude Code (#82) | @FiredMosquito831 | 2026-08-11 16:58:15 | `6c9f324b3287` |
| [83](pr-0083.md) | fix: 5.0.1 repair installer wheel/distribution names, digest extraction, and restart rebind | @FiredMosquito831 | 2026-08-11 19:17:41 | `39d547e2b26b` |
| [84](pr-0084.md) | fix: 5.0.2 installer running-launcher guard, mcc-first install message, owner-aware version | @FiredMosquito831 | 2026-08-11 19:48:51 | `35fe976add41` |
| [85](pr-0085.md) | fix: 5.0.3 restore install-while-running; Windows defers instead of refusing | @FiredMosquito831 | 2026-08-11 20:40:58 | `1a33baa87660` |
| [86](pr-0086.md) | feat: 5.1.0 add mcc-help command and lead installers with mcc commands | @FiredMosquito831 | 2026-08-11 21:08:41 | `c5e123c2aac6` |
| [87](pr-0087.md) | fix: 5.1.1 installer no longer errors on a single running launcher | @FiredMosquito831 | 2026-08-11 21:27:05 | `65b69ebb5c53` |
| [88](pr-0088.md) | fix: 5.1.2 Windows installer shows the same mcc command reference as WSL | @FiredMosquito831 | 2026-08-11 21:41:10 | `9a7cc5f18369` |
| [89](pr-0089.md) | feat(export): export window (JSON/CSV/XLSX/TXT, fields, grouping, full-DB) + site persistence | @FiredMosquito831 | 2026-08-12 00:06:45 | `3caaa0d493df` |
| [90](pr-0090.md) | fix(install): verify mcc-*/my-claude-code command family in both installers | @FiredMosquito831 | 2026-08-12 00:24:00 | `4db38ad1b23c` |
| [91](pr-0091.md) | fix(install): deferred Windows update now actually runs uv and creates the mcc-* commands | @FiredMosquito831 | 2026-08-12 02:21:19 | `c5ec34efc334` |
| [92](pr-0092.md) | feat(claude): mcc-claude / fcc-claude set ENABLE_WEB_SERVER_TOOLS=true | @FiredMosquito831 | 2026-08-12 10:43:02 | `232f768e8454` |
| [93](pr-0093.md) | fix(claude): mcc-claude web-tools flag mirrors the proxy setting (default on) | @FiredMosquito831 | 2026-08-12 11:05:20 | `58a831750972` |
| [94](pr-0094.md) | fix(install): Windows update completes immediately even with launchers open | @FiredMosquito831 | 2026-08-12 11:54:50 | `f0a116833489` |
| [95](pr-0095.md) | fix(install): rename path must not fall through to the deferred helper after success | @FiredMosquito831 | _closed_ | — |
| [96](pr-0096.md) | fix(install): rename path must not fall through to the deferred helper after success | @FiredMosquito831 | 2026-08-12 12:01:46 | `537008264a25` |
| [97](pr-0097.md) | fix(install): rename launcher shims aside so uv can install fresh while windows open | @FiredMosquito831 | 2026-08-12 12:17:57 | `1e949d26426c` |
| [98](pr-0098.md) | fix(install): hybrid Windows update -- install now, finish only the running shim later | @FiredMosquito831 | 2026-08-12 12:44:46 | `58b7ceff5c99` |
| [99](pr-0099.md) | fix(install): detect the fresh install landed via dist-info, not the uv receipt | @FiredMosquito831 | 2026-08-12 12:52:56 | `a67c48b4b930` |
| [100](pr-0100.md) | fix(export): CSV/XLSX exports had empty data cells; add provider/model filter + All time period | @FiredMosquito831 | 2026-08-12 14:42:28 | `323fecc1f683` |
| [101](pr-0101.md) | feat(analytics): retain full websearch output + readable request detail (5.5.0) | @FiredMosquito831 | 2026-08-12 17:43:08 | `4c398167bfb2` |
| [102](pr-0102.md) | fix(cli): config reload no longer crashes the server + readable websearch detail (5.5.1) | @FiredMosquito831 | 2026-08-12 20:46:22 | `78d8d9c97761` |
| [103](pr-0103.md) | test: assert no FCC background thread survives a test session | @FiredMosquito831 | 2026-08-13 00:56:05 | `d31f723539df` |
| [104](pr-0104.md) | feat(theme): add Velvet — deep navy base with a vibrant velvet-red accent | @FiredMosquito831 | 2026-08-13 00:56:09 | `1e92067deddd` |
| [105](pr-0105.md) | feat(admin): persist analytics filters + page across refresh, add Clear filters | @FiredMosquito831 | 2026-08-13 10:31:55 | `2a192db23ce0` |
| [106](pr-0106.md) | docs: my-claude-code branding + mcc commands + retaken dashboard screenshots | @FiredMosquito831 | 2026-08-13 10:33:18 | `8a0f4bcecf48` |
| [107](pr-0107.md) | chore: bump 5.5.1 -> 5.6.0 (MINOR: Velvet theme + analytics persistence + docs) | @FiredMosquito831 | 2026-08-13 10:38:55 | `b217be0a6781` |
| [108](pr-0108.md) | feat(admin): dedicated Claude Code page + consistent theme picker | @FiredMosquito831 | 2026-08-13 11:28:16 | `0a836b34e9ab` |
| [109](pr-0109.md) | chore: bump 5.6.0 -> 5.7.0 (MINOR: dedicated Claude Code page + theme picker fix) | @FiredMosquito831 | 2026-08-13 11:33:45 | `a311e3e9091b` |
| [110](pr-0110.md) | feat(admin): rename page to Configure Claude Code + surface per-session alternative | @FiredMosquito831 | 2026-08-13 12:04:27 | `43bc23a10ebb` |
| [111](pr-0111.md) | chore: bump 5.7.0 -> 5.7.1 (PATCH: rename page to Configure Claude Code) | @FiredMosquito831 | 2026-08-13 12:07:05 | `0ad9060a48ae` |
| [112](pr-0112.md) | feat(admin): redesign Configure Claude Code page with 'choose how you connect' | @FiredMosquito831 | 2026-08-13 12:40:56 | `bff5af03e541` |
| [113](pr-0113.md) | chore: bump 5.7.1 -> 5.7.2 (MINOR: Configure Claude Code page redesign) | @FiredMosquito831 | 2026-08-13 12:44:05 | `15e555ec65ac` |
| [114](pr-0114.md) | docs: complete Claude Code configuration reference (322 env vars + 136 settings keys), generated from upstream | @FiredMosquito831 | 2026-08-13 18:23:02 | `7b9225e4ae86` |
| [115](pr-0115.md) | feat(admin): settings-editor API for the Configure Claude Code page (5.8.0) | @FiredMosquito831 | 2026-08-13 18:42:52 | `9db65be03d3b` |
| [116](pr-0116.md) | feat(admin): guided settings editor on the Configure Claude Code page (5.9.0) | @FiredMosquito831 | 2026-08-13 19:00:58 | `29309203317d` |
| [117](pr-0117.md) | feat(admin): restructure Configure Claude Code around what you configure (5.10.0) | @FiredMosquito831 | 2026-08-13 19:24:37 | `629d39195695` |
| [118](pr-0118.md) | feat(admin): discover settings.json files and label which world each is in (5.11.0) | @FiredMosquito831 | 2026-08-13 20:31:42 | `7f24161bd74d` |
| [119](pr-0119.md) | feat(admin): binary settings become true / false / not set (5.12.0) | @FiredMosquito831 | 2026-08-13 20:57:02 | `9e4161c24575` |
| [120](pr-0120.md) | Record image input, and divert on images a tool delivered | @FiredMosquito831 | 2026-08-14 21:21:33 | `b0c342fe1708` |
| [121](pr-0121.md) | Name the vision model, and record an image with nowhere to go | @FiredMosquito831 | 2026-08-14 22:00:03 | `82b352f8c04f` |
| [122](pr-0122.md) | chore: rename repo slug to my-claude-code + switch to PolyForm Noncommercial license | @FiredMosquito831 | 2026-08-15 16:49:47 | `ec81f3a08abf` |
| [123](pr-0123.md) | refactor(providers): port OpenAI model-listing + tool-name + reasoning-detail machinery | @FiredMosquito831 | 2026-08-15 21:28:29 | `9fd3311f99ac` |
| [124](pr-0124.md) | feat(providers): add qwencloud, qwencloud_coding, agnes, wandb, bedrock, tokenrouter, nararoute | @FiredMosquito831 | 2026-08-15 22:56:44 | `f5f37e266823` |
| [125](pr-0125.md) | feat(providers): add xai, together, deepinfra, siliconflow, nebius, chutes, featherless | @FiredMosquito831 | 2026-08-15 23:31:29 | `e46c287b01c0` |
| [126](pr-0126.md) | feat(providers): add zenmux, upgrade cline in place, wire tool-turn boundary | @FiredMosquito831 | 2026-08-16 00:07:05 | `607ee4b5279c` |
| [127](pr-0127.md) | feat(providers): add Google Vertex AI + OpenAI connected account | @FiredMosquito831 | 2026-08-16 07:48:38 | `09c6fdcf43f8` |
| [128](pr-0128.md) | feat(desktop): system tray app with config persistence + startup-at-login | @FiredMosquito831 | 2026-08-16 08:56:53 | `48f916ee21d1` |
| [129](pr-0129.md) | feat(rtk): token-optimizer state model + reconciler + CLI + installer flag | @FiredMosquito831 | 2026-08-16 10:31:22 | `c15489dd5186` |
| [130](pr-0130.md) | feat(rtk): token-optimizer dashboard card + desktop tray toggles | @FiredMosquito831 | 2026-08-16 11:06:16 | `bee5da2a6335` |
| [131](pr-0131.md) | feat(codex): runtime model-catalog publisher for the Codex App | @FiredMosquito831 | 2026-08-16 11:36:58 | `bb136200bdec` |
| [132](pr-0132.md) | feat(desktop): explicit server-ownership mode + per-platform autostart | @FiredMosquito831 | 2026-08-16 12:42:36 | `020d5942043f` |
| [133](pr-0133.md) | docs: comprehensive post-arc pass — providers, desktop, RTK, codex, deployment | @FiredMosquito831 | 2026-08-16 16:29:27 | `e4bbccc8f977` |
| [134](pr-0134.md) | fix(installer): register + surface mcc-desktop and mcc-rtk in installers and mcc-help | @FiredMosquito831 | 2026-08-16 18:21:29 | `a38171800217` |
| [135](pr-0135.md) | fix(installer): deferred install no longer crashes verifying missing shims | @FiredMosquito831 | 2026-08-16 19:24:20 | `c9de471fe2bc` |
| [136](pr-0136.md) | fix(web-tools): intercept Claude Code auto tool requests | @FiredMosquito831 | 2026-08-17 00:13:25 | `a82d4e66bbfb` |
| [137](pr-0137.md) | fix(gemini): enforce mutually exclusive reasoning effort and thinking config (#137) | @supportclone | _open_ | — |
| [138](pr-0138.md) | feat(providers): first-party Anthropic provider (Claude Console API key) | @FiredMosquito831 | 2026-08-18 23:46:21 | `34932bfc453f` |
| [139](pr-0139.md) | feat(providers): Claude subscription OAuth provider, gated to the Claude Code CLI | @FiredMosquito831 | 2026-08-19 00:43:03 | `62efacdf6490` |
| [140](pr-0140.md) | docs: document both Anthropic providers across README, USAGE and the Guide | @FiredMosquito831 | 2026-08-19 01:16:02 | `c22199ba9821` |
| [141](pr-0141.md) | feat(desktop): ship the real brand mark and an opt-in desktop launcher | @FiredMosquito831 | 2026-08-19 08:07:50 | `16beb7487569` |
| [142](pr-0142.md) | feat(desktop): give mcc-desktop a real window, and make its lifecycle honest | @FiredMosquito831 | 2026-08-19 08:21:14 | `f88c849f9ac1` |
| [143](pr-0143.md) | feat(desktop): choose the window provider from the dashboard | @FiredMosquito831 | 2026-08-19 08:59:05 | `b178ff610eb6` |
| [144](pr-0144.md) | feat(desktop): make the desktop process configurable instead of hardcoded | @FiredMosquito831 | 2026-08-19 10:14:15 | `d1b2bb6f4e78` |
| [145](pr-0145.md) | test(admin): guard the coupling between admin.js, index.html and admin.css | @FiredMosquito831 | 2026-08-19 11:51:08 | `59df89555297` |
| [146](pr-0146.md) | feat(desktop): remember where you put the window, and whether it was open | @FiredMosquito831 | 2026-08-19 11:51:14 | `c7f1d567546c` |
| [147](pr-0147.md) | fix(installer): a running mcc-desktop no longer destroys the Windows install | @FiredMosquito831 | 2026-08-19 12:12:34 | `9a49780102e8` |
| [148](pr-0148.md) | docs(desktop): document the desktop app across README, USAGE and the Guide | @FiredMosquito831 | 2026-08-19 12:41:35 | `c9b06000441c` |
| [149](pr-0149.md) | feat(anthropic): sign in or import Claude Code credentials from the dashboard | @FiredMosquito831 | 2026-08-19 13:50:48 | `93f823dbaefb` |
| [150](pr-0150.md) | chore: gitignore JOURNAL.md alongside WORKING-NOTES.md | @FiredMosquito831 | 2026-08-19 17:02:37 | `b553459bd279` |
| [151](pr-0151.md) | fix(fireworks): send a documented reasoning wire shape | @FiredMosquito831 | 2026-08-19 20:15:38 | `d28ba20203cc` |
| [152](pr-0152.md) | fix(deepseek): stop silently discarding a forced tool choice | @FiredMosquito831 | 2026-08-19 20:36:00 | `813ef94ad046` |
| [153](pr-0153.md) | fix(admin): stop writing 54 dev-only smoke keys into the user's .env | @FiredMosquito831 | 2026-08-19 21:13:41 | `2fc8a877e559` |
| [154](pr-0154.md) | fix(desktop): stop the tray reverting settings changed elsewhere | @FiredMosquito831 | 2026-08-19 23:23:10 | `6d8be470ba7a` |
| [155](pr-0155.md) | feat(models): read per-model reasoning capability from models.dev | @FiredMosquito831 | 2026-08-20 00:17:52 | `eaf7a9463679` |
| [156](pr-0156.md) | feat(reasoning): send the effort the resolved model actually accepts | @FiredMosquito831 | 2026-08-20 01:15:42 | `40565b985130` |
| [157](pr-0157.md) | feat(analytics): record what reasoning was requested, not just what was sent | @FiredMosquito831 | 2026-08-20 02:18:00 | `52effd676853` |
| [158](pr-0158.md) | feat(reasoning): offer adaptive thinking as a per-tier choice | @FiredMosquito831 | 2026-08-20 10:22:06 | `e97feeaea01a` |
| [159](pr-0159.md) | fix(models): match four more providers to models.dev, and tolerate :free tags | @FiredMosquito831 | 2026-08-20 10:51:55 | `3f579445d941` |
| [160](pr-0160.md) | docs(reasoning): correct the README, and widen the strippable tag list | @FiredMosquito831 | 2026-08-20 11:39:57 | `f54950a81c42` |
| [161](pr-0161.md) | fix(models,analytics): reverse tag lookup, and record client adaptive | @FiredMosquito831 | 2026-08-20 12:35:51 | `ea4ccbbf96ff` |
| [162](pr-0162.md) | feat(analytics): capture inbound request headers behind a positive allow-list | @FiredMosquito831 | 2026-08-20 16:20:32 | `396540aa242f` |
| [163](pr-0163.md) | feat(routing): let a route's primary model reorder with its fallbacks | @FiredMosquito831 | 2026-08-21 11:21:43 | `ca47400207cf` |
| [164](pr-0164.md) | fix(routing): make model refs readable on the routing cards | @FiredMosquito831 | 2026-08-21 12:21:54 | `305d6ccbafc7` |
| [165](pr-0165.md) | feat(analytics): record what every model on a route did, and why | @FiredMosquito831 | 2026-08-21 13:33:06 | `8f30788714bb` |
| [166](pr-0166.md) | fix(routing): anchor the commit boundary to content, not to the first frame | @FiredMosquito831 | 2026-08-21 14:50:15 | `e051b7d874c5` |
| [167](pr-0167.md) | fix(routing): guarantee the chain a turn, and let a bench outlive a request | @FiredMosquito831 | 2026-08-21 15:08:04 | `2d9f8c49d65c` |
| [168](pr-0168.md) | feat(routing): one failure vocabulary, and a configurable fallback trigger | @FiredMosquito831 | 2026-08-21 15:31:09 | `5486192eba9f` |
| [169](pr-0169.md) | feat(routing): stop a stream that produced output and then went quiet | @FiredMosquito831 | 2026-08-21 17:12:22 | `65c1e21fa4ed` |
| [170](pr-0170.md) | fix(models.dev): let a model's own provider describe it | @FiredMosquito831 | 2026-08-21 18:36:23 | `5f27a8e09304` |
| [171](pr-0171.md) | feat(optimizer): record which local rule answered a request, and stop crediting a provider | @FiredMosquito831 | 2026-08-22 00:28:34 | `f4725ecc157d` |
| [172](pr-0172.md) | fix(fallback): a context-length 400 must not end the route | @FiredMosquito831 | 2026-08-22 11:13:31 | `b7be8f735bb4` |
| [173](pr-0173.md) | chore(optimizer): remove three local rules that have never fired | @FiredMosquito831 | 2026-08-22 11:25:20 | `7ff0f5a48cbe` |
| [174](pr-0174.md) | feat(rtk): read RTK's own savings data instead of discarding it | @FiredMosquito831 | 2026-08-22 12:52:06 | `1b88efa8dd1b` |
| [175](pr-0175.md) | feat(optimizer): find recurring prompt families the rule set does not cover | @FiredMosquito831 | 2026-08-22 13:54:16 | `d13987461673` |
| [176](pr-0176.md) | feat(optimizer): trim Read/Grep/Glob tool results, off by default | @FiredMosquito831 | 2026-08-22 14:01:47 | `fbf19624fdd2` |
| [177](pr-0177.md) | docs(optimizer): the trimming module's prefix-stability claim was false | @FiredMosquito831 | 2026-08-22 15:06:57 | `7200504b588d` |
| [178](pr-0178.md) | feat(optimizer): a dedicated Token Optimizer page | @FiredMosquito831 | 2026-08-22 16:37:41 | `f5ee2eb0d292` |
| [179](pr-0179.md) | docs: bring the public docs up to 5.48.0, and fix what was already wrong | @FiredMosquito831 | 2026-08-22 18:41:15 | `9cdfa7485266` |
| [180](pr-0180.md) | feat(docs): a Docs page in the dashboard, and the Guide brought to 5.48.0 | @FiredMosquito831 | 2026-08-22 20:10:27 | `5b70bf013455` |
| [181](pr-0181.md) | fix(routing): use the fallback chain when a model only thinks, and when a provider is in cooldown | @FiredMosquito831 | 2026-08-23 00:00:33 | `861060aeb543` |
| [182](pr-0182.md) | fix(routing): give a thinking model its own deadline, not the silent one's | @FiredMosquito831 | 2026-08-23 00:53:18 | `b48bb9d72bdf` |
| [183](pr-0183.md) | fix(ci): arm the wheel e2e guard that could never run | @FiredMosquito831 | 2026-08-24 13:29:58 | `e2a77febcfb7` |
| [184](pr-0184.md) | fix(installer): make pinned --version installs verifiable instead of always failing | @FiredMosquito831 | 2026-08-24 14:01:35 | `2bcedb040251` |
| [185](pr-0185.md) | fix(uninstallers): removal was impossible on v5 installs | @FiredMosquito831 | 2026-08-24 13:33:09 | `1d2217a28e01` |
| [186](pr-0186.md) | fix(lifecycle): read BOM'd upgrade receipts and survive Retry-After: 0 | @FiredMosquito831 | 2026-08-24 16:17:15 | `80d01f69951f` |
| [187](pr-0187.md) | feat(messaging): surface open messaging auth via startup warnings + admin payload | @FiredMosquito831 | 2026-08-24 17:29:28 | `1771f5dd7c5f` |
| [188](pr-0188.md) | fix(admin): close XSS sinks in section headings and custom provider cards | @FiredMosquito831 | 2026-08-24 18:16:07 | `e8c97e98a32e` |
| [189](pr-0189.md) | fix(core): redact Telegram bot tokens and Slack legacy tokens in previews | @FiredMosquito831 | 2026-08-24 18:07:06 | `8d0c704b8cb6` |
| [190](pr-0190.md) | fix(desktop): wire autostart reconciliation, close persistence, tray gating, GUI errors | @FiredMosquito831 | 2026-08-24 22:02:15 | `70fed7b9191d` |
| [191](pr-0191.md) | fix: cross-surface correctness batch — responses usage, atomic migrations, TXT fidelity, extra_body, executor telemetry | @FiredMosquito831 | 2026-08-24 21:08:58 | `b3cc35b9c1aa` |
| [192](pr-0192.md) | feat(providers): capability-aware Anthropic reasoning budgets | @FiredMosquito831 | 2026-08-24 19:21:09 | `471a74b4fa3c` |
| [193](pr-0193.md) | fix(desktop): wire autostart reconciliation, close persistence, tray gating, GUI errors (redelivery) | @FiredMosquito831 | _closed_ | — |
| [194](pr-0194.md) | refactor(rotation): consolidate provider and websearch engines into core | @FiredMosquito831 | 2026-08-25 21:22:13 | `9c6f9301d253` |
| [195](pr-0195.md) | feat(fallback): rate-based eject + retry-once toggle, with a legacy mode | @FiredMosquito831 | 2026-08-26 22:31:57 | `901238c3cda0` |
| [196](pr-0196.md) | feat(fallback): rate-based eject + retry-once + bench-off toggle | @FiredMosquito831 | 2026-08-27 15:59:03 | `741992989ffb` |
| [197](pr-0197.md) | feat(reasoning): effort floor 1024, adaptation warnings in log + dashboard, mandatory-model handling, dead-encoder removal | @FiredMosquito831 | 2026-08-28 12:08:35 | `fcd90c010565` |
| [198](pr-0198.md) | fix(providers): stop inventing request parameters and restore route benching | @FiredMosquito831 | 2026-08-28 16:15:27 | `5b9e687055a3` |
| [199](pr-0199.md) | fix(chatgpt_oauth): honour the reasoning policy instead of hardcoding medium | @FiredMosquito831 | 2026-08-28 16:50:21 | `31828bc7b62f` |
| [200](pr-0200.md) | feat(providers): parse the whole /models payload and merge capability gateway-first | @FiredMosquito831 | 2026-08-28 17:25:20 | `95ec180884a6` |
| [201](pr-0201.md) | feat(output): send each model own max output tokens, not a fixed 81920 | @FiredMosquito831 | 2026-08-28 18:13:28 | `40988fc99136` |
| [202](pr-0202.md) | feat(reasoning): size thinking against the model, and leave room for the answer | @FiredMosquito831 | 2026-08-28 19:01:38 | `ceb21bb1acf8` |
| [203](pr-0203.md) | feat(models): hide-only visibility globs, and let NIM own parallel_tool_calls | @FiredMosquito831 | 2026-08-28 22:15:11 | `3cd1551613bc` |
| [204](pr-0204.md) | feat(config): let a user set request parameters per provider and per model | @FiredMosquito831 | 2026-08-28 22:51:53 | `a50089d2ce76` |
| [205](pr-0205.md) | feat(admin): a Models page for visibility, parameter overrides and capability sources | @FiredMosquito831 | 2026-08-28 23:15:29 | `a5891ab8f85e` |
| [206](pr-0206.md) | feat(analytics): record the request body MCC actually sends, per attempt | @FiredMosquito831 | 2026-08-28 23:35:48 | `bed0a0e0ba9f` |
| [207](pr-0207.md) | fix(models): resolve model capabilities down an explicit 8-tier ladder | @FiredMosquito831 | 2026-08-29 00:15:03 | `ba350f324602` |
| [208](pr-0208.md) | feat(commandcode): send the gateway's reasoning_effort and read the reasoning it returns | @FiredMosquito831 | 2026-08-29 00:42:45 | `34b764f3f362` |
| [209](pr-0209.md) | fix(rotation): stop charging request-shaped failures to credential health | @FiredMosquito831 | 2026-08-29 01:37:05 | `b4badf730208` |
| [210](pr-0210.md) | fix(nvidia_nim): only strip a request field when the 400 names it | @FiredMosquito831 | 2026-08-29 01:56:18 | `8570ced9ea8b` |
| [211](pr-0211.md) | fix(output-tokens): floor the context-headroom bound instead of emitting a 3-token budget | @FiredMosquito831 | 2026-08-29 02:10:23 | `ae189c382e64` |
| [212](pr-0212.md) | fix(reasoning): wire OpenCode's effort enum, widen DeepSeek's, and record the NO_REASONING audit | @FiredMosquito831 | 2026-08-29 02:41:09 | `3e8a85924c12` |
| [213](pr-0213.md) | fix(reasoning): give Command Code the on-value a level-less policy needs | @FiredMosquito831 | 2026-08-29 03:29:15 | `7502477c8ec0` |
| [214](pr-0214.md) | fix(rotation): stop credential rotation being preempted before it runs | @FiredMosquito831 | 2026-08-29 03:48:54 | `e0dcf2891e18` |
| [215](pr-0215.md) | fix(admin): make the Models page usable — driven in a real browser | @FiredMosquito831 | 2026-08-29 04:25:15 | `c35cf69a62d2` |
| [216](pr-0216.md) | fix(rotation): judge a key only on signals about the key | @FiredMosquito831 | 2026-08-29 13:57:24 | `fd2bc3495f0a` |
| [217](pr-0217.md) | fix(admin): save what was chosen, never what the defaults happened to be | @FiredMosquito831 | 2026-08-29 14:31:26 | `58759b1c824d` |
| [218](pr-0218.md) | feat(admin): the Limits page becomes Limits & Resilience, and says what a deadline really is | @FiredMosquito831 | 2026-08-29 16:09:01 | `310020a1cca6` |
| [219](pr-0219.md) | feat(reasoning): decide the wire from MODEL capability AND HOST dialect | @FiredMosquito831 | 2026-08-29 17:04:02 | `cedc35fd4365` |
| [220](pr-0220.md) | feat(observability): store the knobs, name the credential, measure the reasoning | @FiredMosquito831 | 2026-08-29 18:02:20 | `64b95d81a8ff` |
| [221](pr-0221.md) | feat(reasoning): one dialect for every OpenAI-compatible host, with a reject-and-remember net under it | @FiredMosquito831 | 2026-08-29 19:02:39 | `9a169922513c` |
| [222](pr-0222.md) | fix(reasoning): a toggle-only model on an effort-only host sends the user's rung, and the richer stated record wins across catalogue rungs | @FiredMosquito831 | 2026-08-29 19:39:47 | `8229b8077d63` |
| [223](pr-0223.md) | feat(models): bulk visibility — one glob per provider, selection, facets, undo | @FiredMosquito831 | 2026-08-29 20:43:53 | `e609b6666038` |
| [224](pr-0224.md) | feat(reasoning): give a thinking turn the model's whole output allowance, and ship the ceiling set | @FiredMosquito831 | 2026-08-29 22:50:40 | `96bc2f1ba72e` |
| [225](pr-0225.md) | docs: carry every user-facing surface to 6.8.0 | @FiredMosquito831 | 2026-08-29 23:38:03 | `164fd16d7c80` |
| [226](pr-0226.md) | feat: answer client model-routing probes locally, echoing the model that would answer | @FiredMosquito831 | 2026-08-30 02:52:43 | `f8e81ed0afc5` |
| [227](pr-0227.md) | feat(deadlines): let the first-token deadline be the number that fires, and raise the shipped deadlines 1.5x | @FiredMosquito831 | 2026-08-30 03:31:16 | `654224ace42f` |
| [228](pr-0228.md) | feat(retries): cap the retry ladder's longest wait at 10s, and expose the last four unreachable settings | @FiredMosquito831 | 2026-08-30 10:27:13 | `972dafb81d25` |
| [229](pr-0229.md) | feat(analytics): record every upstream try behind an attempt, not just the last status | @FiredMosquito831 | 2026-08-30 11:26:16 | `bd5d188a6236` |
| [230](pr-0230.md) | feat(analytics): hide locally answered requests by default, and apply filters as they change | @FiredMosquito831 | 2026-08-30 11:54:03 | `9eec5679c7aa` |
| [231](pr-0231.md) | fix(stall): count buffered tool arguments as upstream progress | @FiredMosquito831 | 2026-08-31 01:26:42 | `306c45b4e8d5` |
| [232](pr-0232.md) | feat(bench): ship chain benching off, count only model-shaped failures, and say why a model was skipped | @FiredMosquito831 | 2026-08-31 02:59:15 | `af50abdc9e4b` |
| [233](pr-0233.md) | feat(stall): end a committed stream as a truncated message, not an API error | @FiredMosquito831 | 2026-08-31 11:17:51 | `a68d05866f3d` |
| [234](pr-0234.md) | feat(deadlines): ship every deadline at 0, and name the knob in the error | @FiredMosquito831 | 2026-08-31 11:57:42 | `0eddd60e1c4b` |
| [235](pr-0235.md) | perf(analytics): serve request stats from an hourly rollup, not a full scan | @FiredMosquito831 | 2026-08-31 13:13:15 | `7e14d997e205` |
| [236](pr-0236.md) | feat(stall): continue a dead answer on the next model, and let the commit holdback wait for characters | @FiredMosquito831 | 2026-08-31 14:11:55 | `c9b90c7a6349` |
| [237](pr-0237.md) | feat(rotation): scope a 429 bench to the (key, model) pair | @FiredMosquito831 | 2026-08-31 16:23:51 | `1d8b1ed520a2` |
| [238](pr-0238.md) | feat(rate-limit): route around a rate-limited model instead of sleeping on it | @FiredMosquito831 | 2026-08-31 18:07:47 | `8a8357610de0` |
| [239](pr-0239.md) | feat(routing): drag a fallback chain, and pause one entry without deleting it | @FiredMosquito831 | 2026-08-31 19:27:55 | `844f322154a6` |
| [240](pr-0240.md) | fix(providers): a custom provider's models must appear the moment it is added | @FiredMosquito831 | 2026-09-01 01:38:20 | `c1c07285dd4d` |
| [241](pr-0241.md) | feat(harnesses): one registry per coding agent, and real model metadata in every catalogue it generates | @FiredMosquito831 | 2026-09-01 14:58:01 | `d1cfd38bbb47` |
| [242](pr-0242.md) | fix(models): one way to hide a model, and a row that says why it is hidden | @FiredMosquito831 | 2026-09-01 16:09:15 | `3878ededb70f` |
| [243](pr-0243.md) | feat(custom-providers): learn the host's reasoning dialect, show per-key health, and stop disable from breaking Settings | @FiredMosquito831 | 2026-09-01 17:42:50 | `abf5e4d16f8d` |
| [244](pr-0244.md) | feat(harnesses): launch OpenCode, OpenCode 2 and Kilo without touching their config, and list every command per agent | @FiredMosquito831 | 2026-09-01 19:23:31 | `7717db7190cd` |
| [245](pr-0245.md) | feat(harnesses): serve Command Code by merging one key into its config, and read the header it actually sends | @FiredMosquito831 | 2026-09-01 22:30:40 | `4b7c08abfad7` |
| [246](pr-0246.md) | feat(harnesses): serve Kimi Code from a config.toml MCC owns, named by the flag Kimi publishes | @FiredMosquito831 | 2026-09-01 23:34:10 | `7c31da53a03d` |
| [247](pr-0247.md) | feat(harnesses): serve Qwen Code and Crush from documents MCC owns, named by variables each CLI publishes | @FiredMosquito831 | 2026-09-02 00:33:11 | `3c3d20078a5e` |
| [248](pr-0248.md) | feat(api): serve OpenAI Chat Completions, and refuse to serve an unauthenticated proxy to the network | @FiredMosquito831 | 2026-09-02 04:19:34 | `c82c0ad0c7e7` |
| [249](pr-0249.md) | fix(install): rename every launcher shim aside, and never report a missing command as verified | @FiredMosquito831 | 2026-09-02 06:31:08 | `b156a6fd9b4f` |
| [250](pr-0250.md) | feat(harnesses): serve Cline, Goose, Aider and Droid, each through the lever its own CLI publishes | @FiredMosquito831 | 2026-09-02 09:32:19 | `06034a976aad` |
| [251](pr-0251.md) | feat(gemini): serve Google's own protocol, and launch the one CLI that speaks nothing else | @FiredMosquito831 | 2026-09-02 12:18:04 | `ecd835104e29` |
| [252](pr-0252.md) | feat(providers): make both upstream recovery nets universal | @FiredMosquito831 | 2026-09-02 13:34:58 | `fcf9b836943a` |
| [253](pr-0253.md) | fix(installer): install while MCC runs, around a shim Windows will not release | @FiredMosquito831 | 2026-09-02 14:44:09 | `f783ed15a758` |
| [254](pr-0254.md) | feat(failures): an empty wallet is not a malformed request | @FiredMosquito831 | 2026-09-02 15:37:03 | `791e795c2a65` |
| [255](pr-0255.md) | feat(catalogues): every field walks the ladder, and every CLI loads the file | @FiredMosquito831 | 2026-09-02 17:41:02 | `cf39899c17a2` |
| [256](pr-0256.md) | fix(admin): a pause writes the pause, and nothing else | @FiredMosquito831 | 2026-09-02 19:26:17 | `f8cdf9266d29` |
| [257](pr-0257.md) | fix(anthropic-oauth): put the subscription token where Anthropic reads it | @FiredMosquito831 | 2026-09-02 21:13:27 | `0c8f78552b45` |
| [258](pr-0258.md) | fix(harnesses): the server writes every agent's model list, the launcher reads it | @FiredMosquito831 | 2026-09-02 22:05:12 | `64d7b6c417f6` |
| [259](pr-0259.md) | feat(analytics): name the coding agent behind every request | @FiredMosquito831 | 2026-09-03 00:28:38 | `0a40abd45d6c` |
| [260](pr-0260.md) | feat(models): one picker entry per tier, in every coding agent | @FiredMosquito831 | 2026-09-03 02:56:23 | `de483f6098ac` |
| [261](pr-0261.md) | feat(providers): add HyperCharm as a generic OpenAI-chat gateway | @FiredMosquito831 | 2026-09-03 03:59:48 | `fb2818ae960e` |
| [262](pr-0262.md) | docs: bring the Guide, Get Started and the reference docs up to 6.39.0 | @FiredMosquito831 | 2026-09-03 09:35:02 | `6939d2707751` |
| [263](pr-0263.md) | chore(license): dual-license under AGPL-3.0-or-later or a commercial license | @FiredMosquito831 | 2026-09-03 10:15:44 | `359b4013c6ea` |
| [264](pr-0264.md) | feat(config): ~/.mcc default for new installs; ~/.fcc stays legacy, opt-in migrate | @FiredMosquito831 | 2026-09-03 17:45:10 | `f7940cf52ebc` |
| [265](pr-0265.md) | fix(shutdown): one bound for the whole stop, and no new work during the drain | @FiredMosquito831 | 2026-09-04 00:59:46 | `2fc04867d461` |
| [266](pr-0266.md) | fix(config-dir): only mcc-migrate ever moves ~/.fcc, and repair what 6.40.0 left behind | @FiredMosquito831 | 2026-09-04 03:36:55 | `0d86fce9e4ab` |
| [267](pr-0267.md) | perf(startup): stop importing what the first /health never asks | @FiredMosquito831 | 2026-09-04 05:19:19 | `be93cf3efd7f` |
| [268](pr-0268.md) | fix(desktop): uninstallers remove what the installers created (6.41.3) | @FiredMosquito831 | 2026-09-04 05:49:46 | `2700ce9e08ca` |
| [269](pr-0269.md) | feat(desktop): mcc-desktop --print-status, the JSON status surface (6.42.0) | @FiredMosquito831 | 2026-09-04 07:12:12 | `9396013b9104` |
| [270](pr-0270.md) | test: no test run may touch the real machine (6.42.1) | @FiredMosquito831 | 2026-09-04 08:16:40 | `22110ceb749a` |
| [271](pr-0271.md) | fix(anthropic-oauth): stop a dead store masking a working credential (6.43.0) | @FiredMosquito831 | 2026-09-04 09:37:39 | `9e55af16a027` |
| [272](pr-0272.md) | Desktop shell: the binary | @FiredMosquito831 | 2026-09-04 10:56:44 | `10b0dfecb0cb` |
| [273](pr-0273.md) | ci(desktop): build the desktop shell on four runners and attach it to the release | @FiredMosquito831 | 2026-09-04 11:30:58 | `62a5ac9c9836` |
| [274](pr-0274.md) | ci(desktop): let a shell-release dispatch build a different ref than it uploads to | @FiredMosquito831 | 2026-09-04 11:55:17 | `28550dd9f9e7` |
| [275](pr-0275.md) | test(desktop): make the smoke actually wait for the shell to run the stub | @FiredMosquito831 | 2026-09-04 12:12:14 | `846fb9599233` |
| [276](pr-0276.md) | ci(desktop): make SHA256SUMS-desktop-shell.txt one format on all four runners | @FiredMosquito831 | 2026-09-04 12:38:07 | `8371ad934e46` |
| [277](pr-0277.md) | Desktop shell: pin, fetch and verify (Path A) | @FiredMosquito831 | 2026-09-04 13:50:35 | `316d99d7fbc2` |
| [278](pr-0278.md) | Desktop shell: Windows installer (Path B) | @FiredMosquito831 | 2026-09-04 14:45:59 | `327ac79d0095` |
| [279](pr-0279.md) | Desktop shell: Linux .deb + tarball installer | @FiredMosquito831 | 2026-09-04 15:38:15 | `b1c3896807e1` |
| [280](pr-0280.md) | Desktop shell: unsigned macOS .dmg (S9) | @FiredMosquito831 | 2026-09-04 16:22:15 | `53215c69c9a6` |
| [281](pr-0281.md) | Desktop shell: the macOS dmg smoke exits on its own snapshot | @FiredMosquito831 | 2026-09-04 16:38:23 | `ca1ddd45d739` |
| [282](pr-0282.md) | Desktop shell: the dmg smoke's image-root check sorted the wrong way | @FiredMosquito831 | 2026-09-04 16:51:54 | `3122cff8fb8c` |
| [283](pr-0283.md) | Desktop shell: winget manifest, and repin path A to 6.45.2 (S8) | @FiredMosquito831 | 2026-09-04 17:48:34 | `3dbe223b3f5a` |
| [284](pr-0284.md) | docs: bring every user-facing surface up to 6.45.3 (6.45.4) | @FiredMosquito831 | 2026-09-04 20:51:49 | `805a57a9774e` |
