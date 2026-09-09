//! The lifecycle controller: one state, one tick, one pure `step`.
//!
//! Everything this window does about the server is decided here, in a function
//! with no I/O in it. The seven `wait_for_*` loops this replaces --
//! `wait_for_retry`, `wait_for_start`, `wait_for_retry_or_health`,
//! `wait_for_drain`, `watch_health`, and the once-per-episode `respawned` flag
//! that governed the only spawn any of them could make -- each owned a piece of
//! the timing, and between them there were six ways to reach a page with no
//! loop behind it. The audit's rule is the design:
//!
//! > **every state has an outgoing edge on a tick.**
//!
//! That is a property test in this module, not a convention.
//!
//! ## The user's rule (decision Q4, 2026-09-08)
//!
//! Probe every ten seconds, forever. A server that is dead -- no health, no
//! live child, no helper installing -- is **force-started on that tick**. There
//! is no attempt cap, no exponential backoff beyond the tick, and no state that
//! parks on a button. The page says *last checked N s ago, next start attempt
//! in M s*, and it means it.
//!
//! ## Two clocks
//!
//! The loop paints on a fast tick ([`PAINT_TICK`], a property of this binary --
//! how often a countdown redraws is not something an operator configures) and
//! probes on the document's `tick_seconds` (ten). `Observation::fresh` says
//! which kind of tick this is, and **only a fresh tick may spawn**, which is
//! what bounds the window to one start attempt per probe interval however often
//! the page is repainted.
//!
//! ## What is deliberately not here
//!
//! No process is started, no socket is opened and no file is read in this
//! module. `step` is handed an [`Observation`] and returns the next [`State`]
//! and the effects the caller must apply. C1 still holds too: every number and
//! URL in an observation came from Python's status document.

use crate::install;
use crate::ui::Page;

/// How often the loop repaints. Compiled in, deliberately: this is the refresh
/// rate of a countdown, not a budget on the user's machine (C9 -- see
/// `status.rs` for the list of what is compiled in and why).
pub const PAINT_TICK_SECONDS: f64 = 1.0;

/// The probe cadence used when the document does not carry `tick_seconds`.
/// The two-release rule means 6.61.0 must work under a 6.60.2 wheel, and this
/// is the value decision Q4 fixed.
pub const DEFAULT_TICK_SECONDS: f64 = 10.0;

/// The shortest gap between two spawns, when the document does not say. Equal
/// to the tick on purpose: Q4 asks for one attempt per tick and no backoff
/// beyond it.
pub const DEFAULT_START_BACKOFF_SECONDS: f64 = 10.0;

/// How long an unidentifiable holder is given before it is called foreign.
/// A holder nobody can name during our own startup is overwhelmingly us.
pub const DEFAULT_FOREIGN_GRACE_SECONDS: f64 = 45.0;

/// How many times MCC may be installed from this window before it stops trying
/// and says so. A property of the binary: there is no status document to read
/// when `mcc-desktop` cannot be run at all.
pub const INSTALL_ATTEMPTS: u32 = 3;

/// How many start attempts the window makes before it stops calling itself
/// "starting" and says what actually happened.
///
/// NOT a cap on starting. Decision Q4 of 2026-09-08 stands in full: the tick
/// force-starts a dead server for ever, with no backoff beyond the tick and
/// no state that parks on a button. This is the threshold at which the page
/// stops being a spinner -- the user's rule of 2026-09-09 00:03, "three
/// attempts, twenty seconds each, and the page must say what happened".
/// The two are answers to different questions and both are kept.
pub const START_ATTEMPTS_BEFORE_THE_TRUTH: u32 = 3;

/// The per-attempt budget used when the document does not carry
/// `start_timeout_seconds`. Python's own default; the shell never invents a
/// number (C9).
pub const DEFAULT_START_TIMEOUT_SECONDS: f64 = 15.0;

/// Further attempts after the first, when the document does not say.
pub const DEFAULT_SERVER_START_RETRIES: u32 = 2;

/// How a server this window started ended.
///
/// Mirrors `process::ChildExit` rather than importing it, so `step` stays a
/// pure function of plain data and the module keeps its no-I/O rule.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChildExit {
    pub code: Option<i32>,
    pub last_lines: String,
}

/// What the health probe said. One probe, one vocabulary, in both languages.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Health {
    /// 2xx from `/health`.
    Healthy,
    /// 503 + `x-mcc-starting: 1` -- the server has the port and is coming up.
    /// Shipped server-side in 6.59.0; consumed here.
    Starting,
    /// 503 + `x-mcc-shutdown: 1` -- the server has the port and is going away.
    Draining,
    /// Nothing answered, or something answered that is not a server state.
    Absent,
}

/// Who holds the port, decided by **process identity** and never by a bind
/// test (BUG-5). Python answers this in `classify_port_holder`; the shell only
/// branches on the answer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Holder {
    /// Nothing is listening.
    Absent,
    /// MCC's own server, answering.
    OursHealthy,
    /// MCC's own server, mid-startup.
    OursStarting,
    /// MCC's own server, mid-drain.
    OursDraining,
    /// MCC's own server, holding the socket and answering nothing.
    OursStale,
    /// Someone else, identified as such by their image and command line.
    Foreign,
    /// Not asked yet, or the question could not be answered. Never treated as
    /// foreign: an unknown holder is a reason to look again, not to give up.
    Unknown,
}

impl Holder {
    /// Whether a spawn may proceed against this holder.
    ///
    /// `OursStale` says yes: the server's own `SERVER_PORT_TAKEOVER` (6.59.0)
    /// kills a stale holder from inside `mcc-server` before it binds, so the
    /// shell's force-start is simply "spawn `mcc-server`" and the takeover
    /// happens there. `Unknown` also says yes -- the commonest unknown holder
    /// during a start is our own process, and the grace window below is what
    /// stops that guess ever becoming a permanent one.
    pub fn allows_start(self) -> bool {
        !matches!(self, Self::Foreign)
    }

    /// Whether the "Take port" button may be offered (decision Q1: only for a
    /// holder identified as MCC's own).
    pub fn may_take_port(self) -> bool {
        matches!(
            self,
            Self::OursStale | Self::OursStarting | Self::OursDraining
        )
    }
}

/// What the update helper is doing. Read from `progress.json`'s `helper_pid`
/// and stage (6.58.3), never from a stage name alone -- a helper killed
/// mid-install leaves `installing` behind forever.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Helper {
    /// No helper has run, or the last one's record is old news.
    None,
    /// A helper process is alive right now. Nothing else may install.
    Alive { stage: Option<String> },
    /// The helper wrote a terminal stage (`done`, `failed`, `recovered`,
    /// `install-failed`) and is gone. This is the fact `RestartPending` waits
    /// for, and the whole of the reported bug: nothing acted on it before.
    Finished { stage: Option<String> },
}

/// Whether `mcc-desktop --print-status` could be run at all.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StatusHealth {
    /// A document was read this session (possibly on an earlier tick).
    Ok,
    /// `mcc-desktop` is not on PATH.
    NotInstalled,
    /// It ran and failed, or printed something unusable.
    Unreadable { detail: String },
}

/// Why the window is blocked. Nothing enters `Blocked` that a person does not
/// have to resolve -- and even `Blocked` re-evaluates on every tick, so the
/// window heals itself the moment the cause goes away.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Blocked {
    /// A genuinely foreign process has held the port past the grace window.
    ForeignPort,
    /// `server_mode` is not `spawn`: starting the server is somebody else's
    /// job and this window must not do it.
    NotOurServer { server_mode: String },
    /// The status document could not be read, or is a schema this build does
    /// not know.
    Status { detail: String },
    /// Installing MCC has been tried its three times and did not take.
    ///
    /// It carries its own `attempts`. Until 6.66.0 the count lived only in
    /// `State::Installing`, and `attempts_of` answered `0` for every other
    /// state -- so the very next PAINT tick rewrote `Blocked` back to
    /// `Installing { attempts: 0 }` and the installer ran again, for ever.
    /// Measured on the real 6.63.0 binary: ten installs in ninety-four
    /// seconds, under a page that said "attempt 10 of 3".
    Install { detail: String, attempts: u32 },
}

