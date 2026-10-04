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
use crate::ui::{Page, StageLine};

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

/// How long a holder of OURS that is alive and answered recently is left
/// alone, whatever this tick's probe said. `DESKTOP_BUSY_GRACE_SECONDS`.
///
/// This is the number the 2026-09-18 report is about. Six times between
/// 09-16 and 09-18 a `/health` answer that arrived late -- because the
/// server's event loop was busy with a three-hundred-address bulk add, not
/// because anything had died -- was read as "absent" on a single sample, and
/// the second server this window then started took the port from the first
/// one by pid. A server that answered a moment ago is *busy*, and busy is not
/// a reason to replace anything.
///
/// Fifteen seconds is the user's answer (decision I, 2026-09-18 21:25). It is
/// deliberately short: the escalating ladder below is where the ~30 s of real
/// tolerance comes from, and the grace is the floor under it.
pub const DEFAULT_BUSY_GRACE_SECONDS: f64 = 15.0;

/// How many consecutive failed probes of a LIVE holder of ours it takes
/// before the window is allowed to call the server absent.
///
/// `DESKTOP_HEALTH_FAILURE_THRESHOLD` has existed, been on the dashboard and
/// been shipped on the status document since 6.61.0, and until now the Rust
/// controller never read it -- while `desktop-shell/README.md` promised
/// *"Was healthy, now failing, under `health_failure_threshold` -> Nothing at
/// all"*. This is that row, finally implemented. The document's value is used
/// whenever there is one; this is only the floor under a document that says 0.
pub const DEFAULT_HEALTH_FAILURE_THRESHOLD: u32 = 3;

/// The escalating probe timeouts, in seconds: one per consecutive failed
/// probe of a live holder of ours, the last entry repeating for ever after.
///
/// `DESKTOP_HEALTH_PROBE_TIMEOUTS`. 5 + 10 + 15 is about thirty seconds of
/// patience before the window concludes that a server it can see, whose pid
/// is alive, is not coming back -- which is the user's design, and roughly
/// twice the longest loop hold measured on the reporting machine.
///
/// It does NOT replace `health_probe_timeout_seconds` (1.5 s). That one still
/// times every probe taken before this window has ever seen this server
/// answer, so a cold start, a genuinely dead server and a refused connection
/// all cost exactly what they cost today. See `Facts::probe_timeouts`.
pub fn default_probe_timeouts() -> Vec<f64> {
    vec![5.0, 10.0, 15.0]
}

/// How long after an update helper stops the environment is still treated as
/// mid-replacement (decision Q3 of 2026-09-10: "yes, and 30 s").
///
/// The window this exists for is measured, not guessed. `uv tool install
/// --force` empties the live tool environment **in place** before it resolves
/// a single new byte, so for the whole install `mcc-desktop --print-status`
/// exits 1 -- with `ModuleNotFoundError`, then with a missing file, then
/// normally again. On 2026-09-09 at 04:03 the helper finished at 04:01:58 and
/// the window went on showing *My Claude Code could not start --
/// `mcc-desktop --print-status` exited with 1 ... No module named
/// 'my_claude_code'* for five minutes, because the stale verdict from a probe
/// taken mid-install had become permanent.
///
/// Thirty seconds is the user's answer to the trade-off: shorter risks the
/// tail end of a slow disk, longer delays a genuine failure report.
pub const HELPER_SETTLE_SECONDS: f64 = 30.0;

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

    /// Whether this holder is one of MCC's own servers.
    ///
    /// `Unknown` is deliberately not ours: an unknown holder is a reason to
    /// look again, and the busy grace must never be granted to a process
    /// nobody has identified.
    pub fn is_ours(self) -> bool {
        matches!(
            self,
            Self::OursHealthy | Self::OursStarting | Self::OursDraining | Self::OursStale
        )
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

/// Fact L of the rescue spec: what the operating system says about the
/// listening socket on the configured port.
///
/// Read from `mcc-desktop --print-status`'s `holder` (a bind test the OS
/// answered, then `netstat`), never from an HTTP timeout. **Only `Free`, read
/// fresh, can ever lead to a rescue** (decision R4): a server whose process
/// still holds the listening socket is slow, however long it has been silent.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Listener {
    /// Nothing listens: the OS let a socket bind the port.
    Free,
    /// Something listens. `pid` is the OS's answer when it gave one; `mcc` is
    /// whether that process is My Claude Code (`None`: it could not tell).
    Held { pid: Option<i64>, mcc: Option<bool> },
    /// The lookup failed, timed out, or has not been made. Treated exactly like
    /// a held port: an unanswered question is never a reason to act.
    Unknown,
}

/// Why a server is dead, by the OS. The three rows of the decision table that
/// may lead to a rescue (rows 8, 9 and 11), and nothing else can.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeadReason {
    /// Row 8: nothing listens and the server's process has exited.
    ProcessGone,
    /// Row 9: nothing listens and the server's process is still running --
    /// it lost its listener. Confirmed over three checks spanning 30 s, so an
    /// in-process reload (which closes and re-binds its own socket) is never
    /// touched.
    ListenerLost,
    /// Row 11 (user answer 2): a server THIS window started is alive and has
    /// never opened its port, past the start budget.
    NeverBound,
}

impl DeadReason {
    /// The word the rescue command and its JSON use.
    pub fn as_arg(self) -> &'static str {
        match self {
            Self::ProcessGone => "process-gone",
            Self::ListenerLost => "listener-lost",
            Self::NeverBound => "never-bound",
        }
    }
}

/// Why the window is being patient rather than acting (rows 5, 6 and 6b).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SlowReason {
    /// The OS says a process of ours (or the pid that last answered) holds the
    /// port: the server is alive and busy.
    Holds { pid: Option<i64> },
    /// The OS lookup failed or could not identify the holder (approved rule:
    /// unknown = alive, be patient).
    CouldNotTell,
    /// The handshake completed but the OS said nothing listens: a race, and a
    /// race is not a fact.
    Contradiction,
    /// The last OS answer is older than the last thing that answered: it says
    /// nothing about the port now.
    NotRecent,
}

/// What the facts add up to on a tick where nothing answered. Pure.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verdict {
    /// Row 4: this window started a server under 15 s ago. It is watched and
    /// never acted on (user answer 3).
    InGrace,
    /// Rows 5, 6, 6b.
    Slow(SlowReason),
    /// Row 7: a process positively identified as not My Claude Code holds the
    /// port. Never spawned over, never stopped.
    Foreign,
    /// Nothing listens and this window knows of no server to replace -- a cold
    /// start, or a child that exited before it ever bound. Started directly,
    /// exactly as before 7.71.0 (decision Q4).
    Free,
    /// Rows 8, 9, 11 -- each still subject to its own confirmation.
    Dead(DeadReason),
}

/// How a rescue ended, as `mcc-desktop --rescue` reported it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RescueResult {
    /// The old servers are gone and nothing holds the port: start one.
    PortFree,
    /// The rescue looked again and something holds the port, or it could not
    /// look. Nothing was started and nothing more is stopped.
    Refused,
    /// The installed `mcc-desktop` predates `--rescue` (it exited 2 with its
    /// usage). The port was free by the OS when the rescue was asked for, so
    /// the window starts a server exactly as 7.26.0 did -- and that server is
    /// started with `--no-port-takeover`, so it can never stop anything.
    Unsupported,
    /// It did not finish inside its wall, crashed, or printed nothing usable.
    /// Treated as `Refused`: not proven, nothing started.
    Failed,
}

/// One old server the rescue dealt with.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoppedServer {
    /// The launch's pids, outermost first, as the OS listed them.
    pub pids: Vec<i64>,
    /// Whether it finished and exited by itself inside the stop wait, rather
    /// than being stopped by pid.
    pub exited_by_itself: bool,
}

/// Everything a finished rescue says, in plain data, so `step` can word it.
#[derive(Debug, Clone, PartialEq)]
pub struct RescueOutcome {
    pub result: RescueResult,
    /// Why the rescue was asked for.
    pub reason: DeadReason,
    /// The pid the window last heard from, when it knew one.
    pub known_pid: Option<i64>,
    /// The child this window started, when the rescue was asked to include it.
    pub child_pid: Option<i64>,
    pub servers: Vec<StoppedServer>,
    /// Pids the rescue found and left alone because they are not this port's
    /// and this configuration folder's (decision R1).
    pub left_alone: Vec<i64>,
    /// The rescue's own one-line reason, for `Refused` and `Failed`.
    pub detail: String,
    /// How long the old servers were given to finish, in seconds.
    pub stop_wait_seconds: f64,
}

/// Where the rescue is, as the sampler last saw it.
#[derive(Debug, Clone, PartialEq, Default)]
pub enum RescueProgress {
    #[default]
    Idle,
    /// `mcc-desktop --rescue` is running on its own thread.
    Running,
    /// It finished; this is the tick that hears about it.
    Done(RescueOutcome),
}

/// What the update helper is doing. Read from `progress.json`'s `helper_pid`
/// and stage (6.58.3), never from a stage name alone -- a helper killed
/// mid-install leaves `installing` behind forever.
// `Eq` is gone from 6.70.0: `Finished` carries an age in seconds, and an age
// is a measurement. Nothing compares two helpers for equality outside the
// tests, which compare structurally.
#[derive(Debug, Clone, PartialEq)]
pub enum Helper {
    /// No helper has run, or the last one's record is old news.
    None,
    /// A helper process is alive right now. Nothing else may install.
    Alive { stage: Option<String> },
    /// The helper wrote a terminal stage (`done`, `failed`, `recovered`,
    /// `install-failed`) and is gone. This is the fact `RestartPending` waits
    /// for, and the whole of the reported bug: nothing acted on it before.
    ///
    /// `seconds_ago` is how long ago that record was written, and `None` means
    /// *unknown*. Unknown must never be read as "just now": a receipt is
    /// truncated only when the next episode starts, so the `done` line from
    /// the last update is still the last line in the file on every machine
    /// that has ever updated. See [`HELPER_SETTLE_SECONDS`].
    Finished {
        stage: Option<String>,
        seconds_ago: Option<f64>,
    },
}

/// One stage an installer recorded, mirrored from `update_progress::
/// StageRecord` rather than imported, so `step` stays a pure function of plain
/// data and this module keeps its no-I/O rule -- the same reason `ChildExit`
/// is mirrored from `process`.
#[derive(Debug, Clone, PartialEq)]
pub struct UpdateStage {
    pub stage: String,
    pub message: Option<String>,
    /// The writer's own ISO 8601 stamp, verbatim.
    pub at: Option<String>,
    /// Seconds from the start of the episode to this record.
    pub elapsed_seconds: Option<f64>,
}

impl UpdateStage {
    /// `HH:MM:SS` out of the writer's stamp; `None` when there is not one.
    fn clock(&self) -> Option<String> {
        let at = self.at.as_deref()?.trim();
        let time = at.split_once('T').or_else(|| at.split_once(' '))?.1;
        let time = time.split(['Z', 'z', '+']).next()?;
        let mut parts = time.split(':');
        let hour = parts.next()?;
        let minute = parts.next()?;
        let second = parts.next()?.split('.').next()?;
        if hour.len() != 2 || minute.len() != 2 || second.len() != 2 {
            return None;
        }
        Some(format!("{hour}:{minute}:{second}"))
    }
}

/// Everything an update in flight is saying about itself right now.
///
/// This is 6.71.0's answer to "we should see everything happening during an
/// update": the stages with the installer's own timestamps, how long the
/// episode has run, where the installer is writing, and the last lines it
/// wrote. All of it is READ from the one progress document and the file that
/// document names, on every tick, by `Lifecycle::refresh_helper` -- never from
/// `--print-status`, which is the one command that cannot answer while an
/// environment is being replaced (spec F9).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct UpdateNarration {
    /// The episode's records, oldest first.
    pub stages: Vec<UpdateStage>,
    /// The installer transcript's path, as the receipt names it.
    pub log_path: Option<String>,
    /// Its last lines, freshly read.
    pub log_tail: Vec<String>,
    /// The installer's process id, when it recorded one.
    pub helper_pid: Option<i64>,
    /// Seconds since the episode started, from the newest record.
    pub elapsed_seconds: Option<f64>,
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
    /// The server is SLOW (rows 5, 6, 6b): it has not answered, and the OS
    /// says its process still holds the port -- or could not say. The
    /// dashboard stays on screen under a busy banner, nothing is reloaded,
    /// nothing is started and nothing is stopped, for as long as this holds
    /// (decision R4). 7.71.0.
    Busy { since: f64 },
    /// Nothing listens and the server is dead by the OS, but this reason needs
    /// more than one look before it is acted on: a lost listener three checks
    /// across 30 s, a child that never bound checks across its whole start
    /// budget. Any disagreeing check leaves this state, which is the reset.
    Confirming {
        reason: DeadReason,
        /// When the first agreeing check was taken.
        first: f64,
        /// How many agreeing checks so far, this one included.
        checks: u32,
        since: f64,
    },
    /// `mcc-desktop --rescue` is running: waiting for the old servers to
    /// finish, stopping by exact pid what is left, waiting for the port. The
    /// window starts the new server when it reports the port free. 7.71.0.
    Rescuing { since: f64 },
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
            Self::Busy { .. } => "busy",
            Self::Confirming { .. } => "confirming",
            Self::Rescuing { .. } => "rescuing",
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
    /// How many consecutive failed probes of a live holder of ours are needed
    /// before it is called absent. `DESKTOP_HEALTH_FAILURE_THRESHOLD`, on the
    /// status document since 6.61.0 and read by nothing here until 7.26.0.
    pub health_failure_threshold: u32,
    /// The escalating probe timeouts, in seconds, one per consecutive failure;
    /// the last repeats. Empty means "this document does not carry the key",
    /// and the ladder falls back to [`default_probe_timeouts`].
    pub probe_timeouts: Vec<f64>,
    /// How long a live holder of ours that answered recently is left alone.
    pub busy_grace_seconds: f64,
    /// The holder's image name and pid, for the port-conflict page. Python's
    /// words, not a guess assembled here.
    pub holder_image: Option<String>,
    pub holder_pid: Option<i64>,
    /// How often a SLOW server's OS facts are re-read while it keeps not
    /// answering. `DESKTOP_RECONNECT_RESTATUS_SECONDS`, emitted since 6.50.0
    /// and read by nothing until 7.71.0 (rescue spec 2.2 row 5): `netstat` on
    /// a busy machine is itself load, so a slow server is not asked about
    /// every ten seconds.
    pub reconnect_restatus_seconds: f64,
    /// How long an old server is given to finish its open requests and exit
    /// by itself before the rescue may stop it by pid. The status document's
    /// `server_stop_wait_seconds` (7.70.0): `SERVER_GRACEFUL_SHUTDOWN_SECONDS`
    /// plus the server's own fixed stop margins, 24 s at the default.
    pub server_stop_wait_seconds: f64,
}

/// `server_stop_wait_seconds` when the document does not carry it (a wheel
/// before 7.70.0): the shipped default, 20 + 3 + 1. Tolerated, never required,
/// until the pin moves past 7.70.0 (C9).
pub const DEFAULT_SERVER_STOP_WAIT_SECONDS: f64 = 24.0;

