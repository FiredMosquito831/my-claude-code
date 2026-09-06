//! The three processes this shell ever starts, and nothing else.
//!
//! 1. `mcc-desktop --print-status`, read for its stdout. This is the only way
//!    the shell learns where anything is (C1).
//! 2. `mcc-server`, started when -- and only when -- the ladder says `Start`.
//! 3. The projects own install script, when `mcc-desktop` is not on `PATH`
//!    (decision Q4).
//!
//! It never takes `desktop.lock`, never writes `desktop.json`, and never
//! registers autostart (C4). Every one of those stays Pythons.

use std::io::{BufRead, BufReader, Read};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
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
const STATUS_WALL: Duration = Duration::from_secs(15);

/// Why `mcc-desktop --print-status` did not produce a document.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StatusRunError {
    /// The command is not on `PATH`. This is the signal that MCC is not
    /// installed, and the only condition that triggers an install.
    NotInstalled,
    /// It ran and failed.
    Failed { code: Option<i32>, stderr: String },
    /// It could not be run for some other reason.
    Unrunnable(String),
}

/// A command line, split. `mcc-desktop` may be overridden with something that
/// takes arguments of its own, so the override is split on whitespace.
fn resolve(env_key: &str, fallback: &str) -> (String, Vec<String>) {
    match std::env::var(env_key) {
        Ok(raw) if !raw.trim().is_empty() => {
            let mut parts = raw.split_whitespace().map(str::to_owned);
            let program = parts.next().unwrap_or_else(|| fallback.to_owned());
            (program, parts.collect())
        }
        _ => (fallback.to_owned(), Vec::new()),
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
    print_status_within(STATUS_WALL)
}

/// The same, with the wall spelled out. Split for the test, which cannot wait
/// fifteen seconds to prove that waiting ends.
pub fn print_status_within(wall: Duration) -> Result<String, StatusRunError> {
    let (program, mut args) = resolve(DESKTOP_COMMAND_ENV, DESKTOP_COMMAND);
    args.push("--print-status".to_owned());
    args.push(PRESENCE_V2_FLAG.to_owned());

    let mut command = Command::new(&program);
    command
        .args(&args)
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
                "{program} --print-status did not answer within {:.0} seconds, so \
                 it was stopped. Something is holding it up -- a shim being \
                 scanned, or a configuration directory on a drive that is not \
                 answering.",
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

/// Start `mcc-server`, detached, and forget about it.
///
/// The child is deliberately not waited on: the server outlives this window,
/// exactly as it does when the Python tray spawns it. Health is then read off
/// the port, which is the only signal that means anything anyway.
pub fn spawn_server() -> Result<Child, String> {
    let (program, args) = resolve(SERVER_COMMAND_ENV, SERVER_COMMAND);
    let mut command = Command::new(&program);
    command
        .args(&args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    hide_console(&mut command);
    command.spawn().map_err(|error| match error.kind() {
        std::io::ErrorKind::NotFound => format!(
            "{program} is not on PATH. Re-run the My Claude Code installer, \
                 or start the server yourself."
        ),
        _ => format!("{program} could not be started: {error}"),
    })
}

/// Run the install command, calling `on_line` for every line it writes.
///
/// stderr is merged into stdout on purpose: an installer that is failing says
/// so on stderr, and a window that shows only stdout would show a blank pane
/// and then an error with no explanation.
pub fn run_install(command: &InstallCommand, mut on_line: impl FnMut(&str)) -> Result<i32, String> {
    let mut child = Command::new(&command.program)
        .args(&command.args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("{} could not be started: {error}", command.program))?;

    if let Some(stderr) = child.stderr.take() {
        // Drained on its own thread; a full stderr pipe deadlocks the child.
        std::thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                eprintln!("{line}");
            }
        });
    }
    if let Some(stdout) = child.stdout.take() {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            on_line(&line);
        }
    }
    let status = child
        .wait()
        .map_err(|error| format!("the installer could not be waited on: {error}"))?;
    Ok(status.code().unwrap_or(-1))
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use super::*;

    #[test]
    fn the_default_command_is_the_installed_shim() {
        // Nothing here may spell a path, a directory or a port; the shim name
        // is the whole of what this binary knows (C1).
        assert_eq!(
            resolve("MCC_SHELL_UNSET_FOR_THIS_TEST", DESKTOP_COMMAND).0,
            "mcc-desktop"
        );
        assert!(
            resolve("MCC_SHELL_UNSET_FOR_THIS_TEST", DESKTOP_COMMAND)
                .1
                .is_empty()
        );
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
        assert_eq!(resolve(key, SERVER_COMMAND).0, "mcc-server");
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
