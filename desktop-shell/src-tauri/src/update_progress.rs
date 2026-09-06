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
    for line in contents.lines().rev() {
        let line = line.trim().trim_start_matches('\u{feff}');
        if line.is_empty() {
            continue;
        }
        let Ok(value) = serde_json::from_str::<serde_json::Value>(line) else {
            continue;
        };
        let Some(stage) = value.get("stage").and_then(serde_json::Value::as_str) else {
            continue;
        };
        if stage.trim().is_empty() {
            continue;
        }
        return Some(Stage {
            stage: stage.trim().to_owned(),
            message: value
                .get("message")
                .and_then(serde_json::Value::as_str)
                .map(str::to_owned),
        });
    }
    None
}

/// Read the most recent stage from the receipt in `config_dir`, if there is
/// one. Every failure -- no file, no directory, unreadable, too large, garbage
/// -- is `None`: an update receipt is a nicety, and a window must never fail to
/// paint because one could not be read.
pub fn read_stage(config_dir: &str) -> Option<Stage> {
    let path = progress_path(config_dir);
    let metadata = std::fs::metadata(&path).ok()?;
    if !metadata.is_file() || metadata.len() > MAX_BYTES {
        return None;
    }
    let contents = std::fs::read_to_string(&path).ok()?;
    latest_stage(&contents)
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
}
