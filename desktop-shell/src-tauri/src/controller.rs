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
    Install { detail: String },
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
    pub port: u32,
    pub server_mode: String,
    pub tick_seconds: f64,
    pub start_backoff_seconds: f64,
    pub foreign_grace_seconds: f64,
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
            port: 0,
            server_mode: "spawn".to_owned(),
            tick_seconds: DEFAULT_TICK_SECONDS,
            start_backoff_seconds: DEFAULT_START_BACKOFF_SECONDS,
            foreign_grace_seconds: DEFAULT_FOREIGN_GRACE_SECONDS,
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
        let attempts = match state {
            State::Installing { attempts } => *attempts,
            _ => 0,
        };
        if attempts >= INSTALL_ATTEMPTS {
            return (
                State::Blocked {
                    reason: Blocked::Install {
                        detail: format!(
                            "My Claude Code was installed {attempts} times from this \
                             window and mcc-desktop still cannot be run."
                        ),
                    },
                },
                vec![Effect::Show(Page::Error {
                    message: format!(
                        "My Claude Code was installed {attempts} times from this window \
                         and mcc-desktop still cannot be run. This window keeps checking \
                         every {:.0} seconds; Retry checks again now.",
                        observation.facts.tick_seconds
                    ),
                    server_log: None,
                })],
            );
        }
        if !observation.fresh {
            return (State::Installing { attempts }, effects);
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
                server_log: None,
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
        effects.push(Effect::Show(starting_page(
            observation,
            if post_update {
                "The update finished, so this window is starting the server"
            } else {
                "Starting the My Claude Code server"
            },
        )));
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
    if was_attached {
        effects.push(Effect::Show(reconnecting_page(observation)));
        return (
            State::Reconnecting {
                since: since(state, now),
            },
            effects,
        );
    }

    effects.push(Effect::Show(starting_page(
        observation,
        "Starting the My Claude Code server",
    )));
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
        for expected in 1..=INSTALL_ATTEMPTS {
            let (next, effects) = step(&state, &missing, f64::from(expected));
            assert!(effects.contains(&Effect::Install));
            assert_eq!(next, State::Installing { attempts: expected });
            state = next;
        }
        let (next, effects) = step(&state, &missing, 99.0);
        assert!(!effects.contains(&Effect::Install));
        assert!(matches!(
            next,
            State::Blocked {
                reason: Blocked::Install { .. }
            }
        ));
        // And it still heals: MCC arrives, the next tick starts a server.
        let (healed, effects) = step(&next, &observation(Health::Absent), 109.0);
        assert!(effects.contains(&Effect::Spawn));
        assert!(matches!(healed, State::Starting { .. }));
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
