//! The My Claude Code desktop window.
//!
//! It renders one URL: the admin dashboard the MCC server already serves.
//! There is no product surface here and there is deliberately no second copy
//! of any decision Python already makes -- where the configuration lives,
//! which port to use, which URL to load, how long to wait for a restart. All
//! of that arrives in one JSON document from `mcc-desktop --print-status`.
//!
//! What lives here, and nowhere else, is the window: a splash while the
//! answer is being fetched, a tray icon, a single-instance guard, remembered
//! geometry, and the lifecycle controller that decides between attaching to a
//! healthy server, starting one, explaining a port conflict, or installing MCC.
//!
//! From 6.61.0 all of that deciding lives in one pure function --
//! `controller::step` -- run on one tick by [`run_controller`]. The seven
//! `wait_for_*` loops it replaced each owned a piece of the timing, and six of
//! them could reach a page with no loop behind it.
//!
//! Contracts this file is answerable for: C1 (nothing is resolved here),
//! C4 (nothing under the configuration directory is written), C5 (no
//! updater), C8 (the pages it serves itself never fetch), C9 (every budget
//! comes from the status document).

pub mod activation;
pub mod controller;
pub mod health;
pub mod install;
pub mod process;
pub mod status;
pub mod swap;
pub mod ui;
pub mod update_progress;
pub mod window_state;

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::webview::{PageLoadEvent, WebviewWindowBuilder};
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindow, WindowEvent, Wry};

use crate::controller::Effect;
use crate::status::Status;
use crate::ui::Page;

/// The single windows label. One window, one label, everywhere.
const MAIN_WINDOW: &str = "main";

/// Injected into every document this window loads.
///
/// The dashboard's Update button reads `window.__mccShellWatching` and, when
/// it is set, tells the server that the caller owns the restart -- so
/// `apply-upgrade.ps1` installs and exits and this window's next tick starts
/// the new server. The same page in a browser tab does not see it, and the
/// helper restarts exactly as it did before (GAP-3, decision Q4).
const SHELL_MARKER_SCRIPT: &str = "window.__mccShellWatching = true;";

/// The release this binary was built from, stamped in by `shell-release.yml`
/// (`MCC_SHELL_RELEASE_TAG`), and printed by `--version`.
///
/// `option_env!` rather than `env!`: a developer build has no release to name,
/// and refusing to compile without one would mean this window could only be
/// built in CI. `None` is "I do not know what I am", and a window that does
/// not know what it is never claims to be stale -- asking Python to replace a
/// binary on a guess is exactly the wrong response to missing information.
pub const RELEASE_TAG: Option<&str> = option_env!("MCC_SHELL_RELEASE_TAG");

/// Whether the wheel on this machine pins a different release of this window.
///
/// The comparison BUG-0 needed and nothing more: two strings, from two places
/// that are updated by two different mechanisms. Unknown on either side means
/// no. A trimmed comparison because a tag that arrives with a newline is the
/// same tag.
pub fn shell_is_stale(compiled: Option<&str>, pinned: Option<&str>) -> bool {
    match (compiled, pinned) {
        (Some(compiled), Some(pinned)) => {
            let (compiled, pinned) = (compiled.trim(), pinned.trim());
            !compiled.is_empty() && !pinned.is_empty() && compiled != pinned
        }
        _ => false,
    }
}

/// First-paint size, used only until the status document says what the
/// operator configured. Not a configuration value: nothing about the config
/// directory, the port or the URL is decided here (C1).
///
/// It matches `DESKTOP_WINDOW_WIDTH`/`HEIGHT`'s own default deliberately --
/// "by default it should start 1400x900" -- so the window does not visibly
/// resize itself a second after opening on the overwhelmingly common machine
/// where the operator has not changed either setting.
const FIRST_PAINT_WIDTH: f64 = 1400.0;
const FIRST_PAINT_HEIGHT: f64 = 900.0;

/// The tray mark, compiled in. The tray has to exist before any status has
/// been read, so it cannot come from a file the installer may not have put
/// anywhere yet.
const TRAY_ICON: &[u8] = include_bytes!("../icons/tray-icon.png");

/// How long to let a freshly navigated local page define its receiver before
/// pushing state at it. The push is idempotent and the page also picks up a
/// pending state on load, so this is a smoothing delay, not a correctness one.
const PAGE_SETTLE: Duration = Duration::from_millis(500);

/// Set to a non-empty value to launch a window that does not join the
/// single-instance group.
///
/// Test-only, and it exists for exactly one reason: verifying a change to this
/// shell means running a *second* shell on a machine where the developer's own
/// is already open, and `tauri-plugin-single-instance` would otherwise hand
/// the launch straight to that one -- so the build under test never runs and
/// the proof is of the wrong binary. A launch with this set registers no
/// handler and signals nothing, so the already-running window is not disturbed
/// either. It is the same family of override as `MCC_SHELL_DESKTOP_COMMAND`
/// and the window-state directory: nothing a normal launch reads.
pub const SEPARATE_INSTANCE_ENV: &str = "MCC_SHELL_SEPARATE_INSTANCE";

/// How long a process started by "Restart now" waits before it does anything.
///
/// Not a cosmetic pause. The window that presses that button is holding the
/// single-instance group: `tauri-plugin-single-instance` registers a mutex and
/// a hidden window under the app identifier, and a second launch that finds
/// that window hands its arguments to it and *exits*. So a staged binary
/// started while the old one is still shutting down would hand itself straight
/// back to the process it was meant to replace, and the user would be left
/// with no window and an update that did not happen.
///
/// The old process sets this on the child and then exits; the child waits it
/// out before it builds anything. It is bounded and short: the parent's exit
/// is `app.exit(0)` on a window that has already saved its geometry.
///
/// The other handover -- an ordinary launch finding a `.new` beside it -- does
/// not need this and does not set it: that parent has not registered anything
/// yet, because the swap runs before the Tauri builder.
pub const SWAP_DELAY_ENV: &str = "MCC_SHELL_SWAP_DELAY_MS";

/// The delay `SWAP_DELAY_ENV` asks for, clamped to something a person will sit
/// through. Anything unreadable is no delay at all.
pub fn swap_delay(raw: Option<&str>) -> Duration {
    let millis = raw
        .and_then(|value| value.trim().parse::<u64>().ok())
        .unwrap_or(0)
        .min(10_000);
    Duration::from_millis(millis)
}

/// Whether this launch should stand apart from the single-instance group.
pub fn separate_instance(raw: Option<&str>) -> bool {
    raw.is_some_and(|value| !value.trim().is_empty())
}