/// The lifecycle state. Eight from the audit (§5.1), plus `RestartPending` --
/// see its own documentation for why it had to be a state rather than a flag.
#[derive(Debug, Clone, PartialEq)]
pub enum State {
    /// Before the first observation. Exactly one tick long.
    Booting,
    /// The dashboard is loaded and the server is answering.
    Attached,
    /// A server is coming up: we spawned it, or it is answering the startup
    /// gate, or a child of ours is still alive.
    Starting { since: f64, attempts: u32 },
    /// The server was answering and stopped. Identical machinery to `Starting`
    /// -- it differs only in what the page says, because "it went away" and
    /// "it has not arrived yet" are different sentences to a user.
    Reconnecting { since: f64 },
    /// MCC's own server is refusing everything while it finishes stopping.
    Draining { since: f64 },
    /// An update helper is installing right now. The one state in which this
    /// window starts nothing: two installers into one tool directory is how
    /// the 2026-09-07 update lost all five of its attempts.
    Updating { since: f64 },
    /// The helper is **gone** and the server is **not** answering.
    ///
    /// This is the user's bug, as a state: "after Update-and-restart the app
    /// stops the server then only watches; F5 fixes it". F5 fixed it because a
    /// reload re-ran the ladder, and the ladder was the only code that could
    /// spawn. Now the tick spawns, so no path depends on a reload -- and this
    /// state exists so that the transition is a named, tested edge rather than
    /// a fall-through.
    RestartPending { since: f64 },
    /// MCC itself is missing and this window is installing it.
    Installing { attempts: u32 },
    /// An install has finished and this window is asking, again, whether MCC
    /// is there now.
    ///
    /// A state rather than a flag because it is the edge that was missing:
    /// the `NotInstalled` arm returned `vec![Effect::Install]` and never a
    /// `Restatus`, and the loop's own opportunistic re-read was gated on
    /// `status_health == Ok`, so after an install nothing ever asked whether
    /// the install had worked. A non-zero installer exit is evidence for the
    /// page, never the verdict -- `install.ps1` threw *after* a complete and
    /// correct install for two releases (6.64.0 fixed that half).
    Verifying { attempts: u32 },
    /// Something a person has to resolve. Still re-checked every tick.
    Blocked { reason: Blocked },
}

impl State {
    /// A short name, for logs and for the tray line.
    pub fn name(&self) -> &'static str {
        match self {
            Self::Booting => "booting",
            Self::Attached => "attached",
            Self::Starting { .. } => "starting",
            Self::Reconnecting { .. } => "reconnecting",
            Self::Draining { .. } => "draining",
            Self::Updating { .. } => "updating",
            Self::RestartPending { .. } => "restart-pending",
            Self::Installing { .. } => "installing",
            Self::Verifying { .. } => "verifying",
            Self::Blocked { .. } => "blocked",
        }
    }
}

/// The numbers and strings an observation carries from the status document.
/// Every one of them is Python's answer, used verbatim (C1).
#[derive(Debug, Clone)]
pub struct Facts {
    pub admin_url: String,
    pub health_url: String,
    pub server_log: String,
    /// Where this window writes its own installer and server-start
    /// transcripts. The one path derived here rather than read verbatim, and
    /// it is still derived from `config_dir`, which Python supplies (C1).
    /// Empty until a status document has named a directory.
    pub shell_log: String,
    pub port: u32,
    pub server_mode: String,
    pub tick_seconds: f64,
    pub start_backoff_seconds: f64,
    pub foreign_grace_seconds: f64,
    /// How long one start attempt is given. `DESKTOP_SERVER_START_TIMEOUT`,
    /// required by the parser since 6.61.0 and read by nothing until now.
    pub start_timeout_seconds: f64,
    /// Further attempts after the first: `DESKTOP_SERVER_START_RETRIES`.
    /// One plus this is the user's "three attempts", and it comes from the
    /// document rather than from a new setting nobody asked for (C9).
    pub server_start_retries: u32,
    /// The holder's image name and pid, for the port-conflict page. Python's
    /// words, not a guess assembled here.
    pub holder_image: Option<String>,
    pub holder_pid: Option<i64>,
}

impl Default for Facts {
    fn default() -> Self {
        Self {
            admin_url: String::new(),
            health_url: String::new(),
            server_log: String::new(),
            shell_log: String::new(),
            port: 0,
            server_mode: "spawn".to_owned(),
            tick_seconds: DEFAULT_TICK_SECONDS,
            start_backoff_seconds: DEFAULT_START_BACKOFF_SECONDS,
            foreign_grace_seconds: DEFAULT_FOREIGN_GRACE_SECONDS,
            start_timeout_seconds: DEFAULT_START_TIMEOUT_SECONDS,
            server_start_retries: DEFAULT_SERVER_START_RETRIES,
            holder_image: None,
            holder_pid: None,
        }
    }
}

/// One tick's worth of facts about the world.
#[derive(Debug, Clone)]
pub struct Observation {
    /// Whether this tick took a fresh health probe. Only a fresh tick may
    /// spawn; a paint tick only redraws the countdown.
    pub fresh: bool,
    pub health: Health,
    pub holder: Holder,
    /// How long the current holder classification has been unbroken. A
    /// `Foreign` holder becomes a reason to stop only past the grace window.
    pub holder_age: f64,
    pub helper: Helper,
    pub status: StatusHealth,
    /// Whether a child this window started is still running. The one signal
    /// that separates "slow" from "gone" while the port is still free.
    pub child_alive: bool,
    /// Seconds since the last spawn from this window, or `None` if it has not
    /// spawned in this session.
    pub since_last_start: Option<f64>,
    /// Seconds since the last health probe -- what "last checked N s ago"
    /// reports, and it is a real measurement rather than the constant zero
    /// BUG-6 named.
    pub since_probe: f64,
    /// Whether a replacement for this binary is staged and this launch has not
    /// asked for it yet.
    pub shell_stale: bool,
    /// How the last server this window started ended, and its last words.
    /// `None` while one is running, or before any has been started. This is
    /// the fact the window did not have: a child that exited is a different
    /// observation from a port that is merely quiet.
    pub last_child_exit: Option<ChildExit>,
    /// How many servers this window has started since it last saw a healthy
    /// one. Reset by health, never by a repaint.
    pub start_attempts: u32,
    /// Seconds since the first of those, or `None` before the first.
    pub since_first_start: Option<f64>,
    /// The installer's last meaningful line, for the page that gives up.
    pub last_install_line: String,
    /// Where this session's last installer transcript went. Separate from
    /// `facts.shell_log` because it is known in exactly the state where no
    /// status document has ever been read and `facts` is therefore empty --
    /// which is the state the page that gives up is shown in.
    pub install_log: String,
    pub facts: Facts,
}

impl Observation {
    /// Seconds until the next start attempt: what the page promises.
    pub fn next_start_in(&self) -> f64 {
        let backoff = self.facts.start_backoff_seconds.max(1.0);
        let waited = self.since_last_start.unwrap_or(backoff);
        (backoff - waited).max(0.0)
    }
}

