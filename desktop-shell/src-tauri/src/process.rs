//! The four processes this shell ever starts, and nothing else.
//!
//! 1. `mcc-desktop --print-status`, read for its stdout. This is the only way
//!    the shell learns where anything is (C1).
//! 2. `mcc-server`, started when -- and only when -- the ladder says `Start`.
//! 3. The projects own install script, when `mcc-desktop` is not on `PATH`
//!    (decision Q4).
//! 4. `mcc-desktop --ensure-shell`, when the tag compiled into this binary
//!    disagrees with the tag the status document pins (BUG-0, decision Q5).
//!    Note what that is and is not: this window does not download, verify or
//!    choose anything -- it asks the Python side to, and reads the one JSON
//!    line it prints. C5 stands.
//!
//! It never takes `desktop.lock`, never writes `desktop.json`, and never
//! registers autostart (C4). Every one of those stays Pythons.

use std::collections::VecDeque;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex, OnceLock, mpsc};
use std::time::{Duration, Instant};

use crate::install::InstallCommand;

/// Overrides the `mcc-desktop` command. Set to an absolute path, or to a
/// script, when running the release smoke against a scratch install so a real
/// window can be exercised without reading the developers own configuration.
pub const DESKTOP_COMMAND_ENV: &str = "MCC_SHELL_DESKTOP_COMMAND";

/// Overrides the `mcc-server` command, for the same reason.
pub const SERVER_COMMAND_ENV: &str = "MCC_SHELL_SERVER_COMMAND";

const DESKTOP_COMMAND: &str = "mcc-desktop";
const SERVER_COMMAND: &str = "mcc-server";

/// The flag that opts this window into every server state the wheel can
/// report. Today that means `draining`: MCC's own server on the port, refusing
/// everything while it finishes stopping.
///
/// It is passed unconditionally because this build has a branch for that
/// value. A window built before it existed does not pass the flag and is
/// answered with the three presences it was written against, so a new wheel
/// never hands an old window a state it would render as an error page. See
/// `cli/desktop_status.py`'s module docstring for the whole argument.
const PRESENCE_V2_FLAG: &str = "--presence-v2";

/// How long `mcc-desktop --print-status` may take before this window stops
/// waiting on it.
///
/// The Python side bounds its own work -- one 1.5s loopback probe, and
/// otherwise a path read -- so in practice this expires only when the *process*
/// cannot make progress: a cold shim being scanned by antivirus, a `.env` on a
/// network drive that has gone away, an interpreter paging in on a machine
/// under load. `Command::output()` has no wall of its own, and this call runs
/// on the ladder thread, so a wedged child used to block every page update in
/// the window including the error page that would have explained it.
///
/// Fifteen seconds is chosen to be far longer than the operation can honestly
/// take and far shorter than a person will sit in front of a frozen window.
/// It is not a policy the operator tunes: it is the difference between a
/// window that reports a problem and a window that hangs.
///
/// From 6.61.0 it is only the *default*: `status_wall_seconds` in the status
/// document overrides it (audit S5.4 -- this decides whether a slow machine
/// gets a window at all, which is a property of the machine and not of this
/// binary). The constant stays as what 6.61.0 ships with, for the one release
/// in which the shell only tolerates the key.
pub const DEFAULT_STATUS_WALL: Duration = Duration::from_secs(15);

/// Why `mcc-desktop --print-status` did not produce a document.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StatusRunError {
    /// There is no `mcc-desktop` to run. Decided by [`command_is_installed`]
    /// -- a file in `uv tool dir --bin`, then `PATH` -- and never by a spawn
    /// error alone. A spawn error is a question about the PATH of a process
    /// that was started before the installer ran, which is exactly the
    /// mistake 6.64.0 took out of `install.ps1`.
    NotInstalled,
    /// The file is there and running it is broken -- a uv trampoline whose
    /// environment was deleted, or any non-zero exit with nothing on stdout.
    /// Treated as not-installed by the controller, because the remedy is the
    /// same installer and the state is indistinguishable to a user.
    Broken { detail: String },
    /// It ran and failed.
    Failed { code: Option<i32>, stderr: String },
    /// It could not be run for some other reason.
    Unrunnable(String),
}

