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

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};

use crate::install::InstallCommand;

/// Overrides the `mcc-desktop` command. Set to an absolute path, or to a
/// script, when running the release smoke against a scratch install so a real
/// window can be exercised without reading the developers own configuration.
pub const DESKTOP_COMMAND_ENV: &str = "MCC_SHELL_DESKTOP_COMMAND";

/// Overrides the `mcc-server` command, for the same reason.
pub const SERVER_COMMAND_ENV: &str = "MCC_SHELL_SERVER_COMMAND";

const DESKTOP_COMMAND: &str = "mcc-desktop";
const SERVER_COMMAND: &str = "mcc-server";

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
    let (program, mut args) = resolve(DESKTOP_COMMAND_ENV, DESKTOP_COMMAND);
    args.push("--print-status".to_owned());

    let mut command = Command::new(&program);
    command.args(&args).stdin(Stdio::null());
    hide_console(&mut command);

    let output = match command.output() {
        Ok(output) => output,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Err(StatusRunError::NotInstalled);
        }
        Err(error) => return Err(StatusRunError::Unrunnable(error.to_string())),
    };
    if !output.status.success() {
        return Err(StatusRunError::Failed {
            code: output.status.code(),
            stderr: String::from_utf8_lossy(&output.stderr).trim().to_owned(),
        });
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
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
    fn a_missing_command_reads_as_not_installed() {
        let key = DESKTOP_COMMAND_ENV;
        let previous = std::env::var(key).ok();
        unsafe { std::env::set_var(key, "mcc-desktop-that-does-not-exist-9d2f") };
        assert_eq!(print_status(), Err(StatusRunError::NotInstalled));
        match previous {
            Some(value) => unsafe { std::env::set_var(key, value) },
            None => unsafe { std::env::remove_var(key) },
        }
    }
}
