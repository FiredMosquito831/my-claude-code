//! The doorbell.
//!
//! `mcc-desktop`, run from a terminal while a window is already open, does
//! not open a second one -- it touches `desktop.activate` in the
//! configuration directory and expects whoever owns the window to come to the
//! front. That file is the whole protocol, and this module is the whole of
//! this shells half of it.
//!
//! The directory it lives in is read from the status document. This module
//! never resolves it (C1).
//!
//! What this shell must *not* do is take `desktop.lock`: that is a
//! byte-range lock on Windows and an advisory `flock` elsewhere, the two are
//! not portably interoperable with a Rust lock on the same file, and getting
//! it wrong yields two trays. Python stays the owner of that file; this
//! window has its own guard, in `tauri-plugin-single-instance`.

use std::fs;
use std::path::{Path, PathBuf};

/// The doorbell file, as `cli/desktop.py` spells it.
pub const ACTIVATION_FILENAME: &str = "desktop.activate";

/// The doorbell inside a configuration directory.
pub fn activation_path(config_dir: &str) -> PathBuf {
    Path::new(config_dir).join(ACTIVATION_FILENAME)
}

/// Answer the doorbell: `true` when it had been rung, and it is now cleared.
///
/// Removing the file is what makes the ring an edge rather than a level. If
/// removal fails -- another process is mid-write, a permissions oddity -- the
/// ring is reported anyway and the next poll simply rings again. A window
/// that raises itself twice is a nuisance; one that never raises is a bug.
pub fn take_ring(path: &Path) -> bool {
    if !path.exists() {
        return false;
    }
    let _ = fs::remove_file(path);
    true
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
    fn the_doorbell_file_raises_the_window_once() {
        let directory = scratch_dir("doorbell");
        let path = activation_path(&directory.to_string_lossy());
        assert!(!take_ring(&path), "a silent doorbell is not a ring");

        fs::write(&path, "").expect("rang");
        assert!(take_ring(&path), "a rung doorbell is a ring");
        assert!(
            !take_ring(&path),
            "one ring raises the window once, not forever"
        );
        fs::remove_dir_all(&directory).ok();
    }

    #[test]
    fn the_path_is_built_from_the_directory_python_reported() {
        let path = activation_path("/somewhere/else/entirely");
        assert!(path.ends_with(ACTIVATION_FILENAME));
        assert!(path.to_string_lossy().contains("somewhere"));
    }
}