static RETRY_REQUESTED: AtomicBool = AtomicBool::new(false);
/// Whether the user pressed "Take port" (decision Q1). Only ever offered for a
/// holder Python identified, by process, as one of MCC's own.
static TAKE_PORT_REQUESTED: AtomicBool = AtomicBool::new(false);
/// The page this window is currently showing, kept so a reload can be answered
/// with the same one.
///
/// BUG-2, and it is worth spelling out. Every page here was *pushed* into the
/// document with `eval`; a reload -- F5, Ctrl+R, or a webview that reloaded
/// itself -- threw the pushed state away and left a window that said "Checking
/// the server..." over a loop that was in fact working. The page now asks for
/// the current state on load (`shell_page`) and the shell re-pushes it when a
/// load finishes, so the two halves cannot disagree. `None` means the window is
/// showing the dashboard, which is not this shell's page to restore.
static CURRENT_PAGE: Mutex<Option<Page>> = Mutex::new(None);
/// Installs run in this session that did not end with a runnable
/// `mcc-desktop`. Reset by the first status read that works.
static INSTALLS_RUN: AtomicU32 = AtomicU32::new(0);
/// The installer's last word, kept so the page that gives up can quote it
/// rather than showing a spinner over nothing.
static LAST_INSTALL_LINE: Mutex<String> = Mutex::new(String::new());
/// Whether closing the window hides it instead of ending the app. Read from
/// the status document on every status read; `false` until one has been read,
/// so a window that has learned nothing yet still closes when told to.
static CLOSE_TO_TRAY: AtomicBool = AtomicBool::new(false);
static TRAY_BUILT: AtomicBool = AtomicBool::new(false);
/// Whether a verified replacement for this binary is staged and waiting for a
/// restart, and which release it is. Set once, by the thread that ran
/// `mcc-desktop --ensure-shell`.
static UPDATE_STAGED: AtomicBool = AtomicBool::new(false);
static STAGED_TAG: Mutex<Option<String>> = Mutex::new(None);
/// Whether the pin has already been compared with this build's tag. Once per
/// launch: the answer cannot change while the process runs, and re-running a
/// download every tick would be a denial of service on the release page.
static SHELL_PIN_CHECKED: AtomicBool = AtomicBool::new(false);
/// Whether this launch has already applied the configured window size. The
/// geometry rules are per *launch*, not per tick -- a window that
/// re-centred itself every five seconds would be worse than one that opened in
/// the wrong place.
static GEOMETRY_APPLIED: AtomicBool = AtomicBool::new(false);
/// This process's own executable, resolved once and after any rename the swap
/// step made, so `--ensure-shell` is pointed at the file that is actually
/// running.
static EXE_PATH: OnceLock<PathBuf> = OnceLock::new();
/// Whether this launch was asked to forget the remembered geometry. Read in
/// `setup()`, which is the first place the data directory is known.
static RESET_WINDOW: AtomicBool = AtomicBool::new(false);
static ACTIVATION_STARTED: AtomicBool = AtomicBool::new(false);
static QUITTING: AtomicBool = AtomicBool::new(false);

/// The configuration directory Python last reported, and the file it is
/// remembered in.
///
/// The shell resolves nothing (C1), so this is not a resolution: it is the
/// last answer `mcc-desktop --print-status` gave. It has to be remembered
/// because the one moment the shell most needs it is the moment it cannot ask
/// -- `mcc-desktop` is not runnable, which is exactly when an update helper
/// may be rewriting the shims, and reading its receipt is the difference
/// between waiting for that helper and racing it. Kept in the shell's own data
/// directory, never under the configuration directory (C4).
static LAST_CONFIG_DIR: Mutex<Option<String>> = Mutex::new(None);
const LAST_CONFIG_DIR_FILE: &str = "last-config-dir.txt";

static DATA_DIR: OnceLock<PathBuf> = OnceLock::new();
static LOCAL_URL: OnceLock<String> = OnceLock::new();
static TRAY_STATUS_ITEM: Mutex<Option<MenuItem<Wry>>> = Mutex::new(None);
static TRAY_UPDATE_ITEM: Mutex<Option<MenuItem<Wry>>> = Mutex::new(None);

// -- commands the page may call -------------------------------------------

/// The Retry button. The only thing the user can ask of this window that the
/// window itself has to act on.
#[tauri::command]
fn shell_retry() {
    RETRY_REQUESTED.store(true, Ordering::SeqCst);
}

/// Called once by every page this shell serves, so a broken control channel
/// is visible in the window rather than only in a log nobody opens.
#[tauri::command]
fn shell_ready() -> bool {
    true
}

/// The page the controller is on, asked for by the document as it loads.
///
/// The other half of BUG-2's fix. A pushed state is lost by a reload; a state
/// the page *pulls* cannot be. Both halves are kept because they answer
/// different races -- this one covers a document that loaded before Rust
/// noticed, and the re-push in `on_page_load` covers a document that loaded
/// while Rust was between ticks.
#[tauri::command]
fn shell_page() -> Option<Page> {
    CURRENT_PAGE.lock().ok().and_then(|guard| guard.clone())
}

/// The "Take port" button. Decision Q1: it is only ever rendered for a holder
/// identified as MCC's own, and all it does is bring the next start forward --
/// the kill is the server's own `SERVER_PORT_TAKEOVER`, inside `mcc-server`,
/// where it belongs.
#[tauri::command]
fn shell_take_port() {
    TAKE_PORT_REQUESTED.store(true, Ordering::SeqCst);
    RETRY_REQUESTED.store(true, Ordering::SeqCst);
}

// -- window plumbing -------------------------------------------------------

fn data_dir(app: &AppHandle) -> PathBuf {
    DATA_DIR
        .get_or_init(|| {
            // The override exists for the release smoke, which must exercise a
            // real window without disturbing the geometry of the one the
            // developer actually uses.
            if let Ok(raw) = std::env::var(window_state::DATA_DIR_ENV) {
                if !raw.trim().is_empty() {
                    return PathBuf::from(raw);
                }
            }
            app.path()
                .app_data_dir()
                .unwrap_or_else(|_| std::env::temp_dir().join("my-claude-code-shell"))
        })
        .clone()
}

/// Remember where the window is -- but only while it is somewhere (BUG-7).
///
/// The guard is the fix. Tray **Quit** called this on a window a close-to-tray
/// had already hidden, and Windows answers `inner_size` and `outer_position`
/// for a hidden, minimized window with `0x0` at `-32000, -32000`. Those values
/// were written to `window.json` and restored verbatim on the next launch, so
/// the app came back as a tray icon with no window anywhere on any screen.
/// It happened to this user twice on 2026-09-08.
///
/// Skipping is right and clamping is not: a hidden window has no geometry
/// worth recording, and what is already on disk is the last geometry it really
/// had. A read that fails is treated the same way -- an unanswerable question
/// is not a reason to overwrite a good answer with a guess.
fn save_geometry(window: &WebviewWindow) {
    let Some(directory) = DATA_DIR.get() else {
        return;
    };
    let visible = window.is_visible().unwrap_or(false);
    let minimized = window.is_minimized().unwrap_or(false);
    if !window_state::may_save_geometry(visible, minimized) {
        return;
    }
    let maximized = window.is_maximized().unwrap_or(false);
    let mut state = window_state::WindowState {
        maximized,
        ..window_state::load(&window_state::state_path(directory))
    };
    // A maximized windows inner size is the screen, not the size to restore
    // to, so only an ordinary window updates the remembered geometry.
    if !maximized {
        // The *inner* size, because that is what is restored: an outer size
        // written here and applied as an inner size next launch would grow the
        // window by the height of its own title bar on every run.
        if let Ok(size) = window.inner_size() {
            state.width = Some(size.width);
            state.height = Some(size.height);
        }
        if let Ok(position) = window.outer_position() {
            state.x = Some(position.x);
            state.y = Some(position.y);
        }
    }
    let _ = window_state::save(&window_state::state_path(directory), &state);
}

