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

/// The smallest window this shell will ever restore to. The same numbers
/// `WebviewWindowBuilder::min_inner_size` is given, because a remembered
/// geometry smaller than the floor the window enforces is not a geometry the
/// window ever actually had.
pub const MIN_WIDTH: u32 = 640;
pub const MIN_HEIGHT: u32 = 480;

/// How much of a window has to land inside a monitor's work area for the
/// window to count as reachable.
///
/// Not one pixel. A window whose extreme corner clips a screen is not
/// something a person can grab: the fix has to leave enough title bar to drag.
/// Roughly a button and a bit.
pub const VISIBLE_WIDTH: i64 = 120;
pub const VISIBLE_HEIGHT: i64 = 40;

/// A rectangle in physical pixels: a window, or a monitor's work area.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Rect {
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
}

impl Rect {
    /// The width and height the two rectangles share. `(0, 0)` when they do
    /// not touch. `i64` throughout: `x + width` overflows `i32` at the
    /// coordinates Windows reports for a minimized window, and an overflow
    /// here would be a panic in the one code path whose entire job is to
    /// survive a nonsense rectangle.
    fn overlap(&self, other: &Self) -> (i64, i64) {
        let span = |a: i32, a_len: u32, b: i32, b_len: u32| {
            let start = i64::from(a).max(i64::from(b));
            let end = (i64::from(a) + i64::from(a_len)).min(i64::from(b) + i64::from(b_len));
            (end - start).max(0)
        };
        (
            span(self.x, self.width, other.x, other.width),
            span(self.y, self.height, other.y, other.height),
        )
    }
}

/// Whether a remembered rectangle may be restored (BUG-7).
///
/// Two ways a saved geometry can be unusable, and this machine produced both
/// on 2026-09-08:
///
/// * it is smaller than the window's own minimum -- `0x0` is what Windows
///   reports for a minimized window's inner size;
/// * it is nowhere near a screen -- `-32000, -32000` is where Windows parks a
///   minimized window, and it is also where a window ends up when the monitor
///   it was on has been unplugged.
///
/// Restoring either produces an app that is running, has a tray icon, and has
/// no window anybody can find. The caller falls back to the *configured* size,
/// centred, rather than clamping: a rectangle this function rejects is not a
/// rectangle with a small mistake in it, it is one that was never a window.
///
/// An empty monitor list means the question cannot be answered -- no display
/// server, a query that failed -- and the honest answer to a question that
/// cannot be answered is not "reject the user's geometry". Size still decides.
pub fn geometry_is_usable(rect: Rect, work_areas: &[Rect]) -> bool {
    if rect.width < MIN_WIDTH || rect.height < MIN_HEIGHT {
        return false;
    }
    if work_areas.is_empty() {
        return true;
    }
    work_areas.iter().any(|area| {
        let (width, height) = rect.overlap(area);
        width >= VISIBLE_WIDTH && height >= VISIBLE_HEIGHT
    })
}

