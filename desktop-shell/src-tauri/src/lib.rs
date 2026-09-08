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
//! geometry, and the ladder that decides between attaching to a healthy
//! server, starting one, explaining a port conflict, or installing MCC.
//!
//! Contracts this file is answerable for: C1 (nothing is resolved here),
//! C4 (nothing under the configuration directory is written), C5 (no
//! updater), C8 (the pages it serves itself never fetch), C9 (every budget
//! comes from the status document).

pub mod activation;
pub mod health;
pub mod install;
pub mod ladder;
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

use crate::ladder::{Decision, Reconnect, Respawn, StartAttempt};
use crate::status::Status;
use crate::ui::Page;
use crate::update_progress::Stage;

/// The single windows label. One window, one label, everywhere.
const MAIN_WINDOW: &str = "main";

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

/// How often a blocked ladder re-checks whether Retry was pressed.
const RETRY_POLL: Duration = Duration::from_millis(200);

/// How long to let a freshly navigated local page define its receiver before
/// pushing state at it. The push is idempotent and the page also picks up a
/// pending state on load, so this is a smoothing delay, not a correctness one.
const PAGE_SETTLE: Duration = Duration::from_millis(500);

/// How many times MCC may be installed from this window before it stops
/// trying and says why.
///
/// Not a budget from the status document, because there is no status document:
/// this is the one state the shell is in when `mcc-desktop` cannot be run at
/// all. Three, for the same reason a start gets three: the first one is the
/// one that usually works, and a fourth would only be the third again.
const INSTALL_ATTEMPTS: u32 = 3;

/// How often the window re-checks for `mcc-desktop` after it has stopped
/// installing. Each check runs a short-lived process, so this is not the
/// 200ms Retry poll.
const INSTALL_RECHECK: Duration = Duration::from_secs(5);

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
/// Installs run in this session that did not end with a runnable
/// `mcc-desktop`. Reset by the first status read that works.
static INSTALLS_RUN: AtomicU32 = AtomicU32::new(0);
/// The installer's last word, kept so the page that gives up can quote it
/// rather than showing a spinner over nothing.
static LAST_INSTALL_LINE: Mutex<String> = Mutex::new(String::new());
/// Whether closing the window hides it instead of ending the app. Read from
/// the status document on every ladder pass; `false` until one has been read,
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
/// download every ladder pass would be a denial of service on the release page.
static SHELL_PIN_CHECKED: AtomicBool = AtomicBool::new(false);
/// Whether this launch has already applied the configured window size. The
/// geometry rules are per *launch*, not per ladder pass -- a window that
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

fn append_output(window: &WebviewWindow, line: &str) {
    let _ = window.eval(ui::append_output_script(line));
}