fn raise(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

// -- the page ---------------------------------------------------------------

/// Show one of the shells own pages, navigating back from the dashboard if
/// that is where the window currently is.
fn show_page(window: &WebviewWindow, page: &Page) {
    remember_page(Some(page.clone()));
    let local = LOCAL_URL.get().cloned().unwrap_or_default();
    let current = window.url().map(|url| url.to_string()).unwrap_or_default();
    let elsewhere = current != local;
    if elsewhere && !local.is_empty() {
        if let Ok(parsed) = url::Url::parse(&local) {
            let _ = window.navigate(parsed);
            std::thread::sleep(PAGE_SETTLE);
        }
    }
    let _ = window.eval(ui::render_script(page));
}

/// Record the page the window is on, for a reload to ask about.
fn remember_page(page: Option<Page>) {
    if let Ok(mut guard) = CURRENT_PAGE.lock() {
        *guard = page;
    }
}

/// Re-push the current page after a document finished loading.
///
/// A reload lands here, and so does the navigation back from the dashboard.
/// Idempotent by construction: the page renders whatever it is given.
fn repush_current_page(window: &WebviewWindow) {
    let Some(page) = shell_page() else {
        return;
    };
    let _ = window.eval(ui::render_script(&page));
}

fn append_output(window: &WebviewWindow, line: &str) {
    let _ = window.eval(ui::append_output_script(line));
}

/// Load the dashboard itself. The URL is whatever Python said it was (C1).
fn show_dashboard(window: &WebviewWindow, admin_url: &str) {
    remember_page(None);
    match url::Url::parse(admin_url) {
        Ok(parsed) => {
            let _ = window.navigate(parsed);
        }
        Err(error) => show_page(
            window,
            &Page::Error {
                message: format!("The dashboard address {admin_url} could not be read: {error}"),
                server_log: None,
            },
        ),
    }
}

/// Whether a close request should hide the window instead of ending the app.
///
/// Pure, so the rule is a unit test rather than a hand-run of a window that
/// only exists on a desktop. Two inputs, and the second one is the whole
/// subtlety: Quit -- from the tray menu or from this window -- sets `quitting`
/// first, and a quit that got hidden instead of quitting is an app the user
/// cannot close at all.
pub fn should_hide_on_close(close_to_tray: bool, quitting: bool) -> bool {
    close_to_tray && !quitting
}

// -- tray -------------------------------------------------------------------

fn set_tray_status(text: &str) {
    if let Ok(guard) = TRAY_STATUS_ITEM.lock() {
        if let Some(item) = guard.as_ref() {
            let _ = item.set_text(text);
        }
    }
}

/// Build the tray once, and only when the operator has one enabled.
///
/// `tray_enabled` is the same switch the Python tray reads. Honouring it is
/// what stops two icons appearing while the Python tray remains the fallback
/// on Windows and macOS.
fn ensure_tray(app: &AppHandle, status: &Status) {
    if !status.tray_enabled || TRAY_BUILT.swap(true, Ordering::SeqCst) {
        return;
    }
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        if let Err(error) = build_tray(&handle) {
            eprintln!("the tray could not be created: {error}");
        }
    });
}

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "Open My Claude Code", true, None::<&str>)?;
    let status = MenuItem::with_id(app, "status", "Checking the server...", false, None::<&str>)?;
    // Disabled and quiet until there is something staged. The item exists from
    // the start rather than being added later because a tray menu cannot grow
    // an item after it is built, and a menu that changes shape under the
    // cursor is worse than one line that is greyed out.
    let update = MenuItem::with_id(
        app,
        "restart-update",
        "Desktop app is up to date",
        false,
        None::<&str>,
    )?;
    // The escape hatch for BUG-7. It is in the tray and not in the window
    // because the whole failure mode is that there is no window to click.
    let reset = MenuItem::with_id(
        app,
        "reset-window",
        "Reset window position",
        true,
        None::<&str>,
    )?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let second_separator = PredefinedMenuItem::separator(app)?;
    let menu = Menu::with_items(
        app,
        &[
            &open,
            &status,
            &separator,
            &update,
            &reset,
            &second_separator,
            &quit,
        ],
    )?;

    let mut builder = TrayIconBuilder::with_id("mcc-shell-tray")
        .tooltip("My Claude Code")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => raise(app),
            "restart-update" => restart_into_staged(app),
            "reset-window" => reset_window_position(app),
            "quit" => {
                QUITTING.store(true, Ordering::SeqCst);
                if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
                    save_geometry(&window);
                }
                app.exit(0);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                raise(tray.app_handle());
            }
        });
    // The tray mark, not the app mark: it carries a 2% margin instead of 10%,
    // which is what makes it legible at the 16-24px a status area draws.
    match tauri::image::Image::from_bytes(TRAY_ICON) {
        Ok(icon) => builder = builder.icon(icon),
        // A tray with the app icon is worse than a tray with the tray icon and
        // far better than no tray at all.
        Err(_) => {
            if let Some(icon) = app.default_window_icon().cloned() {
                builder = builder.icon(icon);
            }
        }
    }
    builder.build(app)?;

    if let Ok(mut guard) = TRAY_UPDATE_ITEM.lock() {
        *guard = Some(update);
    }
    // A staged update found before the tray existed still has to reach it.
    announce_staged_update();
    if let Ok(mut guard) = TRAY_STATUS_ITEM.lock() {
        *guard = Some(status);
    }
    Ok(())
}

/// Remember the configuration directory a status document named.
fn remember_config_dir(config_dir: &str) {
    if config_dir.trim().is_empty() {
        return;
    }
    if let Ok(mut guard) = LAST_CONFIG_DIR.lock() {
        if guard.as_deref() == Some(config_dir) {
            return;
        }
        *guard = Some(config_dir.to_owned());
    }
    if let Some(directory) = DATA_DIR.get() {
        let _ = std::fs::create_dir_all(directory);
        let _ = std::fs::write(directory.join(LAST_CONFIG_DIR_FILE), config_dir);
    }
}

/// The configuration directory Python last named, from this session or the
/// previous one. `None` before the shell has ever read a status document.
fn last_config_dir() -> Option<String> {
    if let Ok(guard) = LAST_CONFIG_DIR.lock() {
        if let Some(directory) = guard.as_deref() {
            return Some(directory.to_owned());
        }
    }
    let directory = DATA_DIR.get()?;
    let remembered = std::fs::read_to_string(directory.join(LAST_CONFIG_DIR_FILE)).ok()?;
    let remembered = remembered.trim();
    if remembered.is_empty() {
        return None;
    }
    if let Ok(mut guard) = LAST_CONFIG_DIR.lock() {
        *guard = Some(remembered.to_owned());
    }
    Some(remembered.to_owned())
}

// -- installing MCC ---------------------------------------------------------

/// Run the install script, streaming its output into the window.
///
/// Returns the installer's last meaningful line, which is what the page that
/// gives up quotes. A first install is minutes long and the only thing the
/// user has to go on is what the installer itself said.
fn run_install(window: &WebviewWindow, attempt: u32) -> String {
    let command = install::install_command_for_this_machine();
    show_page(
        window,
        &Page::Installing {
            command: command.display.clone(),
            message: format!(
                "My Claude Code is not installed here yet, so this window is \
                 installing it (attempt {attempt} of {}). This takes a few \
                 minutes the first time.",
                controller::INSTALL_ATTEMPTS
            ),
        },
    );
    set_tray_status("Installing My Claude Code...");
    let mut last = String::new();
    let outcome = process::run_install(&command, |line| {
        append_output(window, line);
        if !line.trim().is_empty() && !line.starts_with("-- still installing") {
            last = line.to_owned();
        }
    });
    let clean = matches!(outcome, Ok(0));
    let ending = match outcome {
        Ok(0) => "-- install finished, checking again --".to_owned(),
        Ok(code) => format!("-- the installer exited with status {code} --"),
        Err(error) => format!("-- {error} --"),
    };
    append_output(window, &ending);
    // A clean install has nothing to complain about, so the useful last word
    // is the installer's own; anything else is the failure itself.
    if clean { last } else { ending }
}

/// Apply the parts of the status document that shape the window itself.
fn apply_status(window: &WebviewWindow, status: &Status) {
    // Python's answer, used verbatim (C1). Deliberately NOT
    // `minimize_to_tray && tray_enabled`: `tray_enabled` in this document
    // means "should THIS window draw an icon", and it is false on Windows and
    // macOS *because* the Python tray is already drawing one. ANDing them is
    // what made the close button end the app -- and take the tray and the
    // server with it -- on the two platforms that have a tray at all.
    CLOSE_TO_TRAY.store(status.close_to_tray, Ordering::SeqCst);
    let Some(directory) = DATA_DIR.get() else {
        return;
    };
    apply_window_size(window, directory, status);
}