/// What the caller must do about a step. At most one of these *acts*; `Show`
/// paints, and a paint accompanies almost every tick.
#[derive(Debug, Clone, PartialEq)]
pub enum Effect {
    /// Render one of the shell's own pages.
    Show(Page),
    /// Navigate to the dashboard.
    Attach { admin_url: String },
    /// Start `mcc-server`. The takeover of any stale holder happens inside it.
    Spawn,
    /// Run `mcc-desktop --print-status` -- deliberately *not* on the tick path.
    Restatus,
    /// Run the install script for this machine.
    Install,
    /// Raise the window, once, because a restarted server just answered
    /// (decision Q3).
    RaiseOnce,
    /// Ask Python to stage the pinned shell.
    EnsureShell,
}

impl Effect {
    /// Whether this effect reaches outside the window.
    ///
    /// The invariant the audit asked for -- **at most one side effect per
    /// tick** -- is asserted over this predicate, so where the line falls
    /// matters. It falls between "this window changed what it is showing" and
    /// "this window started a process or made a request": `Show`, `Attach` and
    /// `RaiseOnce` all only move pixels, and the audit's own wording puts them
    /// together ("paint / navigate / spawn / nothing"). What is counted is the
    /// expensive, racy half -- a spawn, a `--print-status`, an installer, a
    /// download -- and there is never more than one of those on a tick.
    pub fn acts(&self) -> bool {
        matches!(
            self,
            Self::Spawn | Self::Restatus | Self::Install | Self::EnsureShell
        )
    }
}

/// Whether a start may be made right now. The governor from §5.1, with Q4's
/// amendment: no attempt cap, and the backoff is the tick.
pub fn may_start(observation: &Observation) -> bool {
    if !observation.fresh {
        return false;
    }
    if observation.health != Health::Absent {
        return false;
    }
    if observation.child_alive {
        return false;
    }
    if matches!(observation.helper, Helper::Alive { .. }) {
        return false;
    }
    if !observation.holder.allows_start() {
        return false;
    }
    if observation.facts.server_mode != "spawn" {
        return false;
    }
    match observation.since_last_start {
        None => true,
        Some(waited) => waited >= observation.facts.start_backoff_seconds.max(1.0),
    }
}

/// Whether a genuinely foreign holder has been there long enough to say so.
fn foreign_confirmed(observation: &Observation) -> bool {
    observation.holder == Holder::Foreign
        && observation.holder_age >= observation.facts.foreign_grace_seconds.max(0.0)
}

/// Whether this tick should spend a `--print-status`.
///
/// Never on the healthy path: an attached window pays for one cheap `/health`
/// probe per tick and nothing else, which is BUG-4's fix. The document is
/// re-read when the shell needs holder or helper facts it does not have -- on
/// the first tick, and on any fresh tick where the server is not answering.
fn needs_restatus(state: &State, observation: &Observation) -> bool {
    if !observation.fresh {
        return false;
    }
    match state {
        State::Booting => true,
        State::Attached => false,
        _ => observation.health != Health::Healthy,
    }
}

/// The whole state machine. Pure, total, and every arm has an outgoing edge.
///
/// `now` is monotonic seconds; it is only ever used to stamp a `since`, so the
/// epoch does not matter as long as it is the same one across ticks.
pub fn step(state: &State, observation: &Observation, now: f64) -> (State, Vec<Effect>) {
    let mut effects: Vec<Effect> = Vec::new();

    // The shell pin, once, from anywhere. It is not a lifecycle transition and
    // never changes the state -- see `ensure_shell_if_stale` for the argument.
    if observation.shell_stale {
        effects.push(Effect::EnsureShell);
        return (state.clone(), effects);
    }

    // -- pre-emptions, in the order the audit ranks them -------------------

    // 1. MCC itself is missing. Nothing about the server can be decided until
    //    there is something to ask.
    if observation.status == StatusHealth::NotInstalled {
        if let Helper::Alive { stage } = &observation.helper {
            return (
                State::Updating {
                    since: since(state, now),
                },
                vec![Effect::Show(updating_page(stage.as_deref(), observation))],
            );
        }
        let attempts = install_attempts_of(state);
        // A paint tick must not rewrite the state it was given. This one
        // line is the whole of the 6.63.0 install loop: `attempts_of`
        // answered 0 for `Blocked`, the paint tick one second after the
        // bound was reached wrote `Installing { attempts: 0 }` back, and the
        // next fresh tick installed again.
        if !observation.fresh {
            return (state.clone(), effects);
        }
        if matches!(
            state,
            State::Blocked {
                reason: Blocked::Install { .. }
            }
        ) {
            // Already given up. Still re-checked every tick -- the page goes
            // away by itself if MCC appears -- but never installed again.
            return (
                state.clone(),
                vec![
                    Effect::Restatus,
                    Effect::Show(install_did_not_take_page(observation, attempts)),
                ],
            );
        }
        // An install ran and has not been re-verified yet. Ask the
        // filesystem, on this tick, before spending another attempt.
        if let State::Installing { attempts } = state {
            return (
                State::Verifying {
                    attempts: *attempts,
                },
                vec![
                    Effect::Restatus,
                    Effect::Show(installing_page(observation, *attempts)),
                ],
            );
        }
        if attempts >= INSTALL_ATTEMPTS {
            return (
                State::Blocked {
                    reason: Blocked::Install {
                        detail: install::install_did_not_take_message(
                            attempts,
                            &observation.last_install_line,
                            named(&observation.install_log).as_deref(),
                        ),
                        attempts,
                    },
                },
                vec![Effect::Show(install_did_not_take_page(
                    observation,
                    attempts,
                ))],
            );
        }
        return (
            State::Installing {
                attempts: attempts + 1,
            },
            vec![Effect::Install],
        );
    }

    // 2. A helper is installing. One installer at a time, always.
    //
    // This arm returns without asking for a `Restatus`, which is safe only
    // because the caller refreshes the helper facts itself on every tick
    // (`Lifecycle::refresh_helper`). It did not, once, and the window sat on
    // this page for the rest of its life -- the observation that said "a
    // helper is running" was the same observation that stopped anything
    // asking again.
    if let Helper::Alive { stage } = &observation.helper {
        return (
            State::Updating {
                since: since(state, now),
            },
            vec![Effect::Show(updating_page(stage.as_deref(), observation))],
        );
    }

    // 3. A healthy server ends every argument. Deliberately checked before the
    //    unreadable-status branch: a window that can see the dashboard must
    //    not be taken away from it because a subprocess failed.
    if observation.health == Health::Healthy {
        let mut effects = Vec::new();
        let attaching = !matches!(state, State::Attached);
        if attaching {
            effects.push(Effect::Attach {
                admin_url: observation.facts.admin_url.clone(),
            });
            // Q3: the window comes forward once, when a restarted server first
            // answers. Never on the ordinary healthy tick, and never twice --
            // the caller holds the "once".
            if matches!(
                state,
                State::Reconnecting { .. }
                    | State::RestartPending { .. }
                    | State::Updating { .. }
                    | State::Draining { .. }
            ) {
                effects.push(Effect::RaiseOnce);
            }
        }
        return (State::Attached, effects);
    }

    // 4. The status document could not be read. Not a dead end: the health
    //    probe still runs every tick, and the moment the server answers the
    //    branch above takes over.
    if let StatusHealth::Unreadable { detail } = &observation.status {
        return (
            State::Blocked {
                reason: Blocked::Status {
                    detail: detail.clone(),
                },
            },
            vec![Effect::Show(Page::Error {
                message: format!(
                    "{detail} This window re-checks every {:.0} seconds and picks the \
                     dashboard up by itself when the server answers.",
                    observation.facts.tick_seconds
                ),
                // D6-Q10: every error page names the two paths it has. The
                // field has existed since 6.61.0 and every construction site
                // passed `None`.
                server_log: named(&observation.facts.server_log),
                shell_log: named(&observation.facts.shell_log),
            })],
        );
    }

    // -- the ordinary world: not healthy, nothing installing ---------------

    if needs_restatus(state, observation) {
        effects.push(Effect::Restatus);
    }

    match observation.health {
        Health::Healthy => unreachable!("handled above"),
        Health::Draining => {
            effects.push(Effect::Show(draining_page(observation)));
            (
                State::Draining {
                    since: since(state, now),
                },
                effects,
            )
        }
        Health::Starting => {
            let attempts = attempts_of(state);
            effects.push(Effect::Show(starting_page(
                observation,
                "The server is starting",
            )));
            (
                State::Starting {
                    since: since(state, now),
                    attempts,
                },
                effects,
            )
        }
        Health::Absent => step_absent(state, observation, now, effects),
    }
}

