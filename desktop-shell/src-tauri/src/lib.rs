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
pub mod ui;
pub mod update_progress;
pub mod window_state;

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::webview::{PageLoadEvent, WebviewWindowBuilder};
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindow, WindowEvent, Wry};

use crate::ladder::{Decision, Reconnect, Respawn};
use crate::status::Status;
use crate::ui::Page;
use crate::update_progress::Stage;

/// The single windows label. One window, one label, everywhere.
const MAIN_WINDOW: &str = "main";

/// First-paint size, used only until the status document says what the
/// operator configured. Not a configuration value: nothing about the config
/// directory, the port or the URL is decided here (C1).
const FIRST_PAINT_WIDTH: f64 = 1100.0;
const FIRST_PAINT_HEIGHT: f64 = 800.0;

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

static RETRY_REQUESTED: AtomicBool = AtomicBool::new(false);
/// Whether closing the window hides it instead of ending the app. Read from
/// the status document on every ladder pass; `false` until one has been read,
/// so a window that has learned nothing yet still closes when told to.
static CLOSE_TO_TRAY: AtomicBool = AtomicBool::new(false);
static TRAY_BUILT: AtomicBool = AtomicBool::new(false);
static ACTIVATION_STARTED: AtomicBool = AtomicBool::new(false);
static QUITTING: AtomicBool = AtomicBool::new(false);

static DATA_DIR: OnceLock<PathBuf> = OnceLock::new();
static LOCAL_URL: OnceLock<String> = OnceLock::new();
static TRAY_STATUS_ITEM: Mutex<Option<MenuItem<Wry>>> = Mutex::new(None);

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

fn save_geometry(window: &WebviewWindow) {
    let Some(directory) = DATA_DIR.get() else {
        return;
    };
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
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let menu = Menu::with_items(app, &[&open, &status, &separator, &quit])?;

    let mut builder = TrayIconBuilder::with_id("mcc-shell-tray")
        .tooltip("My Claude Code")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => raise(app),
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

    if let Ok(mut guard) = TRAY_STATUS_ITEM.lock() {
        *guard = Some(status);
    }
    Ok(())
}

// -- the ladder -------------------------------------------------------------

/// Run the install script, streaming its output into the window.
fn run_install(window: &WebviewWindow) {
    let command = install::install_command_for_this_machine();
    show_page(
        window,
        &Page::Installing {
            command: command.display.clone(),
        },
    );
    set_tray_status("Installing My Claude Code...");
    let outcome = process::run_install(&command, |line| append_output(window, line));
    match outcome {
        Ok(0) => append_output(window, "-- install finished, checking again --"),
        Ok(code) => append_output(
            window,
            &format!("-- the installer exited with status {code} --"),
        ),
        Err(error) => append_output(window, &format!("-- {error} --")),
    }
}

/// Poll `/health` until the server answers, or until the documents own start
/// budget runs out (C9).
fn wait_for_start(window: &WebviewWindow, status: &Status, health_url: &str) -> bool {
    let started = Instant::now();
    let interval = Duration::from_secs_f64(status.health_check_interval_seconds.max(0.05));
    loop {
        if health::is_healthy(health_url) {
            return true;
        }
        let elapsed = started.elapsed().as_secs_f64();
        if !ladder::start_may_continue(status, elapsed) {
            return false;
        }
        show_page(
            window,
            &Page::Starting {
                message: format!(
                    "Starting the My Claude Code server... ({elapsed:.0}s of \
                     {:.0}s)",
                    status.start_timeout_seconds
                ),
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
    episode.respawned = true;
    match process::spawn_server() {
        Ok(_) => {
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
    let remembered = window_state::load(&window_state::state_path(directory));
    if remembered.width.is_none() || remembered.height.is_none() {
        // First run: the size the operator configured in Python is the size
        // the window opens at, so DESKTOP_WINDOW_WIDTH/HEIGHT keep meaning
        // something now that geometry is remembered here.
        let (width, height) =
            remembered.size_or(status.window_width.max(480), status.window_height.max(320));
        let _ = window.set_size(tauri::LogicalSize::new(f64::from(width), f64::from(height)));
        let _ = window.center();
    }
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
            // Decision Q4: do not merely offer. Run it, show it, then loop.
            run_install(window);
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

    ensure_tray(app, &status);
    ensure_activation_watcher(app, &status);
    apply_status(window, &status);

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
            set_tray_status("Server: starting");
            show_page(
                window,
                &Page::Starting {
                    message: "Starting the My Claude Code server...".to_owned(),
                },
            );
            if let Err(error) = process::spawn_server() {
                show_page(
                    window,
                    &Page::Error {
                        message: error,
                        server_log: Some(status.server_log.clone()),
                    },
                );
                wait_for_retry(app);
                return;
            }
            if wait_for_start(window, &status, &health_url) {
                set_tray_status("Server: running");
                show_dashboard(window, &admin_url);
                watch_health(app, window, &status);
            } else {
                set_tray_status("Server: did not start");
                show_page(
                    window,
                    &Page::Error {
                        message: ladder::start_timeout_message(&status),
                        server_log: Some(status.server_log.clone()),
                    },
                );
            }
            wait_for_retry(app);
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

/// Build and run the application.
pub fn run() {
    tauri::Builder::default()
        // First, so a second launch is answered before anything else is set up.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            raise(app);
        }))
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
            let remembered = window_state::load(&window_state::state_path(&directory));

            let window =
                WebviewWindowBuilder::new(app, MAIN_WINDOW, WebviewUrl::App("index.html".into()))
                    .title("My Claude Code")
                    .min_inner_size(640.0, 480.0)
                    .inner_size(FIRST_PAINT_WIDTH, FIRST_PAINT_HEIGHT)
                    .center()
                    .build()?;

            // Remembered geometry is physical, and is re-applied physically,
            // so a window on a scaled display comes back the size it was
            // rather than the size a logical round trip would make it.
            if let (Some(width), Some(height)) = (remembered.width, remembered.height) {
                let _ = window.set_size(tauri::PhysicalSize::new(width, height));
            }
            if let (Some(x), Some(y)) = (remembered.x, remembered.y) {
                let _ = window.set_position(tauri::PhysicalPosition::new(x, y));
            }
            if remembered.maximized {
                let _ = window.maximize();
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
}