/// Decide, once per launch, whether the configured size wins over what was
/// remembered -- and apply it if it does (BUG-7).
///
/// Two rules, both the user's, and both broken before 6.60.0:
///
/// * a remembered geometry that is not on any screen, or is below the window's
///   own minimum, is not restored at all: the window opens at the *configured*
///   size, centred. Until now `setup()` restored `0x0` at `-32000, -32000`
///   without looking at it.
/// * a configured size that has changed since this shell last applied it is
///   honoured once, even though something is remembered. Until now
///   `DESKTOP_WINDOW_WIDTH`/`HEIGHT` meant something on the first run and
///   nothing ever again.
///
/// Otherwise the remembered geometry stands, which is what makes dragging the
/// window's corner stick. `last_applied_*` is written whenever the configured
/// size is applied, and it lives in the shell's own state file rather than in
/// `desktop.json` where Python keeps the same idea for Chromium: C4 forbids
/// this binary writing under the configuration directory.
fn apply_window_size(window: &WebviewWindow, directory: &Path, status: &Status) {
    if GEOMETRY_APPLIED.swap(true, Ordering::SeqCst) {
        return;
    }
    let path = window_state::state_path(directory);
    let mut remembered = window_state::load(&path);
    let configured = (
        status.window_width.max(window_state::MIN_WIDTH),
        status.window_height.max(window_state::MIN_HEIGHT),
    );
    if !remembered.configured_size_wins(configured, &work_areas(window)) {
        return;
    }
    let _ = window.set_size(tauri::LogicalSize::new(
        f64::from(configured.0),
        f64::from(configured.1),
    ));
    let _ = window.center();
    remembered.last_applied_width = Some(configured.0);
    remembered.last_applied_height = Some(configured.1);
    // The size is recorded, the position is not: `center()` has not finished
    // when this runs, and the next ordinary save writes the real rectangle.
    remembered.width = Some(configured.0);
    remembered.height = Some(configured.1);
    remembered.x = None;
    remembered.y = None;
    let _ = window_state::save(&path, &remembered);
}

/// The work areas of every connected monitor, in physical pixels.
///
/// The *work* area and not the full bounds, so a window remembered entirely
/// behind the taskbar is treated as unreachable -- which it is. An empty list
/// (no display server, or a query that failed) is passed through as an empty
/// list, and `geometry_is_usable` answers "keep the geometry" rather than
/// pretending it knows better.
fn work_areas(window: &WebviewWindow) -> Vec<window_state::Rect> {
    window
        .available_monitors()
        .unwrap_or_default()
        .iter()
        .map(|monitor| {
            let area = monitor.work_area();
            window_state::Rect {
                x: area.position.x,
                y: area.position.y,
                width: area.size.width,
                height: area.size.height,
            }
        })
        .collect()
}

/// Throw away the remembered geometry and open at the configured size again.
/// The tray's "Reset window position", and what `--reset-window` arranges for
/// the launch that follows it.
fn reset_window_position(app: &AppHandle) {
    if let Some(directory) = DATA_DIR.get() {
        let _ = window_state::clear(&window_state::state_path(directory));
    }
    GEOMETRY_APPLIED.store(false, Ordering::SeqCst);
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let _ = window.unmaximize();
        let _ = window.set_size(tauri::LogicalSize::new(
            FIRST_PAINT_WIDTH,
            FIRST_PAINT_HEIGHT,
        ));
        let _ = window.center();
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

/// Ask Python to stage the pinned shell, once, when this build is not it.
///
/// The whole of BUG-0's second half. Until 6.60.0 the pin was enforced only by
/// `ShellWindow.create()` -- the Python tray's window factory -- so a user who
/// launched `MyClaudeCode.exe` from the Start Menu, the taskbar, or the
/// Programs-folder install kept whichever shell they first received. This user
/// ran v6.43.0 for fifteen releases while the wheel moved to 6.58.4, and every
/// fix shipped in between was source-only for them.
///
/// What runs here is one command on a thread of its own. It downloads nothing
/// and decides nothing (C5 stands, decision Q5): `mcc-desktop --ensure-shell`
/// does the fetch, both digest checks and the staging, and prints a JSON line.
/// The window's part is to notice the disagreement, to stay out of the way
/// while it is resolved, and to offer the restart that completes it.
fn ensure_shell_if_stale(app: &AppHandle, status: &Status) {
    if !shell_is_stale(RELEASE_TAG, status.shell_release_tag.as_deref()) {
        return;
    }
    if SHELL_PIN_CHECKED.swap(true, Ordering::SeqCst) {
        return;
    }
    let Some(target) = exe_path() else {
        // No path to name and nothing sane to guess at. The dashboard's own
        // Update banner still reports the disagreement.
        eprintln!("the desktop app is out of date, but its own path could not be resolved");
        return;
    };
    let pinned = status.shell_release_tag.clone();
    let handle = app.clone();
    std::thread::spawn(move || {
        match process::ensure_shell(&target) {
            Ok(output) => {
                let staged = serde_json::from_str::<serde_json::Value>(&output)
                    .ok()
                    .and_then(|report| {
                        report
                            .get("restart_required")
                            .and_then(serde_json::Value::as_bool)
                    })
                    .unwrap_or(false);
                if !staged {
                    return;
                }
                if let Ok(mut guard) = STAGED_TAG.lock() {
                    *guard = pinned;
                }
                UPDATE_STAGED.store(true, Ordering::SeqCst);
                let _ = handle.run_on_main_thread(announce_staged_update);
            }
            Err(error) => {
                // Never a page: the window is attached to a working server and
                // a failed background download is not a reason to take the
                // dashboard away from the user.
                eprintln!("the desktop app update could not be staged: {error:?}");
            }
        }
    });
}

/// Put the staged update in front of the user, in the one surface a window
/// showing the dashboard still owns.
///
/// The tray, not a page. Navigating away from the dashboard to announce an
/// update the user has not asked for would be a worse defect than the one this
/// fixes; the dashboard's own Update banner carries the same sentence for
/// anyone looking at the window rather than the tray.
fn announce_staged_update() {
    if !UPDATE_STAGED.load(Ordering::SeqCst) {
        return;
    }
    let tag = STAGED_TAG
        .lock()
        .ok()
        .and_then(|guard| guard.clone())
        .unwrap_or_else(|| "a new version".to_owned());
    if let Ok(guard) = TRAY_UPDATE_ITEM.lock() {
        if let Some(item) = guard.as_ref() {
            let _ = item.set_text(format!(
                "Desktop app update ready -- restart the app to use {tag}"
            ));
            let _ = item.set_enabled(true);
        }
    }
    set_tray_status(&format!("Update ready: restart to use {tag}"));
}

/// Start the staged binary and end this process. The tray's "Restart now".
///
/// Deliberately not a rename: this process is the file that would have to be
/// written over. It starts `MyClaudeCode.exe.new`, which does the three
/// renames itself before it builds anything (`swap::adopt`), at the one moment
/// nothing holds either file open.
fn restart_into_staged(app: &AppHandle) {
    let Some(current) = exe_path() else {
        return;
    };
    let staged = swap::staged_path(&current);
    if !staged.is_file() {
        return;
    }
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        save_geometry(&window);
    }
    match std::process::Command::new(&staged)
        // See `SWAP_DELAY_ENV`: this process still holds the single-instance
        // group, and a child that started now would hand itself back to it.
        .env(SWAP_DELAY_ENV, "2000")
        .spawn()
    {
        Ok(_) => {
            QUITTING.store(true, Ordering::SeqCst);
            app.exit(0);
        }
        Err(error) => eprintln!("the staged desktop app could not be started: {error}"),
    }
}

/// This process's own executable, resolved once -- and after the swap step, so
/// it names the file this process actually answers to.
fn exe_path() -> Option<PathBuf> {
    EXE_PATH
        .get()
        .cloned()
        .or_else(|| std::env::current_exe().ok())
}

/// Start the doorbell watcher, once, on the directory Python named.
fn ensure_activation_watcher(app: &AppHandle, status: &Status) {
    if ACTIVATION_STARTED.swap(true, Ordering::SeqCst) {
        return;
    }
    let path = activation::activation_path(&status.config_dir);
    let interval = Duration::from_secs_f64(status.activation_poll_seconds.max(0.2));
    let handle = app.clone();
    std::thread::spawn(move || {
        loop {
            std::thread::sleep(interval);
            if QUITTING.load(Ordering::SeqCst) {
                return;
            }
            if activation::take_ring(&path) {
                let raised = handle.clone();
                let _ = handle.run_on_main_thread(move || raise(&raised));
            }
        }
    });
}

// -- the lifecycle loop -----------------------------------------------------

