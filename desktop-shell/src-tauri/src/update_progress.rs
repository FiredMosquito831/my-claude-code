//! What the update helper is doing, read off a file it appends to.
//!
//! An update on Windows is applied by a detached PowerShell helper that
//! outlives the server it is replacing (`application/release_updates.py`).
//! Measured on a real machine, that helper takes about fourteen minutes end to
//! end and used to leave no trace at all while it ran: the server's log stops
//! at the stop, `uv` writes to a pipe nobody reads, and the window showed one
//! unchanging sentence for the whole of it. Indistinguishable from broken.
//!
//! The helper now appends one JSON object per line as it moves between stages.
//! This module reads the last one. That is the whole of its job: it never
//! writes the file, never creates the directory, and never decides what a stage
//! means. C4 holds -- nothing under the configuration directory is written by
//! this shell -- and C5 holds too, because reading a receipt is not owning an
//! updater.
//!
//! The stage *string* is shown verbatim rather than switched on. A table of
//! stage names here would be a second copy of a list Python already owns, and
//! the first thing a second copy does is disagree with the first.

use std::path::{Path, PathBuf};

/// The directory the helper stages an update in, inside the configuration
/// directory, as `application/release_updates.py` spells it.
pub const STAGE_DIRNAME: &str = "updates";

/// The receipt file, as `release_updates.UPDATE_PROGRESS_FILENAME` spells it.
pub const PROGRESS_FILENAME: &str = "progress.json";

/// How much of the file to look at. The helper appends a handful of short
/// lines, so anything past this is a file that is not what we think it is, and
/// reading it into a window's memory would be the wrong response to that.
const MAX_BYTES: u64 = 64 * 1024;

/// The receipt path inside a configuration directory.
///
/// Built the same way `activation::activation_path` is built: from the
/// directory Python reported, never from one resolved here (C1).
pub fn progress_path(config_dir: &str) -> PathBuf {
    Path::new(config_dir)
        .join(STAGE_DIRNAME)
        .join(PROGRESS_FILENAME)
}

/// One stage the helper reported.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Stage {
    /// The stage name, exactly as Python wrote it.
    pub stage: String,
    /// The sentence Python wrote beside it, if it wrote one.
    pub message: Option<String>,
}

impl Stage {
    /// What to show in the banner: the helper's own sentence when it has one,
    /// and the bare stage name when it does not.
    pub fn describe(&self) -> String {
        match self.message.as_deref() {
            Some(message) if !message.trim().is_empty() => message.trim().to_owned(),
            _ => self.stage.clone(),
        }
    }
}

/// The most recent stage in `contents`, or `None`.
///
/// The last parseable line wins, and an unparseable trailing line is skipped
/// rather than treated as the end of the story: the writer is a separate
/// process appending while this reads, so a torn final line is expected and
/// costs one stale stage instead of the whole file.
pub fn latest_stage(contents: &str) -> Option<Stage> {
    stage_of(&latest_record(contents)?)
}

/// The stage carried by one already-parsed record.
fn stage_of(value: &serde_json::Value) -> Option<Stage> {
    let stage = value.get("stage").and_then(serde_json::Value::as_str)?;
    if stage.trim().is_empty() {
        return None;
    }
    Some(Stage {
        stage: stage.trim().to_owned(),
        message: value
            .get("message")
            .and_then(serde_json::Value::as_str)
            .map(str::to_owned),
    })
}

/// The last parseable record in `contents` that carries a stage.
///
/// The whole record rather than only its stage: 6.58.3's liveness fields
/// (`helper_pid`, `helper_done`, `started_at`, `version`) have to come from the
/// SAME line as the stage, and re-scanning for each of them would be a way to
/// mix two episodes.
fn latest_record(contents: &str) -> Option<serde_json::Value> {
    for line in contents.lines().rev() {
        let line = line.trim().trim_start_matches('\u{feff}');
        if line.is_empty() {
            continue;
        }
        let Ok(value) = serde_json::from_str::<serde_json::Value>(line) else {
            continue;
        };
        if stage_of(&value).is_none() {
            continue;
        }
        return Some(value);
    }
    None
}