/// `reconnect_restatus_seconds` for a document without one. Every wheel since
/// 6.50.0 sends it and the parser requires it; this is only the floor under a
/// value of zero.
pub const DEFAULT_RECONNECT_RESTATUS_SECONDS: f64 = 30.0;

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
            health_failure_threshold: DEFAULT_HEALTH_FAILURE_THRESHOLD,
            probe_timeouts: default_probe_timeouts(),
            busy_grace_seconds: DEFAULT_BUSY_GRACE_SECONDS,
            holder_image: None,

            holder_pid: None,
            reconnect_restatus_seconds: DEFAULT_RECONNECT_RESTATUS_SECONDS,
            server_stop_wait_seconds: DEFAULT_SERVER_STOP_WAIT_SECONDS,
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
    /// Seconds since the last probe that this window saw answered, or `None`
    /// if it has never seen one answer in this session (or since its last
    /// spawn).
    pub seconds_since_healthy: Option<f64>,
    /// How many consecutive probes have come back absent. Reset by any answer
    /// at all -- healthy, starting or draining -- and by a spawn.
    pub consecutive_absent: u32,

    // -- the rescue spec's facts (section 2.1), 7.71.0 ------------------------
    /// Fact P's half the old vocabulary lost: whether this tick's probe got as
    /// far as a completed TCP handshake. `true` with `health == Absent` is
    /// "connected, no answer" -- something holds the port.
    pub probe_connected: bool,
    /// Fact L: what the OS last said about the listening socket.
    pub listener: Listener,
    /// Whether `listener` was read after the last answer and recently enough
    /// to describe the port now. A stale `Free` is never acted on.
    pub listener_fresh: bool,
    /// Fact K: the pid of the server this window last heard from -- the
    /// `x-mcc-pid` of the last answer of any kind, else the document's own
    /// `server_pid`. Always the header when there is one, never a cache that
    /// a healthy tick does not refresh (spec section 1.3).
    pub known_pid: Option<i64>,
    /// Fact A: whether `known_pid` is alive. `None` is "could not tell", and
    /// unknown is alive (the approved rule) -- never dead.
    pub known_alive: Option<bool>,
    /// Fact C: the exact pid of a server THIS window started and that is still
    /// running. `None` when there is none. The only process a "never opened
    /// its port" rescue may name (user answer 2, safeguard e).
    pub child_pid: Option<i64>,
    /// Whether the current child ever answered anything (healthy, starting or
    /// draining) or the OS ever saw the port held since it was started. Such a
    /// server is never "one that never opened its port" (safeguard d).
    pub child_ever_bound: bool,
    /// The pid this tick's answer named, when it named one.
    pub answer_pid: Option<i64>,
    /// `x-mcc-busy` on this tick's answer. The tray line, and nothing else.
    pub busy_header: bool,
    /// Whether the window is showing the dashboard right now.
    pub on_dashboard: bool,
    /// The pid of the server the dashboard on screen was loaded from.
    pub dashboard_pid: Option<i64>,
    /// Where the rescue is.
    pub rescue: RescueProgress,
    /// Seconds since this window last asked for a rescue.
    pub since_rescue: Option<f64>,
    /// Seconds since the status document was last read.
    pub since_restatus: Option<f64>,

    pub helper: Helper,
    /// What the update in flight is saying about itself. Empty when there is
    /// no receipt, which is every ordinary tick.
    pub update: UpdateNarration,
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

    /// Whether this window has evidence of a server it would be replacing: one
    /// named itself, one answered, or one this window started ever bound.
    /// Without any of that, "nothing listens" is a cold start, not a death.
    fn knows_a_server(&self) -> bool {
        self.known_pid.is_some() || self.seconds_since_healthy.is_some() || self.child_ever_bound
    }
}

/// What the caller must do about a step. At most one of these *acts*; `Show`
/// paints, and a paint accompanies almost every tick.
#[derive(Debug, Clone, PartialEq)]
pub enum Effect {
    /// Render one of the shell's own pages.
    Show(Page),
    /// Show the dashboard. `navigate` is false when the window is already on
    /// it and the server that answered is the one it was loaded from: the
    /// busy banner is cleared and nothing is reloaded (rescue spec row 1).
    Attach { admin_url: String, navigate: bool },
    /// Keep the dashboard on screen under a banner that says the server is
    /// busy (rows 5-6). On a window that is not showing the dashboard, the
    /// same sentence is shown as the busy page instead. Never navigates away.
    Overlay { message: String },
    /// Start `mcc-server`, with `--no-port-takeover` and `MCC_OPEN_BROWSER=0`:
    /// a server this window starts can never stop anything, and never opens a
    /// browser tab.
    Spawn,
    /// Run `mcc-desktop --rescue` on a worker thread. Only ever asked for on a
    /// port the OS reported free, read fresh; the command re-reads everything
    /// itself and refuses if anything holds the port.
    Rescue {
        known_pid: Option<i64>,
        child_pid: Option<i64>,
        reason: DeadReason,
    },
    /// Say something to the user: a notification shown by the app under its
    /// own name where the platform allows, always the same sentence in the
    /// window, always a line in the shell log. Every rescue ends in one.
    Notify { message: String },
    /// The same, at most once per outage: "the server is dead and nothing was
    /// started" (non-spawn modes, a stranger on the port). The caller holds the
    /// "once", exactly as it does for `RaiseOnce`; a healthy tick re-arms it.
    AnnounceDead { message: String },
    /// One line for the shell's own transcript.
    Log(String),
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
    ///
    /// A rescue is on the expensive side. A notification is not: like
    /// `RaiseOnce` it reaches the desktop rather than the server, and the tick
    /// that starts the server after a rescue must be able to say why.
    pub fn acts(&self) -> bool {
        matches!(
            self,
            Self::Spawn | Self::Restatus | Self::Install | Self::EnsureShell | Self::Rescue { .. }
        )
    }
}

/// The decision table of the rescue spec (section 2.2), for a tick on which
/// nothing answered. Pure, and the only place "slow" and "dead" are told apart.
///
/// Until 7.71.0 the window decided "dead" from one failed probe plus a pid it
/// had cached from a status document that a healthy answer never refreshed,
/// and an unknown pid counted as dead -- so on 2026-09-28 at 14:40 one late
/// answer from a busy server was enough to start a second server that killed
/// it. The order below is the fix:
///
/// 1. A server this window started under 15 s ago is watched, never judged
///    (user answer 3).
/// 2. **Anything the OS says about a held port is patience.** Held by the pid
///    that last answered, or by a process identified as My Claude Code: slow.
///    Held by something the lookup could not identify, or a lookup that
///    failed: alive, be patient. Only a holder positively identified as NOT
///    My Claude Code is foreign -- and foreign is never spawned over either.
/// 3. Only a port the OS reported **free**, read after the last answer, can be
///    dead -- and even then a completed handshake on this very tick (the OS
///    disagreeing with itself) is patience.
/// 4. Free, with a server known: its process gone is row 8; its process alive
///    is row 9 (it lost its listener); a child of this window's that never
///    once bound is row 11. Free with nothing known is a cold start.
pub fn verdict(observation: &Observation) -> Verdict {
    let grace = observation.facts.busy_grace_seconds.max(0.0);
    if observation.since_last_start.is_some_and(|age| age < grace) {
        return Verdict::InGrace;
    }
    if !observation.listener_fresh && observation.known_alive == Some(false) {
        // The OS answer predates the probe that failed, and the process it
        // named is gone: it describes a port that no longer looks like that.
        // Patience until it is re-read -- and never the sentence "it is
        // running and still holds the port" about a process that has exited.
        return Verdict::Slow(SlowReason::NotRecent);
    }
    match observation.listener {
        Listener::Held { pid, mcc } => {
            if pid.is_some() && pid == observation.known_pid {
                return Verdict::Slow(SlowReason::Holds { pid });
            }
            match mcc {
                Some(true) => Verdict::Slow(SlowReason::Holds { pid }),
                Some(false) => Verdict::Foreign,
                None => Verdict::Slow(SlowReason::CouldNotTell),
            }
        }
        Listener::Unknown => Verdict::Slow(SlowReason::CouldNotTell),
        Listener::Free => {
            if !observation.listener_fresh {
                return Verdict::Slow(SlowReason::NotRecent);
            }
            if observation.probe_connected {
                return Verdict::Slow(SlowReason::Contradiction);
            }
            if observation.child_pid.is_some() && !observation.child_ever_bound {
                return Verdict::Dead(DeadReason::NeverBound);
            }
            if !observation.knows_a_server() {
                return Verdict::Free;
            }
            let gone = observation.known_alive == Some(false)
                || (observation.known_pid.is_none() && observation.child_pid.is_none());
            if gone {
                Verdict::Dead(DeadReason::ProcessGone)
            } else {
                Verdict::Dead(DeadReason::ListenerLost)
            }
        }
    }
}

/// Whether the verdict is SLOW: the one family no sequence of observations
/// may turn into a spawn or a rescue.
pub fn is_slow(verdict: Verdict) -> bool {
    matches!(verdict, Verdict::Slow(_))
}

/// The whole start budget in seconds: the document's own
/// `start_timeout_seconds * (server_start_retries + 1)` -- 60 s at the user's
/// settings, 45 s at the shipped defaults.
pub fn start_budget_seconds(facts: &Facts) -> f64 {
    facts.start_timeout_seconds.max(1.0) * f64::from(facts.server_start_retries + 1)
}

/// Whether a dead reason has been looked at enough times, for long enough.
///
/// * Process gone (row 8): at once, exactly as 7.26.0 started a dead server.
/// * Listener lost (row 9): `DESKTOP_HEALTH_FAILURE_THRESHOLD` agreeing checks
///   spanning that many `DESKTOP_TICK_SECONDS` -- 3 checks, 30 s. An in-process
///   reload closes and re-binds its own listener inside that, and is never
///   touched.
/// * Never bound (row 11, user answer 2): the same number of agreeing checks,
///   spanning the WHOLE window from the end of the 15 s grace to the end of
///   the start budget (safeguard c), and the child at least that old. One
///   disagreeing check -- the port held, the lookup failed, an answer --
///   leaves `Confirming`, so the span starts again from the next agreeing one.
pub fn dead_is_confirmed(
    observation: &Observation,
    reason: DeadReason,
    first: f64,
    checks: u32,
    now: f64,
) -> bool {
    let threshold = observation.facts.health_failure_threshold.max(1);
    let span = now - first;
    match reason {
        DeadReason::ProcessGone => true,
        DeadReason::ListenerLost => {
            checks >= threshold
                && span >= f64::from(threshold) * observation.facts.tick_seconds.max(1.0)
        }
        DeadReason::NeverBound => {
            let budget = start_budget_seconds(&observation.facts);
            let watched = (budget - observation.facts.busy_grace_seconds.max(0.0)).max(0.0);
            checks >= threshold
                && span >= watched
                && observation
                    .since_last_start
                    .is_some_and(|age| age >= budget)
        }
    }
}

/// The timeout for the next probe, given how many have failed in a row.
///
/// The ladder is indexed by the failure count and saturates on its last
/// entry, so 5, 10, 15, 15, 15... An empty ladder means the document did not
/// carry one and the caller keeps its own single timeout.
pub fn probe_timeout_for(consecutive_absent: u32, ladder: &[f64]) -> Option<f64> {
    let usable: Vec<f64> = ladder
        .iter()
        .copied()
        .filter(|value| *value > 0.0)
        .collect();
    if usable.is_empty() {
        return None;
    }
    let index = (consecutive_absent as usize).min(usable.len() - 1);
    Some(usable[index])
}

/// Whether a start may be made right now: the governor from §5.1, with Q4's
/// amendment (no attempt cap; the backoff is the tick).
///
/// From 7.71.0 this is the governor ONLY. Whether the port is free -- the
/// thing that used to be guessed here from a cached holder and a probe count
/// -- is [`verdict`]'s, from the OS, and `step` asks this only after the
/// verdict allows a start at all.
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
    if observation.facts.server_mode != "spawn" {
        return false;
    }
    match observation.since_last_start {
        None => true,
        Some(waited) => waited >= observation.facts.start_backoff_seconds.max(1.0),
    }
}

/// Whether a rescue may be asked for right now.
///
/// The same governor as a start -- a fresh tick, no installer, spawn mode, the
/// start backoff -- with two differences: a live child of this window's does
/// not stop it (the rescue is how such a child is dealt with, by exact pid),
/// and only one rescue runs at a time, never two inside one backoff.
pub fn may_rescue(observation: &Observation) -> bool {
    if !observation.fresh || observation.health != Health::Absent {
        return false;
    }
    if matches!(observation.helper, Helper::Alive { .. }) {
        return false;
    }
    if observation.facts.server_mode != "spawn" {
        return false;
    }
    if matches!(observation.rescue, RescueProgress::Running) {
        return false;
    }
    let backoff = observation.facts.start_backoff_seconds.max(1.0);
    observation
        .since_rescue
        .is_none_or(|waited| waited >= backoff)
        && observation
            .since_last_start
            .is_none_or(|waited| waited >= backoff)
}