/// Load the dashboard itself. The URL is whatever Python said it was (C1).
fn show_dashboard(window: &WebviewWindow, admin_url: &str) {
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

/// Block until Retry is pressed. Returns when it is.
fn wait_for_retry(app: &AppHandle) {
    RETRY_REQUESTED.store(false, Ordering::SeqCst);
    while !RETRY_REQUESTED.swap(false, Ordering::SeqCst) {
        if QUITTING.load(Ordering::SeqCst) || app.get_webview_window(MAIN_WINDOW).is_none() {
            return;
        }
        std::thread::sleep(RETRY_POLL);
    }
}

/// Wait on the error page, and keep checking behind it.
///
/// The whole of the reported defect, in one function. Until 6.58.1 a start
/// that ran out of budget called [`wait_for_retry`] -- an unbounded loop on a
/// static page -- and `watch_health`, the entire self-healing reconnect
/// machinery, was reachable only from the success branch. The user waited at
/// that page while the server they were waiting for answered seven seconds
/// later, and closing and reopening the app was literally the only recovery.
///
/// Now the page is the same page and the loop behind it is alive: it probes
/// the health URL on the document's own cadence and returns the moment the
/// server answers, so the ladder runs again and the dashboard loads with
/// nothing asked of the user. Retry still returns immediately -- it is an
/// accelerator now, not the only way out.
///
/// Returns true when the server answered.
fn wait_for_retry_or_health(
    app: &AppHandle,
    window: &WebviewWindow,
    status: &Status,
    health_url: &str,
    message: &str,
) -> bool {
    RETRY_REQUESTED.store(false, Ordering::SeqCst);
    let poll = Duration::from_secs_f64(status.health_poll_seconds.max(0.5));
    let mut probed_at = Instant::now();
    show_page(
        window,
        &Page::Error {
            message: ladder::still_checking_text(message, 0.0),
            server_log: Some(status.server_log.clone()),
        },
    );
    loop {
        if RETRY_REQUESTED.swap(false, Ordering::SeqCst) {
            return false;
        }
        if QUITTING.load(Ordering::SeqCst) || app.get_webview_window(MAIN_WINDOW).is_none() {
            return false;
        }
        if probed_at.elapsed() >= poll {
            probed_at = Instant::now();
            if health::is_healthy(health_url) {
                return true;
            }
            // Repainted every probe, for the reason the reconnect banner is:
            // a page that never changes is indistinguishable from a page
            // behind a loop that has stopped.
            show_page(
                window,
                &Page::Error {
                    message: ladder::still_checking_text(message, 0.0),
                    server_log: Some(status.server_log.clone()),
                },
            );
        }
        std::thread::sleep(RETRY_POLL);
    }
}

/// The same idea one rung lower: MCC is not installed, the installer has had
/// its attempts, and the window watches for `mcc-desktop` to become runnable
/// instead of installing it again forever.
fn wait_for_install_to_land(app: &AppHandle) {
    RETRY_REQUESTED.store(false, Ordering::SeqCst);
    let mut checked_at = Instant::now();
    loop {
        if RETRY_REQUESTED.swap(false, Ordering::SeqCst) {
            return;
        }
        if QUITTING.load(Ordering::SeqCst) || app.get_webview_window(MAIN_WINDOW).is_none() {
            return;
        }
        if checked_at.elapsed() >= INSTALL_RECHECK {
            checked_at = Instant::now();
            if !matches!(
                process::print_status(),
                Err(process::StatusRunError::NotInstalled)
            ) {
                return;
            }
        }
        std::thread::sleep(RETRY_POLL);
    }
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

/// An update helper that is installing right now, if one is.
fn helper_installing_now() -> Option<update_progress::ActiveHelper> {
    update_progress::active_helper(&last_config_dir()?)
}

/// Watch an update helper finish instead of installing over the top of it.
///
/// The 2026-09-07 failure in one function: the helper renamed `mcc-desktop`
/// aside, the ladder read `NotInstalled`, and the window started its own
/// `uv tool install` into the tool directory the helper was mid-way through
/// writing. The helper lost all five of its attempts and never reached the
/// step that starts a server. So while a helper is alive the window says what
/// it is waiting for and does nothing else -- and it repaints every pass,
/// because a page that never changes is the thing that gets reported as a
/// hang.
///
/// Returns when the helper is gone. Whether MCC is installed by then is the
/// caller's question, asked the way it always is: by running the status
/// command again.
fn wait_for_update_helper(
    app: &AppHandle,
    window: &WebviewWindow,
    first: update_progress::ActiveHelper,
) {
    set_tray_status("Updating My Claude Code...");
    let mut helper = first;
    loop {
        show_page(
            window,
            &Page::Updating {
                message: format!(
                    "{} This window is waiting for it rather than starting a second \
                     installer, and picks the dashboard up again by itself.",
                    helper.describe()
                ),
            },
        );
        for _ in 0..(INSTALL_RECHECK.as_millis() / RETRY_POLL.as_millis()).max(1) {
            if QUITTING.load(Ordering::SeqCst) || app.get_webview_window(MAIN_WINDOW).is_none() {
                return;
            }
            std::thread::sleep(RETRY_POLL);
        }
        match helper_installing_now() {
            Some(next) => helper = next,
            None => return,
        }
    }
}

// -- the ladder -------------------------------------------------------------

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
                 installing it (attempt {attempt} of {INSTALL_ATTEMPTS}). This \
                 takes a few minutes the first time."
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

/// Poll `/health` until the server answers, or until the documents own start
/// budget runs out (C9).
fn wait_for_start(window: &WebviewWindow, status: &Status, health_url: &str, attempt: u32) -> bool {
    let started = Instant::now();
    let interval = Duration::from_secs_f64(status.health_check_interval_seconds.max(0.05));
    loop {
        if health::is_healthy(health_url) {
            return true;
        }
        if QUITTING.load(Ordering::SeqCst) {
            return false;
        }
        let elapsed = started.elapsed().as_secs_f64();
        if !ladder::start_may_continue(status, elapsed) {
            return false;
        }
        show_page(
            window,
            &Page::Starting {
                message: ladder::start_progress_text(status, elapsed, attempt),
            },
        );
        std::thread::sleep(interval);
    }
}

/// One reconnect episode's state. An episode begins at the first failed probe
/// past the debounce and ends when the server answers again or the budget runs
/// out. `respawned` is per-episode on purpose: it is what stops a server that
/// crash-loops on start from being started again every cadence for the whole
/// budget.
struct Episode {
    started: Instant,
    last_restatus: Instant,
    respawned: bool,
}

/// Re-read the status document mid-reconnect and act on the one unambiguous
/// answer. Returns true when a server was started.
///
/// This is the half of the fix that answers "the server hangs until we close
/// the desktop app and restart it manually". Closing and relaunching the app
/// worked because relaunching re-runs the ladder; nothing else in the design
/// ever did. Now the reconnect loop does, on the document's own cadence.
///
/// It never resolves anything (C1) and never installs anything (C5): the only
/// action available to it is `spawn_server`, which spells the shim name and
/// nothing else.
fn restatus_during_reconnect(window: &WebviewWindow, episode: &mut Episode) -> bool {
    episode.last_restatus = Instant::now();
    let Ok(raw) = process::print_status() else {
        // A status read that failed mid-reconnect is not news: the commonest
        // reason is that the machine is busy applying an update. Keep polling.
        return false;
    };
    let Ok(fresh) = status::parse_status(&raw) else {
        return false;
    };
    if ladder::respawn_verdict(&fresh, episode.respawned) != Respawn::Start {
        return false;
    }
    match process::spawn_server() {
        Ok(_) => {
            // Only now. Setting it before the spawn -- which is what this did
            // until 6.58.1 -- meant a spawn that *failed* burned the episode's
            // one attempt for the whole reconnect budget, twenty-two minutes.
            // And the commonest reason for a spawn to fail is the one where
            // the retry matters most: an update helper has renamed
            // mcc-server.exe aside and will put it back in a moment.
            episode.respawned = true;
            set_tray_status("Server: starting");
            true
        }
        Err(error) => {
            // Say it in the window rather than only in a log nobody opens, but
            // do not abandon the episode: the update helper may still start a
            // server of its own well inside the budget.
            append_output(window, &error);
            false
        }
    }
}

/// Watch a dashboard that is already loaded. Returns when the window needs a
/// page again -- i.e. when the reconnect budget has run out.
fn watch_health(app: &AppHandle, window: &WebviewWindow, status: &Status) {
    let poll = Duration::from_secs_f64(status.health_poll_seconds.max(0.5));
    let mut failures: u32 = 0;
    let mut episode: Option<Episode> = None;
    let mut showing_banner = false;

    loop {
        std::thread::sleep(poll);
        if QUITTING.load(Ordering::SeqCst) || app.get_webview_window(MAIN_WINDOW).is_none() {
            return;
        }
        let probed_at = Instant::now();
        let outcome = health::probe_outcome(&status.health_url);
        if outcome.is_healthy() {
            if showing_banner {
                // It came back. Reload the dashboard rather than leaving the
                // user looking at a banner about a problem that is over.
                show_dashboard(window, &status.admin_url);
                showing_banner = false;
            }
            failures = 0;
            episode = None;
            set_tray_status("Server: running");
            continue;
        }

        failures = failures.saturating_add(1);
        let episode = episode.get_or_insert_with(|| Episode {
            started: Instant::now(),
            // Not `Instant::now() - cadence`: the first thirty seconds of an
            // outage are overwhelmingly a restart in progress, and re-reading
            // the status document in that window would spawn a second server
            // into a port the old one has not finished releasing.
            last_restatus: Instant::now(),
            respawned: false,
        });
        let elapsed = episode.started.elapsed().as_secs_f64();
        match ladder::reconnect_verdict(status, failures, elapsed) {
            // Below the debounce: a routine update must not paint anything.
            Reconnect::Ignore => {}
            Reconnect::Waiting => {
                set_tray_status("Server: reconnecting");
                // Re-read the whole document on the document's own cadence,
                // and start a server if -- and only if -- the answer is the
                // unambiguous one. Once per episode.
                if ladder::should_restatus(status, episode.last_restatus.elapsed().as_secs_f64()) {
                    restatus_during_reconnect(window, episode);
                }
                // Repainted every tick. The old banner was painted once and
                // never touched again, so a loop that was in fact probing
                // every five seconds was indistinguishable from a frozen
                // window -- which is exactly what was reported.
                let stage = update_progress::read_stage(&status.config_dir);
                show_page(
                    window,
                    &Page::Reconnecting {
                        message: ladder::reconnect_progress_text(
                            status,
                            elapsed,
                            probed_at.elapsed().as_secs_f64(),
                            &outcome.describe(),
                            stage.as_ref().map(Stage::describe).as_deref(),
                        ),
                    },
                );
                showing_banner = true;
            }
            Reconnect::Failed { server_log } => {
                set_tray_status("Server: not answering");
                show_page(
                    window,
                    &Page::Error {
                        message: format!(
                            "The server never came back. The last check said: {}.",
                            outcome.describe()
                        ),
                        server_log: Some(server_log),
                    },
                );
                return;
            }
        }
    }
}

/// Wait for a draining server to let go of the port, then let the ladder run
/// again.
///
/// Bounded by the document's own reconnect budget, which is the same budget
/// every other wait in this window uses (C9), and it repaints as it goes for
/// the same reason the reconnect banner does. It starts nothing and kills
/// nothing: the server hard-exits itself one beat past its own stop budget and
/// the update helper force-kills the exact parent pid it was given, so a third
/// killer here would only be a way to lose an in-flight request that two other
/// bounded paths were about to end cleanly.
fn wait_for_drain(app: &AppHandle, status: &Status) {
    let poll = Duration::from_secs_f64(status.health_poll_seconds.max(0.5));
    let started = Instant::now();
    let Some(window) = app.get_webview_window(MAIN_WINDOW) else {
        return;
    };
    loop {
        std::thread::sleep(poll);
        if QUITTING.load(Ordering::SeqCst) || app.get_webview_window(MAIN_WINDOW).is_none() {
            return;
        }
        let probed_at = Instant::now();
        let outcome = health::probe_outcome(&status.health_url);
        if outcome.is_healthy() {
            return;
        }
        let elapsed = started.elapsed().as_secs_f64();
        if elapsed >= status.reconnect_timeout_seconds {
            return;
        }
        let stage = update_progress::read_stage(&status.config_dir);
        show_page(
            &window,
            &Page::Reconnecting {
                message: ladder::reconnect_progress_text(
                    status,
                    elapsed,
                    probed_at.elapsed().as_secs_f64(),
                    &outcome.describe(),
                    stage.as_ref().map(Stage::describe).as_deref(),
                ),
            },
        );
    }
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

/// One pass of the ladder. Returns when the window needs another status read.
fn ladder_pass(app: &AppHandle, window: &WebviewWindow) {
    show_page(window, &Page::Checking);

    let raw = match process::print_status() {
        Ok(raw) => raw,
        Err(process::StatusRunError::NotInstalled) => {
            // Decision Q4: do not merely offer. Run it, show it, then loop --
            // but a bounded number of times. The ladder thread loops, so an
            // installer that succeeds without putting mcc-desktop on *this*
            // process's PATH (the ordinary Windows first launch: PATH changes
            // reach new processes only) used to mean installing MCC again
            // every few minutes for as long as the window was open, under a
            // spinner, with no way out but quitting. That is the first-launch
            // hang, and it is the same shape as the start one: a wait with no
            // end and no explanation.
            // An update helper rewriting the shims makes `mcc-desktop`
            // briefly unrunnable, and that is not a machine without MCC on it
            // -- it is a machine mid-update. Installing here is how one update
            // came to race itself (R3). The helper starts a server itself when
            // it is done, on both branches, so there is nothing to do but
            // watch.
            if let Some(helper) = helper_installing_now() {
                wait_for_update_helper(app, window, helper);
                return;
            }
            let used = INSTALLS_RUN.load(Ordering::SeqCst);
            if used >= INSTALL_ATTEMPTS {
                set_tray_status("My Claude Code is not installed");
                let last = LAST_INSTALL_LINE
                    .lock()
                    .map(|line| line.clone())
                    .unwrap_or_default();
                show_page(
                    window,
                    &Page::Error {
                        message: install::install_did_not_take_message(used, &last),
                        server_log: None,
                    },
                );
                wait_for_install_to_land(app);
                return;
            }
            INSTALLS_RUN.store(used.saturating_add(1), Ordering::SeqCst);
            let last = run_install(window, used.saturating_add(1));
            if let Ok(mut line) = LAST_INSTALL_LINE.lock() {
                *line = last;
            }
            return;
        }
        Err(process::StatusRunError::Failed { code, stderr }) => {
            let code =
                code.map_or_else(|| "an unknown status".to_owned(), |value| value.to_string());
            show_page(
                window,
                &Page::Error {
                    message: format!("mcc-desktop --print-status exited with {code}. {stderr}"),
                    server_log: None,
                },
            );
            wait_for_retry(app);
            return;
        }
        Err(process::StatusRunError::Unrunnable(detail)) => {
            show_page(
                window,
                &Page::Error {
                    message: format!("mcc-desktop could not be run: {detail}"),
                    server_log: None,
                },
            );
            wait_for_retry(app);
            return;
        }
    };

    let status = match status::parse_status(&raw) {
        Ok(status) => status,
        Err(error) => {
            show_page(
                window,
                &Page::Error {
                    message: error.to_string(),
                    server_log: None,
                },
            );
            wait_for_retry(app);
            return;
        }
    };

    // A status document that could be read is proof the install took, so the
    // next time MCC goes missing the window gets its attempts again.
    INSTALLS_RUN.store(0, Ordering::SeqCst);
    // And it is the only chance to learn where the configuration lives before
    // the next time `mcc-desktop` cannot be run.
    remember_config_dir(&status.config_dir);

    ensure_tray(app, &status);
    ensure_activation_watcher(app, &status);
    apply_status(window, &status);
    ensure_shell_if_stale(app, &status);

    match ladder::decide(&status) {
        Decision::Attach { admin_url } => {
            set_tray_status("Server: running");
            show_dashboard(window, &admin_url);
            watch_health(app, window, &status);
            wait_for_retry(app);
        }
        Decision::Start {
            admin_url,
            health_url,
        } => {
            // The document says how many attempts a start gets (C9). Each one
            // is a full `start_timeout_seconds` of probing; only the first
            // necessarily spawns, because a child that is still running is a
            // server that is still coming up and a second one would only lose
            // the bind race.
            let attempts = ladder::start_attempts(&status);
            let mut child: Option<std::process::Child> = None;
            let mut spawn_error: Option<String> = None;
            let mut healthy = false;
            for attempt in 1..=attempts {
                if QUITTING.load(Ordering::SeqCst) {
                    return;
                }
                let running = child.as_mut().is_some_and(process::still_running);
                if ladder::start_attempt_action(attempt, running) == StartAttempt::Spawn {
                    set_tray_status("Server: starting");
                    show_page(
                        window,
                        &Page::Starting {
                            message: ladder::start_progress_text(&status, 0.0, attempt),
                        },
                    );
                    match process::spawn_server() {
                        Ok(started) => {
                            child = Some(started);
                            spawn_error = None;
                        }
                        // Not fatal any more, and not the end of the attempts:
                        // during an update the server shim is renamed aside
                        // for a few seconds, and the old code turned those few
                        // seconds into a Retry wall.
                        Err(error) => spawn_error = Some(error),
                    }
                }
                if wait_for_start(window, &status, &health_url, attempt) {
                    healthy = true;
                    break;
                }
            }
            if healthy {
                set_tray_status("Server: running");
                show_dashboard(window, &admin_url);
                watch_health(app, window, &status);
                wait_for_retry(app);
                return;
            }
            set_tray_status("Server: did not start");
            let message = spawn_error.unwrap_or_else(|| ladder::start_timeout_message(&status));
            // And the page is not the end: the loop behind it keeps probing,
            // so a server that binds late is picked up without the user
            // touching anything.
            wait_for_retry_or_health(app, window, &status, &health_url, &message);
        }
        Decision::NotOurServer { server_mode } => {
            set_tray_status("Server: not running");
            show_page(
                window,
                &Page::NotOurServer {
                    message: ladder::not_our_server_message(&server_mode),
                },
            );
            wait_for_retry(app);
        }
        Decision::PortConflict { message } => {
            set_tray_status("Server: port conflict");
            show_page(window, &Page::PortConflict { message });
            wait_for_retry(app);
        }
        Decision::Starting { .. } => {
            // MCC's own server, mid-start. Not a free port and not a conflict:
            // it already holds the socket, and a spawn here would start a
            // second server into a bind race the first one is about to win.
            // Returning rather than blocking on Retry is what makes this
            // self-healing -- the ladder thread loops, and the next pass sees
            // `healthy`.
            set_tray_status("Server: starting");
            show_page(
                window,
                &Page::Reconnecting {
                    message: ladder::starting_message(&status),
                },
            );
            wait_for_drain(app, &status);
        }
        Decision::Stale => {
            // Ours, holding the port, silent. Waiting is right and the port
            // conflict page is wrong: the server's own takeover reclaims the
            // port on the next start, and this window must not send the user
            // off to stop "another program" that is My Claude Code.
            set_tray_status("Server: not answering");
            show_page(
                window,
                &Page::Reconnecting {
                    message: ladder::stale_message(&status),
                },
            );
            wait_for_drain(app, &status);
        }
        Decision::Draining => {
            // MCC's own server, mid-stop. Not a conflict and not a free port:
            // wait for it to finish and let the next pass of the ladder pick
            // it up. Returning rather than blocking on Retry is what makes
            // this self-healing -- the ladder thread loops.
            set_tray_status("Server: shutting down");
            show_page(
                window,
                &Page::Reconnecting {
                    message: ladder::draining_message(&status),
                },
            );
            wait_for_drain(app, &status);
        }
        Decision::UnknownPresence { presence } => {
            show_page(
                window,
                &Page::Error {
                    message: format!(
                        "mcc-desktop reported a server state this window does \
                         not know: {presence}. Update the desktop window."
                    ),
                    server_log: Some(status.server_log.clone()),
                },
            );
            wait_for_retry(app);
        }
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
        .invoke_handler(tauri::generate_handler![shell_retry, shell_ready])
        // The first page this window ever finishes loading is the shell's own,
        // and its URL is whatever the platform's asset protocol actually is --
        // `tauri://` on some, `http://tauri.localhost/` on Windows. Reading it
        // here rather than assuming it is what stops a navigation back to the
        // splash from landing on a scheme this platform does not serve.
        .on_page_load(|webview, payload| {
            if payload.event() == PageLoadEvent::Finished {
                let _ = LOCAL_URL.set(payload.url().to_string());
                let _ = webview.window().set_title("My Claude Code");
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
            let ladder_handle = handle.clone();
            std::thread::spawn(move || {
                while !QUITTING.load(Ordering::SeqCst) {
                    let Some(window) = ladder_handle.get_webview_window(MAIN_WINDOW) else {
                        return;
                    };
                    ladder_pass(&ladder_handle, &window);
                }
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
        assert!(helper_installing_now().is_none());
    }
}