/// An update helper that is running right now.
///
/// The one question that stops an update racing itself: the shell's status
/// ladder reads `NotInstalled` for the seconds `uv` spends rewriting the shims,
/// and by design it answers that by starting an install of its own into the
/// same tool directory. Measured 2026-09-07: the helper lost all five of its
/// attempts to the shell's concurrent installer, and the 6.56.0 upgrade landed
/// only through the shell's emergency reinstall at 23:25:43. So before
/// installing anything, the shell asks this.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ActiveHelper {
    /// The stage the helper last reported, verbatim.
    pub stage: Stage,
    /// The version it is installing, when the helper named one.
    pub version: Option<String>,
    /// Seconds since the helper started, when it recorded a start time.
    pub elapsed_seconds: Option<u64>,
}

impl ActiveHelper {
    /// The sentence the window shows while it waits for the helper.
    ///
    /// It says the same three things every time: that an INSTALLER is running
    /// (not that something is merely slow), which version it is heading for,
    /// and how long it has been at it -- so a window that is waiting is never
    /// mistaken for a window that has stopped.
    pub fn describe(&self) -> String {
        let target = match self.version.as_deref() {
            Some(version) if !version.trim().is_empty() => {
                format!("Updating to {}", version.trim())
            }
            _ => "Updating".to_owned(),
        };
        let elapsed = match self.elapsed_seconds {
            Some(seconds) => format!("installer running, {seconds} s"),
            None => "installer running".to_owned(),
        };
        format!("{target}... ({elapsed}). {}", self.stage.describe())
    }
}

/// Whether `pid` names a live process. `None` when it cannot be told.
///
/// `None` is a real answer rather than a failure, and every caller has to treat
/// it as "assume the helper is alive": reading an unknown pid as "gone" is what
/// starts the second installer this whole module exists to prevent.
fn pid_is_running(pid: i64) -> Option<bool> {
    if pid <= 0 {
        return Some(false);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // No `windows-sys` dependency for one question asked a few times a
        // minute at most, and only while an update is in flight.
        // CREATE_NO_WINDOW so the check never flashes a console over the
        // user's window.
        let output = std::process::Command::new("tasklist")
            .args(["/FI", &format!("PID eq {pid}"), "/NH", "/FO", "CSV"])
            .creation_flags(0x0800_0000)
            .output()
            .ok()?;
        if !output.status.success() {
            return None;
        }
        // A filter that matched nothing still exits zero and prints a sentence,
        // so the pid has to be looked for in the row tasklist would print.
        let text = String::from_utf8_lossy(&output.stdout);
        Some(text.contains(&format!("\"{pid}\"")))
    }
    #[cfg(not(windows))]
    {
        // The deferred helper is a Windows mechanism; this branch exists so the
        // module builds and tests everywhere, and /proc is the cheapest honest
        // answer where it exists.
        if std::path::Path::new("/proc").is_dir() {
            Some(std::path::Path::new("/proc").join(pid.to_string()).exists())
        } else {
            None
        }
    }
}

/// The helper described by `contents`, if one is still working.
///
/// Split from the I/O so the decision is testable without a live process: the
/// caller supplies the liveness oracle.
pub fn active_helper_in(
    contents: &str,
    alive: impl Fn(i64) -> Option<bool>,
) -> Option<ActiveHelper> {
    let value = latest_record(contents)?;
    // The helper's own word for "I have finished", written on every terminal
    // stage. The fast path for the ordinary ending.
    if value
        .get("helper_done")
        .and_then(serde_json::Value::as_bool)
        == Some(true)
    {
        return None;
    }
    let stage = stage_of(&value)?;
    let pid = value.get("helper_pid").and_then(serde_json::Value::as_i64);
    if pid.and_then(&alive) == Some(false) {
        return None;
    }
    // A receipt from a build that wrote no pid (6.58.2 and earlier). Judge it by
    // its stage alone, which is the reading that errs towards waiting.
    if pid.is_none() && matches!(stage.stage.as_str(), "done" | "failed" | "recovered") {
        return None;
    }
    let started = value.get("started_at").and_then(serde_json::Value::as_i64);
    let elapsed = started.and_then(|started| {
        let now = i64::try_from(
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .ok()?
                .as_secs(),
        )
        .ok()?;
        u64::try_from(now - started).ok()
    });
    Some(ActiveHelper {
        stage,
        version: value
            .get("version")
            .and_then(serde_json::Value::as_str)
            .map(str::trim)
            .filter(|version| !version.is_empty())
            .map(str::to_owned),
        elapsed_seconds: elapsed,
    })
}

/// Read the receipt in `config_dir` and report a helper that is still working.
pub fn active_helper(config_dir: &str) -> Option<ActiveHelper> {
    active_helper_in(&read_receipt(config_dir)?, pid_is_running)
}