/// The signatures of a uv shim whose environment is gone.
///
/// Measured, not guessed. Two different machines produce two different
/// spellings of the same fact, and until 6.70.0 only the first was listed:
///
/// * with `<UV_TOOL_DIR>/my-claude-code` **deleted**, the shim exits 1 having
///   printed `failed to canonicalize script path`, and `uv tool list` says
///   `No tools installed` -- the receipt lives inside the directory that was
///   removed;
/// * while `uv tool install --force` is **replacing** that directory, the
///   launcher survives (on Windows uv copies shims rather than symlinking
///   them) and the interpreter behind it does not. It exits 1 with
///   `ModuleNotFoundError: No module named 'my_claude_code'` -- and, for the
///   first second of the window, with a *dependency's* name instead, because
///   uv tears the dependencies down first. Measured 2026-09-10 with a scratch
///   `UV_TOOL_DIR`, polling every 250 ms:
///   `+7.17 s No module named 'annotated_types'`,
///   `+7.99 s No module named 'my_claude_code'`, `+9.36 s` the shim file
///   itself gone, `+14.5 s` installed and answering again.
///
/// That second spelling is the whole of the user's 2026-09-09 04:03 report: it
/// matched nothing here, so a probe during the replacement window became
/// `StatusHealth::Unreadable` rather than "the install is incomplete", and the
/// window sat on *My Claude Code could not start* for five minutes. To a user
/// both machines look the same, and the remedy for both is the same installer
/// -- unless an update helper is alive, in which case the remedy is to wait,
/// which is `controller::environment_may_be_replaced`.
const BROKEN_SHIM_MARKERS: &[&str] = &[
    // Broadened from `failed to canonicalize script path`: newer uv
    // trampolines canonicalize the interpreter and the base too, and the
    // sentence continues differently in each case.
    "failed to canonicalize",
    "trampoline",
    "no tools installed",
    "modulenotfounderror",
    "no module named",
];

/// Whether this stderr is a broken shim rather than a program complaining.
pub fn looks_like_a_broken_shim(stderr: &str) -> bool {
    let lowered = stderr.to_lowercase();
    BROKEN_SHIM_MARKERS
        .iter()
        .any(|marker| lowered.contains(marker))
}

/// The directory `uv` puts console scripts in, asked once per session.
///
/// `None` when uv cannot be run or says nothing useful. Cached because the
/// answer is a property of the machine, and cleared by
/// [`forget_uv_tool_bin_dir`] after an install -- which is the one moment it
/// can change.
static UV_TOOL_BIN_DIR: OnceLock<Mutex<Option<Option<PathBuf>>>> = OnceLock::new();

/// How long `uv tool dir --bin` may take. It reads a configuration file and
/// prints a path; anything slower than this is a machine problem, and the
/// fallback (ask PATH) is always available.
const UV_DIR_WALL: Duration = Duration::from_secs(10);

fn uv_cache() -> &'static Mutex<Option<Option<PathBuf>>> {
    UV_TOOL_BIN_DIR.get_or_init(|| Mutex::new(None))
}

/// Forget the cached answer, so the next lookup asks uv again.
pub fn forget_uv_tool_bin_dir() {
    if let Ok(mut guard) = uv_cache().lock() {
        *guard = None;
    }
}

/// `uv tool dir --bin`, or `None`.
///
/// This is the question `cli/tool_paths.py` already asks on the Python side,
/// and its docstring says why: it is "the difference between the desktop app
/// finding mcc-server/mcc-desktop and reporting that My Claude Code is not
/// installed on a machine where it plainly is". A window's `PATH` is frozen
/// at launch, and `uv tool install` writes both the bin directory and (with
/// `UV_TOOL_UPDATE_SHELL`) the *user's* `PATH` -- neither of which a running
/// process ever sees. So the first install from this window could never be
/// noticed by it.
pub fn uv_tool_bin_dir() -> Option<PathBuf> {
    if let Ok(guard) = uv_cache().lock() {
        if let Some(cached) = guard.as_ref() {
            return cached.clone();
        }
    }
    let answer = ask_uv_for_its_bin_dir();
    if let Ok(mut guard) = uv_cache().lock() {
        *guard = Some(answer.clone());
    }
    answer
}

fn ask_uv_for_its_bin_dir() -> Option<PathBuf> {
    let raw = run_for_stdout(
        "uv",
        &["tool".to_owned(), "dir".to_owned(), "--bin".to_owned()],
        UV_DIR_WALL,
    )
    .ok()?;
    let line = raw.lines().map(str::trim).find(|line| !line.is_empty())?;
    let path = PathBuf::from(line);
    if path.is_dir() { Some(path) } else { None }
}

/// The launcher file for `name` in uv's bin directory, if there is one.
fn launcher_in_the_uv_bin_dir(name: &str) -> Option<PathBuf> {
    let directory = uv_tool_bin_dir()?;
    launcher_in(&directory, name)
}