/// Everything the loop remembers between ticks.
///
/// Deliberately small, and deliberately *not* the state machine: the machine
/// is [`controller::step`], which is pure. This is the sampler that feeds it --
/// the last status document, the last probe, the child this window started,
/// and the clocks the observation is measured against.
struct Lifecycle {
    state: controller::State,
    status: Option<Status>,
    /// The child this window started, if it is still ours to ask about. The
    /// one signal that separates "still coming up" from "gone" while the port
    /// is still free.
    child: Option<std::process::Child>,
    health: controller::Health,
    holder: controller::Holder,
    holder_since: Instant,
    helper: controller::Helper,
    status_health: controller::StatusHealth,
    last_probe: Instant,
    last_spawn: Option<Instant>,
    last_restatus: Option<Instant>,
    /// Whether this launch has already raised the window for a server that
    /// came back (decision Q3: once, then never again).
    raised: bool,
    started: Instant,
}

impl Lifecycle {
    fn new() -> Self {
        Self {
            state: controller::State::Booting,
            status: None,
            child: None,
            health: controller::Health::Absent,
            holder: controller::Holder::Unknown,
            holder_since: Instant::now(),
            helper: controller::Helper::None,
            status_health: controller::StatusHealth::Ok,
            // Far enough in the past that the first tick is a fresh one.
            last_probe: Instant::now() - Duration::from_secs(3600),
            last_spawn: None,
            last_restatus: None,
            raised: false,
            started: Instant::now(),
        }
    }

    /// The probe cadence.
    ///
    /// Ten seconds (the document's `tick_seconds`, decision Q4) everywhere
    /// except while a start is in flight, where it is the document's own
    /// `health_check_interval_seconds` instead -- floored at the paint tick,
    /// because a probe cannot usefully be more frequent than the loop that
    /// makes it.
    ///
    /// The distinction matters and it is not a second policy. Ten seconds is
    /// how often the window *decides whether to start a server*; a server it
    /// has just started deserves to be noticed the moment it answers, and
    /// making the user look at "Starting..." for eight seconds after the
    /// dashboard was ready would be a new way to look stuck. Nothing about the
    /// start rate changes: `start_backoff_seconds` governs that, and it is ten
    /// seconds whatever this returns.
    fn tick(&self) -> Duration {
        let facts = self.facts();
        let starting = matches!(
            self.state,
            controller::State::Starting { .. }
                | controller::State::RestartPending { .. }
                | controller::State::Draining { .. }
        );
        if starting {
            let interval = self
                .status
                .as_ref()
                .map_or(controller::PAINT_TICK_SECONDS, |status| {
                    status.health_check_interval_seconds
                });
            return Duration::from_secs_f64(interval.max(controller::PAINT_TICK_SECONDS));
        }
        Duration::from_secs_f64(facts.tick_seconds.max(1.0))
    }

    /// One health probe's timeout, from the document (C9) or this build's
    /// default while the key is still only tolerated.
    fn probe_timeout(&self) -> Duration {
        self.status
            .as_ref()
            .and_then(|status| status.health_probe_timeout_seconds)
            .filter(|value| *value > 0.0)
            .map_or(health::DEFAULT_PROBE_TIMEOUT, Duration::from_secs_f64)
    }

    /// How long `mcc-desktop --print-status` may take. Out of the binary since
    /// 6.61.0 (audit §5.4): it decides whether a slow machine gets a window.
    fn status_wall(&self) -> Duration {
        self.status
            .as_ref()
            .and_then(|status| status.status_wall_seconds)
            .filter(|value| *value > 0.0)
            .map_or(process::DEFAULT_STATUS_WALL, Duration::from_secs_f64)
    }

    /// The numbers and strings an observation carries, from the last document
    /// that could be read. Every one of them is Python's answer (C1).
    fn facts(&self) -> controller::Facts {
        let Some(status) = self.status.as_ref() else {
            return controller::Facts::default();
        };
        let holder = status.holder.as_ref();
        controller::Facts {
            admin_url: status.admin_url.clone(),
            health_url: status.health_url.clone(),
            server_log: status.server_log.clone(),
            port: status.port,
            server_mode: status.server_mode.clone(),
            tick_seconds: status
                .tick_seconds
                .filter(|value| *value > 0.0)
                .unwrap_or(controller::DEFAULT_TICK_SECONDS),
            start_backoff_seconds: status
                .start_backoff_seconds
                .filter(|value| *value > 0.0)
                .unwrap_or(controller::DEFAULT_START_BACKOFF_SECONDS),
            foreign_grace_seconds: status
                .foreign_grace_seconds
                .filter(|value| *value >= 0.0)
                .unwrap_or(controller::DEFAULT_FOREIGN_GRACE_SECONDS),
            holder_image: holder.and_then(|holder| holder.image.clone()),
            holder_pid: holder.and_then(|holder| holder.pid),
        }
    }

    /// Take one health probe. The whole of the tick's cost on the happy path.
    fn probe(&mut self) {
        self.last_probe = Instant::now();
        let Some(url) = self
            .status
            .as_ref()
            .map(|status| status.health_url.clone())
            .filter(|url| !url.is_empty())
        else {
            self.health = controller::Health::Absent;
            return;
        };
        self.health = match health::probe_outcome_within(&url, self.probe_timeout()) {
            health::ProbeOutcome::Healthy => controller::Health::Healthy,
            health::ProbeOutcome::StartingUp => controller::Health::Starting,
            health::ProbeOutcome::ShuttingDown => controller::Health::Draining,
            _ => controller::Health::Absent,
        };
        // A healthy answer settles who holds the port without a process
        // lookup, which is what keeps an attached window off `--print-status`
        // entirely (BUG-4).
        if self.health == controller::Health::Healthy {
            self.remember_holder(controller::Holder::OursHealthy);
        }
    }

    /// Re-read what the update helper is doing. Cheap, and on every tick.
    ///
    /// This is separate from [`Self::restatus`] on purpose, and the scratch
    /// run of 2026-09-08 is why. The helper facts used to be refreshed only by
    /// a `--print-status`, and `step` returns as soon as it sees a live helper
    /// -- so the observation that said "a helper is installing" was also the
    /// observation that stopped anything asking again, and the window sat on
    /// *Updating My Claude Code (installer running, 6 s)* for as long as it
    /// was open. A cache that suppresses its own refresh is a hang with a
    /// spinner on it.
    ///
    /// It costs one small file read and one pid liveness check, which is why
    /// it can be on the tick path when `--print-status` cannot (BUG-4).
    fn refresh_helper(&mut self) {
        let directory = self
            .status
            .as_ref()
            .map(|status| status.config_dir.clone())
            .or_else(last_config_dir);
        if let Some(directory) = directory {
            self.helper = helper_state(&directory);
        }
    }

    /// Record a holder classification, keeping the clock running while the
    /// answer is unchanged. The grace window BUG-5 asked for is measured here.
    fn remember_holder(&mut self, holder: controller::Holder) {
        if self.holder != holder {
            self.holder = holder;
            self.holder_since = Instant::now();
        }
    }

    /// Read the status document and everything derived from it.
    ///
    /// Never on the tick path: it runs only when [`controller::step`] asks for
    /// it, which it does on the first tick and on a tick where the server is
    /// not answering. That is BUG-4's fix -- the old ladder paid for this
    /// process on every decision it made.
    fn restatus(&mut self, app: &AppHandle, window: &WebviewWindow) {
        self.last_restatus = Some(Instant::now());
        let wall = self.status_wall();
        match process::print_status_within(wall) {
            Ok(raw) => match status::parse_status(&raw) {
                Ok(status) => {
                    INSTALLS_RUN.store(0, Ordering::SeqCst);
                    remember_config_dir(&status.config_dir);
                    ensure_tray(app, &status);
                    ensure_activation_watcher(app, &status);
                    apply_status(window, &status);
                    self.remember_holder(holder_from(&status));
                    self.helper = helper_state(&status.config_dir);
                    self.status = Some(status);
                    self.status_health = controller::StatusHealth::Ok;
                }
                Err(error) => {
                    self.status_health = controller::StatusHealth::Unreadable {
                        detail: error.to_string(),
                    };
                }
            },
            Err(process::StatusRunError::NotInstalled) => {
                self.status_health = controller::StatusHealth::NotInstalled;
                // The one thing still readable when `mcc-desktop` is not: the
                // helper's own progress file, under the directory the last
                // document named. An update mid-flight is exactly when the
                // shims are renamed aside, and installing over it is how one
                // update came to race itself.
                self.helper =
                    last_config_dir().map_or(controller::Helper::None, |dir| helper_state(&dir));
            }
            Err(process::StatusRunError::Failed { code, stderr }) => {
                let code =
                    code.map_or_else(|| "an unknown status".to_owned(), |value| value.to_string());
                self.status_health = controller::StatusHealth::Unreadable {
                    detail: format!("mcc-desktop --print-status exited with {code}. {stderr}"),
                };
            }
            Err(process::StatusRunError::Unrunnable(detail)) => {
                self.status_health = controller::StatusHealth::Unreadable {
                    detail: format!("mcc-desktop could not be run: {detail}"),
                };
            }
        }
    }

