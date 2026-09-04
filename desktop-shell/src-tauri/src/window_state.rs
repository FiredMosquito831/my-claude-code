//! Where the window was last time, remembered by the shell itself.
//!
//! Deliberately *not* stored beside the MCC configuration. Geometry is a
//! property of this machines display, not of the users MCC install, and
//! writing into the configuration directory would make this binary a writer
//! of a directory it is not allowed to even locate (C1, C4). It goes in the
//! OS application-data directory instead.
//!
//! `MCC_SHELL_DATA_DIR` overrides the location. That exists for tests and for
//! the release smoke, which must be able to run a real window without
//! disturbing the geometry of the one the developer actually uses.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

/// The environment variable that relocates everything this module writes.
pub const DATA_DIR_ENV: &str = "MCC_SHELL_DATA_DIR";

/// The file, inside whichever data directory is in force.
pub const STATE_FILENAME: &str = "window.json";

/// Remembered geometry. Every field is optional in the file: a state written
/// by a later build with more fields still loads here, and a state missing a
/// field falls back to the first-run default rather than to zero.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct WindowState {
    #[serde(default)]
    pub x: Option<i32>,
    #[serde(default)]
    pub y: Option<i32>,
    #[serde(default)]
    pub width: Option<u32>,
    #[serde(default)]
    pub height: Option<u32>,
    #[serde(default)]
    pub maximized: bool,
}

impl WindowState {
    /// The size to open at: what was remembered, else what the status
    /// document says `DESKTOP_WINDOW_WIDTH`/`HEIGHT` are set to. The
    /// first-run default therefore still comes from Python (C1).
    pub fn size_or(&self, default_width: u32, default_height: u32) -> (u32, u32) {
        (
            self.width
                .filter(|value| *value > 0)
                .unwrap_or(default_width),
            self.height
                .filter(|value| *value > 0)
                .unwrap_or(default_height),
        )
    }
}

/// The state file inside `directory`.
pub fn state_path(directory: &Path) -> PathBuf {
    directory.join(STATE_FILENAME)
}

/// Read remembered geometry. A missing or unreadable file is not an error --
/// it is a first run, and a first run must not stop the window opening.
pub fn load(path: &Path) -> WindowState {
    fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default()
}

/// Write geometry, creating the directory if this is the first run.
pub fn save(path: &Path, state: &WindowState) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let text = serde_json::to_string_pretty(state)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    fs::write(path, text)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch_dir(name: &str) -> PathBuf {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("a clock after 1970")
            .as_nanos();
        let directory = std::env::temp_dir().join(format!("mcc-shell-{name}-{stamp}"));
        fs::create_dir_all(&directory).expect("scratch directory");
        directory
    }

    #[test]
    fn round_trips_geometry() {
        let directory = scratch_dir("round-trip");
        let path = state_path(&directory);
        let state = WindowState {
            x: Some(-12),
            y: Some(40),
            width: Some(1024),
            height: Some(768),
            maximized: true,
        };
        save(&path, &state).expect("saved");
        assert_eq!(load(&path), state);
        fs::remove_dir_all(&directory).ok();
    }

    #[test]
    fn a_first_run_loads_the_default_and_falls_back_to_the_python_size() {
        let directory = scratch_dir("first-run");
        let state = load(&state_path(&directory));
        assert_eq!(state, WindowState::default());
        assert_eq!(state.size_or(1280, 860), (1280, 860));
        fs::remove_dir_all(&directory).ok();
    }

    #[test]
    fn a_corrupt_file_is_a_first_run_and_not_a_crash() {
        let directory = scratch_dir("corrupt");
        let path = state_path(&directory);
        fs::write(&path, "{ this is not json").expect("wrote garbage");
        assert_eq!(load(&path), WindowState::default());
        fs::remove_dir_all(&directory).ok();
    }

    #[test]
    fn a_remembered_size_beats_the_default() {
        let state = WindowState {
            width: Some(900),
            height: Some(600),
            ..WindowState::default()
        };
        assert_eq!(state.size_or(1280, 860), (900, 600));
    }

    #[test]
    fn a_zero_size_is_ignored_rather_than_opening_an_invisible_window() {
        let state = WindowState {
            width: Some(0),
            height: Some(0),
            ..WindowState::default()
        };
        assert_eq!(state.size_or(1280, 860), (1280, 860));
    }

    #[test]
    fn saving_creates_the_directory_on_a_first_run() {
        let directory = scratch_dir("nested").join("deeper");
        let path = state_path(&directory);
        save(&path, &WindowState::default()).expect("saved");
        assert!(path.is_file());
        fs::remove_dir_all(directory.parent().expect("a parent")).ok();
    }
}