/// The interesting half: nothing is answering. This is where Q4 lives.
fn step_absent(
    state: &State,
    observation: &Observation,
    now: f64,
    mut effects: Vec<Effect>,
) -> (State, Vec<Effect>) {
    // A genuinely foreign holder, past its grace. The only thing here that
    // stops a start -- and it still re-checks every tick.
    if foreign_confirmed(observation) {
        effects.push(Effect::Show(port_conflict_page(observation)));
        return (
            State::Blocked {
                reason: Blocked::ForeignPort,
            },
            effects,
        );
    }

    // Not this window's server to start.
    if observation.facts.server_mode != "spawn" {
        effects.push(Effect::Show(Page::NotOurServer {
            message: format!(
                "The server is not running. Server mode is {}, so this window will not \
                 start one; run mcc-server yourself, or switch to spawn in the dashboard. \
                 Re-checking every {:.0} seconds.",
                observation.facts.server_mode, observation.facts.tick_seconds
            ),
        }));
        return (
            State::Blocked {
                reason: Blocked::NotOurServer {
                    server_mode: observation.facts.server_mode.clone(),
                },
            },
            effects,
        );
    }

    // The post-update path, named. The helper is gone, it wrote its terminal
    // stage, and nothing is answering -- so this tick spawns. No reload, no
    // button, no other path involved.
    let post_update = matches!(state, State::Updating { .. } | State::RestartPending { .. })
        || matches!(observation.helper, Helper::Finished { .. });

    if may_start(observation) {
        effects.retain(|effect| !effect.acts());
        effects.push(Effect::Spawn);
        // The budget changes what the page SAYS and nothing else: the spawn
        // above is unconditional, exactly as decision Q4 of 2026-09-08
        // requires. A window that has spent its budget is still starting a
        // server every tick, for ever -- it has merely stopped pretending
        // that nothing has gone wrong.
        effects.push(Effect::Show(if start_budget_spent(observation) {
            server_failed_page(observation)
        } else {
            starting_page(
                observation,
                if post_update {
                    "The update finished, so this window is starting the server"
                } else {
                    "Starting the My Claude Code server"
                },
            )
        }));
        return (
            State::Starting {
                since: now,
                attempts: attempts_of(state).saturating_add(1),
            },
            effects,
        );
    }

    // Cannot start yet: a child is still coming up, or the backoff has not
    // elapsed, or this is a paint tick. Say which, and keep the countdown
    // honest.
    if post_update && observation.since_last_start.is_none() {
        effects.push(Effect::Show(starting_page(
            observation,
            "The update finished and this window is starting the server",
        )));
        return (
            State::RestartPending {
                since: since(state, now),
            },
            effects,
        );
    }

    let was_attached = matches!(state, State::Attached | State::Reconnecting { .. });
    if was_attached && !start_budget_spent(observation) {
        effects.push(Effect::Show(reconnecting_page(observation)));
        return (
            State::Reconnecting {
                since: since(state, now),
            },
            effects,
        );
    }

    effects.push(Effect::Show(if start_budget_spent(observation) {
        server_failed_page(observation)
    } else {
        starting_page(observation, "Starting the My Claude Code server")
    }));
    (
        State::Starting {
            since: since(state, now),
            attempts: attempts_of(state),
        },
        effects,
    )
}

/// Keep the `since` of a state we are already in, and stamp `now` otherwise.
fn since(state: &State, now: f64) -> f64 {
    match state {
        State::Starting { since, .. }
        | State::Reconnecting { since }
        | State::Draining { since }
        | State::Updating { since }
        | State::RestartPending { since } => *since,
        _ => now,
    }
}

fn attempts_of(state: &State) -> u32 {
    match state {
        State::Starting { attempts, .. } => *attempts,
        _ => 0,
    }
}

/// How many installs this window has run, from whichever state holds it.
///
/// The counterpart of `attempts_of`, and the reason it is a function of its
/// own: the install count has to survive `Blocked`, or the bound does not
/// bound. See `Blocked::Install`.
fn install_attempts_of(state: &State) -> u32 {
    match state {
        State::Installing { attempts } | State::Verifying { attempts } => *attempts,
        State::Blocked {
            reason: Blocked::Install { attempts, .. },
        } => *attempts,
        _ => 0,
    }
}

/// A path, when there is one to name.
fn named(path: &str) -> Option<String> {
    let trimmed = path.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_owned())
    }
}

/// The page shown while an install is being re-verified.
fn installing_page(observation: &Observation, attempts: u32) -> Page {
    Page::Installing {
        command: install::install_command_for_this_machine().display,
        message: format!(
            "My Claude Code was installed from this window (attempt {attempts} of \
             {INSTALL_ATTEMPTS}); checking whether it took ({}).",
            cadence_tail(observation)
        ),
    }
}

/// The page that says the installs did not take, and why.
///
/// D6-Q4: `install::install_did_not_take_message` has been written,
/// unit-tested and called by nothing since 6.58.1, and `LAST_INSTALL_LINE` has
/// been written and read by nothing since 6.61.0. What did show was a
/// two-second flash before the next install overwrote it.
fn install_did_not_take_page(observation: &Observation, attempts: u32) -> Page {
    let mut message = install::install_did_not_take_message(
        attempts,
        &observation.last_install_line,
        named(&observation.install_log).as_deref(),
    );
    message.push_str(&format!(
        " This window re-checks every {:.0} seconds and picks MCC up by itself \
         if it appears; Retry checks again now.",
        observation.facts.tick_seconds
    ));
    Page::Error {
        message,
        server_log: named(&observation.facts.server_log),
        shell_log: named(&observation.install_log).or_else(|| named(&observation.facts.shell_log)),
    }
}

/// The whole start budget, in one place: three attempts, or the document's own
/// `start_timeout_seconds * (server_start_retries + 1)`.
///
/// Both halves, because they fail differently: a server that exits in one
/// second spends three attempts in thirty, and a server that hangs spends none
/// at all. `false` while nothing has been started yet.
pub fn start_budget_spent(observation: &Observation) -> bool {
    if observation.start_attempts >= START_ATTEMPTS_BEFORE_THE_TRUTH {
        return true;
    }
    let per_attempt = observation.facts.start_timeout_seconds.max(1.0);
    let budget = per_attempt * f64::from(observation.facts.server_start_retries + 1);
    observation
        .since_first_start
        .is_some_and(|elapsed| elapsed >= budget)
}

/// The page that stops being a spinner.
///
/// Everything the window knew and never said: how many servers it has started,
/// how the last one ended, its last words, and the two log paths. Plus the same
/// countdown every other page carries, because Q4 is not weakened -- the next
/// tick still starts another one.
fn server_failed_page(observation: &Observation) -> Page {
    let attempts = observation.start_attempts;
    let mut detail = String::new();
    match observation.last_child_exit.as_ref() {
        Some(exit) => {
            let code = exit
                .code
                .map_or_else(|| "an unknown status".to_owned(), |value| value.to_string());
            detail.push_str(&format!("mcc-server exited with {code}."));
            let words = exit.last_lines.trim();
            if !words.is_empty() {
                detail.push_str("\n\n");
                detail.push_str(words);
            }
        }
        None => detail.push_str(
            "mcc-server was started and has not answered. It did not exit, so it \
             is still coming up or it is stuck.",
        ),
    }
    Page::ServerFailed {
        message: format!(
            "The server has been started {attempts} time{} from this window and has \
             not answered on port {}. This window keeps trying ({}).",
            if attempts == 1 { "" } else { "s" },
            observation.facts.port,
            cadence_tail(observation),
        ),
        detail,
        server_log: named(&observation.facts.server_log),
        shell_log: named(&observation.facts.shell_log),
    }
}