    /// Assemble this tick's observation. Pure sampling: nothing here decides.
    fn observe(&mut self, fresh: bool) -> controller::Observation {
        let child_alive = self.child.as_mut().is_some_and(process::still_running);
        if !child_alive {
            self.child = None;
        }
        controller::Observation {
            fresh,
            health: self.health,
            holder: self.holder,
            holder_age: self.holder_since.elapsed().as_secs_f64(),
            helper: self.helper.clone(),
            status: self.status_health.clone(),
            child_alive,
            since_last_start: self.last_spawn.map(|at| at.elapsed().as_secs_f64()),
            since_probe: self.last_probe.elapsed().as_secs_f64(),
            shell_stale: self.status.as_ref().is_some_and(|status| {
                shell_is_stale(RELEASE_TAG, status.shell_release_tag.as_deref())
                    && !SHELL_PIN_CHECKED.load(Ordering::SeqCst)
            }),
            facts: self.facts(),
        }
    }
}

/// Python's answer about the port holder, in the shell's vocabulary.
///
/// The `holder` object is 6.61.0's; `server_presence` is the fallback for the
/// one release in which the shell only tolerates the new key, so a 6.61.0
/// window under a 6.60.2 wheel still classifies by the presence Python already
/// decided by process (`port_is_held_by_mcc`, 6.59.0).
fn holder_from(status: &Status) -> controller::Holder {
    if let Some(holder) = status.holder.as_ref() {
        return match holder.kind.as_str() {
            "absent" => controller::Holder::Absent,
            "ours_healthy" => controller::Holder::OursHealthy,
            "ours_starting" => controller::Holder::OursStarting,
            "ours_draining" => controller::Holder::OursDraining,
            "ours_stale" => controller::Holder::OursStale,
            "foreign" => controller::Holder::Foreign,
            _ => controller::Holder::Unknown,
        };
    }
    match status.server_presence.as_str() {
        "free" => controller::Holder::Absent,
        "healthy" => controller::Holder::OursHealthy,
        "starting" => controller::Holder::OursStarting,
        "draining" => controller::Holder::OursDraining,
        "mcc-stale" => controller::Holder::OursStale,
        "foreign" => controller::Holder::Foreign,
        _ => controller::Holder::Unknown,
    }
}

/// What the update helper is doing, from `progress.json` alone.
fn helper_state(config_dir: &str) -> controller::Helper {
    if let Some(helper) = update_progress::active_helper(config_dir) {
        return controller::Helper::Alive {
            stage: Some(helper.describe()),
        };
    }
    match update_progress::read_stage(config_dir) {
        Some(stage) if update_progress::stage_is_terminal(&stage) => controller::Helper::Finished {
            stage: Some(stage.describe()),
        },
        _ => controller::Helper::None,
    }
}

/// Apply one step's effects. At most one of them acts; the rest paint.
fn apply(
    app: &AppHandle,
    window: &WebviewWindow,
    life: &mut Lifecycle,
    effects: Vec<Effect>,
    observation: &controller::Observation,
) {
    for effect in effects {
        match effect {
            Effect::Show(page) => {
                set_tray_status(tray_line(&life.state));
                show_page(window, &page);
            }
            Effect::Attach { admin_url } => {
                set_tray_status("Server: running");
                remember_page(None);
                show_dashboard(window, &admin_url);
            }
            Effect::Spawn => {
                life.last_spawn = Some(Instant::now());
                set_tray_status("Server: starting");
                match process::spawn_server() {
                    Ok(child) => life.child = Some(child),
                    Err(error) => {
                        // Not a page and not the end of anything: the next
                        // tick tries again, ten seconds from now, forever.
                        // The commonest reason a spawn fails is the one where
                        // retrying matters most -- an update helper has
                        // renamed `mcc-server.exe` aside and will put it back.
                        eprintln!("the server could not be started: {error}");
                        append_output(window, &error);
                    }
                }
            }
            Effect::Restatus => life.restatus(app, window),
            Effect::Install => {
                let attempt = INSTALLS_RUN.load(Ordering::SeqCst).saturating_add(1);
                INSTALLS_RUN.store(attempt, Ordering::SeqCst);
                let last = run_install(window, attempt);
                if let Ok(mut line) = LAST_INSTALL_LINE.lock() {
                    *line = last;
                }
            }
            Effect::RaiseOnce => {
                // Decision Q3, and the "once" is here rather than in `step`
                // so the pure function stays a function of its inputs.
                if !life.raised && life.started.elapsed() >= Duration::from_secs(2) {
                    life.raised = true;
                    raise(app);
                }
            }
            Effect::EnsureShell => {
                if let Some(status) = life.status.as_ref() {
                    ensure_shell_if_stale(app, status);
                }
            }
        }
    }
    let _ = observation;
}

/// The tray's status line, derived from the state like everything else.
fn tray_line(state: &controller::State) -> &'static str {
    match state {
        controller::State::Booting => "Checking the server...",
        controller::State::Attached => "Server: running",
        controller::State::Starting { .. } | controller::State::RestartPending { .. } => {
            "Server: starting"
        }
        controller::State::Reconnecting { .. } => "Server: reconnecting",
        controller::State::Draining { .. } => "Server: shutting down",
        controller::State::Updating { .. } => "Updating My Claude Code...",
        controller::State::Installing { .. } => "Installing My Claude Code...",
        controller::State::Blocked { .. } => "Server: needs attention",
    }
}

/// The loop. One thread, one state, one tick -- and it never returns except to
/// end the process.
///
/// Two clocks, for one reason: the countdown on the page has to move every
/// second to be believable, and the server must be probed exactly as often as
/// decision Q4 says (ten seconds) and no oftener. `fresh` says which kind of
/// tick this is, and only a fresh tick may spawn.
fn run_controller(app: &AppHandle, window: &WebviewWindow) {
    let mut life = Lifecycle::new();
    let paint = Duration::from_secs_f64(controller::PAINT_TICK_SECONDS);
    show_page(window, &Page::Checking);
    remember_page(Some(Page::Checking));

    loop {
        if QUITTING.load(Ordering::SeqCst) || app.get_webview_window(MAIN_WINDOW).is_none() {
            return;
        }
        // Retry is an accelerator, never a way out: it brings the next probe
        // forward and changes nothing else. The same is true of "Take port",
        // which additionally forgets the backoff so the spawn -- and with it
        // the server's own port takeover -- happens on this tick.
        let asked = RETRY_REQUESTED.swap(false, Ordering::SeqCst);
        if TAKE_PORT_REQUESTED.swap(false, Ordering::SeqCst) {
            life.last_spawn = None;
            life.remember_holder(controller::Holder::OursStale);
        }
        let fresh = asked || life.last_probe.elapsed() >= life.tick();
        if life.status.is_none()
            && life.status_health == controller::StatusHealth::Ok
            && !matches!(life.helper, controller::Helper::Alive { .. })
        {
            // The one status read that is not optional: nothing -- not even the
            // health URL -- is known before it.
            life.restatus(app, window);
        }
        if fresh {
            // The helper first, and always: it is the one fact that stops a
            // start, and the observation that carries it must never be the
            // stale one. See `refresh_helper`.
            life.refresh_helper();
            life.probe();
        }

        let observation = life.observe(fresh);
        let now = life.started.elapsed().as_secs_f64();
        let (next, effects) = controller::step(&life.state, &observation, now);
        life.state = next;
        apply(app, window, &mut life, effects, &observation);

        std::thread::sleep(paint);
    }
}