/// Split out for the test: does `directory` hold a launcher called `name`?
pub fn launcher_in(directory: &Path, name: &str) -> Option<PathBuf> {
    for suffix in ["", ".exe", ".cmd", ".bat"] {
        let candidate = directory.join(format!("{name}{suffix}"));
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

/// Whether `name` can be run on this machine, asked of the *filesystem*.
///
/// uv's bin directory first, then `PATH` as a fallback for an install that
/// did not come from uv. Only when neither answers is MCC not installed.
pub fn command_is_installed(name: &str) -> bool {
    if launcher_in_the_uv_bin_dir(name).is_some() {
        return true;
    }
    on_path(name)
}

/// A `which`-shaped PATH search, without a dependency.
pub fn on_path(name: &str) -> bool {
    let Some(paths) = std::env::var_os("PATH") else {
        return false;
    };
    std::env::split_paths(&paths).any(|directory| launcher_in(&directory, name).is_some())
}

/// A command line, split. `mcc-desktop` may be overridden with something that
/// takes arguments of its own, so the override is split on whitespace.
fn resolve(env_key: &str, fallback: &str) -> (String, Vec<String>) {
    resolve_in(
        launcher_in_the_uv_bin_dir(fallback).as_deref(),
        env_key,
        fallback,
    )
}

/// The same, with the bin-directory answer supplied. Split for the test,
/// which must not read whatever `uv` happens to say on this machine.
fn resolve_in(installed: Option<&Path>, env_key: &str, fallback: &str) -> (String, Vec<String>) {
    match std::env::var(env_key) {
        Ok(raw) if !raw.trim().is_empty() => {
            let mut parts = raw.split_whitespace().map(str::to_owned);
            let program = parts.next().unwrap_or_else(|| fallback.to_owned());
            (program, parts.collect())
        }
        // The absolute path in uv's bin directory, when there is one. A bare
        // name is resolved against a `PATH` this process inherited when it
        // launched, which is precisely the `PATH` that cannot contain an
        // install this window has just performed.
        _ => (
            installed.map_or_else(
                || fallback.to_owned(),
                |path| path.to_string_lossy().into_owned(),
            ),
            Vec::new(),
        ),
    }
}

#[cfg(windows)]
fn hide_console(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    // CREATE_NO_WINDOW. Without it every poll of the status flashes a console
    // window over whatever the user is doing.
    command.creation_flags(0x0800_0000);
}

#[cfg(not(windows))]
fn hide_console(_command: &mut Command) {}

/// Run `mcc-desktop --print-status` and return its stdout verbatim.
pub fn print_status() -> Result<String, StatusRunError> {
    print_status_within(DEFAULT_STATUS_WALL)
}

/// The same, with the wall spelled out. Split for the test, which cannot wait
/// fifteen seconds to prove that waiting ends.
pub fn print_status_within(wall: Duration) -> Result<String, StatusRunError> {
    // The verdict comes from the filesystem, before anything is spawned.
    // A `NotFound` from `spawn` answers a question about this process's own
    // frozen PATH; `command_is_installed` asks uv where it puts launchers.
    let overridden = std::env::var(DESKTOP_COMMAND_ENV)
        .map(|raw| !raw.trim().is_empty())
        .unwrap_or(false);
    if !overridden && !command_is_installed(DESKTOP_COMMAND) {
        return Err(StatusRunError::NotInstalled);
    }
    let (program, mut args) = resolve(DESKTOP_COMMAND_ENV, DESKTOP_COMMAND);
    args.push("--print-status".to_owned());
    args.push(PRESENCE_V2_FLAG.to_owned());
    match run_for_stdout(&program, &args, wall) {
        // The file is there and it will not run: a uv trampoline whose
        // environment was deleted by a failed `--force` install. The remedy
        // is the installer, so the classification has to be one the
        // controller installs over rather than parks on.
        Err(StatusRunError::Failed { code, stderr }) if looks_like_a_broken_shim(&stderr) => {
            let code =
                code.map_or_else(|| "an unknown status".to_owned(), |value| value.to_string());
            Err(StatusRunError::Broken {
                detail: format!(
                    "mcc-desktop is on this machine but will not run \
                     (it exited with {code}: {stderr}). The install is \
                     incomplete -- uv's launcher is there and the environment \
                     behind it is not."
                ),
            })
        }
        other => other,
    }
}

/// Run one short-lived child, bounded, and return its stdout.
///
/// The whole of the care here is the two pipes and the wall, and both are
/// load-bearing: a child that fills a pipe deadlocks against a parent waiting
/// on the other, and a child with no wall blocks the ladder thread -- which is
/// also the thread that would have painted the page explaining why.
fn run_for_stdout(
    program: &str,
    args: &[String],
    wall: Duration,
) -> Result<String, StatusRunError> {
    let mut command = Command::new(program);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    hide_console(&mut command);

    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Err(StatusRunError::NotInstalled);
        }
        Err(error) => return Err(StatusRunError::Unrunnable(error.to_string())),
    };

    // Both pipes are drained on threads of their own. A child that fills one
    // of them deadlocks against a parent that is waiting on the other, and a
    // status document is comfortably larger than a pipe buffer on some
    // platforms, so this is not a hypothetical.
    let stdout = child.stdout.take().map(drain_on_a_thread);
    let stderr = child.stderr.take().map(drain_on_a_thread);

    let deadline = Instant::now() + wall;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => {}
            Err(error) => return Err(StatusRunError::Unrunnable(error.to_string())),
        }
        if Instant::now() >= deadline {
            // Killed, and reaped: a zombie left behind by a window that runs
            // this every thirty seconds during a reconnect would accumulate.
            let _ = child.kill();
            let _ = child.wait();
            return Err(StatusRunError::Unrunnable(format!(
                "{program} {} did not answer within {:.0} seconds, so \
                 it was stopped. Something is holding it up -- a shim being \
                 scanned, or a configuration directory on a drive that is not \
                 answering.",
                args.first().map(String::as_str).unwrap_or_default(),
                wall.as_secs_f64()
            )));
        }
        std::thread::sleep(Duration::from_millis(25));
    };

    let out = stdout.map(collect).unwrap_or_default();
    let err = stderr.map(collect).unwrap_or_default();
    if !status.success() {
        return Err(StatusRunError::Failed {
            code: status.code(),
            stderr: err.trim().to_owned(),
        });
    }
    Ok(out)
}