/// Whether the window's geometry may be written to disk right now (BUG-7).
///
/// The save side of the same defect, and the one that actually caused it.
/// Tray **Quit** saved geometry *after* a close-to-tray `hide()`, so what was
/// persisted was Windows' answer for a hidden, minimized window -- `0x0` at
/// `-32000, -32000` -- and the next launch restored it faithfully. The rule is
/// to skip, not to clamp: a hidden window has no geometry worth recording, and
/// the geometry already on disk is the last one it really had.
pub fn may_save_geometry(visible: bool, minimized: bool) -> bool {
    visible && !minimized
}

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
    /// The configured size this shell last opened at, so a change made in the
    /// dashboard is honoured once even after the window has been resized by
    /// hand.
    ///
    /// The same rule `AppModeWindow._pending_window_size` has followed for
    /// Chromium since 6.44.0, and for the same reason: without it
    /// `DESKTOP_WINDOW_WIDTH`/`HEIGHT` mean something on the first run and
    /// nothing ever again. It is kept *here*, in the shell's own data
    /// directory, and not in `desktop.json` where Python keeps its own copy of
    /// the same idea, because C4 forbids this binary writing under the
    /// configuration directory.
    #[serde(default)]
    pub last_applied_width: Option<u32>,
    #[serde(default)]
    pub last_applied_height: Option<u32>,
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

    /// The remembered rectangle, when all four numbers are there.
    pub fn rect(&self) -> Option<Rect> {
        Some(Rect {
            x: self.x?,
            y: self.y?,
            width: self.width?,
            height: self.height?,
        })
    }

    /// Whether the configured size must be applied to this launch (BUG-7).
    ///
    /// Two reasons, and the user named both:
    ///
    /// * there is no usable remembered geometry -- a first run, or a file that
    ///   says `0x0` at `-32000, -32000`. "By default it should start
    ///   1400x900."
    /// * the configured size has changed since this shell last applied it. "A
    ///   dashboard change to the size must not be silently ignored after the
    ///   first run."
    ///
    /// Otherwise the remembered geometry wins, which is what makes resizing
    /// the window by hand stick.
    pub fn configured_size_wins(&self, configured: (u32, u32), work_areas: &[Rect]) -> bool {
        if !self
            .rect()
            .is_some_and(|rect| geometry_is_usable(rect, work_areas))
        {
            return true;
        }
        (self.last_applied_width, self.last_applied_height)
            != (Some(configured.0), Some(configured.1))
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

/// Forget the remembered geometry, so the next paint is the configured size,
/// centred. What `--reset-window` and the tray's "Reset window position" do.
/// A file that is not there is already reset, so a missing file is a success.
pub fn clear(path: &Path) -> io::Result<()> {
    match fs::remove_file(path) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        other => other,
    }
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
            last_applied_width: Some(1400),
            last_applied_height: Some(900),
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

    /// One 1920x1080 screen with a 40px taskbar, which is what the machine
    /// that produced BUG-7 actually has.
    fn one_screen() -> Vec<Rect> {
        vec![Rect {
            x: 0,
            y: 0,
            width: 1920,
            height: 1040,
        }]
    }

    #[test]
    fn the_geometry_windows_reports_for_a_minimized_window_is_rejected() {
        // The exact file this machine wrote on 2026-09-08:
        // {"x":-32000,"y":-32000,"width":0,"height":0,"maximized":false}.
        // Restoring it produced an app with a tray icon and no window.
        let poisoned = Rect {
            x: -32000,
            y: -32000,
            width: 0,
            height: 0,
        };
        assert!(!geometry_is_usable(poisoned, &one_screen()));
        // And with a real size but the minimized position, which is what a
        // window minimized from a normal size reports.
        assert!(!geometry_is_usable(
            Rect {
                x: -32000,
                y: -32000,
                width: 1400,
                height: 900,
            },
            &one_screen()
        ));
    }

    #[test]
    fn an_ordinary_window_is_kept() {
        assert!(geometry_is_usable(
            Rect {
                x: 260,
                y: 70,
                width: 1400,
                height: 900,
            },
            &one_screen()
        ));
    }

    #[test]
    fn a_window_on_a_monitor_that_is_no_longer_plugged_in_is_rejected() {
        // A second screen to the left, then unplugged. The rectangle is
        // perfectly well formed; there is simply nothing there any more.
        let on_the_second_screen = Rect {
            x: -1900,
            y: 100,
            width: 1400,
            height: 900,
        };
        let two = vec![
            Rect {
                x: -1920,
                y: 0,
                width: 1920,
                height: 1040,
            },
            one_screen()[0],
        ];
        assert!(geometry_is_usable(on_the_second_screen, &two));
        assert!(!geometry_is_usable(on_the_second_screen, &one_screen()));
    }

    #[test]
    fn a_window_hanging_off_an_edge_is_kept_only_while_it_can_be_grabbed() {
        // 200px of it on screen: draggable, so it survives.
        assert!(geometry_is_usable(
            Rect {
                x: 1720,
                y: 200,
                width: 1400,
                height: 900,
            },
            &one_screen()
        ));
        // 10px of it on screen: there is nothing to click, so it does not.
        assert!(!geometry_is_usable(
            Rect {
                x: 1910,
                y: 200,
                width: 1400,
                height: 900,
            },
            &one_screen()
        ));
    }

    #[test]
    fn a_window_smaller_than_the_windows_own_minimum_is_rejected() {
        assert!(!geometry_is_usable(
            Rect {
                x: 100,
                y: 100,
                width: MIN_WIDTH - 1,
                height: MIN_HEIGHT,
            },
            &one_screen()
        ));
    }

    #[test]
    fn a_machine_that_reports_no_monitors_still_gets_its_window_back() {
        // No display server, or a query that failed. Refusing every remembered
        // geometry here would move the window on every launch for a reason
        // that is not about the window at all.
        assert!(geometry_is_usable(
            Rect {
                x: 260,
                y: 70,
                width: 1400,
                height: 900,
            },
            &[]
        ));
        // The size floor still applies -- it needs no monitor to decide.
        assert!(!geometry_is_usable(
            Rect {
                x: -32000,
                y: -32000,
                width: 0,
                height: 0,
            },
            &[]
        ));
    }

    #[test]
    fn geometry_is_never_saved_from_a_hidden_or_minimized_window() {
        // The mechanism of BUG-7: tray Quit ran `save_geometry` on a window a
        // close-to-tray had already hidden.
        assert!(!may_save_geometry(false, false));
        assert!(!may_save_geometry(true, true));
        assert!(!may_save_geometry(false, true));
        assert!(may_save_geometry(true, false));
    }

    #[test]
    fn a_poisoned_state_opens_at_the_configured_size() {
        let poisoned = WindowState {
            x: Some(-32000),
            y: Some(-32000),
            width: Some(0),
            height: Some(0),
            ..WindowState::default()
        };
        assert!(poisoned.configured_size_wins((1400, 900), &one_screen()));
    }

    #[test]
    fn a_dashboard_size_change_is_honoured_once_after_the_first_run() {
        // The user's rule (13:24): the configured size must not be silently
        // ignored after the first run.
        let resized_by_hand = WindowState {
            x: Some(200),
            y: Some(100),
            width: Some(1000),
            height: Some(700),
            last_applied_width: Some(1400),
            last_applied_height: Some(900),
            ..WindowState::default()
        };
        // Nothing changed in the dashboard: the hand-resized window wins.
        assert!(!resized_by_hand.configured_size_wins((1400, 900), &one_screen()));
        // The operator set a new size: it is applied once.
        assert!(resized_by_hand.configured_size_wins((1600, 1000), &one_screen()));
    }

    #[test]
    fn a_first_run_takes_the_configured_size() {
        assert!(WindowState::default().configured_size_wins((1400, 900), &one_screen()));
    }

    #[test]
    fn clearing_the_state_is_a_reset_and_a_missing_file_is_already_reset() {
        let directory = scratch_dir("reset");
        let path = state_path(&directory);
        clear(&path).expect("a missing file is already reset");
        save(
            &path,
            &WindowState {
                width: Some(0),
                height: Some(0),
                ..WindowState::default()
            },
        )
        .expect("saved");
        clear(&path).expect("cleared");
        assert!(!path.exists());
        assert_eq!(load(&path), WindowState::default());
        fs::remove_dir_all(&directory).ok();
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