// -- entry point ------------------------------------------------------------

/// What the command line asked for, before a window exists.
///
/// Three answers, and only three: this is a window, not a CLI. `--version`
/// exists so a release smoke -- and a person -- can ask a binary which release
/// it is without reading its bytes, which is the question BUG-0 made
/// unanswerable. `--reset-window` is BUG-7's escape hatch for the case where
/// there is no window left to click a tray item in.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Invocation {
    Run,
    PrintVersion,
    ResetWindow,
}

/// Read the invocation from the arguments, ignoring the program name.
///
/// Pure, and unknown arguments simply run the window: an app launched by a
/// desktop environment can be handed arguments nobody here chose, and refusing
/// to open because of one would be a window that does not open.
pub fn invocation(args: &[String]) -> Invocation {
    for argument in args {
        match argument.as_str() {
            "--version" | "-V" => return Invocation::PrintVersion,
            "--reset-window" => return Invocation::ResetWindow,
            _ => {}
        }
    }
    Invocation::Run
}

/// The release this binary names itself, for `--version`.
pub fn version_line() -> String {
    format!(
        "My Claude Code desktop app {}",
        RELEASE_TAG.unwrap_or("(development build)")
    )
}

/// Take over from a staged replacement, or hand over to one. Runs before
/// anything else in `run()`; see `swap` for the whole argument.
///
/// Returns `false` when this process has handed over and must exit.
fn take_over_or_hand_over() -> bool {
    let Ok(current) = std::env::current_exe() else {
        return true;
    };
    match swap::plan(&current, swap::staged_path(&current).is_file()) {
        swap::Plan::Nothing => {
            if let Some(directory) = current.parent() {
                swap::sweep_aside(directory);
            }
            let _ = EXE_PATH.set(current);
            true
        }
        swap::Plan::Adopt { canonical } => {
            let _ = EXE_PATH.set(swap::adopt(&current, &canonical));
            true
        }
        swap::Plan::Relaunch { staged } => match std::process::Command::new(&staged).spawn() {
            Ok(_) => false,
            Err(error) => {
                // A staged binary that will not start is not a reason to have
                // no window: carry on as the build we already are, and leave
                // the staged file for the next attempt.
                eprintln!("the staged desktop app could not be started: {error}");
                let _ = EXE_PATH.set(current);
                true
            }
        },
    }
}