// -- the pages, built from the state and nothing else ------------------------

/// The sentence the audit asked for, verbatim: never a dead end, always a
/// countdown, and the elapsed figure is measured rather than the constant zero
/// BUG-6 found.
fn cadence_tail(observation: &Observation) -> String {
    format!(
        "last checked {} ago, next start attempt in {}",
        seconds(observation.since_probe),
        seconds(observation.next_start_in()),
    )
}

fn seconds(value: f64) -> String {
    format!("{:.0} s", value.max(0.0))
}

fn starting_page(observation: &Observation, lead: &str) -> Page {
    Page::Starting {
        message: format!("{lead}... ({})", cadence_tail(observation)),
    }
}

fn reconnecting_page(observation: &Observation) -> Page {
    Page::Reconnecting {
        message: format!(
            "Reconnecting... The server stopped answering ({}). It is started \
             again automatically -- nothing here needs clicking.",
            cadence_tail(observation)
        ),
    }
}

fn draining_page(observation: &Observation) -> Page {
    Page::Reconnecting {
        message: format!(
            "The My Claude Code server is shutting down and refuses new requests \
             until it has finished. This window starts it again as soon as the \
             port is free ({}).",
            cadence_tail(observation)
        ),
    }
}

fn updating_page(stage: Option<&str>, observation: &Observation) -> Page {
    let named = stage.map(str::trim).filter(|value| !value.is_empty());
    let detail = named.map_or_else(String::new, |stage| format!(" ({stage})"));
    Page::Updating {
        message: format!(
            "Updating My Claude Code{detail}: the installer is running. This window \
             waits for it rather than starting a second one, and starts the server \
             itself the moment it finishes -- last checked {} ago.",
            seconds(observation.since_probe)
        ),
    }
}