/// How long `mcc-desktop --ensure-shell` may take.
///
/// It downloads an archive of a few megabytes and verifies two digests, on a
/// connection this window knows nothing about, so it is bounded by the
/// download timeout Python uses (60s for each of two reads) plus room for the
/// extraction -- not by the 15s that bounds a status read. It runs on a thread
/// of its own, so nothing the user can see is waiting on it.
const ENSURE_SHELL_WALL: Duration = Duration::from_secs(300);

/// Ask Python to bring `target` up to the pinned release. Returns its stdout.
///
/// `target` is this process's own executable: the copy that has to change is
/// the one being run, which on Windows is as likely to be
/// `%LOCALAPPDATA%/Programs/My Claude Code` (the native installer's) as
/// `~/.local/bin` (the tray's). Python stages the replacement beside it and
/// prints `{updated, from_tag, to_tag, staged_path, restart_required}`.
pub fn ensure_shell(target: &std::path::Path) -> Result<String, StatusRunError> {
    let (program, mut args) = resolve(DESKTOP_COMMAND_ENV, DESKTOP_COMMAND);
    args.push("--ensure-shell".to_owned());
    args.push("--target".to_owned());
    args.push(target.display().to_string());
    run_for_stdout(&program, &args, ENSURE_SHELL_WALL)
}

/// Read one pipe to the end on its own thread.
fn drain_on_a_thread(mut pipe: impl Read + Send + 'static) -> mpsc::Receiver<String> {
    let (sender, receiver) = mpsc::channel();
    std::thread::spawn(move || {
        let mut buffer = Vec::new();
        let _ = pipe.read_to_end(&mut buffer);
        let _ = sender.send(String::from_utf8_lossy(&buffer).into_owned());
    });
    receiver
}

/// What a drained pipe held. A reader that never finished -- the child was
/// killed with the pipe still open -- contributes nothing rather than blocking
/// the wall it was just enforced by.
fn collect(receiver: mpsc::Receiver<String>) -> String {
    receiver
        .recv_timeout(Duration::from_secs(2))
        .unwrap_or_default()
}

/// How many of the child's last lines are kept for the page.
///
/// Bounded on purpose: a server that crash-loops printing a traceback per
/// second would otherwise be held entirely in this window's memory, and the
/// only part anyone reads is the end.
pub const CHILD_TAIL_LINES: usize = 40;
/// ...and a cap on the total, for a child that prints one enormous line.
pub const CHILD_TAIL_BYTES: usize = 8 * 1024;

/// The last words of a server this window started, and its exit code.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChildExit {
    pub code: Option<i32>,
    pub last_lines: String,
}

/// A server child, with both of its streams drained into a bounded buffer.
///
/// Until 6.66.0 `spawn_server` gave the child `Stdio::null()` for both
/// streams and never read its exit code, so a server that refused to start in
/// 1.1 seconds with the 6.30.0 message was, to this window, indistinguishable
/// from a server that was merely slow. The window said "Starting the My Claude
/// Code server..." and kept saying it, for as long as it was open.
pub struct ServerChild {
    child: Child,
    tail: Arc<Mutex<VecDeque<String>>>,
    exit: Option<ChildExit>,
}

impl ServerChild {
    /// Whether it is still running. Reaps the exit code when it is not, so
    /// the *reason* survives the process that had it.
    pub fn still_running(&mut self) -> bool {
        match self.child.try_wait() {
            Ok(None) => true,
            Ok(Some(status)) => {
                if self.exit.is_none() {
                    // A moment for the drain threads to finish the last
                    // lines: the words that explain an exit are written
                    // immediately before it.
                    std::thread::sleep(Duration::from_millis(150));
                    self.exit = Some(ChildExit {
                        code: status.code(),
                        last_lines: self.tail_text(),
                    });
                }
                false
            }
            Err(_) => false,
        }
    }