/// Build and run the application.
pub fn run() {
    // First of all: if the process that started us is still finishing, wait.
    // It is holding the single-instance group and the file we are about to
    // rename. See `SWAP_DELAY_ENV`.
    let delay = swap_delay(std::env::var(SWAP_DELAY_ENV).ok().as_deref());
    if !delay.is_zero() {
        std::thread::sleep(delay);
    }

    // Then, before the single-instance plugin, before the builder, before
    // anything else: a start is the one moment at which neither the old binary
    // nor the staged one is being held open, and it is the only moment the
    // swap can be made.
    if !take_over_or_hand_over() {
        return;
    }

    let args: Vec<String> = std::env::args().skip(1).collect();
    match invocation(&args) {
        Invocation::PrintVersion => {
            // `windows_subsystem = "windows"` means Windows allocates no
            // console of its own, but an inherited or redirected stdout still
            // works -- which is how a script asks this question.
            println!("{}", version_line());
            return;
        }
        // The file itself is deleted in `setup()`, which is the first
        // place the data directory is resolved -- and it is deleted there
        // before the window is built, so the very first paint is already the
        // configured size, centred.
        Invocation::ResetWindow => RESET_WINDOW.store(true, Ordering::SeqCst),
        Invocation::Run => {}
    }

    let mut builder = tauri::Builder::default();
    // First, so a second launch is answered before anything else is set up.
    if !separate_instance(std::env::var(SEPARATE_INSTANCE_ENV).ok().as_deref()) {
        builder = builder.plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            raise(app);
        }));
    }
    builder
        .invoke_handler(tauri::generate_handler![
            shell_retry,
            shell_ready,
            shell_page,
            shell_take_port
        ])
        // The first page this window ever finishes loading is the shell's own,
        // and its URL is whatever the platform's asset protocol actually is --
        // `tauri://` on some, `http://tauri.localhost/` on Windows. Reading it
        // here rather than assuming it is what stops a navigation back to the
        // splash from landing on a scheme this platform does not serve.
        .on_page_load(|webview, payload| {
            if payload.event() == PageLoadEvent::Finished {
                let _ = LOCAL_URL.set(payload.url().to_string());
                let _ = webview.window().set_title("My Claude Code");
                // BUG-2: a reload used to leave the window showing the
                // splash's "Checking the server..." over a loop that was
                // working perfectly, and the only recovery anybody found was
                // to close the app. The page is re-pushed here and pulled by
                // the document itself on load; either one alone would still
                // lose a race.
                if let Some(main) = webview
                    .window()
                    .app_handle()
                    .get_webview_window(MAIN_WINDOW)
                {
                    repush_current_page(&main);
                }
            }
        })
        .on_window_event(|window, event| match event {
            WindowEvent::CloseRequested { api, .. } => {
                if should_hide_on_close(
                    CLOSE_TO_TRAY.load(Ordering::SeqCst),
                    QUITTING.load(Ordering::SeqCst),
                ) {
                    api.prevent_close();
                    if let Some(webview) = window.app_handle().get_webview_window(MAIN_WINDOW) {
                        save_geometry(&webview);
                        let _ = webview.hide();
                    }
                } else if let Some(webview) = window.app_handle().get_webview_window(MAIN_WINDOW) {
                    save_geometry(&webview);
                }
            }
            WindowEvent::Destroyed => QUITTING.store(true, Ordering::SeqCst),
            _ => {}
        })
        .setup(|app| {
            let handle = app.handle().clone();
            let directory = data_dir(&handle);
            let state_path = window_state::state_path(&directory);
            if RESET_WINDOW.load(Ordering::SeqCst) {
                let _ = window_state::clear(&state_path);
            }
            let remembered = window_state::load(&state_path);

            let window =
                WebviewWindowBuilder::new(app, MAIN_WINDOW, WebviewUrl::App("index.html".into()))
                    .title("My Claude Code")
                    .min_inner_size(
                        f64::from(window_state::MIN_WIDTH),
                        f64::from(window_state::MIN_HEIGHT),
                    )
                    // One flag, on every page this window loads, including the
                    // dashboard. It says only "a process with a lifecycle tick
                    // is watching this server", and the dashboard's Update
                    // button reads it to ask the helper NOT to restart -- this
                    // window owns the restart from 6.61.0 (GAP-3). It is not a
                    // capability and it grants nothing: the same page in a
                    // browser tab simply does not see it and the helper
                    // restarts as it always has.
                    .initialization_script(SHELL_MARKER_SCRIPT)
                    .inner_size(FIRST_PAINT_WIDTH, FIRST_PAINT_HEIGHT)
                    .center()
                    .build()?;

            // Remembered geometry is physical, and is re-applied physically,
            // so a window on a scaled display comes back the size it was
            // rather than the size a logical round trip would make it.
            //
            // It is applied only when it survives `geometry_is_usable`
            // (BUG-7). A rectangle that does not is left alone entirely: the
            // window keeps the first-paint size the builder just centred, and
            // `apply_window_size` replaces it with the operator's configured
            // size the moment the first status document arrives. Restoring an
            // off-screen rectangle "and then fixing it" would show the user a
            // window that vanishes.
            let usable = remembered
                .rect()
                .is_some_and(|rect| window_state::geometry_is_usable(rect, &work_areas(&window)));
            if usable {
                if let Some(rect) = remembered.rect() {
                    let _ = window.set_size(tauri::PhysicalSize::new(rect.width, rect.height));
                    let _ = window.set_position(tauri::PhysicalPosition::new(rect.x, rect.y));
                }
                if remembered.maximized {
                    let _ = window.maximize();
                }
            } else if remembered != window_state::WindowState::default() {
                eprintln!(
                    "the remembered window geometry is not on any screen; opening at the \
                     configured size instead"
                );
            }
            let loop_handle = handle.clone();
            std::thread::spawn(move || {
                let Some(window) = loop_handle.get_webview_window(MAIN_WINDOW) else {
                    return;
                };
                run_controller(&loop_handle, &window);
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("the My Claude Code window could not be started");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_window_asks_to_be_replaced_only_when_it_knows_it_is_the_wrong_one() {
        // BUG-0: the wheel pinned v6.58.3 and the machine ran v6.43.0 for
        // fifteen releases, because nothing ever compared the two.
        assert!(shell_is_stale(Some("v6.43.0"), Some("v6.59.0")));
        assert!(!shell_is_stale(Some("v6.59.0"), Some("v6.59.0")));
        // Whitespace is not a release.
        assert!(!shell_is_stale(Some("v6.59.0"), Some(" v6.59.0\n")));
        // A build with no tag of its own -- a developer's -- must never ask
        // Python to replace it: it does not know what it is, and acting on
        // missing information is how a working window gets swapped for a guess.
        assert!(!shell_is_stale(None, Some("v6.59.0")));
        assert!(!shell_is_stale(Some(""), Some("v6.59.0")));
        // And a wheel too old to say what it pins is not an accusation either.
        assert!(!shell_is_stale(Some("v6.43.0"), None));
        assert!(!shell_is_stale(Some("v6.43.0"), Some("")));
    }

    #[test]
    fn a_restart_waits_for_the_window_that_asked_for_it_to_finish() {
        // The window pressing "Restart now" is holding the single-instance
        // group. A child that started immediately would find its window, hand
        // its arguments over and exit -- leaving no window and no update.
        assert_eq!(swap_delay(Some("2000")), Duration::from_millis(2000));
        // Nothing to wait for on an ordinary launch.
        assert_eq!(swap_delay(None), Duration::ZERO);
        assert_eq!(swap_delay(Some("")), Duration::ZERO);
        assert_eq!(swap_delay(Some("soon")), Duration::ZERO);
        // And never long enough to look like a window that did not open.
        assert_eq!(swap_delay(Some("600000")), Duration::from_millis(10_000));
    }

    #[test]
    fn the_command_line_has_exactly_three_answers() {
        assert_eq!(invocation(&[]), Invocation::Run);
        assert_eq!(
            invocation(&["--version".to_owned()]),
            Invocation::PrintVersion
        );
        assert_eq!(invocation(&["-V".to_owned()]), Invocation::PrintVersion);
        assert_eq!(
            invocation(&["--reset-window".to_owned()]),
            Invocation::ResetWindow
        );
        // An argument a desktop environment supplied is not a reason to
        // refuse to open a window.
        assert_eq!(
            invocation(&["--enable-features=Whatever".to_owned()]),
            Invocation::Run
        );
    }

    #[test]
    fn the_version_line_says_what_this_build_is_or_says_it_does_not_know() {
        let line = version_line();
        assert!(line.starts_with("My Claude Code desktop app "), "{line}");
        match RELEASE_TAG {
            // The release build: `shell-release.yml` stamps the tag it is
            // uploading to, and `--version` is how a smoke reads it back.
            Some(tag) => assert!(line.ends_with(tag), "{line}"),
            None => assert!(line.ends_with("(development build)"), "{line}"),
        }
    }

    #[test]
    fn a_launch_stands_apart_only_when_it_was_asked_to() {
        // Unset, empty and whitespace all mean the ordinary single-instance
        // behaviour: a second launch raises the window you already have.
        assert!(!separate_instance(None));
        assert!(!separate_instance(Some("")));
        assert!(!separate_instance(Some("   ")));
        assert!(separate_instance(Some("1")));
    }

    #[test]
    fn closing_hides_the_window_when_there_is_a_tray_to_hide_into() {
        // The reported behaviour: "when I close the desktop app it should
        // minimize to tray so I can reopen it from the tray. Right now closing
        // the desktop app also closes the tray."
        assert!(should_hide_on_close(true, false));
    }

    #[test]
    fn closing_ends_the_app_when_the_user_asked_for_that() {
        assert!(!should_hide_on_close(false, false));
    }

    #[test]
    fn quit_is_never_turned_into_a_hide() {
        // Quit sets the flag before it closes the window. An app whose Quit
        // hides it is an app that cannot be quit.
        assert!(!should_hide_on_close(true, true));
        assert!(!should_hide_on_close(false, true));
    }

    #[test]
    fn the_config_directory_is_remembered_so_it_survives_mcc_desktop_going_missing() {
        // The one moment the shell most needs the configuration directory is
        // the moment it cannot ask for it: `mcc-desktop` is not runnable,
        // which is exactly when an update helper may be rewriting the shims.
        // Nothing is resolved here (C1) -- this is the last answer Python
        // gave, kept so the receipt can be read without asking again.
        if let Ok(mut guard) = LAST_CONFIG_DIR.lock() {
            *guard = None;
        }
        assert_eq!(last_config_dir(), None);
        remember_config_dir("C:\\Users\\somebody\\config-dir");
        assert_eq!(
            last_config_dir().as_deref(),
            Some("C:\\Users\\somebody\\config-dir")
        );
        // A blank answer is not an answer and must not erase a good one.
        remember_config_dir("   ");
        assert_eq!(
            last_config_dir().as_deref(),
            Some("C:\\Users\\somebody\\config-dir")
        );
        if let Ok(mut guard) = LAST_CONFIG_DIR.lock() {
            *guard = None;
        }
    }

    #[test]
    fn nothing_is_believed_to_be_installing_before_a_status_has_ever_been_read() {
        // The first launch on a machine with no MCC at all: there is no
        // remembered configuration directory, so there is no receipt to read,
        // and the window must be free to install. A gate that blocked here
        // would be a shell that can never install anything.
        if let Ok(mut guard) = LAST_CONFIG_DIR.lock() {
            *guard = None;
        }
        assert_eq!(
            last_config_dir().map_or(controller::Helper::None, |dir| helper_state(&dir)),
            controller::Helper::None
        );
    }

    #[test]
    fn a_reload_is_answered_with_the_page_the_controller_is_on() {
        // BUG-2, as a unit test. Every page was *pushed* with `eval`, so F5
        // threw the state away and left "Checking the server..." over a loop
        // that was working. The page now pulls the state as it loads, and
        // `shell_page` is what it pulls.
        remember_page(Some(Page::Reconnecting {
            message: "still trying".to_owned(),
        }));
        assert_eq!(
            shell_page(),
            Some(Page::Reconnecting {
                message: "still trying".to_owned()
            })
        );
        // And the dashboard is deliberately not one of this shell's pages to
        // restore: a reload there reloads the dashboard.
        remember_page(None);
        assert_eq!(shell_page(), None);
    }

    #[test]
    fn every_lifecycle_state_has_a_tray_line() {
        // A tray whose status line stopped changing is the same defect as a
        // page that stopped repainting, in a smaller box.
        for state in [
            controller::State::Booting,
            controller::State::Attached,
            controller::State::Starting {
                since: 0.0,
                attempts: 1,
            },
            controller::State::Reconnecting { since: 0.0 },
            controller::State::Draining { since: 0.0 },
            controller::State::Updating { since: 0.0 },
            controller::State::RestartPending { since: 0.0 },
            controller::State::Installing { attempts: 1 },
            controller::State::Blocked {
                reason: controller::Blocked::ForeignPort,
            },
        ] {
            assert!(!tray_line(&state).is_empty(), "{}", state.name());
        }
    }
}
