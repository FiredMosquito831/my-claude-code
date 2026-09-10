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
    /// Whether this is a stage the helper writes on its way *out*.
    ///
    /// The list is the helper's own vocabulary (`release_updates.py`'s
    /// `Write-Stage` calls), and it is the fact the controller's
    /// `RestartPending` waits for: the installer is finished, one way or
    /// another, so whatever is watching may start a server. `starting` is
    /// deliberately absent -- the helper writes it just before its own
    /// `Start-Process`, which the window now suppresses with `--no-restart`,
    /// so treating it as terminal would race the very last thing the helper
    /// does with its own file.
    pub fn is_terminal(&self) -> bool {
        matches!(
            self.stage.trim(),
            "done" | "failed" | "recovered" | "install-failed" | "installed"
        )
    }

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

/// Whether the last stage in this configuration directory is a terminal one.
pub fn stage_is_terminal(stage: &Stage) -> bool {
    stage.is_terminal()
}

/// The most recent stage in `config_dir`, and how many seconds ago it was
/// written.
///
/// The age is the fact U1 needs and 6.66.1 did not have. `uv tool install
/// --force` empties the environment *before* it resolves anything, so the
/// seconds either side of the helper's own last record are exactly the seconds
/// in which `mcc-desktop --print-status` cannot answer -- and a window that
/// cannot tell "the helper stopped a moment ago" from "the helper stopped last
/// Tuesday" has to treat both as a failure, which is the five-minute error page
/// the user was shown on 2026-09-09.
///
/// `None` for the age means *unknown*, and every caller must read that as
/// **not** settling. The receipt is truncated only when a new episode starts,
/// so a `done` record from a month ago is still the last line in the file on
/// every machine that has ever updated; treating an unknown age as "just
/// finished" would suppress the genuine error page for ever.
pub fn read_stage_with_age(config_dir: &str) -> Option<(Stage, Option<f64>)> {
    let contents = read_receipt(config_dir)?;
    let stage = latest_stage(&contents)?;
    let age = seconds_since_last_record(&contents).or_else(|| receipt_age_seconds(config_dir));
    Some((stage, age))
}

/// Seconds since the last record in `contents` was written, from the `at`
/// stamp the helper puts in every record (`release_updates.py`'s `Write-Stage`
/// writes `(Get-Date).ToUniversalTime().ToString('o')`).
///
/// `None` when there is no record, no stamp, or a stamp this cannot read --
/// and never a negative number: a clock that has moved backwards would
/// otherwise read as a helper that finishes in the future.
pub fn seconds_since_last_record(contents: &str) -> Option<f64> {
    let stamp = latest_record(contents)?
        .get("at")
        .and_then(serde_json::Value::as_str)
        .and_then(parse_iso8601_utc)?;
    let now = unix_now()?;
    Some((now - stamp).max(0.0))
}

/// The receipt file's own modification time, as an age in seconds.
///
/// The fallback for a receipt written by 6.58.2 or earlier, which carried no
/// `at`. Weaker than the stamp -- a copy or a restore moves it -- but the
/// question it answers ("was this file touched in the last thirty seconds")
/// is the same one, and a wrong answer here only ever costs one extra
/// re-check.
fn receipt_age_seconds(config_dir: &str) -> Option<f64> {
    let modified = std::fs::metadata(progress_path(config_dir))
        .ok()?
        .modified()
        .ok()?;
    Some(modified.elapsed().ok()?.as_secs_f64())
}

/// Now, as seconds since the Unix epoch.
fn unix_now() -> Option<f64> {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .ok()
        .map(|since| since.as_secs_f64())
}

/// Parse the one timestamp shape the helper writes: round-trip ISO 8601 in
/// UTC, `2026-09-09T04:01:58.1234567Z`.
///
/// Hand-written rather than pulled in with a date crate, because this is the
/// only date the shell ever reads and a dependency is a supply chain. A
/// trailing `+HH:MM`/`-HH:MM` offset is accepted too, so a helper on a build
/// that stopped calling `ToUniversalTime` would still be read correctly rather
/// than silently mis-read.
fn parse_iso8601_utc(text: &str) -> Option<f64> {
    let text = text.trim();
    let (date, rest) = text.split_once('T').or_else(|| text.split_once(' '))?;
    let mut date = date.split('-');
    let year: i32 = date.next()?.parse().ok()?;
    let month: i32 = date.next()?.parse().ok()?;
    let day: i32 = date.next()?.parse().ok()?;
    if date.next().is_some() || !(1..=12).contains(&month) || !(1..=31).contains(&day) {
        return None;
    }

    // Split the offset off the time before anything else is read: the sign
    // characters cannot appear inside a time, so this is unambiguous.
    let (time, offset_minutes) = match rest.strip_suffix(['Z', 'z']) {
        Some(time) => (time, 0_i32),
        None => match rest.rfind(['+', '-']) {
            Some(index) => {
                let (time, offset) = rest.split_at(index);
                (time, parse_offset(offset)?)
            }
            None => (rest, 0),
        },
    };

    let mut time = time.split(':');
    let hour: i32 = time.next()?.parse().ok()?;
    let minute: i32 = time.next()?.parse().ok()?;
    let seconds: f64 = time.next()?.parse().ok()?;
    if time.next().is_some() || hour > 23 || minute > 59 || !(0.0..60.0).contains(&seconds) {
        return None;
    }

    // Every part is converted to f64 through `i32`, which is lossless, and the
    // multiplications are done in f64 -- so there is no year-2038 edge here
    // even though each individual field is small. A day count fits `i32` until
    // long after this program is of any interest.
    let days = f64::from(days_from_civil(year, month, day));
    Some(
        days * 86_400.0 + f64::from(hour) * 3_600.0 + f64::from(minute) * 60.0
            - f64::from(offset_minutes) * 60.0
            + seconds,
    )
}