    /// What it said and how it ended, once it has ended.
    pub fn exit(&self) -> Option<ChildExit> {
        self.exit.clone()
    }

    fn tail_text(&self) -> String {
        let Ok(guard) = self.tail.lock() else {
            return String::new();
        };
        let mut lines: Vec<String> = guard.iter().cloned().collect();
        let mut total = 0usize;
        let mut kept: Vec<String> = Vec::new();
        while let Some(line) = lines.pop() {
            total += line.len() + 1;
            if total > CHILD_TAIL_BYTES && !kept.is_empty() {
                break;
            }
            kept.push(line);
        }
        kept.reverse();
        kept.join("\n")
    }
}

/// Start `mcc-server`, capturing what it says and how it ends.
///
/// The child still outlives this window in the sense that matters -- nothing
/// waits on it, and health is read off the port -- but its streams are piped
/// into a bounded ring buffer and, when a log path is given, appended to a
/// file. A child that exited is a different observation from a port that is
/// merely quiet, and until now the window could not tell them apart.
pub fn spawn_server(log_path: Option<PathBuf>) -> Result<ServerChild, String> {
    let (program, args) = resolve(SERVER_COMMAND_ENV, SERVER_COMMAND);
    let mut command = Command::new(&program);
    command
        .args(&args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    hide_console(&mut command);
    let mut child = command.spawn().map_err(|error| match error.kind() {
        std::io::ErrorKind::NotFound => format!(
            "{program} could not be found. Re-run the My Claude Code \
             installer, or start the server yourself."
        ),
        _ => format!("{program} could not be started: {error}"),
    })?;

    let tail: Arc<Mutex<VecDeque<String>>> = Arc::new(Mutex::new(VecDeque::new()));
    let sink = log_path.map(|path| Arc::new(Mutex::new(path)));
    for stream in [
        child.stdout.take().map(StreamOfAChild::Out),
        child.stderr.take().map(StreamOfAChild::Err),
    ]
    .into_iter()
    .flatten()
    {
        let tail = Arc::clone(&tail);
        let sink = sink.clone();
        std::thread::spawn(move || {
            let reader: Box<dyn Read + Send> = match stream {
                StreamOfAChild::Out(handle) => Box::new(handle),
                StreamOfAChild::Err(handle) => Box::new(handle),
            };
            for line in BufReader::new(reader).lines().map_while(Result::ok) {
                if let Ok(mut guard) = tail.lock() {
                    if guard.len() >= CHILD_TAIL_LINES {
                        guard.pop_front();
                    }
                    guard.push_back(line.clone());
                }
                if let Some(sink) = sink.as_ref() {
                    if let Ok(path) = sink.lock() {
                        append_line(&path, &line);
                    }
                }
            }
        });
    }

    Ok(ServerChild {
        child,
        tail,
        exit: None,
    })
}

enum StreamOfAChild {
    Out(std::process::ChildStdout),
    Err(std::process::ChildStderr),
}

/// Append one line to a log, creating the directory. Never fails a caller:
/// a log that cannot be written is not a reason to stop starting servers.
pub fn append_line(path: &Path, line: &str) {
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    if let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
    {
        let _ = writeln!(file, "{line}");
    }
}

/// How long the installer may run before this window stops waiting on it.
///
/// Generous: a cold `uv tool install` on a slow link, behind an antivirus
/// scanner, genuinely takes minutes. It exists because the alternative is
/// unbounded, and an unbounded wait behind a spinner is the exact shape of the
/// first-launch hang this release is about -- an installer that stops to ask a
/// question it will never be given an answer to (stdin is null) would
/// otherwise hold this window for the life of the process.
const INSTALL_WALL: Duration = Duration::from_secs(900);

/// How often the installer page says something when the installer itself has
/// gone quiet. Not a policy either: it is the difference between a window that
/// is visibly working and a window that looks frozen.
const INSTALL_HEARTBEAT: Duration = Duration::from_secs(10);

/// Run the install command, calling `on_line` for every line it writes.
///
/// stderr is merged into stdout on purpose: an installer that is failing says
/// so on stderr, and a window that shows only stdout would show a blank pane
/// and then an error with no explanation.
pub fn run_install(
    command: &InstallCommand,
    log_path: Option<PathBuf>,
    on_line: impl FnMut(&str),
) -> Result<i32, String> {
    run_install_within(command, INSTALL_WALL, INSTALL_HEARTBEAT, log_path, on_line)
}

/// The same, with the walls spelled out. Split for the test, which cannot wait
/// fifteen minutes to prove that waiting ends.
pub fn run_install_within(
    command: &InstallCommand,
    wall: Duration,
    heartbeat: Duration,
    log_path: Option<PathBuf>,
    mut on_line: impl FnMut(&str),
) -> Result<i32, String> {
    // Both streams, interleaved in the order they arrived, into a file that
    // outlives the page. Until 6.66.0 stdout reached the pane and was lost on
    // the next repaint, and stderr went to `eprintln!` -- which in a binary
    // built with `windows_subsystem = "windows"` goes nowhere at all. The
    // doc comment below promised the opposite for three releases.
    let sink = log_path.map(|path| Arc::new(Mutex::new(path)));
    if let Some(sink) = sink.as_ref() {
        if let Ok(path) = sink.lock() {
            append_line(&path, &format!("-- running: {} --", command.display));
        }
    }
    let record = |sink: &Option<Arc<Mutex<PathBuf>>>, line: &str| {
        if let Some(sink) = sink.as_ref() {
            if let Ok(path) = sink.lock() {
                append_line(&path, line);
            }
        }
    };
    let mut child = Command::new(&command.program)
        .args(&command.args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("{} could not be started: {error}", command.program))?;

    // One channel, both streams: the page and the log then see the
    // installer's own order rather than stdout first and stderr never.
    let (sender, receiver) = mpsc::channel::<String>();
    for stream in [
        child.stdout.take().map(StreamOfAChild::Out),
        child.stderr.take().map(StreamOfAChild::Err),
    ]
    .into_iter()
    .flatten()
    {
        let sender = sender.clone();
        std::thread::spawn(move || {
            let reader: Box<dyn Read + Send> = match stream {
                StreamOfAChild::Out(handle) => Box::new(handle),
                StreamOfAChild::Err(handle) => Box::new(handle),
            };
            for line in BufReader::new(reader).lines().map_while(Result::ok) {
                if sender.send(line).is_err() {
                    return;
                }
            }
        });
    }
    drop(sender);
    let lines = Some(receiver);

    let started = Instant::now();
    let mut last_said = Instant::now();
    let status = loop {
        if let Some(receiver) = lines.as_ref() {
            while let Ok(line) = receiver.try_recv() {
                record(&sink, &line);
                on_line(&line);
                last_said = Instant::now();
            }
        }
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => {}
            Err(error) => {
                return Err(format!("the installer could not be waited on: {error}"));
            }
        }
        if started.elapsed() >= wall {
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!(
                "the installer was still running after {:.0} minutes, so it was \
                 stopped. Run it yourself in a terminal to see what it is \
                 waiting for.",
                wall.as_secs_f64() / 60.0
            ));
        }
        if last_said.elapsed() >= heartbeat {
            on_line(&format!(
                "-- still installing, {:.0}s elapsed --",
                started.elapsed().as_secs_f64()
            ));
            last_said = Instant::now();
        }
        std::thread::sleep(Duration::from_millis(50));
    };
    // Whatever the reader had not handed over yet, now that the child is gone.
    if let Some(receiver) = lines {
        while let Ok(line) = receiver.recv_timeout(Duration::from_millis(200)) {
            record(&sink, &line);
            on_line(&line);
        }
    }
    let code = status.code().unwrap_or(-1);
    record(&sink, &format!("-- installer exited with {code} --"));
    // The bin directory may have just been created, or repopulated. The
    // cached answer from before the install is the wrong one from here on.
    forget_uv_tool_bin_dir();
    Ok(code)
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use super::*;

    #[test]
    fn the_default_command_is_the_installed_shim() {
        // Nothing here may spell a path, a directory or a port; the shim name
        // is the whole of what this binary knows (C1). With nothing found in
        // uv's bin directory the bare name is still the answer, and PATH
        // resolves it as it always did.
        assert_eq!(
            resolve_in(None, "MCC_SHELL_UNSET_FOR_THIS_TEST", DESKTOP_COMMAND).0,
            "mcc-desktop"
        );
        assert!(
            resolve_in(None, "MCC_SHELL_UNSET_FOR_THIS_TEST", DESKTOP_COMMAND)
                .1
                .is_empty()
        );
    }

    #[test]
    fn the_installed_command_is_looked_up_in_the_uv_bin_directory_before_path() {
        // D6-Q1. `uv tool install` writes its shims into `uv tool dir --bin`
        // and, with `UV_TOOL_UPDATE_SHELL`, the *user's* PATH -- neither of
        // which a process that is already running ever sees. Asking PATH is
        // therefore a question this window cannot get a useful answer to
        // about an install it has itself just performed, and reporting
        // 'not installed' from a spawn error is the same mistake 6.64.0
        // took out of install.ps1.
        let directory =
            std::env::temp_dir().join(format!("mcc-shell-resolve-{}", std::process::id()));
        std::fs::create_dir_all(&directory).expect("a scratch bin directory");
        let shim = directory.join(if cfg!(windows) {
            "mcc-desktop.exe"
        } else {
            "mcc-desktop"
        });
        std::fs::write(&shim, b"stub").expect("a stub launcher");

        assert_eq!(launcher_in(&directory, "mcc-desktop"), Some(shim.clone()));
        let (program, args) = resolve_in(
            Some(&shim),
            "MCC_SHELL_UNSET_FOR_THIS_TEST",
            DESKTOP_COMMAND,
        );
        assert_eq!(program, shim.to_string_lossy());
        assert!(args.is_empty());

        // ...and an explicit override still wins over both.
        let key = "MCC_SHELL_TEST_OVERRIDE_BEATS_THE_BIN_DIR";
        unsafe { std::env::set_var(key, "somewhere-else") };
        assert_eq!(
            resolve_in(Some(&shim), key, DESKTOP_COMMAND).0,
            "somewhere-else"
        );
        unsafe { std::env::remove_var(key) };

        std::fs::remove_dir_all(&directory).ok();
    }

    #[test]
    fn a_missing_uv_falls_back_to_path_rather_than_reporting_not_installed() {
        // The fallback is the whole reason `command_is_installed` asks two
        // questions: an install that did not come from uv is still an
        // install, and a machine without uv on PATH must not be told MCC is
        // missing.
        assert!(launcher_in(std::path::Path::new("does-not-exist"), "mcc-desktop").is_none());
        // Something every platform has, resolved the way `on_path` does.
        let ubiquitous = if cfg!(windows) { "cmd" } else { "sh" };
        assert!(on_path(ubiquitous), "{ubiquitous} should be on PATH");
        assert!(!on_path("mcc-a-command-that-does-not-exist"));
    }

    #[test]
    fn a_status_run_that_fails_with_a_uv_trampoline_error_is_reported_as_broken() {
        // Measured on the real binary with `<UV_TOOL_DIR>/my-claude-code`
        // deleted: exit 1, empty stdout, and this on stderr. `uv tool list`
        // says 'No tools installed' -- the receipt is inside the directory
        // that was removed.
        assert!(looks_like_a_broken_shim(
            "error: failed to canonicalize script path"
        ));
        assert!(looks_like_a_broken_shim("uv trampoline could not start"));
        assert!(looks_like_a_broken_shim("No tools installed"));
        // ...and an ordinary complaint from a program that ran is not.
        assert!(!looks_like_a_broken_shim(
            "Traceback (most recent call last)"
        ));
        assert!(!looks_like_a_broken_shim(""));
    }

    #[test]
    fn a_module_not_found_is_a_broken_shim() {
        // The user's exact stderr, 2026-09-09 04:03, for five minutes. `uv
        // tool install --force` empties the environment in place before it
        // resolves a byte, so for the whole install the launcher is there and
        // the interpreter behind it is not.
        assert!(looks_like_a_broken_shim(
            "Traceback (most recent call last):\n  File \"<frozen runpy>\", \
             line 198, in _run_module_as_main\nModuleNotFoundError: No module \
             named 'my_claude_code'"
        ));
        // ...and the first second of the same window, where uv has taken the
        // dependencies down and not yet the package itself.
        assert!(looks_like_a_broken_shim(
            "ModuleNotFoundError: No module named 'annotated_types'"
        ));
        // The newer uv trampoline spelling, which continues past `script path`.
        assert!(looks_like_a_broken_shim(
            "error: failed to canonicalize base path"
        ));
    }

    #[test]
    fn an_override_may_carry_arguments() {
        let key = "MCC_SHELL_TEST_OVERRIDE_WITH_ARGS";
        // Safety: this process is the only reader, and the key is unique to
        // this test.
        unsafe { std::env::set_var(key, "python scratch-desktop.py") };
        let (program, args) = resolve(key, DESKTOP_COMMAND);
        assert_eq!(program, "python");
        assert_eq!(args, vec!["scratch-desktop.py".to_owned()]);
        unsafe { std::env::remove_var(key) };
    }

    #[test]
    fn a_blank_override_falls_back_rather_than_running_nothing() {
        let key = "MCC_SHELL_TEST_BLANK_OVERRIDE";
        unsafe { std::env::set_var(key, "   ") };
        assert_eq!(resolve_in(None, key, SERVER_COMMAND).0, "mcc-server");
        unsafe { std::env::remove_var(key) };
    }

    #[test]
    fn the_status_call_asks_for_every_presence_this_build_understands() {
        // The opt-in that keeps an OLD window from being handed `draining`.
        // If this flag ever stops being sent, this build silently loses the
        // distinction between "MCC is restarting" and "a stranger has the
        // port", which is the whole of the release.
        assert_eq!(PRESENCE_V2_FLAG, "--presence-v2");
    }

    /// `DESKTOP_COMMAND_ENV` is process-global and `cargo test` runs its tests
    /// on threads, so the two tests below have to take turns or each will read
    /// the other's override.
    static COMMAND_ENV: Mutex<()> = Mutex::new(());

    /// Write a script that ignores every argument and then blocks for a good
    /// while. It has to ignore arguments because `print_status` appends its
    /// own, and an override that *errors* on them would prove the wrong thing:
    /// the test is about a child that never answers, not one that fails fast.
    fn a_command_that_never_answers() -> (std::path::PathBuf, String) {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("a clock after 1970")
            .as_nanos();
        let directory = std::env::temp_dir().join(format!("mcc-shell-wall-{stamp}"));
        std::fs::create_dir_all(&directory).expect("scratch directory");
        let path = if cfg!(windows) {
            let path = directory.join("never-answers.cmd");
            std::fs::write(&path, "@echo off\r\nping -n 200 127.0.0.1 >nul\r\n")
                .expect("wrote the sleeper");
            path
        } else {
            let path = directory.join("never-answers.sh");
            std::fs::write(&path, "#!/bin/sh\nsleep 200\n").expect("wrote the sleeper");
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755))
                    .expect("made it executable");
            }
            path
        };
        let command = path.to_string_lossy().into_owned();
        (directory, command)
    }

    #[test]
    fn print_status_gives_up_rather_than_blocking_forever() {
        // `Command::output()` has no wall, so a wedged child used to block the
        // ladder thread for the life of the process -- and the ladder thread is
        // what paints every page, including the error page that would have
        // explained the problem.
        let guard = COMMAND_ENV
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let (directory, sleeper) = a_command_that_never_answers();
        let key = DESKTOP_COMMAND_ENV;
        let previous = std::env::var(key).ok();
        // Safety: the lock above makes this process the only reader for the
        // duration, and the value is restored before it is released.
        unsafe { std::env::set_var(key, &sleeper) };

        let started = Instant::now();
        let outcome = print_status_within(Duration::from_millis(600));
        let waited = started.elapsed();

        match previous {
            Some(value) => unsafe { std::env::set_var(key, value) },
            None => unsafe { std::env::remove_var(key) },
        }
        drop(guard);
        std::fs::remove_dir_all(&directory).ok();

        match outcome {
            Err(StatusRunError::Unrunnable(detail)) => {
                assert!(detail.contains("did not answer"), "{detail}");
                assert!(
                    detail.contains("was stopped"),
                    "the message has to say the child was ended, not merely that \
                     it was slow: {detail}"
                );
            }
            other => panic!("expected a bounded give-up, got {other:?}"),
        }
        assert!(
            waited < Duration::from_secs(30),
            "the wall did not hold: waited {waited:?}"
        );
    }

    #[test]
    fn the_installer_is_given_a_wall_rather_than_the_rest_of_the_session() {
        // The first-launch hang, in one test: an installer that never returns
        // used to hold the ladder thread -- and therefore every page in the
        // window, including the one that would have explained it -- forever.
        let (directory, sleeper) = a_command_that_never_answers();
        let command = InstallCommand {
            program: sleeper.clone(),
            args: Vec::new(),
            display: sleeper,
        };
        let mut said: Vec<String> = Vec::new();
        let started = Instant::now();
        let outcome = run_install_within(
            &command,
            Duration::from_millis(700),
            Duration::from_millis(100),
            None,
            |line: &str| said.push(line.to_owned()),
        );
        let waited = started.elapsed();
        std::fs::remove_dir_all(&directory).ok();

        match outcome {
            Err(detail) => {
                assert!(detail.contains("was stopped"), "{detail}");
                assert!(detail.contains("still running after"), "{detail}");
            }
            other => panic!("expected a bounded give-up, got {other:?}"),
        }
        assert!(
            waited < Duration::from_secs(20),
            "the wall did not hold: {waited:?}"
        );
        // And it was not a bare spinner while it waited.
        assert!(
            said.iter().any(|line| line.contains("still installing")),
            "a silent installer must still say something: {said:?}"
        );
    }

    #[test]
    fn a_missing_command_reads_as_not_installed() {
        let guard = COMMAND_ENV
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let key = DESKTOP_COMMAND_ENV;
        let previous = std::env::var(key).ok();
        unsafe { std::env::set_var(key, "mcc-desktop-that-does-not-exist-9d2f") };
        let outcome = print_status();
        match previous {
            Some(value) => unsafe { std::env::set_var(key, value) },
            None => unsafe { std::env::remove_var(key) },
        }
        drop(guard);
        assert_eq!(outcome, Err(StatusRunError::NotInstalled));
    }
}