/// Read the most recent stage from the receipt in `config_dir`, if there is
/// one. Every failure -- no file, no directory, unreadable, too large, garbage
/// -- is `None`: an update receipt is a nicety, and a window must never fail to
/// paint because one could not be read.
pub fn read_stage(config_dir: &str) -> Option<Stage> {
    latest_stage(&read_receipt(config_dir)?)
}

/// The receipt's text, or `None` for every failure there can be -- no file, no
/// directory, unreadable, too large.
fn read_receipt(config_dir: &str) -> Option<String> {
    let path = progress_path(config_dir);
    let metadata = std::fs::metadata(&path).ok()?;
    if !metadata.is_file() || metadata.len() > MAX_BYTES {
        return None;
    }
    std::fs::read_to_string(&path).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_path_is_built_from_the_directory_python_reported() {
        let path = progress_path("/somewhere/else/entirely");
        assert!(path.ends_with(PROGRESS_FILENAME));
        assert!(path.to_string_lossy().contains("updates"));
        assert!(path.to_string_lossy().contains("somewhere"));
    }

    #[test]
    fn the_last_line_is_the_stage() {
        let contents = "{\"stage\":\"waiting-for-parent\",\"message\":\"Waiting.\"}\n\
                        {\"stage\":\"installing\",\"message\":\"Installing the new version.\"}\n";
        let stage = latest_stage(contents).expect("a stage");
        assert_eq!(stage.stage, "installing");
        assert_eq!(stage.describe(), "Installing the new version.");
    }

    #[test]
    fn a_half_written_final_line_falls_back_to_the_one_before_it() {
        // The writer is a detached process appending while this reads. A torn
        // line must cost one stale stage, not the whole file.
        let contents = "{\"stage\":\"installing\",\"message\":\"Installing.\"}\n\
                        {\"stage\":\"star";
        assert_eq!(latest_stage(contents).expect("a stage").stage, "installing");
    }

    #[test]
    fn a_stage_without_a_message_still_says_something() {
        let stage = latest_stage("{\"stage\":\"done\"}").expect("a stage");
        assert_eq!(stage.describe(), "done");
    }

    #[test]
    fn nothing_readable_is_no_stage_rather_than_an_error() {
        assert_eq!(latest_stage(""), None);
        assert_eq!(latest_stage("\n\n   \n"), None);
        assert_eq!(latest_stage("not json at all"), None);
        assert_eq!(latest_stage("{\"message\":\"no stage key\"}"), None);
        assert_eq!(latest_stage("{\"stage\":\"   \"}"), None);
    }

    #[test]
    fn a_bom_written_by_windows_powershell_does_not_hide_the_stage() {
        let contents = "\u{feff}{\"stage\":\"done\",\"message\":\"Started.\"}";
        assert_eq!(latest_stage(contents).expect("a stage").stage, "done");
    }

    #[test]
    fn a_missing_receipt_is_no_stage() {
        let directory = std::env::temp_dir().join("mcc-shell-no-such-config-9d2f");
        assert_eq!(read_stage(&directory.to_string_lossy()), None);
    }

    #[test]
    fn a_receipt_on_disk_is_read() {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("a clock after 1970")
            .as_nanos();
        let config_dir = std::env::temp_dir().join(format!("mcc-shell-progress-{stamp}"));
        let stage_dir = config_dir.join(STAGE_DIRNAME);
        std::fs::create_dir_all(&stage_dir).expect("scratch directory");
        std::fs::write(
            stage_dir.join(PROGRESS_FILENAME),
            "{\"stage\":\"starting\",\"message\":\"Starting the updated server.\"}\n",
        )
        .expect("wrote the receipt");
        let stage = read_stage(&config_dir.to_string_lossy()).expect("a stage");
        assert_eq!(stage.stage, "starting");
        assert_eq!(stage.describe(), "Starting the updated server.");
        std::fs::remove_dir_all(&config_dir).ok();
    }

    /// The 2026-09-07 receipt, as 6.58.3 writes it: mid-install, helper alive.
    fn installing(pid: i64) -> String {
        format!(
            "{{\"stage\":\"waiting-for-parent\",\"helper_pid\":{pid},\"helper_done\":false}}\n\
             {{\"stage\":\"installing\",\"message\":\"Installing the new version.\",\
             \"helper_pid\":{pid},\"helper_done\":false,\"version\":\"6.58.3\"}}\n"
        )
    }

    #[test]
    fn a_live_helper_is_an_active_update() {
        let helper = active_helper_in(&installing(4242), |pid| {
            assert_eq!(pid, 4242);
            Some(true)
        })
        .expect("an active helper");
        assert_eq!(helper.stage.stage, "installing");
        assert_eq!(helper.version.as_deref(), Some("6.58.3"));
        assert!(helper.describe().starts_with("Updating to 6.58.3..."));
        assert!(helper.describe().contains("installer running"));
    }

    #[test]
    fn a_helper_whose_process_is_gone_is_not_an_active_update() {
        // The receipt still says 'installing' -- a helper killed mid-install
        // leaves that behind forever. The pid is the fact, not the stage: this
        // is the case where the window MUST be free to install again.
        assert_eq!(active_helper_in(&installing(4242), |_| Some(false)), None);
    }

    #[test]
    fn a_pid_that_cannot_be_checked_is_treated_as_alive() {
        // Reading "I could not tell" as "the helper is gone" is what starts the
        // second installer. It has to err the other way.
        assert!(active_helper_in(&installing(4242), |_| None).is_some());
    }

    #[test]
    fn a_helper_that_says_it_is_done_is_not_active_whatever_its_pid() {
        // Process ids are recycled quickly on Windows, so a pid that answers
        // "alive" is not proof the HELPER is alive. Its own last word wins.
        let contents = "{\"stage\":\"done\",\"message\":\"The updated server was started.\",\
                        \"helper_pid\":4242,\"helper_done\":true}";
        assert_eq!(active_helper_in(contents, |_| Some(true)), None);
        let recovered = "{\"stage\":\"recovered\",\"helper_pid\":4242,\"helper_done\":true}";
        assert_eq!(active_helper_in(recovered, |_| Some(true)), None);
    }

    #[test]
    fn a_receipt_from_an_older_helper_is_judged_by_its_stage_alone() {
        // 6.58.2 and earlier wrote no pid. An update in flight under one of
        // those must still be waited for, and a finished one must not be.
        let old_installing = "{\"stage\":\"installing\",\"message\":\"Installing.\"}";
        assert!(active_helper_in(old_installing, |_| Some(true)).is_some());
        let old_done = "{\"stage\":\"done\",\"message\":\"Started.\"}";
        assert_eq!(active_helper_in(old_done, |_| Some(true)), None);
    }

    #[test]
    fn no_receipt_at_all_is_no_active_update() {
        // The ordinary case, and the one that must not block a first install.
        assert_eq!(active_helper_in("", |_| Some(true)), None);
        assert_eq!(active_helper_in("not json", |_| Some(true)), None);
    }

    #[test]
    fn the_liveness_fields_come_from_the_same_line_as_the_stage() {
        // Two episodes in one file: the first helper is done, the second is
        // installing. Scanning per-field would mix them and report the dead
        // helper's `helper_done` against the live one's stage.
        let contents = "{\"stage\":\"done\",\"helper_pid\":11,\"helper_done\":true}\n\
                        {\"stage\":\"installing\",\"helper_pid\":22,\"helper_done\":false}\n";
        let helper = active_helper_in(contents, |pid| {
            assert_eq!(pid, 22);
            Some(true)
        })
        .expect("the second episode");
        assert_eq!(helper.stage.stage, "installing");
    }

    #[test]
    fn elapsed_seconds_are_counted_from_the_helpers_own_start() {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("a clock after 1970")
            .as_secs();
        let contents = format!(
            "{{\"stage\":\"installing\",\"helper_pid\":7,\"helper_done\":false,\
             \"started_at\":{}}}",
            now - 42
        );
        let helper = active_helper_in(&contents, |_| Some(true)).expect("a helper");
        let elapsed = helper.elapsed_seconds.expect("an elapsed count");
        assert!((42..=60).contains(&elapsed), "{elapsed}");
        assert!(helper.describe().contains("installer running, "));
    }

    #[test]
    fn a_helper_with_no_version_still_describes_itself() {
        let contents = "{\"stage\":\"installing\",\"helper_pid\":7,\"helper_done\":false}";
        let helper = active_helper_in(contents, |_| Some(true)).expect("a helper");
        assert!(
            helper
                .describe()
                .starts_with("Updating... (installer running)")
        );
    }

    #[test]
    fn this_processs_own_id_is_seen_as_running() {
        // The liveness oracle itself, against the one pid we know is alive.
        let mine = i64::from(std::process::id());
        assert_ne!(pid_is_running(mine), Some(false));
        assert_eq!(pid_is_running(0), Some(false));
        assert_eq!(pid_is_running(-1), Some(false));
    }
}