/// `+HH:MM` or `-HHMM`, in minutes east of UTC.
fn parse_offset(offset: &str) -> Option<i32> {
    let sign = match offset.as_bytes().first()? {
        b'+' => 1,
        b'-' => -1,
        _ => return None,
    };
    let digits: String = offset
        .chars()
        .skip(1)
        .filter(char::is_ascii_digit)
        .collect();
    if digits.len() != 4 {
        return None;
    }
    let hours: i32 = digits[..2].parse().ok()?;
    let minutes: i32 = digits[2..].parse().ok()?;
    if hours > 23 || minutes > 59 {
        return None;
    }
    Some(sign * (hours * 60 + minutes))
}

/// Days from 1970-01-01 to a proleptic-Gregorian civil date.
///
/// Howard Hinnant's `days_from_civil`, which is the standard answer and is
/// exact for every year this program can be handed.
fn days_from_civil(year: i32, month: i32, day: i32) -> i32 {
    let year = if month <= 2 { year - 1 } else { year };
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let year_of_era = year - era * 400;
    let day_of_year = (153 * (if month > 2 { month - 3 } else { month + 9 }) + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
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
    fn the_round_trip_stamp_the_helper_writes_is_read_back_exactly() {
        // `Write-Stage` writes `(Get-Date).ToUniversalTime().ToString('o')`.
        // The two records below are the real ones from the live 6.66.1 update
        // of 2026-09-10, read off the helper's own receipt.
        let installing = parse_iso8601_utc("2026-09-10T08:21:37.7250000Z").expect("a stamp");
        let done = parse_iso8601_utc("2026-09-10T08:22:35.3990000Z").expect("a stamp");
        assert!((done - installing - 57.674).abs() < 0.001, "{done}");
        // The epoch value itself, against an independently computed instant:
        // 2026-09-10T00:00:00Z is 1_788_998_400 seconds after the epoch.
        let midnight = parse_iso8601_utc("2026-09-10T00:00:00Z").expect("a stamp");
        assert!((midnight - 1_788_998_400.0).abs() < 0.001, "{midnight}");
        // ...and the epoch itself, which is the one value a sign error moves.
        assert!(
            parse_iso8601_utc("1970-01-01T00:00:00Z")
                .expect("a stamp")
                .abs()
                < 0.001
        );
    }

    #[test]
    fn an_offset_stamp_is_read_as_the_instant_it_names() {
        // Not written by any helper today, but a build that stopped calling
        // `ToUniversalTime` must be mis-read loudly or not at all -- never
        // silently by three hours, which is longer than the settle window.
        let utc = parse_iso8601_utc("2026-09-10T00:00:00Z").expect("a stamp");
        let east = parse_iso8601_utc("2026-09-10T03:00:00+03:00").expect("a stamp");
        assert!((utc - east).abs() < 0.001, "{utc} vs {east}");
        let west = parse_iso8601_utc("2026-09-09T19:00:00-0500").expect("a stamp");
        assert!((utc - west).abs() < 0.001, "{utc} vs {west}");
    }

    #[test]
    fn a_stamp_this_cannot_read_is_unknown_rather_than_a_guess() {
        assert_eq!(parse_iso8601_utc(""), None);
        assert_eq!(parse_iso8601_utc("2026-09-10"), None);
        assert_eq!(parse_iso8601_utc("yesterday"), None);
        assert_eq!(parse_iso8601_utc("2026-13-10T00:00:00Z"), None);
        assert_eq!(parse_iso8601_utc("2026-09-10T25:00:00Z"), None);
        // A record with no stamp at all: 6.58.2 and earlier.
        assert_eq!(
            seconds_since_last_record("{\"stage\":\"done\",\"helper_done\":true}"),
            None
        );
    }

    #[test]
    fn the_age_of_the_last_record_is_measured_from_its_own_stamp() {
        // A record from an update that ran in 2020 is not a settling helper,
        // whatever the file says -- and this is the case every machine that
        // has ever updated is in, because the receipt is truncated only when
        // the NEXT episode starts.
        let old = seconds_since_last_record("{\"stage\":\"done\",\"at\":\"2020-01-01T00:00:00Z\"}")
            .expect("an age");
        assert!(old > 150_000_000.0, "{old}");

        // A stamp from the future -- a clock that moved backwards, or a
        // helper on a machine whose timezone handling differs -- reads as
        // "just now" rather than as a negative age, so the settle window
        // errs towards waiting exactly once and then expires.
        let ahead =
            seconds_since_last_record("{\"stage\":\"done\",\"at\":\"2999-01-01T00:00:00Z\"}")
                .expect("an age");
        assert!(ahead.abs() < f64::EPSILON, "{ahead}");

        // And the age tracks the stamp: two records a minute apart give ages
        // a minute apart, whichever day the suite runs on.
        let earlier =
            seconds_since_last_record("{\"stage\":\"done\",\"at\":\"2026-09-10T08:21:35Z\"}")
                .expect("an age");
        let later =
            seconds_since_last_record("{\"stage\":\"done\",\"at\":\"2026-09-10T08:22:35Z\"}")
                .expect("an age");
        assert!((earlier - later - 60.0).abs() < 1.0, "{earlier} {later}");
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