fn port_conflict_page(observation: &Observation) -> Page {
    let holder = match (
        observation.facts.holder_image.as_deref(),
        observation.facts.holder_pid,
    ) {
        (Some(image), Some(pid)) => format!("{image} (pid {pid})"),
        (Some(image), None) => image.to_owned(),
        (None, Some(pid)) => format!("pid {pid}"),
        (None, None) => "another program".to_owned(),
    };
    Page::PortConflict {
        message: format!(
            "Port {} is held by {holder}, which is not My Claude Code. Stop it, or \
             change the port in the dashboard. This window re-checks every {:.0} \
             seconds and starts the server itself the moment the port is free.",
            observation.facts.port, observation.facts.tick_seconds
        ),
        // Decision Q1: offered only for a holder identified as MCC's own, and
        // a confirmed-foreign holder is by definition not one.
        take_port: observation.holder.may_take_port(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn observation(health: Health) -> Observation {
        Observation {
            last_child_exit: None,
            start_attempts: 0,
            since_first_start: None,
            last_install_line: String::new(),
            install_log: String::new(),
            fresh: true,
            health,
            holder: Holder::Absent,
            holder_age: 0.0,
            helper: Helper::None,
            status: StatusHealth::Ok,
            child_alive: false,
            since_last_start: None,
            since_probe: 0.0,
            shell_stale: false,
            facts: Facts {
                admin_url: "http://127.0.0.1:9999/admin".to_owned(),
                health_url: "http://127.0.0.1:9999/health".to_owned(),
                server_log: "/logs/server.log".to_owned(),
                port: 9999,
                ..Facts::default()
            },
        }
    }

    /// Every state, for the exhaustive sweeps below.
    fn all_states() -> Vec<State> {
        vec![
            State::Booting,
            State::Attached,
            State::Starting {
                since: 0.0,
                attempts: 1,
            },
            State::Reconnecting { since: 0.0 },
            State::Draining { since: 0.0 },
            State::Updating { since: 0.0 },
            State::RestartPending { since: 0.0 },
            State::Installing { attempts: 1 },
            State::Blocked {
                reason: Blocked::ForeignPort,
            },
            State::Blocked {
                reason: Blocked::NotOurServer {
                    server_mode: "external".to_owned(),
                },
            },
            State::Blocked {
                reason: Blocked::Status {
                    detail: "boom".to_owned(),
                },
            },
            State::Blocked {
                reason: Blocked::Install {
                    detail: "boom".to_owned(),
                    attempts: INSTALL_ATTEMPTS,
                },
            },
        ]
    }

    /// Every observation class the audit's contract table names, plus the ones
    /// Q4 added.
    fn all_observations() -> Vec<(&'static str, Observation)> {
        let mut cases: Vec<(&'static str, Observation)> = vec![
            ("healthy", observation(Health::Healthy)),
            ("absent", observation(Health::Absent)),
            ("ours-starting", observation(Health::Starting)),
            ("ours-draining", observation(Health::Draining)),
        ];

        let mut foreign = observation(Health::Absent);
        foreign.holder = Holder::Foreign;
        foreign.holder_age = 600.0;
        cases.push(("foreign-confirmed", foreign));

        let mut fresh_foreign = observation(Health::Absent);
        fresh_foreign.holder = Holder::Foreign;
        fresh_foreign.holder_age = 1.0;
        cases.push(("foreign-unconfirmed", fresh_foreign));

        let mut stale = observation(Health::Absent);
        stale.holder = Holder::OursStale;
        cases.push(("ours-stale", stale));

        let mut helper = observation(Health::Absent);
        helper.helper = Helper::Alive {
            stage: Some("installing".to_owned()),
        };
        cases.push(("helper-alive", helper));

        let mut done = observation(Health::Absent);
        done.helper = Helper::Finished {
            stage: Some("done".to_owned()),
        };
        cases.push(("helper-finished", done));

        let mut unreadable = observation(Health::Absent);
        unreadable.status = StatusHealth::Unreadable {
            detail: "exit 1".to_owned(),
        };
        cases.push(("status-unreadable", unreadable));

        let mut missing = observation(Health::Absent);
        missing.status = StatusHealth::NotInstalled;
        cases.push(("not-installed", missing));

        let mut external = observation(Health::Absent);
        external.facts.server_mode = "external".to_owned();
        cases.push(("not-our-server", external));

        let mut child = observation(Health::Absent);
        child.child_alive = true;
        cases.push(("child-alive", child));

        let mut backoff = observation(Health::Absent);
        backoff.since_last_start = Some(2.0);
        cases.push(("inside-backoff", backoff));

        let mut paint = observation(Health::Absent);
        paint.fresh = false;
        paint.since_probe = 3.0;
        cases.push(("paint-tick", paint));

        cases
    }

    // -- the property the whole redesign exists for -----------------------

    #[test]
    fn every_state_has_an_outgoing_edge_on_every_observation() {
        // BUG-1 generalised. It is not enough that `step` returns: it must
        // return a state from which the *next* tick also returns, and it must
        // never sit in a state that only a button can leave. Two ticks of
        // every (state, observation) pair, and the assertion is that the loop
        // is still alive at the end of both.
        for state in all_states() {
            for (name, observation) in all_observations() {
                let (next, effects) = step(&state, &observation, 100.0);
                let (after, _) = step(&next, &observation, 110.0);
                assert!(
                    !matches!(after, State::Booting) || matches!(state, State::Booting),
                    "{} + {name} fell back to Booting",
                    state.name()
                );
                // At most one acting effect: the audit's "one side effect per
                // tick", asserted rather than asserted-in-prose.
                assert!(
                    effects.iter().filter(|effect| effect.acts()).count() <= 1,
                    "{} + {name} asked for more than one side effect: {effects:?}",
                    state.name()
                );
                // And a tick always leaves something for the user to look at
                // or something for the caller to do -- unless nothing changed
                // at all, which is only ever "the dashboard is up and the
                // server answered again". A tick that produced neither *and*
                // moved would be the frozen window all over again.
                assert!(
                    !effects.is_empty() || next == state,
                    "{} + {name} moved to {} and produced nothing at all",
                    state.name(),
                    next.name()
                );
            }
        }
    }

    #[test]
    fn no_state_is_reachable_that_a_tick_cannot_leave() {
        // The other half: from every state, a healthy observation must reach
        // Attached in one tick. Six terminal states were the defect (BUG-1).
        let healthy = observation(Health::Healthy);
        for state in all_states() {
            let (next, _) = step(&state, &healthy, 1.0);
            assert_eq!(
                next,
                State::Attached,
                "{} could not attach to a healthy server",
                state.name()
            );
        }
    }

    // -- the user's sequence ---------------------------------------------

    #[test]
    fn the_post_update_sequence_spawns_on_the_next_tick() {
        // The exact report: "after Update-and-restart the app stops the server
        // then only watches; F5 fixes it." Update -> helper alive -> helper
        // done -> no health. The next tick must SPAWN, with nothing reloaded.
        let mut helper_alive = observation(Health::Absent);
        helper_alive.helper = Helper::Alive {
            stage: Some("installing".to_owned()),
        };
        let (updating, _) = step(&State::Attached, &helper_alive, 0.0);
        assert!(matches!(updating, State::Updating { .. }));

        let mut helper_done = observation(Health::Absent);
        helper_done.helper = Helper::Finished {
            stage: Some("done".to_owned()),
        };
        let (next, effects) = step(&updating, &helper_done, 10.0);
        assert!(
            effects.contains(&Effect::Spawn),
            "the tick after the helper finished must spawn, not watch: {effects:?}"
        );
        assert!(matches!(next, State::Starting { .. }));
    }

    #[test]
    fn a_failed_helper_that_recovered_the_old_server_is_attached_to() {
        // Scenario (d): the install failed, the helper wrote `recovered`, and
        // the previous version answers. Nothing is spawned; the window
        // attaches and raises once.
        let mut recovered = observation(Health::Healthy);
        recovered.helper = Helper::Finished {
            stage: Some("recovered".to_owned()),
        };
        let (next, effects) = step(&State::Updating { since: 0.0 }, &recovered, 10.0);
        assert_eq!(next, State::Attached);
        assert!(
            effects
                .iter()
                .any(|effect| matches!(effect, Effect::Attach { .. }))
        );
        assert!(effects.contains(&Effect::RaiseOnce));
        assert!(!effects.contains(&Effect::Spawn));
    }

    #[test]
    fn a_dead_server_is_force_started_on_the_tick() {
        // Decision Q4, in one assertion. Attached, then nothing answers, and
        // the very next fresh tick spawns -- no attempt budget, no wall.
        let absent = observation(Health::Absent);
        let (reconnecting, effects) = step(&State::Attached, &absent, 0.0);
        assert!(effects.contains(&Effect::Spawn), "{effects:?}");
        assert!(matches!(reconnecting, State::Starting { .. }));
    }

    #[test]
    fn a_stale_mcc_holder_is_started_over_because_the_server_takes_the_port() {
        // 6.59.0's SERVER_PORT_TAKEOVER kills the stale holder from inside
        // mcc-server, so the shell's force-start is simply "spawn".
        let mut stale = observation(Health::Absent);
        stale.holder = Holder::OursStale;
        let (_, effects) = step(&State::Reconnecting { since: 0.0 }, &stale, 5.0);
        assert!(effects.contains(&Effect::Spawn), "{effects:?}");
    }

    #[test]
    fn a_foreign_holder_inside_the_grace_window_is_not_a_conflict_yet() {
        let mut fresh = observation(Health::Absent);
        fresh.holder = Holder::Foreign;
        fresh.holder_age = 5.0;
        let (state, _) = step(&State::Booting, &fresh, 1.0);
        assert!(!matches!(state, State::Blocked { .. }), "{state:?}");

        let mut confirmed = fresh.clone();
        confirmed.holder_age = 120.0;
        let (state, effects) = step(&State::Booting, &confirmed, 1.0);
        assert_eq!(
            state,
            State::Blocked {
                reason: Blocked::ForeignPort
            }
        );
        assert!(!effects.contains(&Effect::Spawn));
    }

    #[test]
    fn take_port_is_offered_only_for_a_holder_that_is_ours() {
        let mut confirmed = observation(Health::Absent);
        confirmed.holder = Holder::Foreign;
        confirmed.holder_age = 120.0;
        confirmed.facts.holder_image = Some("python.exe".to_owned());
        confirmed.facts.holder_pid = Some(4242);
        let (_, effects) = step(&State::Booting, &confirmed, 1.0);
        let page = effects
            .iter()
            .find_map(|effect| match effect {
                Effect::Show(page) => Some(page.clone()),
                _ => None,
            })
            .expect("a page");
        match page {
            Page::PortConflict { take_port, message } => {
                assert!(!take_port, "a genuinely foreign holder gets no Take port");
                assert!(message.contains("python.exe (pid 4242)"), "{message}");
            }
            other => panic!("expected a port conflict page, got {other:?}"),
        }
        assert!(Holder::OursStale.may_take_port());
        assert!(!Holder::Foreign.may_take_port());
        assert!(!Holder::Absent.may_take_port());
    }

    #[test]
    fn a_blocked_window_heals_itself_when_the_cause_goes_away() {
        let blocked = State::Blocked {
            reason: Blocked::ForeignPort,
        };
        let mut freed = observation(Health::Absent);
        freed.holder = Holder::Absent;
        let (next, effects) = step(&blocked, &freed, 50.0);
        assert!(effects.contains(&Effect::Spawn), "{effects:?}");
        assert!(matches!(next, State::Starting { .. }));
    }

    #[test]
    fn a_paint_tick_never_spawns() {
        // The two clocks. A repaint every second must not turn Q4's one
        // attempt per ten seconds into ten.
        let mut paint = observation(Health::Absent);
        paint.fresh = false;
        paint.since_probe = 4.0;
        let (_, effects) = step(&State::Reconnecting { since: 0.0 }, &paint, 4.0);
        assert!(!effects.contains(&Effect::Spawn));
        assert!(
            effects
                .iter()
                .any(|effect| matches!(effect, Effect::Show(_)))
        );
    }

    #[test]
    fn the_backoff_and_not_the_probe_rate_is_what_bounds_starts() {
        // The loop probes faster while a start is in flight, so that a server
        // that answers two seconds after it was spawned is picked up in two
        // seconds rather than in ten. That must not become ten start attempts
        // in ten seconds, and it does not: `start_backoff_seconds` is the only
        // thing that licenses a spawn, and it is unchanged.
        let mut just_started = observation(Health::Absent);
        just_started.since_last_start = Some(1.0);
        for _ in 0..9 {
            let (_, effects) = step(
                &State::Starting {
                    since: 0.0,
                    attempts: 1,
                },
                &just_started,
                1.0,
            );
            assert!(!effects.contains(&Effect::Spawn), "{effects:?}");
        }
        let mut backoff_elapsed = just_started.clone();
        backoff_elapsed.since_last_start = Some(10.0);
        let (_, effects) = step(
            &State::Starting {
                since: 0.0,
                attempts: 1,
            },
            &backoff_elapsed,
            10.0,
        );
        assert!(effects.contains(&Effect::Spawn), "{effects:?}");
    }

    #[test]
    fn a_start_inside_the_backoff_waits_and_says_how_long() {
        let mut soon = observation(Health::Absent);
        soon.since_last_start = Some(3.0);
        soon.since_probe = 3.0;
        let (_, effects) = step(
            &State::Starting {
                since: 0.0,
                attempts: 1,
            },
            &soon,
            3.0,
        );
        assert!(!effects.contains(&Effect::Spawn));
        let Some(Effect::Show(Page::Starting { message })) = effects
            .iter()
            .find(|effect| matches!(effect, Effect::Show(Page::Starting { .. })))
            .cloned()
        else {
            panic!("expected a starting page, got {effects:?}");
        };
        assert!(message.contains("last checked 3 s ago"), "{message}");
        assert!(message.contains("next start attempt in 7 s"), "{message}");
        assert!(!message.contains("Retry"), "{message}");
    }

    #[test]
    fn a_live_child_is_waited_for_rather_than_spawned_over() {
        let mut child = observation(Health::Absent);
        child.child_alive = true;
        let (_, effects) = step(
            &State::Starting {
                since: 0.0,
                attempts: 1,
            },
            &child,
            20.0,
        );
        assert!(!effects.contains(&Effect::Spawn), "{effects:?}");
    }

    #[test]
    fn a_helper_that_is_alive_stops_every_start() {
        let mut helper = observation(Health::Absent);
        helper.helper = Helper::Alive {
            stage: Some("installing".to_owned()),
        };
        for state in all_states() {
            let (next, effects) = step(&state, &helper, 1.0);
            assert!(
                !effects.contains(&Effect::Spawn),
                "{} spawned",
                state.name()
            );
            assert!(
                !effects.contains(&Effect::Install),
                "{} installed",
                state.name()
            );
            assert!(
                matches!(next, State::Updating { .. }) || matches!(next, State::Blocked { .. })
            );
        }
    }

    #[test]
    fn a_draining_server_is_waited_out_and_then_started() {
        let (draining, effects) = step(&State::Attached, &observation(Health::Draining), 0.0);
        assert!(matches!(draining, State::Draining { .. }));
        assert!(!effects.contains(&Effect::Spawn));
        let (next, effects) = step(&draining, &observation(Health::Absent), 10.0);
        assert!(effects.contains(&Effect::Spawn));
        assert!(matches!(next, State::Starting { .. }));
    }

    #[test]
    fn a_starting_server_is_never_spawned_over() {
        for state in all_states() {
            let (_, effects) = step(&state, &observation(Health::Starting), 1.0);
            assert!(
                !effects.contains(&Effect::Spawn),
                "{} spawned",
                state.name()
            );
        }
    }

    #[test]
    fn a_helper_that_has_finished_releases_the_updating_state_at_once() {
        // The scratch failure of 2026-09-08, as a unit test. `Updating` must
        // be a function of the CURRENT helper fact and nothing else: one tick
        // with a finished helper and no health is a spawn, not another page
        // about an installer that is not running.
        let mut finished = observation(Health::Absent);
        finished.helper = Helper::Finished {
            stage: Some("done".to_owned()),
        };
        let (next, effects) = step(&State::Updating { since: 0.0 }, &finished, 30.0);
        assert!(effects.contains(&Effect::Spawn), "{effects:?}");
        assert!(matches!(next, State::Starting { .. }));
    }

    #[test]
    fn the_status_document_is_never_read_on_the_attached_tick() {
        // BUG-4: `--print-status` cost sat on the critical path of every
        // decision. An attached window pays for one /health probe and nothing
        // else.
        let (_, effects) = step(&State::Attached, &observation(Health::Healthy), 1.0);
        assert!(!effects.contains(&Effect::Restatus), "{effects:?}");
        // And it IS read when the shell needs holder facts.
        let mut absent = observation(Health::Absent);
        absent.since_last_start = Some(1.0);
        let (_, effects) = step(&State::Reconnecting { since: 0.0 }, &absent, 1.0);
        assert!(effects.contains(&Effect::Restatus), "{effects:?}");
    }

    #[test]
    fn a_stale_shell_asks_for_the_pin_and_changes_nothing_else() {
        let mut stale = observation(Health::Healthy);
        stale.shell_stale = true;
        let (next, effects) = step(&State::Attached, &stale, 1.0);
        assert_eq!(next, State::Attached);
        assert_eq!(effects, vec![Effect::EnsureShell]);
    }

    #[test]
    fn missing_mcc_installs_three_times_and_then_says_so_without_parking() {
        let mut missing = observation(Health::Absent);
        missing.status = StatusHealth::NotInstalled;
        let mut state = State::Booting;
        let mut installs = 0;
        // Install, re-verify, install, re-verify... The re-verification is
        // D6-Q2: a non-zero installer exit is evidence for the page and never
        // the verdict, so the only thing that decides whether an install took
        // is asking again (`install.ps1` threw *after* a complete install for
        // two releases).
        for expected in 1..=INSTALL_ATTEMPTS {
            let (next, effects) = step(&state, &missing, f64::from(expected) * 10.0);
            assert!(effects.contains(&Effect::Install), "{effects:?}");
            assert_eq!(next, State::Installing { attempts: expected });
            installs += 1;
            state = next;

            let (next, effects) = step(&state, &missing, f64::from(expected) * 10.0 + 1.0);
            assert!(
                effects.contains(&Effect::Restatus),
                "an install must be re-verified before another is spent: {effects:?}"
            );
            assert!(!effects.contains(&Effect::Install), "{effects:?}");
            assert_eq!(next, State::Verifying { attempts: expected });
            state = next;
        }
        assert_eq!(installs, INSTALL_ATTEMPTS);

        let (next, effects) = step(&state, &missing, 99.0);
        assert!(!effects.contains(&Effect::Install));
        assert!(matches!(
            next,
            State::Blocked {
                reason: Blocked::Install { .. }
            }
        ));
        // The page that stays: it quotes the installer and names the logs.
        let page = effects
            .iter()
            .find_map(|effect| match effect {
                Effect::Show(page) => Some(page.clone()),
                _ => None,
            })
            .expect("a page");
        let json = serde_json::to_string(&page).expect("a page serializes");
        assert!(json.contains("The installer ran 3 times"), "{json}");

        // And it still heals: MCC arrives, the next tick starts a server.
        let (healed, effects) = step(&next, &observation(Health::Absent), 109.0);
        assert!(effects.contains(&Effect::Spawn));
        assert!(matches!(healed, State::Starting { .. }));
    }

    #[test]
    fn a_fourth_install_is_never_asked_for_however_the_ticks_fall() {
        // The property the 6.61.0 bound failed. Measured on the real 6.63.0
        // binary: ten installs in ninety-four seconds under a page that said
        // "attempt 10 of 3". The mechanism was one line -- the `!fresh` branch
        // returned `State::Installing { attempts }` where `attempts` came from
        // `attempts_of`, which answered 0 for every state that was not
        // `Starting`, so the paint tick one second after `Blocked` rewrote it
        // to `Installing { attempts: 0 }`.
        let mut missing = observation(Health::Absent);
        missing.status = StatusHealth::NotInstalled;
        for pattern in 0u32..64 {
            let mut state = State::Booting;
            let mut installs = 0;
            for tick in 0..24 {
                // Every interleaving of fresh and paint ticks in six bits.
                missing.fresh = pattern & (1 << (tick % 6)) != 0;
                let (next, effects) = step(&state, &missing, f64::from(tick));
                installs += effects
                    .iter()
                    .filter(|effect| **effect == Effect::Install)
                    .count();
                state = next;
            }
            assert!(
                installs <= INSTALL_ATTEMPTS as usize,
                "pattern {pattern:b} asked for {installs} installs"
            );
        }
    }

    #[test]
    fn a_paint_tick_never_rewrites_the_state_it_was_given() {
        // The general form of the same bug, over every state the machine has.
        let mut missing = observation(Health::Absent);
        missing.status = StatusHealth::NotInstalled;
        missing.fresh = false;
        for state in all_states() {
            let (next, effects) = step(&state, &missing, 7.0);
            assert_eq!(next, state, "a paint tick moved {}", state.name());
            assert!(
                !effects.iter().any(Effect::acts),
                "a paint tick acted from {}: {effects:?}",
                state.name()
            );
        }
    }

    #[test]
    fn a_broken_shim_is_installed_over_rather_than_parked_on() {
        // D6-Q9 / state 2 of the investigation: `--print-status` exits 1 having
        // printed a uv trampoline error, and 6.63.0 parked on an error page for
        // ever although the remedy was the installer it already knows how to
        // run. `process::print_status_within` maps that to `NotInstalled`, so
        // the arm below is the one that runs.
        let mut broken = observation(Health::Absent);
        broken.status = StatusHealth::NotInstalled;
        let (next, effects) = step(&State::Booting, &broken, 1.0);
        assert!(effects.contains(&Effect::Install), "{effects:?}");
        assert_eq!(next, State::Installing { attempts: 1 });
    }

    #[test]
    fn a_server_that_exited_is_reported_with_its_code_and_its_last_words() {
        let mut spent = observation(Health::Absent);
        spent.start_attempts = START_ATTEMPTS_BEFORE_THE_TRUTH;
        spent.since_first_start = Some(90.0);
        spent.since_last_start = Some(1.0);
        spent.last_child_exit = Some(ChildExit {
            code: Some(1),
            last_lines: "Refusing to start: HOST is '0.0.0.0'".to_owned(),
        });
        let (_, effects) = step(
            &State::Starting {
                since: 0.0,
                attempts: 3,
            },
            &spent,
            90.0,
        );
        let json = effects
            .iter()
            .find_map(|effect| match effect {
                Effect::Show(page) => serde_json::to_string(page).ok(),
                _ => None,
            })
            .expect("a page");
        assert!(json.contains("server-failed"), "{json}");
        assert!(json.contains("exited with 1"), "{json}");
        assert!(json.contains("Refusing to start"), "{json}");
    }

    #[test]
    fn the_start_budget_is_three_attempts_or_the_documents_own_timeout() {
        // D6-Q8: the two keys the status document has always carried and
        // nothing has ever read. No new setting.
        let mut slow = observation(Health::Absent);
        slow.facts.start_timeout_seconds = 20.0;
        slow.facts.server_start_retries = 2;
        slow.start_attempts = 1;
        slow.since_first_start = Some(59.0);
        assert!(!start_budget_spent(&slow));
        slow.since_first_start = Some(60.0);
        assert!(start_budget_spent(&slow));

        // The 6.60.2-era defaults: 15 x 3 = 45.
        let mut older = slow.clone();
        older.facts.start_timeout_seconds = 15.0;
        older.since_first_start = Some(44.0);
        assert!(!start_budget_spent(&older));
        older.since_first_start = Some(45.0);
        assert!(start_budget_spent(&older));

        // ...and three attempts is the other half, however fast they were.
        let mut quick = observation(Health::Absent);
        quick.start_attempts = START_ATTEMPTS_BEFORE_THE_TRUTH;
        quick.since_first_start = Some(3.0);
        assert!(start_budget_spent(&quick));

        // Nothing started yet is never "spent".
        assert!(!start_budget_spent(&observation(Health::Absent)));
    }

    #[test]
    fn the_failed_page_still_counts_down_and_still_starts_the_server() {
        // Decision Q4 of 2026-09-08 is not weakened by D6: the tick keeps
        // force-starting for ever. The budget changes the sentence, not the
        // spawn.
        let mut spent = observation(Health::Absent);
        spent.start_attempts = 9;
        spent.since_first_start = Some(300.0);
        spent.since_last_start = Some(30.0);
        let (next, effects) = step(
            &State::Starting {
                since: 0.0,
                attempts: 9,
            },
            &spent,
            300.0,
        );
        assert!(effects.contains(&Effect::Spawn), "{effects:?}");
        assert!(matches!(next, State::Starting { .. }));
        let json = effects
            .iter()
            .find_map(|effect| match effect {
                Effect::Show(page) => serde_json::to_string(page).ok(),
                _ => None,
            })
            .expect("a page");
        assert!(json.contains("server-failed"), "{json}");
        assert!(json.contains("next start attempt in"), "{json}");
    }

    #[test]
    fn the_failed_page_goes_away_by_itself_when_health_answers() {
        let mut spent = observation(Health::Healthy);
        spent.start_attempts = 5;
        spent.since_first_start = Some(300.0);
        let (next, effects) = step(
            &State::Starting {
                since: 0.0,
                attempts: 5,
            },
            &spent,
            300.0,
        );
        assert_eq!(next, State::Attached);
        assert!(
            effects
                .iter()
                .any(|effect| matches!(effect, Effect::Attach { .. })),
            "{effects:?}"
        );
    }

    #[test]
    fn every_error_page_names_the_logs_it_has() {
        // D6-Q10. `Page::Error::server_log` has existed since 6.61.0 and every
        // construction site passed `None`.
        let mut unreadable = observation(Health::Absent);
        unreadable.status = StatusHealth::Unreadable {
            detail: "boom".to_owned(),
        };
        unreadable.facts.server_log = "/logs/server.log".to_owned();
        unreadable.facts.shell_log = "/logs/desktop-server-start.log".to_owned();
        let (_, effects) = step(&State::Booting, &unreadable, 1.0);
        let json = effects
            .iter()
            .find_map(|effect| match effect {
                Effect::Show(page) => serde_json::to_string(page).ok(),
                _ => None,
            })
            .expect("a page");
        assert!(json.contains("/logs/server.log"), "{json}");
        assert!(json.contains("/logs/desktop-server-start.log"), "{json}");
    }

    #[test]
    fn the_pages_never_promise_a_button() {
        // None of them parks, so none of them may say "press Retry to
        // continue". Retry accelerates the tick; it is not the way out.
        let mut cases = all_observations();
        cases.retain(|(name, _)| *name != "not-installed");
        for (name, observation) in cases {
            for state in all_states() {
                let (_, effects) = step(&state, &observation, 5.0);
                for effect in effects {
                    let Effect::Show(page) = effect else { continue };
                    let json = serde_json::to_string(&page).expect("a page serializes");
                    assert!(
                        !json.contains("press Retry") && !json.contains("try again later"),
                        "{} + {name} painted a dead end: {json}",
                        state.name()
                    );
                }
            }
        }
    }

    #[test]
    fn every_countdown_sentence_carries_both_numbers() {
        let mut absent = observation(Health::Absent);
        absent.since_last_start = Some(1.0);
        absent.since_probe = 6.0;
        for state in [
            State::Booting,
            State::Attached,
            State::Starting {
                since: 0.0,
                attempts: 1,
            },
            State::Reconnecting { since: 0.0 },
        ] {
            let (_, effects) = step(&state, &absent, 6.0);
            let painted = effects.iter().any(|effect| match effect {
                Effect::Show(Page::Starting { message })
                | Effect::Show(Page::Reconnecting { message }) => {
                    message.contains("last checked 6 s ago")
                        && message.contains("next start attempt in 9 s")
                }
                _ => false,
            });
            assert!(painted, "{} did not say when it last checked", state.name());
        }
    }
}