/// Whether the environment behind `mcc-desktop` may be being replaced right
/// now -- an installer is alive, or one stopped less than
/// [`HELPER_SETTLE_SECONDS`] ago.
///
/// This is the observation U1 adds, and everything about the fix follows from
/// it: inside this window a status-run failure, a broken shim and a missing
/// shim are all *expected*, so none of them is an error, none of them starts
/// an installer of this window's own, and the window keeps asking on every
/// tick until the answer changes.
///
/// A `Finished` helper whose age cannot be told is deliberately **not**
/// settling. The receipt is truncated only when a new episode begins, so the
/// terminal record of the last update outlives it indefinitely; reading an
/// unknown age as "just now" would suppress the genuine "could not start"
/// page for the rest of the machine's life, which is a worse bug than the one
/// being fixed.
pub fn environment_may_be_replaced(observation: &Observation) -> bool {
    match &observation.helper {
        Helper::Alive { .. } => true,
        Helper::Finished { seconds_ago, .. } => {
            seconds_ago.is_some_and(|age| age < HELPER_SETTLE_SECONDS)
        }
        Helper::None => false,
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
///
/// `Attached` is the one state that used to answer "never". It now answers
/// "when nothing is answering at all", because the tick on which an attached
/// window's server vanishes is exactly the tick on which it needs holder facts
/// it does not have -- and if that tick cannot also spawn (a live child, a
/// foreign holder, the backoff) it would otherwise paint a page about a
/// problem while asking nothing about it. It costs nothing on the healthy
/// path, which is the path BUG-4 was about, and nothing in the common absent
/// case either: a tick that spawns drops the `Restatus` for it.
///
/// `Rescuing` is the second state that answers "never": the rescue command is
/// re-reading the process table, the sockets and the session log itself, and
/// a `--print-status` beside it would only be the same question twice.
fn needs_restatus(state: &State, observation: &Observation) -> bool {
    if !observation.fresh {
        return false;
    }
    match state {
        State::Booting => true,
        State::Attached => observation.health == Health::Absent,
        State::Rescuing { .. } => false,
        _ => observation.health != Health::Healthy,
    }
}

/// Whether a SLOW server's OS facts are due for another look.
///
/// Rescue spec row 5: while the probe keeps connecting and getting no answer,
/// the holder is re-read at most every `reconnect_restatus_seconds` (30 s) --
/// `netstat` on a busy machine is itself load, and the answer does not change
/// while the handshake keeps completing. At once when the connect is refused
/// (the port may have gone free), and at once for every other kind of
/// patience: a lookup that failed is retried on the next tick (row 6).
fn slow_restatus_due(observation: &Observation, reason: SlowReason) -> bool {
    if !matches!(reason, SlowReason::Holds { .. }) || !observation.probe_connected {
        return true;
    }
    observation
        .since_restatus
        .is_none_or(|seconds| seconds >= observation.facts.reconnect_restatus_seconds.max(1.0))
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
        // ...unless an installer is replacing it right now, or stopped a
        // moment ago. `uv tool install --force` deletes the environment before
        // it downloads anything, so "MCC is not installed" is the *expected*
        // reading for the whole of an update -- and installing over it is how
        // the 2026-09-07 update lost all five of its attempts. Decision Q3:
        // this is shown as an update in progress, and this window starts
        // nothing.
        if environment_may_be_replaced(observation) {
            return (
                State::Updating {
                    since: since(state, now),
                },
                keep_asking(observation, environment_replaced_page(observation)),
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
                // U1: the tick that gives up asks again too. Every page that
                // tells the user something is wrong is accompanied by a fresh
                // question -- see `no_page_that_reports_a_problem_is_painted_
                // without_asking_again`.
                keep_asking(
                    observation,
                    install_did_not_take_page(observation, attempts),
                ),
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
            // U1 / decision Q3: "the window keeps asking every tick". The
            // `Restatus` is what lets the window notice that the environment
            // is back *before* the helper's own last record lands -- and it is
            // safe on this arm precisely because `may_start` refuses to spawn
            // while a helper is alive, so nothing this asks for can start a
            // second installer.
            //
            // Which sentence depends on whether the window can still read a
            // status document. While it can, the installer is merely running
            // somewhere; the moment it cannot, the reason is worth saying out
            // loud, because that failure is the one a user would otherwise
            // see reported as "My Claude Code could not start".
            keep_asking(
                observation,
                if observation.status == StatusHealth::Ok {
                    updating_page(stage.as_deref(), observation)
                } else {
                    environment_replaced_page(observation)
                },
            ),
        );
    }

    // 3. A healthy server ends every argument. Deliberately checked before the
    //    unreadable-status branch: a window that can see the dashboard must
    //    not be taken away from it because a subprocess failed.
    if observation.health == Health::Healthy {
        let mut effects = Vec::new();
        let attaching = !matches!(state, State::Attached);
        if attaching {
            // Rescue spec row 1: a server that answers again after being busy
            // is the SAME server, and the dashboard it rendered is still on
            // screen -- reloading it is a full page load (two or three seconds
            // of the server's own loop, and the next flap). The dashboard is
            // navigated to only when the window is on a page of its own, or
            // when the answer names a different process than the one the
            // dashboard was loaded from.
            let new_process = matches!(
                (observation.answer_pid, observation.dashboard_pid),
                (Some(answered), Some(loaded)) if answered != loaded
            );
            effects.push(Effect::Attach {
                admin_url: observation.facts.admin_url.clone(),
                navigate: !observation.on_dashboard || new_process,
            });
            // Q3: the window comes forward once, when a restarted server first
            // answers. Never on the ordinary healthy tick, and never twice --
            // the caller holds the "once". A busy server that answers again
            // was never restarted, so it raises nothing.
            if matches!(
                state,
                State::Reconnecting { .. }
                    | State::RestartPending { .. }
                    | State::Updating { .. }
                    | State::Draining { .. }
                    | State::Rescuing { .. }
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
        // The five-minute park of 2026-09-09 04:03, and its fix, are both
        // here.
        //
        // An installer replacing the environment makes `mcc-desktop
        // --print-status` fail *by design*: uv empties the tool directory in
        // place before it resolves anything, and the shim behind it exits 1
        // with `ModuleNotFoundError`. Reporting that as "My Claude Code could
        // not start" is reporting a normal step of an update as a failure.
        if environment_may_be_replaced(observation) {
            return (
                State::Updating {
                    since: since(state, now),
                },
                keep_asking(observation, environment_replaced_page(observation)),
            );
        }
        return (
            State::Blocked {
                reason: Blocked::Status {
                    detail: detail.clone(),
                },
            },
            // ...and outside an update this page is still not a verdict. Until
            // 6.69.0 this arm returned `Show(Page::Error)` and NOTHING else:
            // no `Restatus`, and `needs_restatus` below is evaluated after the
            // return, so nothing ever re-ran `--print-status`. The loop's own
            // opportunistic re-read was gated on never having read a document
            // at all, which stops being true the first time one parses. The
            // only exit left was a healthy server, and no path could start
            // one. Every subsequent tick repainted the same sentence about a
            // subprocess that had failed once, minutes ago.
            keep_asking(
                observation,
                Page::Error {
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
                },
            ),
        );
    }

    // -- the ordinary world: not healthy, nothing installing ---------------

    if needs_restatus(state, observation) {
        effects.push(Effect::Restatus);
    }

    match observation.health {
        Health::Healthy => unreachable!("handled above"),
        // Rows 2 and 3: a server that answers -- starting or shutting down --
        // is never rescued. Over the dashboard it is a banner, so a restart
        // the dashboard itself asked for does not take the page away.
        Health::Draining => {
            effects.push(if observation.on_dashboard {
                Effect::Overlay {
                    message: draining_overlay(observation),
                }
            } else {
                Effect::Show(draining_page(observation))
            });
            (
                State::Draining {
                    since: since(state, now),
                },
                effects,
            )
        }
        Health::Starting => {
            let attempts = attempts_of(state);
            effects.push(if observation.on_dashboard {
                Effect::Overlay {
                    message: format!(
                        "The server on port {} is restarting and is still loading. The \
                         dashboard comes back by itself the moment it answers.",
                        observation.facts.port
                    ),
                }
            } else {
                Effect::Show(starting_page(observation, "The server is starting"))
            });
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

/// The interesting half: nothing is answering. Decided by [`verdict`], from
/// the OS's facts; this function only turns a verdict into a page and, for a
/// death the OS has proven, a rescue.
fn step_absent(
    state: &State,
    observation: &Observation,
    now: f64,
    mut effects: Vec<Effect>,
) -> (State, Vec<Effect>) {
    // A rescue in flight, or the tick that hears it finished.
    if let State::Rescuing { since: started } = state {
        return step_rescuing(*started, observation, now, effects);
    }

    // A genuinely foreign holder, past its grace (row 7). The conflict page and
    // one announcement; never a start, never a stop -- and it still re-checks
    // every tick.
    if foreign_confirmed(observation) {
        effects.push(Effect::Show(port_conflict_page(observation)));
        if observation.fresh {
            effects.push(Effect::AnnounceDead {
                message: foreign_sentence(observation),
            });
        }
        return (
            State::Blocked {
                reason: Blocked::ForeignPort,
            },
            effects,
        );
    }

    // The post-update path, named. The helper is gone, it wrote its terminal
    // stage, and nothing is answering -- so a free port is started on at once,
    // as before: the helper stopped the old server on purpose, and there is
    // nothing to rescue.
    let post_update = matches!(state, State::Updating { .. } | State::RestartPending { .. })
        || matches!(observation.helper, Helper::Finished { .. });

    match verdict(observation) {
        Verdict::Slow(reason) => {
            // Rows 5, 6, 6b: the dashboard stays, under a banner. Nothing is
            // reloaded, started or stopped, however long this lasts.
            if !slow_restatus_due(observation, reason) {
                effects.retain(|effect| *effect != Effect::Restatus);
            }
            effects.push(Effect::Overlay {
                message: slow_message(observation, reason),
            });
            (
                State::Busy {
                    since: since(state, now),
                },
                effects,
            )
        }
        Verdict::Foreign => {
            // Row 7 inside its grace: say what is being checked, start nothing.
            effects.push(Effect::Show(Page::Reconnecting {
                message: foreign_waiting_message(observation),
            }));
            (
                State::Reconnecting {
                    since: since(state, now),
                },
                effects,
            )
        }
        Verdict::InGrace => {
            // Row 4 (user answer 3): watched, never acted on. The page still
            // tells the truth once the budget is spent.
            effects.push(Effect::Show(if start_budget_spent(observation) {
                server_failed_page(observation)
            } else {
                starting_page(
                    observation,
                    &format!(
                        "Started the server; giving it {:.0} s before anything counts against it",
                        observation.facts.busy_grace_seconds
                    ),
                )
            }));
            (
                State::Starting {
                    since: since(state, now),
                    attempts: attempts_of(state),
                },
                effects,
            )
        }
        Verdict::Free => step_free(state, observation, now, effects, post_update),
        Verdict::Dead(reason) if post_update && reason != DeadReason::NeverBound => {
            step_free(state, observation, now, effects, true)
        }
        Verdict::Dead(reason) => step_dead(state, observation, now, effects, reason),
    }
}

/// A port the OS reports free and a server that is not there to be replaced:
/// a cold start, the start after an update, or a child that exited before it
/// ever bound. Decision Q4, unchanged: started on the tick, for ever, with the
/// page telling the truth once the budget is spent.
fn step_free(
    state: &State,
    observation: &Observation,
    now: f64,
    mut effects: Vec<Effect>,
    post_update: bool,
) -> (State, Vec<Effect>) {
    // Not this window's server to start.
    if observation.facts.server_mode != "spawn" {
        effects.push(Effect::Show(not_our_server_page(observation, None)));
        return (
            State::Blocked {
                reason: Blocked::NotOurServer {
                    server_mode: observation.facts.server_mode.clone(),
                },
            },
            effects,
        );
    }

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

    let was_attached = matches!(
        state,
        State::Attached | State::Reconnecting { .. } | State::Busy { .. }
    );
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

/// Rows 8, 9 and 11: nothing listens, by a fresh OS answer, and a server this
/// window knows of is dead -- its process gone, alive without its port, or a
/// child of ours that never opened it.
///
/// Each reason is confirmed on fresh ticks only ([`dead_is_confirmed`]); the
/// count lives in `State::Confirming`, so a disagreeing tick -- which lands in
/// any other state -- is the reset. Once confirmed: in spawn mode the rescue,
/// in any other mode the page and one announcement. Never a direct spawn: the
/// spawn comes from the rescue's own report that the port is free.
fn step_dead(
    state: &State,
    observation: &Observation,
    now: f64,
    mut effects: Vec<Effect>,
    reason: DeadReason,
) -> (State, Vec<Effect>) {
    let (first, checks) = match state {
        State::Confirming {
            reason: held,
            first,
            checks,
            ..
        } if *held == reason => (
            *first,
            if observation.fresh {
                checks.saturating_add(1)
            } else {
                *checks
            },
        ),
        _ => (now, u32::from(observation.fresh)),
    };
    if checks == 0 {
        // A paint tick on the way in: it repaints and counts nothing.
        effects.push(confirming_effect(observation, reason, 0, now, now));
        return (state.clone(), effects);
    }
    let confirming = State::Confirming {
        reason,
        first,
        checks,
        since: since(state, now),
    };
    if !dead_is_confirmed(observation, reason, first, checks, now) {
        effects.push(confirming_effect(observation, reason, checks, first, now));
        return (confirming, effects);
    }
    let sentence = dead_sentence(
        &observation.facts,
        reason,
        observation.known_pid,
        observation.child_pid,
    );
    if observation.facts.server_mode != "spawn" {
        // Report only (spec section 2.2's pre-emption): the page, and one
        // announcement per outage. Nothing is started and nothing is stopped.
        effects.push(Effect::Show(not_our_server_page(
            observation,
            Some(&sentence),
        )));
        if observation.fresh {
            effects.push(Effect::AnnounceDead {
                message: format!(
                    "{sentence} Server mode is {}, so this app does not start one, and \
                     nothing was stopped.",
                    observation.facts.server_mode
                ),
            });
        }
        return (
            State::Blocked {
                reason: Blocked::NotOurServer {
                    server_mode: observation.facts.server_mode.clone(),
                },
            },
            effects,
        );
    }
    if may_rescue(observation) {
        effects.retain(|effect| !effect.acts());
        effects.push(Effect::Rescue {
            known_pid: observation.known_pid,
            child_pid: observation.child_pid,
            reason,
        });
        effects.push(Effect::Log(format!(
            "-- rescue asked for ({}): {sentence} --",
            reason.as_arg()
        )));
        effects.push(Effect::Show(rescuing_page(observation, 0.0)));
        return (State::Rescuing { since: now }, effects);
    }
    // Proven, and not this tick: the backoff, or a rescue still finishing.
    effects.push(Effect::Show(Page::Reconnecting {
        message: format!(
            "{sentence} It is replaced automatically -- nothing here needs clicking ({}).",
            cadence_tail(observation)
        ),
    }));
    (confirming, effects)
}

/// `State::Rescuing`: the rescue is running, or this is the tick that hears it
/// has finished.
fn step_rescuing(
    started: f64,
    observation: &Observation,
    now: f64,
    mut effects: Vec<Effect>,
) -> (State, Vec<Effect>) {
    effects.retain(|effect| *effect != Effect::Restatus);
    let outcome = match &observation.rescue {
        RescueProgress::Running => {
            effects.push(Effect::Show(rescuing_page(observation, now - started)));
            return (State::Rescuing { since: started }, effects);
        }
        RescueProgress::Idle => {
            // The caller lost the rescue (it never reports this today). Back to
            // the facts, asking again.
            if observation.fresh {
                effects.push(Effect::Restatus);
            }
            effects.push(Effect::Show(reconnecting_page(observation)));
            return (State::Reconnecting { since: now }, effects);
        }
        RescueProgress::Done(outcome) => outcome,
    };
    match outcome.result {
        RescueResult::PortFree | RescueResult::Unsupported => {
            if !observation.fresh {
                // The spawn belongs to a fresh tick, like every spawn; the
                // caller forces one the moment a rescue reports.
                effects.push(Effect::Show(rescuing_page(observation, now - started)));
                return (State::Rescuing { since: started }, effects);
            }
            if observation.child_alive {
                // The rescue left this window's own server running (it held a
                // socket somewhere, or it re-bound): never start a second one
                // beside it. Watch it instead.
                effects.push(Effect::Log(format!(
                    "-- rescue finished, but this window's own server (pid {}) is still \
                     running; watching it rather than starting another --",
                    observation
                        .child_pid
                        .map_or_else(|| "unknown".to_owned(), |pid| pid.to_string())
                )));
                effects.push(Effect::Show(starting_page(
                    observation,
                    "Waiting for the server this window started",
                )));
                return (
                    State::Starting {
                        since: now,
                        attempts: 0,
                    },
                    effects,
                );
            }
            if observation.facts.server_mode != "spawn" {
                effects.push(Effect::Show(not_our_server_page(observation, None)));
                return (
                    State::Blocked {
                        reason: Blocked::NotOurServer {
                            server_mode: observation.facts.server_mode.clone(),
                        },
                    },
                    effects,
                );
            }
            effects.push(Effect::Spawn);
            effects.push(Effect::Notify {
                message: rescue_sentence(outcome, &observation.facts),
            });
            effects.push(Effect::Show(starting_page(
                observation,
                &format!(
                    "Started a new server; giving it {:.0} s",
                    observation.facts.busy_grace_seconds
                ),
            )));
            (
                State::Starting {
                    since: now,
                    attempts: 1,
                },
                effects,
            )
        }
        RescueResult::Refused | RescueResult::Failed => {
            // Not proven after all, or not finished: nothing was started, and
            // the next fresh tick decides from fresh facts (row 12).
            effects.push(Effect::Log(format!(
                "-- rescue did not go ahead ({}): {} -- nothing was started --",
                match outcome.result {
                    RescueResult::Refused => "refused",
                    _ => "failed",
                },
                outcome.detail.trim()
            )));
            if observation.fresh {
                effects.push(Effect::Restatus);
            }
            effects.push(Effect::Show(reconnecting_page(observation)));
            (State::Reconnecting { since: now }, effects)
        }
    }
}

/// Keep the `since` of a state we are already in, and stamp `now` otherwise.
fn since(state: &State, now: f64) -> f64 {
    match state {
        State::Starting { since, .. }
        | State::Reconnecting { since }
        | State::Draining { since }
        | State::Updating { since }
        | State::RestartPending { since }
        | State::Busy { since }
        | State::Confirming { since, .. }
        | State::Rescuing { since } => *since,
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

/// Paint `page`, and on a fresh tick ask the question behind it again.
///
/// The one rule U1 adds, in one function: **no page that reports a problem is
/// ever the last word.** Every arm that shows an error, or an update in
/// progress, goes through here, so the state it leaves behind is one the very
/// next fresh tick re-evaluates against a freshly-read status document rather
/// than against a verdict recorded minutes ago.
///
/// A paint tick asks nothing -- repainting a countdown must not spend a
/// subprocess, and `a_paint_tick_never_rewrites_the_state_it_was_given`
/// asserts it.
fn keep_asking(observation: &Observation, page: Page) -> Vec<Effect> {
    if observation.fresh {
        vec![Effect::Restatus, Effect::Show(page)]
    } else {
        vec![Effect::Show(page)]
    }
}

/// The page shown while an installer is replacing the environment.
///
/// Decision Q3's sentence: the window says what is *actually* happening rather
/// than reporting the symptom. The failure it is covering for --
/// `--print-status` exiting 1, or a shim with no interpreter behind it -- is a
/// normal step of an update, so the page names the step and the elapsed time
/// and promises to keep looking, and it never mentions starting anything.
fn environment_replaced_page(observation: &Observation) -> Page {
    let detail = match &observation.helper {
        // A live helper's stage already carries its own elapsed count
        // (`ActiveHelper::describe`), so it is quoted whole.
        Helper::Alive { stage } => stage
            .as_deref()
            .map(str::trim)
            .filter(|stage| !stage.is_empty())
            .map_or_else(String::new, |stage| format!(" ({stage})")),
        Helper::Finished { stage, seconds_ago } => {
            let stage = stage
                .as_deref()
                .map(str::trim)
                .filter(|stage| !stage.is_empty())
                .unwrap_or("finishing");
            match seconds_ago {
                Some(age) => format!(" ({stage}, {} ago)", seconds(*age)),
                None => format!(" ({stage})"),
            }
        }
        Helper::None => String::new(),
    };
    let (stages, elapsed) = narration_of(observation);
    // With a timeline under it, the helper's own sentence in the lead paragraph
    // is the same fact twice inside nested parentheses. Quote it only when
    // there is no timeline to read it off.
    let detail = if stages.is_empty() {
        detail
    } else {
        String::new()
    };
    Page::Updating {
        message: format!(
            "Installing My Claude Code{detail}: the environment is being replaced. \
             The installer empties it before it writes the new version, so \
             mcc-desktop cannot answer for a few seconds -- that is this step \
             working, not a failure. This window keeps asking every {:.0} \
             seconds, starts no installer of its own, and opens the dashboard \
             by itself the moment the new version answers.",
            observation.facts.tick_seconds
        ),
        // The same timeline and the same live transcript as the page above.
        // Losing them here would mean the window went quiet exactly when the
        // environment went away -- which is the minute the user most wants to
        // see something happening.
        stages,
        elapsed,
        log_path: observation.update.log_path.clone(),
        log_tail: observation.update.log_tail.clone(),
        helper: helper_phrase(observation),
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
    let budget = start_budget_seconds(&observation.facts);
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

/// A duration, worded for a timeline: seconds under a minute, minutes and
/// seconds above it. An update is one to three minutes long, so "94 s" and
/// "1 m 34 s" are both readable and only one of them is readable at eight
/// minutes.
fn duration(value: f64) -> String {
    let total = value.max(0.0);
    if total < 60.0 {
        return format!("{total:.0} s");
    }
    // Stays in f64 rather than casting to an integer: the shell has no `as`
    // casts anywhere, and the arithmetic is exact for every duration an update
    // can have.
    let minutes = (total / 60.0).floor();
    let rest = (total - minutes * 60.0).min(59.0);
    format!("{minutes:.0} m {rest:02.0} s")
}

/// The stage timeline, worded.
///
/// `took` is the gap to the NEXT record for a stage that is over, and the gap
/// to now for the one still running -- which is the number that answers the
/// question a user actually has during an update ("has it stopped?"). The
/// writers record `elapsed_seconds` against the start of the episode, so both
/// are subtractions rather than a second clock.
fn stage_lines(update: &UpdateNarration) -> Vec<StageLine> {
    let total = update.elapsed_seconds;
    let count = update.stages.len();
    update
        .stages
        .iter()
        .enumerate()
        .map(|(index, record)| {
            let current = index + 1 == count;
            let next = if current {
                total
            } else {
                update.stages[index + 1].elapsed_seconds
            };
            let took = match (record.elapsed_seconds, next) {
                (Some(from), Some(to)) if to >= from => Some(duration(to - from)),
                _ => None,
            };
            StageLine {
                stage: record.stage.clone(),
                message: record
                    .message
                    .as_deref()
                    .map(str::trim)
                    .filter(|message| !message.is_empty())
                    .map(str::to_owned),
                at: record.clock(),
                took,
                current,
            }
        })
        .collect()
}

/// The installer, in one phrase, so a window that is waiting says what it is
/// waiting for rather than only that it is waiting.
fn helper_phrase(observation: &Observation) -> Option<String> {
    let alive = matches!(observation.helper, Helper::Alive { .. });
    match (observation.update.helper_pid, alive) {
        (Some(pid), true) => Some(format!("installer pid {pid}, running")),
        (Some(pid), false) => Some(format!("installer pid {pid}, finished")),
        (None, true) => Some("an installer is running".to_owned()),
        (None, false) => None,
    }
}

/// Everything the window shows about an update in flight, beyond its stage.
///
/// Assembled once here so the two pages that narrate an update -- the one shown
/// while `mcc-desktop` still answers and the one shown while it cannot -- show
/// the same thing. They differ in the sentence at the top and nothing else,
/// which is the point: what is happening does not depend on whether the shell
/// can currently ask a subprocess about it.
fn narration_of(observation: &Observation) -> (Vec<StageLine>, Option<String>) {
    (
        stage_lines(&observation.update),
        observation.update.elapsed_seconds.map(duration),
    )
}

fn starting_page(observation: &Observation, lead: &str) -> Page {
    Page::Starting {
        message: format!("{lead}... ({})", cadence_tail(observation)),
    }
}

/// "for 37 s", or "yet" for a server this window has never heard from.
fn silent_for(observation: &Observation) -> String {
    observation.seconds_since_healthy.map_or_else(
        || "yet".to_owned(),
        |waited| format!("for {}", duration(waited)),
    )
}

/// The busy banner over the dashboard (rows 5, 6, 6b). Plain words: what the
/// OS said, and the promise that matters -- nothing is restarted while it is
/// true.
pub fn slow_message(observation: &Observation, reason: SlowReason) -> String {
    let port = observation.facts.port;
    let silent = silent_for(observation);
    let checked = seconds(observation.since_probe);
    match reason {
        SlowReason::Holds { pid: Some(pid) } => format!(
            "The server is busy: it is running (process {pid}) and still holds port {port}, \
             but has not answered {silent}. Nothing will be restarted while that is true. \
             Last checked {checked} ago."
        ),
        SlowReason::Holds { pid: None } => format!(
            "The server is busy: My Claude Code still holds port {port}, but has not \
             answered {silent}. Nothing will be restarted while that is true. Last checked \
             {checked} ago."
        ),
        SlowReason::CouldNotTell => format!(
            "The server has not answered {silent}, and this app could not check which \
             process holds port {port} (the check failed or timed out), so it is treating \
             the server as alive. Nothing will be restarted. Last checked {checked} ago."
        ),
        SlowReason::Contradiction => format!(
            "The server accepted the connection on port {port} but has not answered \
             {silent}. It is treated as busy; nothing will be restarted. Last checked \
             {checked} ago."
        ),
        SlowReason::NotRecent => format!(
            "The server has not answered {silent}. Checking which process holds port \
             {port}; nothing is restarted while that is unknown. Last checked {checked} ago."
        ),
    }
}

/// Row 3 over the dashboard: the server is leaving on purpose.
fn draining_overlay(observation: &Observation) -> String {
    format!(
        "The server on port {} is shutting down and finishing its open requests. This \
         app waits for it; it is never replaced while it is still answering.",
        observation.facts.port
    )
}

/// Row 7 inside its grace: a stranger on the port, not called one yet.
fn foreign_waiting_message(observation: &Observation) -> String {
    format!(
        "Port {} is held by {}, which does not look like My Claude Code. This app is \
         checking again before it says so; it never stops another program ({}).",
        observation.facts.port,
        holder_phrase(observation),
        cadence_tail(observation)
    )
}

/// One announcement for a stranger on the port, past its grace.
pub fn foreign_sentence(observation: &Observation) -> String {
    format!(
        "Port {} is held by {}, which is not My Claude Code. Nothing was stopped; this \
         app re-checks every {:.0} s and starts the server itself once the port is free.",
        observation.facts.port,
        holder_phrase(observation),
        observation.facts.tick_seconds
    )
}

/// The port holder in words, from Python's own answer.
fn holder_phrase(observation: &Observation) -> String {
    match (
        observation.facts.holder_image.as_deref(),
        observation.facts.holder_pid,
    ) {
        (Some(image), Some(pid)) => format!("{image} (pid {pid})"),
        (Some(image), None) => image.to_owned(),
        (None, Some(pid)) => format!("pid {pid}"),
        (None, None) => "another program".to_owned(),
    }
}

/// The page for a server that is not this window's to start.
fn not_our_server_page(observation: &Observation, dead: Option<&str>) -> Page {
    let lead = dead.map_or_else(|| "The server is not running.".to_owned(), str::to_owned);
    Page::NotOurServer {
        message: format!(
            "{lead} Server mode is {}, so this window will not start one; run mcc-server \
             yourself, or switch to spawn in the dashboard. Re-checking every {:.0} seconds.",
            observation.facts.server_mode, observation.facts.tick_seconds
        ),
    }
}

/// What a death IS, in one sentence: which process, and what the OS said.
/// The first half of every rescue notification, and the whole of the
/// announcement in a mode that starts nothing.
pub fn dead_sentence(
    facts: &Facts,
    reason: DeadReason,
    known_pid: Option<i64>,
    child_pid: Option<i64>,
) -> String {
    let port = facts.port;
    match reason {
        DeadReason::ProcessGone => match known_pid {
            Some(pid) => format!(
                "The My Claude Code server on port {port} stopped answering: process {pid} \
                 has exited."
            ),
            None => format!(
                "The My Claude Code server on port {port} stopped answering, and nothing \
                 is listening on the port any more."
            ),
        },
        DeadReason::ListenerLost => match known_pid.or(child_pid) {
            Some(pid) => format!(
                "The My Claude Code server on port {port} stopped answering: process {pid} \
                 is still running but no longer holds the port, so it cannot take requests."
            ),
            None => format!(
                "The My Claude Code server on port {port} stopped answering: nothing holds \
                 the port, and the server this app knew of is still running without it."
            ),
        },
        DeadReason::NeverBound => {
            let who = child_pid.map_or_else(String::new, |pid| format!(" (process {pid})"));
            format!(
                "The server this app started{who} has been running for {} without ever \
                 opening port {port}.",
                duration(start_budget_seconds(facts))
            )
        }
    }
}

/// "pid 4242" or "pids 4242, 4243".
fn pid_list(pids: &[i64]) -> String {
    let joined = pids
        .iter()
        .map(i64::to_string)
        .collect::<Vec<_>>()
        .join(", ");
    if pids.len() == 1 {
        format!("pid {joined}")
    } else {
        format!("pids {joined}")
    }
}

/// The notification every rescue ends in (decision R6, safeguard f): what
/// was dead, what was stopped and why, and what happens next. One sentence
/// group, the same in the toast, in the window and in the shell log.
pub fn rescue_sentence(outcome: &RescueOutcome, facts: &Facts) -> String {
    let mut text = dead_sentence(facts, outcome.reason, outcome.known_pid, outcome.child_pid);
    let exited: Vec<i64> = outcome
        .servers
        .iter()
        .filter(|server| server.exited_by_itself)
        .flat_map(|server| server.pids.iter().copied())
        .collect();
    let stopped: Vec<i64> = outcome
        .servers
        .iter()
        .filter(|server| !server.exited_by_itself)
        .flat_map(|server| server.pids.iter().copied())
        .collect();
    let wait = duration(outcome.stop_wait_seconds);
    if !exited.is_empty() {
        text.push_str(&format!(
            " It finished its open requests and exited by itself ({}).",
            pid_list(&exited)
        ));
    }
    if !stopped.is_empty() {
        let why = match outcome.reason {
            DeadReason::NeverBound => "it never opened the port",
            DeadReason::ListenerLost => "it had lost its port",
            DeadReason::ProcessGone => "its server process was gone",
        };
        text.push_str(&format!(
            " It was given {wait} to finish and exit by itself and did not, so it was \
             stopped by process id because {why} ({}).",
            pid_list(&stopped)
        ));
    }
    if exited.is_empty() && stopped.is_empty() && outcome.result == RescueResult::PortFree {
        text.push_str(" Nothing needed stopping.");
    }
    if !outcome.left_alone.is_empty() {
        text.push_str(&format!(
            " Left running, because it does not belong to this port and configuration \
             folder: {}.",
            pid_list(&outcome.left_alone)
        ));
    }
    match outcome.result {
        RescueResult::Unsupported => text.push_str(
            " The installed My Claude Code is older than 7.71.0 and cannot check for old \
             servers, so nothing was stopped. A new server is starting.",
        ),
        _ => text.push_str(" A new server is starting."),
    }
    text
}

/// The page while a rescue runs.
fn rescuing_page(observation: &Observation, elapsed: f64) -> Page {
    Page::Rescuing {
        message: format!(
            "Replacing the server on port {}. Waiting up to {} for the old server to \
             finish its open requests and exit by itself; anything of it still running \
             after that is stopped by its exact process id, then a new server is started \
             ({} so far). Only My Claude Code servers of this port and this configuration \
             folder are ever touched.",
            observation.facts.port,
            duration(observation.facts.server_stop_wait_seconds),
            duration(elapsed)
        ),
    }
}

/// What a dead reason that is still being confirmed shows.
fn confirming_effect(
    observation: &Observation,
    reason: DeadReason,
    checks: u32,
    first: f64,
    now: f64,
) -> Effect {
    let threshold = observation.facts.health_failure_threshold.max(1);
    match reason {
        DeadReason::ListenerLost => {
            let message = format!(
                "The server is not answering and nothing is listening on port {} (check {} \
                 of {threshold}, {} so far). {} If that is still true after {}, it is given \
                 {} to finish and is then replaced.",
                observation.facts.port,
                checks.max(1),
                duration((now - first).max(0.0)),
                match observation.known_pid.or(observation.child_pid) {
                    Some(pid) => format!("Process {pid} is still running -- it lost its port."),
                    None => "Its process is still running -- it lost its port.".to_owned(),
                },
                duration(f64::from(threshold) * observation.facts.tick_seconds.max(1.0)),
                duration(observation.facts.server_stop_wait_seconds)
            );
            if observation.on_dashboard {
                Effect::Overlay { message }
            } else {
                Effect::Show(Page::Reconnecting { message })
            }
        }
        DeadReason::NeverBound => Effect::Show(if start_budget_spent(observation) {
            server_failed_page(observation)
        } else {
            // Row 10: still coming up. The countdown a cold start shows ("next
            // start attempt in N s") would be untrue here -- nothing is started
            // beside a child that is alive -- so the page says what is watched.
            Page::Starting {
                message: format!(
                    "Starting the My Claude Code server... process {} has not opened \
                     port {} yet ({} of its {} start budget; if it never does, it is \
                     stopped and started again). Last checked {} ago.",
                    observation
                        .child_pid
                        .map_or_else(|| "?".to_owned(), |pid| pid.to_string()),
                    observation.facts.port,
                    duration(observation.since_last_start.unwrap_or(0.0)),
                    duration(start_budget_seconds(&observation.facts)),
                    seconds(observation.since_probe)
                ),
            }
        }),
        DeadReason::ProcessGone => Effect::Show(reconnecting_page(observation)),
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
    let (stages, elapsed) = narration_of(observation);
    // The helper's own sentence is quoted only when the timeline below is
    // empty. With a timeline there, repeating it in the lead paragraph gave
    // the page two copies of the same fact inside nested parentheses --
    // "Updating My Claude Code (Updating to 6.71.0... (installer running,
    // 150 s). Installing the new version.)" -- which is what the acceptance
    // screenshots showed.
    let detail = if stages.is_empty() {
        named.map_or_else(String::new, |stage| format!(" ({stage})"))
    } else {
        String::new()
    };
    Page::Updating {
        message: format!(
            "Updating My Claude Code{detail}: the installer is running. This window \
             waits for it rather than starting a second one, and starts the server \
             itself the moment it finishes -- last checked {} ago.",
            seconds(observation.since_probe)
        ),
        stages,
        elapsed,
        log_path: observation.update.log_path.clone(),
        log_tail: observation.update.log_tail.clone(),
        helper: helper_phrase(observation),
    }
}

fn port_conflict_page(observation: &Observation) -> Page {
    let holder = holder_phrase(observation);
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
            seconds_since_healthy: None,
            consecutive_absent: 1,
            // A refused connect on a port the OS just reported free, with no
            // server known: the cold start every pre-7.71.0 test assumed.
            probe_connected: false,
            listener: Listener::Free,
            listener_fresh: true,
            known_pid: None,
            known_alive: None,
            child_pid: None,
            child_ever_bound: false,
            answer_pid: None,
            busy_header: false,
            on_dashboard: false,
            dashboard_pid: None,
            rescue: RescueProgress::Idle,
            since_rescue: None,
            since_restatus: None,

            helper: Helper::None,
            update: UpdateNarration::default(),
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
            State::Busy { since: 0.0 },
            State::Confirming {
                reason: DeadReason::ListenerLost,
                first: 0.0,
                checks: 1,
                since: 0.0,
            },
            State::Confirming {
                reason: DeadReason::NeverBound,
                first: 0.0,
                checks: 2,
                since: 0.0,
            },
            State::Rescuing { since: 0.0 },
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

        let stranger = Listener::Held {
            pid: Some(31337),
            mcc: Some(false),
        };
        let mut foreign = observation(Health::Absent);
        foreign.holder = Holder::Foreign;
        foreign.listener = stranger;
        foreign.holder_age = 600.0;
        cases.push(("foreign-confirmed", foreign));

        let mut fresh_foreign = observation(Health::Absent);
        fresh_foreign.holder = Holder::Foreign;
        fresh_foreign.listener = stranger;
        fresh_foreign.holder_age = 1.0;
        cases.push(("foreign-unconfirmed", fresh_foreign));

        let mut stale = observation(Health::Absent);
        stale.holder = Holder::OursStale;
        stale.listener = Listener::Held {
            pid: Some(4242),
            mcc: Some(true),
        };
        cases.push(("ours-stale", stale));

        // -- 7.71.0's classes: the rescue spec's facts ---------------------
        let mut slow = observation(Health::Absent);
        slow.probe_connected = true;
        slow.known_pid = Some(4242);
        slow.known_alive = Some(true);
        slow.seconds_since_healthy = Some(40.0);
        slow.listener = Listener::Held {
            pid: Some(4242),
            mcc: Some(true),
        };
        slow.on_dashboard = true;
        cases.push(("slow", slow));

        let mut unknown = observation(Health::Absent);
        unknown.listener = Listener::Unknown;
        cases.push(("lookup-failed", unknown));

        let mut gone = observation(Health::Absent);
        gone.known_pid = Some(4242);
        gone.known_alive = Some(false);
        gone.seconds_since_healthy = Some(3.0);
        cases.push(("process-gone", gone));

        let mut lost = observation(Health::Absent);
        lost.known_pid = Some(4242);
        lost.known_alive = Some(true);
        lost.seconds_since_healthy = Some(3.0);
        cases.push(("listener-lost", lost));

        let mut unbound = observation(Health::Absent);
        unbound.child_alive = true;
        unbound.child_pid = Some(7001);
        unbound.since_last_start = Some(70.0);
        cases.push(("child-never-bound", unbound));

        let mut port_free = observation(Health::Absent);
        port_free.rescue = RescueProgress::Done(RescueOutcome {
            result: RescueResult::PortFree,
            reason: DeadReason::ProcessGone,
            known_pid: Some(4242),
            child_pid: None,
            servers: Vec::new(),
            left_alone: Vec::new(),
            detail: String::new(),
            stop_wait_seconds: 24.0,
        });
        cases.push(("rescue-port-free", port_free));

        let mut refused = observation(Health::Absent);
        refused.rescue = RescueProgress::Done(RescueOutcome {
            result: RescueResult::Refused,
            reason: DeadReason::ListenerLost,
            known_pid: Some(4242),
            child_pid: None,
            servers: Vec::new(),
            left_alone: Vec::new(),
            detail: "held".to_owned(),
            stop_wait_seconds: 24.0,
        });
        cases.push(("rescue-refused", refused));

        let mut helper = observation(Health::Absent);
        helper.helper = Helper::Alive {
            stage: Some("installing".to_owned()),
        };
        cases.push(("helper-alive", helper));

        let mut done = observation(Health::Absent);
        done.helper = Helper::Finished {
            stage: Some("done".to_owned()),
            // Two seconds ago: inside the settle window, so this class also
            // proves that settling never stops the post-update SPAWN. Only a
            // status document that cannot be read is diverted by it.
            seconds_ago: Some(2.0),
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

        // -- U1's classes: an installer is replacing the environment -------

        // The user's own machine at 04:00:20 on 2026-09-09: uv has emptied the
        // tool directory, the shim exits 1 with `ModuleNotFoundError`, and
        // `process::print_status_within` now calls that a broken shim, which
        // reads as `NotInstalled`.
        let mut replacing = observation(Health::Absent);
        replacing.status = StatusHealth::NotInstalled;
        replacing.helper = Helper::Alive {
            stage: Some("Updating to 6.65.0... (installer running, 46 s)".to_owned()),
        };
        cases.push(("environment-being-replaced", replacing));

        // ...and at 04:02:00, two seconds after the helper wrote `done`, with
        // uv still putting the shims back.
        let mut settling = observation(Health::Absent);
        settling.status = StatusHealth::Unreadable {
            detail: "mcc-desktop --print-status exited with 1. ModuleNotFoundError".to_owned(),
        };
        settling.helper = Helper::Finished {
            stage: Some("done".to_owned()),
            seconds_ago: Some(2.0),
        };
        cases.push(("status-unreadable-while-settling", settling));

        // The same file an hour later. A `done` record is the last line in
        // `progress.json` on every machine that has ever updated, so this is
        // the ordinary state of the world and must NOT be settling.
        let mut stale_receipt = observation(Health::Absent);
        stale_receipt.helper = Helper::Finished {
            stage: Some("done".to_owned()),
            seconds_ago: Some(3_600.0),
        };
        cases.push(("helper-finished-long-ago", stale_receipt));

        cases
    }

    // -- 7.71.0: the decision table of the rescue spec, one test per row -----
    //
    // Rows are the spec's section 2.2; safeguards (a)-(f) are user answer 2
    // of 2026-10-01 16:24. Every test drives `step` and nothing else.

    /// The server this window was attached to, pid 4242, which answered
    /// until twelve seconds ago, with the dashboard it rendered on screen.
    fn known(listener: Listener) -> Observation {
        let mut observation = observation(Health::Absent);
        observation.known_pid = Some(4242);
        observation.known_alive = Some(true);
        observation.seconds_since_healthy = Some(12.0);
        observation.listener = listener;
        observation.listener_fresh = true;
        observation.on_dashboard = true;
        observation.dashboard_pid = Some(4242);
        observation
    }

    /// Slow, exactly as 2026-09-28 14:40: the loop is held, the kernel still
    /// completes the handshake, and the OS says pid 4242 holds the port.
    fn slow() -> Observation {
        let mut observation = known(Listener::Held {
            pid: Some(4242),
            mcc: Some(true),
        });
        observation.probe_connected = true;
        observation
    }

    /// A child of this window's, pid 7001, alive, never bound, `age` seconds
    /// old, on a port the OS keeps reporting free. The user's 60 s budget.
    fn unbound_child(age: f64) -> Observation {
        let mut observation = observation(Health::Absent);
        observation.child_alive = true;
        observation.child_pid = Some(7001);
        observation.since_last_start = Some(age);
        observation.since_first_start = Some(age);
        observation.start_attempts = 1;
        observation.facts.start_timeout_seconds = 20.0;
        observation.facts.server_start_retries = 2;
        observation
    }

    fn acts_on_a_server(effects: &[Effect]) -> bool {
        effects
            .iter()
            .any(|effect| matches!(effect, Effect::Spawn | Effect::Rescue { .. }))
    }

    fn rescue_in(effects: &[Effect]) -> Option<(Option<i64>, Option<i64>, DeadReason)> {
        effects.iter().find_map(|effect| match effect {
            Effect::Rescue {
                known_pid,
                child_pid,
                reason,
            } => Some((*known_pid, *child_pid, *reason)),
            _ => None,
        })
    }

    fn outcome(
        result: RescueResult,
        reason: DeadReason,
        servers: Vec<StoppedServer>,
    ) -> RescueOutcome {
        RescueOutcome {
            result,
            reason,
            known_pid: Some(4242),
            child_pid: None,
            servers,
            left_alone: Vec::new(),
            detail: String::new(),
            stop_wait_seconds: 24.0,
        }
    }

    /// Fresh ticks `every` seconds apart, from `state` at `start`; every effect
    /// they produced, in order, and the state they ended in.
    fn run_ticks(
        mut state: State,
        observation: &Observation,
        start: f64,
        every: f64,
        ticks: u32,
    ) -> (State, Vec<(f64, Effect)>) {
        let mut seen = Vec::new();
        for tick in 0..ticks {
            let now = start + every * f64::from(tick);
            let (next, effects) = step(&state, observation, now);
            seen.extend(effects.into_iter().map(|effect| (now, effect)));
            state = next;
        }
        (state, seen)
    }

    #[test]
    fn row_1_a_healthy_answer_attaches_without_reloading_the_same_server() {
        // The self-inflicted load investigation's decision 3: a server that
        // answers again after being busy is the SAME server, and its dashboard
        // is still on screen. Reloading it was a full page load per flap.
        let mut back = observation(Health::Healthy);
        back.on_dashboard = true;
        back.dashboard_pid = Some(4242);
        back.answer_pid = Some(4242);
        let (next, effects) = step(&State::Busy { since: 0.0 }, &back, 40.0);
        assert_eq!(next, State::Attached);
        assert_eq!(
            effects,
            vec![Effect::Attach {
                admin_url: back.facts.admin_url.clone(),
                navigate: false
            }],
            "no reload, no raise -- only the banner taken away"
        );
        // A server too old to name itself is still the same dashboard.
        back.answer_pid = None;
        let (_, effects) = step(&State::Busy { since: 0.0 }, &back, 40.0);
        assert!(effects.contains(&Effect::Attach {
            admin_url: back.facts.admin_url.clone(),
            navigate: false
        }));
    }

    #[test]
    fn row_1_a_new_pid_reloads_once_and_a_shell_page_always_navigates() {
        let mut replaced = observation(Health::Healthy);
        replaced.on_dashboard = true;
        replaced.dashboard_pid = Some(4242);
        replaced.answer_pid = Some(5555);
        let (_, effects) = step(&State::Busy { since: 0.0 }, &replaced, 40.0);
        assert!(effects.contains(&Effect::Attach {
            admin_url: replaced.facts.admin_url.clone(),
            navigate: true
        }));
        // ...and only on the transition: an attached window that keeps hearing
        // the new pid does not reload again.
        let (_, effects) = step(&State::Attached, &replaced, 50.0);
        assert!(effects.is_empty(), "{effects:?}");

        let mut from_a_page = observation(Health::Healthy);
        from_a_page.on_dashboard = false;
        from_a_page.answer_pid = Some(4242);
        let (_, effects) = step(
            &State::Starting {
                since: 0.0,
                attempts: 1,
            },
            &from_a_page,
            5.0,
        );
        assert!(effects.contains(&Effect::Attach {
            admin_url: from_a_page.facts.admin_url.clone(),
            navigate: true
        }));
    }

    #[test]
    fn the_pid_used_to_decide_is_the_one_the_answer_named() {
        // Fact K comes from `x-mcc-pid` on every answer. The dashboard is
        // reloaded exactly when THAT pid differs from the one it was loaded
        // from -- never on a cached document's guess.
        for (answered, loaded, navigate) in [
            (Some(10), Some(10), false),
            (Some(11), Some(10), true),
            (None, Some(10), false),
            (Some(10), None, false),
        ] {
            let mut back = observation(Health::Healthy);
            back.on_dashboard = true;
            back.answer_pid = answered;
            back.dashboard_pid = loaded;
            // The document's guess is deliberately different, and ignored.
            back.known_pid = Some(99);
            let (_, effects) = step(&State::Busy { since: 0.0 }, &back, 1.0);
            assert!(
                effects.contains(&Effect::Attach {
                    admin_url: back.facts.admin_url.clone(),
                    navigate
                }),
                "{answered:?}/{loaded:?}: {effects:?}"
            );
        }
    }

    #[test]
    fn row_2_a_starting_answer_never_rescues_and_keeps_the_dashboard() {
        for state in all_states() {
            let mut starting = observation(Health::Starting);
            starting.on_dashboard = true;
            let (next, effects) = step(&state, &starting, 1.0);
            assert!(!acts_on_a_server(&effects), "{}: {effects:?}", state.name());
            if !matches!(state, State::Rescuing { .. }) {
                assert!(matches!(next, State::Starting { .. }), "{}", state.name());
                assert!(
                    effects
                        .iter()
                        .any(|effect| matches!(effect, Effect::Overlay { .. })),
                    "over the dashboard it is a banner, not a page: {effects:?}"
                );
            }
        }
    }

    #[test]
    fn row_3_a_draining_answer_never_rescues() {
        for state in all_states() {
            let (next, effects) = step(&state, &observation(Health::Draining), 1.0);
            assert!(!acts_on_a_server(&effects), "{}: {effects:?}", state.name());
            assert!(matches!(next, State::Draining { .. }), "{}", state.name());
        }
    }

    #[test]
    fn row_4_nothing_counts_inside_the_grace_and_the_new_server_is_still_watched() {
        // User answer 3: watched, never acted on. Every fact that would
        // otherwise be a death, inside the first 15 s after a start.
        let mut cases = vec![known(Listener::Free), unbound_child(3.0)];
        let mut gone = known(Listener::Free);
        gone.known_alive = Some(false);
        cases.push(gone);
        let mut exited = observation(Health::Absent);
        exited.last_child_exit = Some(ChildExit {
            code: Some(1),
            last_lines: "boom".to_owned(),
        });
        cases.push(exited);
        for mut case in cases {
            for age in [0.0, 1.0, 7.5, 14.9] {
                case.since_last_start = Some(age);
                assert_eq!(verdict(&case), Verdict::InGrace, "{age}");
                let (_, effects) = step(
                    &State::Starting {
                        since: 0.0,
                        attempts: 1,
                    },
                    &case,
                    age,
                );
                assert!(!acts_on_a_server(&effects), "{age}: {effects:?}");
            }
        }
        // ...and watching means a server that answers inside the grace is
        // attached at once.
        let mut answered = observation(Health::Healthy);
        answered.since_last_start = Some(4.0);
        let (next, _) = step(
            &State::Starting {
                since: 0.0,
                attempts: 1,
            },
            &answered,
            4.0,
        );
        assert_eq!(next, State::Attached);
    }

    #[test]
    fn row_5_a_listener_owned_by_the_known_pid_is_slow_forever() {
        // 1,000 ticks -- two hours and three quarters at the user's tick --
        // of a server that holds its port and never answers. Nothing is
        // started, nothing is stopped, the dashboard is never navigated away
        // from, and the banner is on every tick.
        let observation = slow();
        assert_eq!(
            verdict(&observation),
            Verdict::Slow(SlowReason::Holds { pid: Some(4242) })
        );
        let (state, seen) = run_ticks(State::Attached, &observation, 100.0, 10.0, 1_000);
        assert!(matches!(state, State::Busy { .. }), "{state:?}");
        assert!(
            !seen.iter().any(|(_, effect)| matches!(
                effect,
                Effect::Spawn | Effect::Rescue { .. } | Effect::Show(_) | Effect::Attach { .. }
            )),
            "a slow server was acted on or navigated away from"
        );
        assert_eq!(
            seen.iter()
                .filter(|(_, effect)| matches!(effect, Effect::Overlay { .. }))
                .count(),
            1_000
        );
    }

    #[test]
    fn row_5_an_mcc_listener_with_another_pid_is_slow() {
        let mut observation = slow();
        observation.listener = Listener::Held {
            pid: Some(9999),
            mcc: Some(true),
        };
        assert!(is_slow(verdict(&observation)));
        let (_, effects) = step(&State::Attached, &observation, 1.0);
        assert!(!acts_on_a_server(&effects), "{effects:?}");
    }

    #[test]
    fn row_6_an_unknown_lookup_is_patience() {
        // The approved rule: unknown = alive, be patient. A lookup that failed
        // or timed out, and a holder that could not be identified.
        for listener in [
            Listener::Unknown,
            Listener::Held {
                pid: Some(31337),
                mcc: None,
            },
            Listener::Held {
                pid: None,
                mcc: None,
            },
        ] {
            let mut observation = known(listener);
            observation.known_alive = None;
            assert_eq!(
                verdict(&observation),
                Verdict::Slow(SlowReason::CouldNotTell),
                "{listener:?}"
            );
            let (state, effects) = step(&State::Attached, &observation, 1.0);
            assert!(matches!(state, State::Busy { .. }));
            assert!(!acts_on_a_server(&effects), "{effects:?}");
            // ...and the lookup is retried on the very next fresh tick.
            assert!(effects.contains(&Effect::Restatus), "{effects:?}");
        }
    }

    #[test]
    fn row_6b_a_handshake_with_an_empty_table_is_patience() {
        let mut observation = known(Listener::Free);
        observation.probe_connected = true;
        assert_eq!(
            verdict(&observation),
            Verdict::Slow(SlowReason::Contradiction)
        );
        let (_, effects) = step(&State::Attached, &observation, 1.0);
        assert!(!acts_on_a_server(&effects), "{effects:?}");
    }

    #[test]
    fn a_free_port_from_before_the_last_answer_is_not_a_fact() {
        // Safeguard (a), generalised: "nothing listens" must be the OS's
        // answer NOW. One the window read before the server last answered
        // says nothing about the port, and is patience until re-read.
        let mut observation = known(Listener::Free);
        observation.known_alive = Some(false);
        observation.listener_fresh = false;
        assert_eq!(verdict(&observation), Verdict::Slow(SlowReason::NotRecent));
        let (_, effects) = step(&State::Attached, &observation, 1.0);
        assert!(!acts_on_a_server(&effects), "{effects:?}");
        assert!(effects.contains(&Effect::Restatus), "it asks: {effects:?}");
    }

    #[test]
    fn an_old_holder_answer_about_a_process_that_has_exited_is_not_repeated() {
        // The OS said pid 4242 holds the port before the probe failed, and
        // 4242 has since exited: patience until the port is re-read, and never
        // "it is running and still holds port" about a process that is gone.
        let mut stale = known(Listener::Held {
            pid: Some(4242),
            mcc: Some(true),
        });
        stale.listener_fresh = false;
        stale.known_alive = Some(false);
        assert_eq!(verdict(&stale), Verdict::Slow(SlowReason::NotRecent));
        let (_, effects) = step(&State::Attached, &stale, 1.0);
        assert!(!acts_on_a_server(&effects), "{effects:?}");
        assert!(effects.contains(&Effect::Restatus));
        assert!(!effects.iter().any(|effect| matches!(
            effect,
            Effect::Overlay { message } if message.contains("is running")
        )));
    }

    #[test]
    fn row_7_a_foreign_listener_is_never_spawned_over_or_stopped() {
        let mut stranger = known(Listener::Held {
            pid: Some(31337),
            mcc: Some(false),
        });
        stranger.holder = Holder::Foreign;
        stranger.facts.holder_pid = Some(31337);
        stranger.facts.holder_image = Some("nginx.exe".to_owned());
        assert_eq!(verdict(&stranger), Verdict::Foreign);
        stranger.holder_age = 5.0;
        let (state, effects) = step(&State::Attached, &stranger, 1.0);
        assert!(matches!(state, State::Reconnecting { .. }), "{state:?}");
        assert!(!acts_on_a_server(&effects), "{effects:?}");

        stranger.holder_age = 600.0;
        let (state, seen) = run_ticks(State::Attached, &stranger, 1.0, 10.0, 50);
        assert_eq!(
            state,
            State::Blocked {
                reason: Blocked::ForeignPort
            }
        );
        assert!(
            !seen
                .iter()
                .any(|(_, effect)| matches!(effect, Effect::Spawn | Effect::Rescue { .. }))
        );
        let announced: Vec<&String> = seen
            .iter()
            .filter_map(|(_, effect)| match effect {
                Effect::AnnounceDead { message } => Some(message),
                _ => None,
            })
            .collect();
        assert!(!announced.is_empty());
        assert!(
            announced[0].contains("nginx.exe (pid 31337)"),
            "{}",
            announced[0]
        );
        assert!(
            announced[0].contains("Nothing was stopped"),
            "{}",
            announced[0]
        );
    }

    #[test]
    fn row_8_a_gone_process_is_rescued_on_the_first_fresh_tick() {
        // The 7.26.0 pin, moved as the spec says: a dead server still costs
        // one tick, and that tick now asks for the rescue rather than spawning
        // into whatever is left -- the spawn comes from the rescue's report.
        let mut gone = known(Listener::Free);
        gone.known_alive = Some(false);
        assert_eq!(verdict(&gone), Verdict::Dead(DeadReason::ProcessGone));
        let (state, effects) = step(&State::Attached, &gone, 100.0);
        assert_eq!(state, State::Rescuing { since: 100.0 });
        assert_eq!(
            rescue_in(&effects),
            Some((Some(4242), None, DeadReason::ProcessGone))
        );
        assert!(!effects.contains(&Effect::Spawn));
        assert_eq!(effects.iter().filter(|effect| effect.acts()).count(), 1);
    }

    #[test]
    fn row_9_a_listener_loss_waits_for_three_checks_and_thirty_seconds() {
        let lost = known(Listener::Free);
        assert_eq!(verdict(&lost), Verdict::Dead(DeadReason::ListenerLost));
        let mut state = State::Attached;
        for (tick, now) in [0.0, 10.0, 20.0].into_iter().enumerate() {
            let (next, effects) = step(&state, &lost, now);
            assert!(
                rescue_in(&effects).is_none(),
                "check {} at {now}: {effects:?}",
                tick + 1
            );
            assert!(
                effects.iter().any(|effect| matches!(
                    effect,
                    Effect::Overlay { message } if message.contains(&format!("check {} of 3", tick + 1))
                )),
                "{effects:?}"
            );
            state = next;
        }
        let (state, effects) = step(&state, &lost, 30.0);
        assert_eq!(
            rescue_in(&effects),
            Some((Some(4242), None, DeadReason::ListenerLost))
        );
        assert!(matches!(state, State::Rescuing { .. }));
    }

    #[test]
    fn row_9_a_reload_that_rebinds_inside_the_confirmation_is_never_restarted() {
        // An in-process RELOAD closes and re-binds its own listener. Two
        // checks see the port free; the third sees it held again.
        let lost = known(Listener::Free);
        let rebound = slow();
        let mut state = State::Attached;
        let mut seen = Vec::new();
        for (now, observation) in [
            (0.0, &lost),
            (10.0, &lost),
            (20.0, &rebound),
            (30.0, &lost),
            (40.0, &lost),
            (50.0, &lost),
        ] {
            let (next, effects) = step(&state, observation, now);
            seen.extend(effects);
            state = next;
        }
        assert!(rescue_in(&seen).is_none(), "{seen:?}");
        // The count started again at 30: three checks spanning 30 s is 60.
        let (_, effects) = step(&state, &lost, 60.0);
        assert!(rescue_in(&effects).is_some(), "{effects:?}");
    }

    #[test]
    fn row_10_a_child_still_coming_up_is_waited_for() {
        let mut state = State::Starting {
            since: 0.0,
            attempts: 1,
        };
        for age in [15.0, 25.0, 35.0, 45.0, 55.0, 59.9] {
            let observation = unbound_child(age);
            assert_eq!(verdict(&observation), Verdict::Dead(DeadReason::NeverBound));
            let (next, effects) = step(&state, &observation, age);
            assert!(!acts_on_a_server(&effects), "{age}: {effects:?}");
            state = next;
        }
    }

    #[test]
    fn row_11_a_child_that_never_bound_is_replaced_after_its_whole_budget() {
        let mut state = State::Starting {
            since: 0.0,
            attempts: 1,
        };
        let mut rescued_at = None;
        let mut age = 15.0;
        while age <= 120.0 {
            let (next, effects) = step(&state, &unbound_child(age), age);
            if let Some(found) = rescue_in(&effects) {
                assert_eq!(found, (None, Some(7001), DeadReason::NeverBound));
                rescued_at = Some(age);
                break;
            }
            state = next;
            age += 10.0;
        }
        assert_eq!(
            rescued_at,
            Some(65.0),
            "the first check at or after 60 s whose agreeing checks span 15 -> 60"
        );
    }

    #[test]
    fn safeguard_a_the_port_state_comes_from_the_os_never_from_a_timeout() {
        // Past the budget, a child alive, and every probe a timeout -- but no
        // fresh OS answer saying the port is free. Never replaced.
        for listener in [Listener::Unknown, Listener::Free] {
            let mut observation = unbound_child(300.0);
            observation.listener = listener;
            observation.listener_fresh = false;
            observation.probe_connected = false;
            let (_, seen) = run_ticks(
                State::Starting {
                    since: 0.0,
                    attempts: 1,
                },
                &observation,
                15.0,
                10.0,
                100,
            );
            assert!(
                !seen
                    .iter()
                    .any(|(_, effect)| matches!(effect, Effect::Rescue { .. })),
                "{listener:?}"
            );
        }
    }

    #[test]
    fn safeguard_b_a_failed_or_slow_lookup_counts_as_alive_and_port_held() {
        let mut failed = unbound_child(40.0);
        failed.listener = Listener::Unknown;
        assert_eq!(verdict(&failed), Verdict::Slow(SlowReason::CouldNotTell));
        let mut unidentified = unbound_child(40.0);
        unidentified.listener = Listener::Held {
            pid: None,
            mcc: None,
        };
        assert!(is_slow(verdict(&unidentified)));
        let (state, effects) = step(
            &State::Confirming {
                reason: DeadReason::NeverBound,
                first: 15.0,
                checks: 3,
                since: 0.0,
            },
            &failed,
            40.0,
        );
        assert!(
            matches!(state, State::Busy { .. }),
            "a failed lookup leaves the count: {state:?}"
        );
        assert!(!acts_on_a_server(&effects));
    }

    #[test]
    fn safeguard_c_one_disagreement_resets_the_count_and_the_span() {
        let mut state = State::Starting {
            since: 0.0,
            attempts: 1,
        };
        let mut rescued_at = None;
        let mut age = 15.0;
        while age <= 200.0 {
            let mut observation = unbound_child(age);
            if (age - 45.0).abs() < 0.1 {
                // One check at 45 s sees the port held.
                observation.listener = Listener::Held {
                    pid: Some(1234),
                    mcc: Some(true),
                };
            }
            let (next, effects) = step(&state, &observation, age);
            if rescue_in(&effects).is_some() {
                rescued_at = Some(age);
                break;
            }
            state = next;
            age += 10.0;
        }
        // The streak restarts at 55: 55 + 45 = 100.
        assert_eq!(rescued_at, Some(105.0));
    }

    #[test]
    fn safeguard_d_a_server_that_answered_or_bound_is_never_one_that_never_opened_its_port() {
        let mut bound = unbound_child(300.0);
        bound.child_ever_bound = true;
        assert_ne!(verdict(&bound), Verdict::Dead(DeadReason::NeverBound));
        let (_, seen) = run_ticks(
            State::Starting {
                since: 0.0,
                attempts: 1,
            },
            &bound,
            15.0,
            10.0,
            40,
        );
        assert!(!seen.iter().any(|(_, effect)| matches!(
            effect,
            Effect::Rescue {
                reason: DeadReason::NeverBound,
                ..
            }
        )));
        // A 503 "starting" answer is an answer: the arm that handles it never
        // asks for a rescue at all.
        let mut starting = unbound_child(300.0);
        starting.health = Health::Starting;
        let (_, effects) = step(
            &State::Starting {
                since: 0.0,
                attempts: 1,
            },
            &starting,
            300.0,
        );
        assert!(!acts_on_a_server(&effects), "{effects:?}");
    }

    #[test]
    fn safeguard_e_only_a_child_this_window_started_and_only_by_its_exact_pid() {
        // No child of ours: a server alive without the port is never a
        // "never bound" case, whatever its age.
        let mut not_ours = observation(Health::Absent);
        not_ours.known_pid = Some(4242);
        not_ours.known_alive = Some(true);
        not_ours.since_last_start = Some(500.0);
        assert_eq!(verdict(&not_ours), Verdict::Dead(DeadReason::ListenerLost));
        // A child of ours: the rescue names exactly its pid, and no other.
        let (_, seen) = run_ticks(
            State::Starting {
                since: 0.0,
                attempts: 1,
            },
            &unbound_child(65.0),
            65.0,
            10.0,
            10,
        );
        let asked: Vec<_> = seen
            .iter()
            .filter_map(|(_, effect)| match effect {
                Effect::Rescue { child_pid, .. } => Some(*child_pid),
                _ => None,
            })
            .collect();
        assert!(!asked.is_empty());
        assert!(asked.iter().all(|pid| *pid == Some(7001)), "{asked:?}");
    }

    #[test]
    fn safeguard_f_the_notification_and_the_log_say_what_was_stopped_and_why() {
        let mut done = observation(Health::Absent);
        done.rescue = RescueProgress::Done(RescueOutcome {
            child_pid: Some(7001),
            known_pid: None,
            ..outcome(
                RescueResult::PortFree,
                DeadReason::NeverBound,
                vec![StoppedServer {
                    pids: vec![7001, 7002],
                    exited_by_itself: false,
                }],
            )
        });
        let (state, effects) = step(&State::Rescuing { since: 60.0 }, &done, 90.0);
        assert!(matches!(state, State::Starting { .. }));
        assert!(effects.contains(&Effect::Spawn));
        let message = effects
            .iter()
            .find_map(|effect| match effect {
                Effect::Notify { message } => Some(message.clone()),
                _ => None,
            })
            .expect("every rescue ends in a notification");
        for needle in [
            "7001",
            "7002",
            "never opened the port",
            "stopped by process id",
            "24 s",
        ] {
            assert!(message.contains(needle), "{needle}: {message}");
        }
        // ...and the request itself is logged with its reason.
        let (_, effects) = step(
            &State::Confirming {
                reason: DeadReason::NeverBound,
                first: 15.0,
                checks: 5,
                since: 0.0,
            },
            &unbound_child(65.0),
            65.0,
        );
        assert!(
            effects.iter().any(|effect| matches!(
                effect,
                Effect::Log(line) if line.contains("never-bound") && line.contains("7001")
            )),
            "{effects:?}"
        );
    }

    #[test]
    fn row_12_a_refused_rescue_starts_nothing_and_asks_again() {
        for result in [RescueResult::Refused, RescueResult::Failed] {
            let mut refused = known(Listener::Free);
            refused.rescue = RescueProgress::Done(RescueOutcome {
                detail: "port 9999 is held by pid 31337".to_owned(),
                ..outcome(result, DeadReason::ListenerLost, Vec::new())
            });
            let (state, effects) = step(&State::Rescuing { since: 0.0 }, &refused, 20.0);
            assert!(matches!(state, State::Reconnecting { .. }), "{state:?}");
            assert!(!acts_on_a_server(&effects), "{effects:?}");
            assert!(effects.contains(&Effect::Restatus));
            assert!(effects.iter().any(|effect| matches!(
                effect,
                Effect::Log(line) if line.contains("31337")
            )));
            assert!(
                !effects
                    .iter()
                    .any(|effect| matches!(effect, Effect::Notify { .. }))
            );
        }
    }

    #[test]
    fn a_running_rescue_is_only_watched() {
        let mut running = known(Listener::Free);
        running.rescue = RescueProgress::Running;
        for fresh in [true, false] {
            running.fresh = fresh;
            let (state, effects) = step(&State::Rescuing { since: 0.0 }, &running, 12.0);
            assert_eq!(state, State::Rescuing { since: 0.0 });
            assert!(!effects.iter().any(Effect::acts), "{effects:?}");
            assert!(
                effects
                    .iter()
                    .any(|effect| matches!(effect, Effect::Show(Page::Rescuing { .. })))
            );
        }
    }

    #[test]
    fn a_dead_server_costs_one_rescue_then_a_spawn() {
        let mut gone = known(Listener::Free);
        gone.known_alive = Some(false);
        let (state, effects) = step(&State::Attached, &gone, 0.0);
        assert!(rescue_in(&effects).is_some());
        let mut running = gone.clone();
        running.rescue = RescueProgress::Running;
        running.since_rescue = Some(3.0);
        let (state, effects) = step(&state, &running, 3.0);
        assert!(!acts_on_a_server(&effects));
        let mut done = gone.clone();
        done.rescue = RescueProgress::Done(outcome(
            RescueResult::PortFree,
            DeadReason::ProcessGone,
            Vec::new(),
        ));
        done.since_rescue = Some(5.0);
        let (state, effects) = step(&state, &done, 5.0);
        assert_eq!(
            effects
                .iter()
                .filter(|effect| **effect == Effect::Spawn)
                .count(),
            1
        );
        assert!(matches!(state, State::Starting { .. }));
        let message = effects
            .iter()
            .find_map(|effect| match effect {
                Effect::Notify { message } => Some(message.clone()),
                _ => None,
            })
            .expect("a notification");
        assert!(message.contains("process 4242 has exited"), "{message}");
        assert!(message.contains("Nothing needed stopping"), "{message}");
    }

    #[test]
    fn an_old_wheel_without_rescue_still_starts() {
        let mut unsupported = known(Listener::Free);
        unsupported.rescue = RescueProgress::Done(outcome(
            RescueResult::Unsupported,
            DeadReason::ProcessGone,
            Vec::new(),
        ));
        let (state, effects) = step(&State::Rescuing { since: 0.0 }, &unsupported, 4.0);
        assert!(matches!(state, State::Starting { .. }));
        assert!(effects.contains(&Effect::Spawn));
        assert!(effects.iter().any(|effect| matches!(
            effect,
            Effect::Notify { message } if message.contains("older than 7.71.0")
        )));
    }

    #[test]
    fn the_2h22m_own_child_zombie_is_rescued_once() {
        // 2026-09-28: a server this window started lost its listener and sat
        // there, alive, for two hours and twenty-two minutes -- 783 ticks.
        let mut zombie = known(Listener::Free);
        zombie.child_alive = true;
        zombie.child_pid = Some(7001);
        zombie.child_ever_bound = true;
        zombie.since_last_start = Some(9_000.0);
        let mut state = State::Attached;
        let mut rescues = Vec::new();
        for tick in 0..783_u32 {
            let now = 10.0 * f64::from(tick);
            let (next, effects) = step(&state, &zombie, now);
            if let Some((known_pid, child_pid, reason)) = rescue_in(&effects) {
                rescues.push((now, known_pid, child_pid, reason));
                // From here the caller reports the rescue as running.
                zombie.rescue = RescueProgress::Running;
            }
            state = next;
        }
        assert_eq!(
            rescues,
            vec![(30.0, Some(4242), Some(7001), DeadReason::ListenerLost)],
            "one rescue, after the confirmation, naming the child by its pid"
        );
        assert!(matches!(state, State::Rescuing { .. }));
    }

    #[test]
    fn the_busy_header_changes_the_tray_line_only() {
        let mut plain = observation(Health::Healthy);
        plain.answer_pid = Some(4242);
        let mut busy = plain.clone();
        busy.busy_header = true;
        for state in all_states() {
            assert_eq!(
                step(&state, &plain, 1.0),
                step(&state, &busy, 1.0),
                "{}",
                state.name()
            );
        }
    }

    #[test]
    fn a_slow_servers_holder_is_re_read_at_most_every_reconnect_restatus_seconds() {
        let mut observation = slow();
        observation.since_restatus = Some(5.0);
        let (_, effects) = step(&State::Busy { since: 0.0 }, &observation, 10.0);
        assert!(!effects.contains(&Effect::Restatus), "{effects:?}");
        observation.since_restatus = Some(31.0);
        let (_, effects) = step(&State::Busy { since: 0.0 }, &observation, 40.0);
        assert!(effects.contains(&Effect::Restatus), "{effects:?}");
        // At once when the connect is refused: the port may have gone free.
        observation.since_restatus = Some(5.0);
        observation.probe_connected = false;
        let (_, effects) = step(&State::Busy { since: 0.0 }, &observation, 50.0);
        assert!(effects.contains(&Effect::Restatus), "{effects:?}");
    }

    #[test]
    fn a_mode_that_starts_nothing_announces_a_death_once_and_stops_nothing() {
        let mut gone = known(Listener::Free);
        gone.known_alive = Some(false);
        gone.facts.server_mode = "attach".to_owned();
        let (state, effects) = step(&State::Attached, &gone, 1.0);
        assert!(matches!(
            state,
            State::Blocked {
                reason: Blocked::NotOurServer { .. }
            }
        ));
        assert!(!acts_on_a_server(&effects), "{effects:?}");
        assert!(effects.iter().any(|effect| matches!(
            effect,
            Effect::AnnounceDead { message } if message.contains("process 4242 has exited")
                && message.contains("Server mode is attach")
        )));
        // Slow in attach mode is still only slow.
        let mut busy = slow();
        busy.facts.server_mode = "attach".to_owned();
        let (state, _) = step(&State::Attached, &busy, 1.0);
        assert!(matches!(state, State::Busy { .. }));
    }

    #[test]
    fn a_free_port_with_no_server_known_is_a_cold_start_as_before() {
        // Q4, unchanged: nothing was ever heard from, nothing listens, so the
        // first fresh tick starts a server -- no rescue to scan for.
        let cold = observation(Health::Absent);
        assert_eq!(verdict(&cold), Verdict::Free);
        let (state, effects) = step(&State::Booting, &cold, 0.0);
        assert!(effects.contains(&Effect::Spawn), "{effects:?}");
        assert!(rescue_in(&effects).is_none());
        assert!(matches!(state, State::Starting { .. }));
    }

    /// A tiny deterministic generator, so the property below is reproducible.
    struct Lcg(u64);

    impl Lcg {
        fn next(&mut self) -> u64 {
            self.0 = self
                .0
                .wrapping_mul(6_364_136_223_846_793_005)
                .wrapping_add(1_442_695_040_888_963_407);
            self.0 >> 33
        }

        fn pick<T: Clone>(&mut self, items: &[T]) -> T {
            items[usize::try_from(self.next()).unwrap_or(0) % items.len()].clone()
        }
    }

    #[test]
    fn no_sequence_without_nothing_listens_ever_spawns_or_rescues() {
        // THE property (decision R4): from any state, with the process alive
        // and the port held -- or a lookup that could not say -- no sequence
        // of observations that never contains "nothing listens" yields a
        // spawn or a rescue. 20,000 random walks of 60 ticks.
        let held = [
            Listener::Held {
                pid: Some(4242),
                mcc: Some(true),
            },
            Listener::Held {
                pid: Some(9999),
                mcc: Some(true),
            },
            Listener::Held {
                pid: Some(4242),
                mcc: None,
            },
            Listener::Held {
                pid: None,
                mcc: None,
            },
            Listener::Unknown,
        ];
        let mut random = Lcg(0x5EED_2026_1004);
        for walk in 0..20_000_u32 {
            let mut state = random.pick(&all_states());
            let mut now = 0.0;
            for _ in 0..60 {
                let mut observation = slow();
                observation.listener = random.pick(&held);
                observation.listener_fresh = random.pick(&[true, false]);
                observation.probe_connected = random.pick(&[true, false]);
                observation.known_pid = random.pick(&[Some(4242), None]);
                observation.known_alive = random.pick(&[Some(true), None]);
                observation.fresh = random.pick(&[true, true, false]);
                observation.child_alive = random.pick(&[true, false]);
                observation.child_pid = if observation.child_alive {
                    Some(7001)
                } else {
                    None
                };
                observation.child_ever_bound = random.pick(&[true, false]);
                observation.since_last_start = random.pick(&[None, Some(20.0), Some(500.0)]);
                observation.seconds_since_healthy = random.pick(&[None, Some(1.0), Some(900.0)]);
                observation.on_dashboard = random.pick(&[true, false]);
                observation.facts.server_mode =
                    random.pick(&["spawn", "spawn", "attach"]).to_owned();
                now += random.pick(&[1.0, 10.0, 30.0]);
                let (next, effects) = step(&state, &observation, now);
                assert!(
                    !acts_on_a_server(&effects),
                    "walk {walk}: {} + {observation:?} -> {effects:?}",
                    state.name()
                );
                state = next;
            }
        }
    }

    #[test]
    fn only_a_fresh_free_port_can_ever_reach_a_rescue() {
        // The converse, over every state and a sweep of facts: wherever a
        // rescue IS asked for, the observation said "nothing listens", read
        // fresh, with no handshake on that tick.
        let mut random = Lcg(0xD15C_0FFE);
        for _ in 0..20_000_u32 {
            let state = random.pick(&all_states());
            let mut observation = known(random.pick(&[
                Listener::Free,
                Listener::Unknown,
                Listener::Held {
                    pid: Some(4242),
                    mcc: Some(true),
                },
            ]));
            observation.listener_fresh = random.pick(&[true, false]);
            observation.probe_connected = random.pick(&[true, false]);
            observation.known_alive = random.pick(&[Some(true), Some(false), None]);
            observation.child_alive = random.pick(&[true, false]);
            observation.child_pid = observation.child_alive.then_some(7001);
            observation.child_ever_bound = random.pick(&[true, false]);
            observation.since_last_start = random.pick(&[None, Some(70.0), Some(500.0)]);
            let (_, effects) = step(&state, &observation, 1_000.0);
            if rescue_in(&effects).is_some() {
                assert_eq!(observation.listener, Listener::Free);
                assert!(observation.listener_fresh);
                assert!(!observation.probe_connected);
            }
        }
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
            seconds_ago: Some(0.5),
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
            seconds_ago: Some(0.5),
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
    fn a_silent_mcc_holder_is_slow_and_is_never_started_over() {
        // Until 7.71.0 this test was the opposite: "6.59.0's
        // SERVER_PORT_TAKEOVER kills the stale holder from inside mcc-server,
        // so the shell's force-start is simply spawn". That spawn is how one
        // late answer on 2026-09-28 at 14:40 ended with a working server
        // killed. A silent My Claude Code that still holds its port is SLOW
        // (decision R4): it is never started over, and a server this window
        // starts carries `--no-port-takeover` and could not take it anyway.
        let mut stale = observation(Health::Absent);
        stale.holder = Holder::OursStale;
        stale.listener = Listener::Held {
            pid: Some(4242),
            mcc: Some(true),
        };
        let (state, effects) = step(&State::Reconnecting { since: 0.0 }, &stale, 5.0);
        assert!(!effects.contains(&Effect::Spawn), "{effects:?}");
        assert!(matches!(state, State::Busy { .. }), "{state:?}");
    }

    #[test]
    fn a_foreign_holder_inside_the_grace_window_is_not_a_conflict_yet() {
        let mut fresh = observation(Health::Absent);
        fresh.holder = Holder::Foreign;
        fresh.listener = Listener::Held {
            pid: Some(4242),
            mcc: Some(false),
        };
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
        // 7.71.0: the first 15 s after a start are the new server's own
        // (user answer 3), so the ten-second backoff is now always inside the
        // grace, and the next start comes when the grace ends.
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
        assert!(!effects.contains(&Effect::Spawn), "{effects:?}");
        let mut grace_over = just_started.clone();
        grace_over.since_last_start = Some(15.0);
        let (_, effects) = step(
            &State::Starting {
                since: 0.0,
                attempts: 1,
            },
            &grace_over,
            15.0,
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
            seconds_ago: Some(1.0),
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

    // -- U1: never park during an update (2026-09-10) ----------------------

    /// Every page whose job is to tell the user that something is wrong.
    fn reports_a_problem(page: &Page) -> bool {
        matches!(
            page,
            Page::Error { .. }
                | Page::ServerFailed { .. }
                | Page::PortConflict { .. }
                | Page::NotOurServer { .. }
        )
    }

    #[test]
    fn no_page_that_reports_a_problem_is_painted_without_asking_again() {
        // THE property U1 exists for, and the one 6.66.1 did not have.
        //
        // On 2026-09-09 at 04:03 the window painted *My Claude Code could not
        // start -- mcc-desktop --print-status exited with 1 ... No module
        // named 'my_claude_code'* and then asked nothing, ever again: the
        // `Blocked::Status` arm returned `Show(Page::Error)` alone,
        // `needs_restatus` is evaluated after that early return, and the
        // loop's own re-read was gated on never having read a document. The
        // user watched it for five minutes with a healthy machine underneath.
        //
        // So: a page that reports a problem is a report, never a verdict. On
        // every fresh tick it is accompanied by a question -- `Restatus` -- or
        // by the act that would answer it, `Spawn` or `Install`.
        for state in all_states() {
            for (name, observation) in all_observations() {
                if !observation.fresh {
                    continue;
                }
                let (_, effects) = step(&state, &observation, 5.0);
                let painted = effects.iter().any(|effect| match effect {
                    Effect::Show(page) => reports_a_problem(page),
                    _ => false,
                });
                if !painted {
                    continue;
                }
                assert!(
                    effects.iter().any(|effect| matches!(
                        effect,
                        Effect::Restatus | Effect::Spawn | Effect::Install
                    )),
                    "{} + {name} reported a problem and asked nothing: {effects:?}",
                    state.name()
                );
            }
        }
    }

    #[test]
    fn every_state_asks_or_acts_on_a_fresh_tick_where_nothing_answers() {
        // The generalisation of the same rule. If no server is answering at
        // all, then whatever the window is showing, that tick must reach
        // outside itself: re-read the document, start the server, or install
        // the thing that is missing. A tick that only repaints is a window
        // waiting for something that will not arrive.
        //
        // One exception, and it is the act itself: while a rescue is running
        // the window is waiting on the process it started, which is already
        // reaching outside (and is bounded by its own wall).
        for state in all_states() {
            for (name, observation) in all_observations() {
                if !observation.fresh || observation.health != Health::Absent {
                    continue;
                }
                let rescue_running = matches!(state, State::Rescuing { .. })
                    && matches!(observation.rescue, RescueProgress::Running);
                if rescue_running {
                    continue;
                }
                let (_, effects) = step(&state, &observation, 5.0);
                assert!(
                    effects.iter().any(Effect::acts),
                    "{} + {name} did nothing on a fresh tick with a dead server: {effects:?}",
                    state.name()
                );
            }
        }
    }

    #[test]
    fn an_unreadable_status_asks_again_on_every_fresh_tick() {
        // C2, at file:line. `Blocked::Install` has pushed a `Restatus` since
        // 6.66.0 (controller.rs:508); `Blocked::Status` did not, and that
        // asymmetry was the bug.
        let mut unreadable = observation(Health::Absent);
        unreadable.status = StatusHealth::Unreadable {
            detail: "mcc-desktop --print-status exited with 1.".to_owned(),
        };
        // A child is alive, so nothing on this tick can spawn its way out --
        // the only edge left is the question.
        unreadable.child_alive = true;

        let mut state = State::Booting;
        for tick in 0..5 {
            let (next, effects) = step(&state, &unreadable, f64::from(tick) * 10.0);
            assert!(
                effects.contains(&Effect::Restatus),
                "tick {tick} from {} asked nothing: {effects:?}",
                state.name()
            );
            assert!(matches!(
                next,
                State::Blocked {
                    reason: Blocked::Status { .. }
                }
            ));
            state = next;
        }

        // ...and a paint tick in between neither asks nor moves.
        let mut paint = unreadable.clone();
        paint.fresh = false;
        let (next, effects) = step(&state, &paint, 60.0);
        assert_eq!(next, state);
        assert!(!effects.iter().any(Effect::acts), "{effects:?}");

        // The document becomes readable again: one tick, and the window is
        // back in the ordinary world.
        let mut readable = observation(Health::Healthy);
        readable.status = StatusHealth::Ok;
        let (next, effects) = step(&state, &readable, 70.0);
        assert_eq!(next, State::Attached);
        assert!(
            effects
                .iter()
                .any(|effect| matches!(effect, Effect::Attach { .. }))
        );
    }

    #[test]
    fn a_probe_failure_while_the_helper_is_settling_is_an_updating_page_not_an_error() {
        // C3 / decision Q3. Both spellings the replacement window produces:
        // a status run that failed outright, and a shim with no environment
        // behind it (which `process.rs` now calls `NotInstalled`).
        for status in [
            StatusHealth::Unreadable {
                detail: "mcc-desktop --print-status exited with 1. \
                         ModuleNotFoundError: No module named 'my_claude_code'"
                    .to_owned(),
            },
            StatusHealth::NotInstalled,
        ] {
            for helper in [
                Helper::Alive {
                    stage: Some("Updating to 6.70.0... (installer running, 46 s)".to_owned()),
                },
                Helper::Finished {
                    stage: Some("done".to_owned()),
                    seconds_ago: Some(29.0),
                },
            ] {
                let mut mid_update = observation(Health::Absent);
                mid_update.status = status.clone();
                mid_update.helper = helper.clone();
                let (next, effects) = step(&State::Attached, &mid_update, 5.0);

                assert!(
                    matches!(next, State::Updating { .. }),
                    "{status:?} + {helper:?} -> {next:?}"
                );
                let page = effects
                    .iter()
                    .find_map(|effect| match effect {
                        Effect::Show(page) => Some(page.clone()),
                        _ => None,
                    })
                    .expect("a page");
                assert!(
                    matches!(page, Page::Updating { .. }),
                    "{status:?} + {helper:?} painted {page:?}"
                );
                let json = serde_json::to_string(&page).expect("a page serializes");
                assert!(json.contains("the environment is being replaced"), "{json}");
                assert!(!json.contains("could not start"), "{json}");
                // ...and it keeps asking.
                assert!(effects.contains(&Effect::Restatus), "{effects:?}");
            }
        }
    }

    #[test]
    fn a_settling_window_never_starts_an_installer_of_its_own() {
        // The other half of Q3, and the 2026-09-07 incident it prevents: the
        // shell's own bounded install (6.66.0) fired into the tool directory
        // uv was in the middle of rewriting, and the helper lost all five of
        // its attempts. Ten ticks, from every state, and not one install.
        for helper in [
            Helper::Alive {
                stage: Some("installing".to_owned()),
            },
            Helper::Finished {
                stage: Some("done".to_owned()),
                seconds_ago: Some(1.0),
            },
            Helper::Finished {
                stage: Some("installed".to_owned()),
                seconds_ago: Some(HELPER_SETTLE_SECONDS - 0.1),
            },
        ] {
            let mut replacing = observation(Health::Absent);
            replacing.status = StatusHealth::NotInstalled;
            replacing.helper = helper.clone();
            for start in all_states() {
                let mut state = start.clone();
                for tick in 0..10 {
                    let (next, effects) = step(&state, &replacing, f64::from(tick) * 10.0);
                    assert!(
                        !effects.contains(&Effect::Install),
                        "{} installed over a live update ({helper:?})",
                        start.name()
                    );
                    assert!(
                        !effects.contains(&Effect::Spawn),
                        "{} spawned during a replacement ({helper:?})",
                        start.name()
                    );
                    state = next;
                }
                assert!(matches!(state, State::Updating { .. }), "{state:?}");
            }
        }
    }

    #[test]
    fn a_helper_that_finished_long_ago_is_not_a_settling_window() {
        // The receipt is truncated only when a NEW episode starts, so the
        // `done` record from the last update is the last line in the file for
        // ever. If an old record counted as settling, the genuine "could not
        // start" page would never be shown again on any machine that had ever
        // updated -- a worse bug than the one being fixed.
        let mut old = observation(Health::Absent);
        old.status = StatusHealth::NotInstalled;
        old.helper = Helper::Finished {
            stage: Some("done".to_owned()),
            seconds_ago: Some(HELPER_SETTLE_SECONDS + 0.1),
        };
        assert!(!environment_may_be_replaced(&old));
        let (next, effects) = step(&State::Booting, &old, 1.0);
        assert!(effects.contains(&Effect::Install), "{effects:?}");
        assert_eq!(next, State::Installing { attempts: 1 });

        // ...and so is a record whose age cannot be told at all (a receipt
        // from 6.58.2, which carried no `at`, on a filesystem whose mtime is
        // also unavailable).
        let mut undated = old.clone();
        undated.helper = Helper::Finished {
            stage: Some("done".to_owned()),
            seconds_ago: None,
        };
        assert!(!environment_may_be_replaced(&undated));
        let (_, effects) = step(&State::Booting, &undated, 1.0);
        assert!(effects.contains(&Effect::Install), "{effects:?}");
    }

    #[test]
    fn the_park_of_2026_09_09_at_04_03_replayed_tick_by_tick() {
        // The user's own episode, as the sequence of observations the window
        // actually saw, at the times it saw them. Live 6.60.2 -> 6.65.0:
        //
        //   03:59:54  Update pressed; helper writes `waiting-for-parent`
        //   04:00:16  helper writes `installing`; uv empties the environment
        //   04:01:58  helper writes `done`; the server is up
        //   04:02:00+ the window showed "My Claude Code could not start --
        //             mcc-desktop --print-status exited with 1 ...
        //             ModuleNotFoundError: No module named 'my_claude_code'"
        //             for five minutes and never attached.
        //
        // Every assertion below is about what the window SHOWS, because that
        // is what the user reported.
        let mut state = State::Attached;
        let mut painted: Vec<(&str, Page)> = Vec::new();
        let mut installs = 0_usize;
        let mut spawns = 0_usize;

        let tick = |state: &mut State,
                    label: &'static str,
                    observation: &Observation,
                    now: f64,
                    painted: &mut Vec<(&'static str, Page)>,
                    installs: &mut usize,
                    spawns: &mut usize| {
            let (next, effects) = step(state, observation, now);
            *state = next;
            for effect in &effects {
                match effect {
                    Effect::Show(page) => painted.push((label, page.clone())),
                    Effect::Install => *installs += 1,
                    Effect::Spawn => *spawns += 1,
                    _ => {}
                }
            }
        };

        // 03:59:54 -- the helper is alive and the old server still answers.
        let mut waiting = observation(Health::Healthy);
        waiting.helper = Helper::Alive {
            stage: Some("Updating to 6.65.0... (installer running, 3 s)".to_owned()),
        };
        tick(
            &mut state,
            "03:59:54 waiting-for-parent",
            &waiting,
            0.0,
            &mut painted,
            &mut installs,
            &mut spawns,
        );

        // 04:00:16 -> 04:01:58 -- the server is gone and so is the
        // environment. This is the whole of the outage: 102 seconds of
        // `ModuleNotFoundError`, ten ticks of it.
        let mut replacing = observation(Health::Absent);
        replacing.status = StatusHealth::NotInstalled;
        replacing.helper = Helper::Alive {
            stage: Some("Updating to 6.65.0... (installer running, 46 s)".to_owned()),
        };
        for step_number in 0..10 {
            tick(
                &mut state,
                "04:00:16 installing",
                &replacing,
                30.0 + f64::from(step_number) * 10.0,
                &mut painted,
                &mut installs,
                &mut spawns,
            );
        }

        // 04:01:58 -- the helper is done, and uv is still putting the shims
        // back, so `--print-status` fails for a few seconds more. THIS is the
        // tick 6.66.1 turned into a permanent verdict.
        let mut settling = observation(Health::Absent);
        settling.status = StatusHealth::Unreadable {
            detail: "mcc-desktop --print-status exited with 1. ModuleNotFoundError: \
                     No module named 'my_claude_code'"
                .to_owned(),
        };
        settling.helper = Helper::Finished {
            stage: Some("done".to_owned()),
            seconds_ago: Some(2.0),
        };
        tick(
            &mut state,
            "04:01:58 done, settling",
            &settling,
            130.0,
            &mut painted,
            &mut installs,
            &mut spawns,
        );

        // 04:02:04 -- the environment is back, the server is not running yet
        // (the helper ran with --no-restart because the window was watching).
        let mut back = observation(Health::Absent);
        back.helper = Helper::Finished {
            stage: Some("done".to_owned()),
            seconds_ago: Some(8.0),
        };
        tick(
            &mut state,
            "04:02:04 environment back",
            &back,
            136.0,
            &mut painted,
            &mut installs,
            &mut spawns,
        );
        assert_eq!(spawns, 1, "the window must start the server, exactly once");
        assert!(matches!(state, State::Starting { .. }), "{state:?}");

        // 04:02:07 -- the server answers.
        tick(
            &mut state,
            "04:02:07 healthy",
            &observation(Health::Healthy),
            139.0,
            &mut painted,
            &mut installs,
            &mut spawns,
        );
        assert_eq!(state, State::Attached);

        // Now the assertions the user's report is made of.
        assert_eq!(installs, 0, "the window must not install during an update");
        for (label, page) in &painted {
            assert!(
                !reports_a_problem(page),
                "at {label} the window said something was wrong: {page:?}"
            );
            let json = serde_json::to_string(page).expect("a page serializes");
            assert!(
                !json.contains("could not start") && !json.contains("ModuleNotFoundError"),
                "at {label}: {json}"
            );
        }
        // ...and it did say what was happening, all the way through.
        assert!(
            painted
                .iter()
                .filter(|(_, page)| matches!(page, Page::Updating { .. }))
                .count()
                >= 11,
            "{painted:?}"
        );
    }

    #[test]
    fn without_a_helper_a_broken_environment_is_still_installed_over() {
        // The regression guard for 6.66.0's bounded install: the settle window
        // narrows the not-installed path, it does not remove it. Same
        // observation as the update case, with nothing running.
        let mut broken = observation(Health::Absent);
        broken.status = StatusHealth::NotInstalled;
        broken.helper = Helper::None;
        let (next, effects) = step(&State::Attached, &broken, 1.0);
        assert!(effects.contains(&Effect::Install), "{effects:?}");
        assert_eq!(next, State::Installing { attempts: 1 });
    }

    // -- 6.71.0: "see everything happening during an update" -----------------

    /// The observation a window has mid-update: an installer alive, three
    /// stages recorded, and a transcript with something in it.
    fn updating_observation(tail: &[&str]) -> Observation {
        let mut observation = observation(Health::Absent);
        observation.helper = Helper::Alive {
            stage: Some("Updating to 6.71.0... (installer running, 25 s).".to_owned()),
        };
        observation.update = UpdateNarration {
            stages: vec![
                UpdateStage {
                    stage: "waiting-for-parent".to_owned(),
                    message: Some("Waiting for the running server to stop.".to_owned()),
                    at: Some("2026-09-11T08:21:14.9680000Z".to_owned()),
                    elapsed_seconds: Some(0.0),
                },
                UpdateStage {
                    stage: "stopping".to_owned(),
                    message: Some("The server has stopped. Preparing to install.".to_owned()),
                    at: Some("2026-09-11T08:21:37.7250000Z".to_owned()),
                    elapsed_seconds: Some(22.757),
                },
                UpdateStage {
                    stage: "installing".to_owned(),
                    message: Some("Installing the new version.".to_owned()),
                    at: Some("2026-09-11T08:21:39.7250000Z".to_owned()),
                    elapsed_seconds: Some(24.757),
                },
            ],
            log_path: Some("C:/config/updates/install-20260911-082114.log".to_owned()),
            log_tail: tail.iter().map(|line| (*line).to_owned()).collect(),
            helper_pid: Some(11372),
            elapsed_seconds: Some(94.5),
        };
        observation
    }

    fn updating_page_of(effects: &[Effect]) -> Page {
        effects
            .iter()
            .find_map(|effect| match effect {
                Effect::Show(page @ Page::Updating { .. }) => Some(page.clone()),
                _ => None,
            })
            .expect("an Updating page")
    }

    #[test]
    fn the_updating_page_draws_the_stage_timeline_with_the_writers_own_stamps() {
        let observation = updating_observation(&["Resolved 101 packages"]);
        let (_, effects) = step(&State::Booting, &observation, 0.0);
        let Page::Updating {
            stages,
            elapsed,
            log_path,
            log_tail,
            helper,
            ..
        } = updating_page_of(&effects)
        else {
            unreachable!("matched above");
        };
        assert_eq!(stages.len(), 3, "{stages:?}");
        assert_eq!(stages[0].at.as_deref(), Some("08:21:14"));
        assert_eq!(
            stages[0].message.as_deref(),
            Some("Waiting for the running server to stop.")
        );
        // A finished stage is timed by the gap to the next record...
        assert_eq!(stages[0].took.as_deref(), Some("23 s"));
        assert_eq!(stages[1].took.as_deref(), Some("2 s"));
        // ...and the one still running by the gap to now, which is the number
        // that answers "has it stopped?".
        assert!(stages[2].current, "{stages:?}");
        assert_eq!(stages[2].took.as_deref(), Some("1 m 10 s"));
        assert!(!stages[0].current);
        assert_eq!(elapsed.as_deref(), Some("1 m 34 s"));
        assert_eq!(
            log_path.as_deref(),
            Some("C:/config/updates/install-20260911-082114.log")
        );
        assert_eq!(log_tail, vec!["Resolved 101 packages"]);
        assert_eq!(helper.as_deref(), Some("installer pid 11372, running"));
    }

    #[test]
    fn the_page_shown_while_the_environment_is_gone_shows_the_same_timeline() {
        // The minute the user most wants to see something happening is the one
        // in which `mcc-desktop` cannot answer. Losing the narration there
        // would be losing it exactly when it is needed.
        let mut observation = updating_observation(&["Preparing packages"]);
        observation.status = StatusHealth::Unreadable {
            detail: "mcc-desktop --print-status exited with 1. No module named 'my_claude_code'"
                .to_owned(),
        };
        let (state, effects) = step(&State::Booting, &observation, 0.0);
        assert!(matches!(state, State::Updating { .. }), "{state:?}");
        let Page::Updating {
            message,
            stages,
            log_tail,
            log_path,
            ..
        } = updating_page_of(&effects)
        else {
            unreachable!("matched above");
        };
        assert!(
            message.contains("environment is being replaced"),
            "{message}"
        );
        assert_eq!(stages.len(), 3);
        assert_eq!(log_tail, vec!["Preparing packages"]);
        assert!(log_path.is_some());
    }

    #[test]
    fn an_updating_window_repaints_when_the_transcript_grows() {
        // The whole of "refreshed every tick": two paint ticks in the same
        // state, one line of installer output apart, must not produce the same
        // page -- or the window would be a screenshot of an update rather than
        // a view of one.
        let state = State::Updating { since: 0.0 };
        let mut first = updating_observation(&["Resolved 101 packages"]);
        first.fresh = false;
        let (_, before) = step(&state, &first, 5.0);

        let mut second = updating_observation(&["Resolved 101 packages", "Prepared 44 packages"]);
        second.fresh = false;
        let (_, after) = step(&state, &second, 6.0);

        assert_ne!(before, after, "a grown transcript must reach the window");
        let Page::Updating { log_tail, .. } = updating_page_of(&after) else {
            unreachable!("matched above");
        };
        assert_eq!(log_tail.len(), 2, "{log_tail:?}");
        assert_eq!(
            log_tail.last().map(String::as_str),
            Some("Prepared 44 packages")
        );
    }

    #[test]
    fn a_receipt_with_no_elapsed_times_is_drawn_without_durations() {
        // A helper from 6.70.1 or earlier records no elapsed seconds. The
        // timeline still draws -- it just does not invent the numbers.
        let mut observation = updating_observation(&[]);
        for stage in &mut observation.update.stages {
            stage.elapsed_seconds = None;
        }
        observation.update.elapsed_seconds = None;
        let (_, effects) = step(&State::Booting, &observation, 0.0);
        let Page::Updating {
            stages, elapsed, ..
        } = updating_page_of(&effects)
        else {
            unreachable!("matched above");
        };
        assert_eq!(stages.len(), 3);
        assert!(
            stages.iter().all(|stage| stage.took.is_none()),
            "{stages:?}"
        );
        assert_eq!(elapsed, None);
    }

    #[test]
    fn an_ordinary_tick_carries_no_update_narration_at_all() {
        // There is no receipt on the overwhelming majority of ticks, and the
        // narration must cost nothing and say nothing then.
        let mut observation = observation(Health::Healthy);
        observation.facts.admin_url = "http://localhost/admin".to_owned();
        let (state, effects) = step(&State::Booting, &observation, 0.0);
        assert_eq!(state, State::Attached);
        assert!(
            !effects
                .iter()
                .any(|effect| matches!(effect, Effect::Show(Page::Updating { .. }))),
            "{effects:?}"
        );
        assert_eq!(observation.update, UpdateNarration::default());
    }

    #[test]
    fn a_duration_is_worded_for_the_length_an_update_actually_takes() {
        assert_eq!(duration(0.0), "0 s");
        assert_eq!(duration(23.4), "23 s");
        assert_eq!(duration(59.9), "60 s");
        assert_eq!(duration(60.0), "1 m 00 s");
        assert_eq!(duration(94.5), "1 m 34 s");
        assert_eq!(duration(766.6), "12 m 47 s");
        // Never negative, whatever a clock that moved backwards produces.
        assert_eq!(duration(-5.0), "0 s");
    }

    #[test]
    fn the_helper_phrase_says_running_or_finished_and_nothing_when_unknown() {
        let mut observation = updating_observation(&[]);
        assert_eq!(
            helper_phrase(&observation).as_deref(),
            Some("installer pid 11372, running")
        );
        observation.helper = Helper::Finished {
            stage: Some("done".to_owned()),
            seconds_ago: Some(2.0),
        };
        assert_eq!(
            helper_phrase(&observation).as_deref(),
            Some("installer pid 11372, finished")
        );
        observation.update.helper_pid = None;
        assert_eq!(helper_phrase(&observation), None);
        observation.helper = Helper::Alive { stage: None };
        assert_eq!(
            helper_phrase(&observation).as_deref(),
            Some("an installer is running")
        );
    }
}
