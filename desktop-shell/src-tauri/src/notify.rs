//! Notifications, shown by the app under its own name (7.71.0).
//!
//! The user's rule, verbatim (rescue spec, answer 1 of 2026-10-01 16:24): "if
//! a desktop app running come from desktop app else come from console where it
//! is running based on OS". So while this window runs, a dead server and every
//! rescue are announced BY THE APP -- never as a toast from another program's
//! name. Three things happen for every announcement, and only the first one
//! depends on the platform:
//!
//! 1. a native notification, where the app can attribute one to itself;
//! 2. the same sentence in the window (a banner over whatever is on screen);
//! 3. the same sentence in the shell's own transcript.
//!
//! Where (1) is not possible the sentence still reaches the user through (2)
//! and the record through (3); the reason (1) was skipped is itself written to
//! the transcript, so "no toast" is never a mystery.
//!
//! | platform | native notification | when |
//! |---|---|---|
//! | Windows | a toast with this app's `AppUserModelID` | only when the Windows installer registered that id under the app's name (`Setting()` is `Enabled`); a copy fetched by `mcc-desktop` (delivery path A) has no registration, and is in-window only rather than a toast labelled with somebody else's name |
//! | Linux | `notify-send --app-name "My Claude Code"` | when `notify-send` is installed |
//! | macOS | none yet | in-window only: an unsigned bundle cannot be shown to attribute a notification to itself, and nothing on this release's machines could verify one |
//! | any | none | when `mcc-desktop`'s own tray icon is the one speaking (`MCC_DESKTOP_SHELL_TRAY=0`): it already announces the outage under the app's name, and two notifications for one event is the defect the 7.70.0 host was written to avoid |

use std::process::Command;

/// The app's identity, from `tauri.conf.json`. On Windows it is also the
/// `AppUserModelID` the installer registers, with the display name "My Claude
/// Code", under `HKCU\Software\Classes\AppUserModelId` (removed by the
/// uninstaller).
pub const APP_USER_MODEL_ID: &str = "com.myclaudecode.desktop";

/// The title every native notification carries.
pub const TITLE: &str = "My Claude Code";

/// Set by `mcc-desktop` on the window it launches: `0` when the Python tray is
/// drawing the icon (and so announcing outages itself), `1` otherwise.
pub const SHELL_TRAY_ENV: &str = "MCC_DESKTOP_SHELL_TRAY";

/// What the platform said about this app's Windows toast registration.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ToastRegistration {
    /// Registered and allowed.
    Enabled,
    /// Registered, and switched off (by the user, by policy).
    Disabled(String),
    /// Not registered at all: a copy the installer did not put there.
    NotRegistered(String),
}

/// Where one announcement's native half goes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Route {
    /// A Windows toast under this app's own registered name.
    WindowsToast,
    /// `notify-send` with this app's name.
    NotifySend,
    /// No native notification; why, for the transcript.
    InWindowOnly(String),
}

/// The platform, as far as the route cares.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Platform {
    Windows,
    Linux,
    MacOs,
    Other,
}

impl Platform {
    /// This build's platform.
    pub fn current() -> Self {
        if cfg!(windows) {
            Self::Windows
        } else if cfg!(target_os = "linux") {
            Self::Linux
        } else if cfg!(target_os = "macos") {
            Self::MacOs
        } else {
            Self::Other
        }
    }
}

/// Whether `mcc-desktop`'s own tray is the one announcing outages.
pub fn python_tray_speaks(raw: Option<&str>) -> bool {
    raw.is_some_and(|value| value.trim() == "0")
}

/// The route, as a pure function of the facts. See the module table.
pub fn choose(
    platform: Platform,
    python_tray: bool,
    toast: Option<&ToastRegistration>,
    notify_send: bool,
) -> Route {
    if python_tray {
        return Route::InWindowOnly(
            "mcc-desktop's own tray icon announces this, so the app does not announce it twice"
                .to_owned(),
        );
    }
    match platform {
        Platform::Windows => match toast {
            Some(ToastRegistration::Enabled) => Route::WindowsToast,
            Some(ToastRegistration::Disabled(why)) => Route::InWindowOnly(format!(
                "notifications for My Claude Code are turned off in Windows ({why})"
            )),
            Some(ToastRegistration::NotRegistered(why)) => Route::InWindowOnly(format!(
                "this copy of the app has no Windows notification registration -- only the \
                 Windows installer creates one ({why})"
            )),
            None => {
                Route::InWindowOnly("the Windows notification registration is unknown".to_owned())
            }
        },
        Platform::Linux => {
            if notify_send {
                Route::NotifySend
            } else {
                Route::InWindowOnly("notify-send is not installed".to_owned())
            }
        }
        Platform::MacOs => Route::InWindowOnly(
            "this build shows macOS notifications in the window only".to_owned(),
        ),
        Platform::Other => {
            Route::InWindowOnly("no notification service on this platform".to_owned())
        }
    }
}

/// Text for an XML document, escaped.
pub fn xml_escape(text: &str) -> String {
    let mut escaped = String::with_capacity(text.len());
    for character in text.chars() {
        match character {
            '&' => escaped.push_str("&amp;"),
            '<' => escaped.push_str("&lt;"),
            '>' => escaped.push_str("&gt;"),
            '"' => escaped.push_str("&quot;"),
            '\'' => escaped.push_str("&apos;"),
            other => escaped.push(other),
        }
    }
    escaped
}

/// The Windows toast document: the app's name as the title, the sentence as
/// the body. Text only; nothing in it is markup the sentence controls.
pub fn toast_xml(title: &str, body: &str) -> String {
    format!(
        "<toast><visual><binding template=\"ToastGeneric\"><text>{}</text><text>{}</text>\
         </binding></visual></toast>",
        xml_escape(title),
        xml_escape(body)
    )
}

/// The route on this machine, right now. Impure: asks the platform.
pub fn route_here() -> Route {
    let platform = Platform::current();
    let python_tray = python_tray_speaks(std::env::var(SHELL_TRAY_ENV).ok().as_deref());
    let toast = if platform == Platform::Windows && !python_tray {
        Some(toast_registration())
    } else {
        None
    };
    let notify_send = platform == Platform::Linux && crate::process::on_path("notify-send");
    choose(platform, python_tray, toast.as_ref(), notify_send)
}

/// Show `body` natively along `route`. Returns what was done, for the log.
pub fn deliver(route: &Route, body: &str) -> Result<String, String> {
    match route {
        Route::WindowsToast => show_toast(body)
            .map(|()| format!("a Windows toast from {APP_USER_MODEL_ID} (\"{TITLE}\")")),
        Route::NotifySend => {
            let status = Command::new("notify-send")
                .args(["--app-name", TITLE, TITLE, body])
                .status()
                .map_err(|error| format!("notify-send could not be run: {error}"))?;
            if status.success() {
                Ok(format!("notify-send --app-name \"{TITLE}\""))
            } else {
                Err(format!("notify-send exited with {status}"))
            }
        }
        Route::InWindowOnly(why) => Ok(format!("in the window only: {why}")),
    }
}

#[cfg(windows)]
fn toast_registration() -> ToastRegistration {
    use windows::UI::Notifications::{NotificationSetting, ToastNotificationManager};
    use windows::core::HSTRING;

    let setting =
        ToastNotificationManager::CreateToastNotifierWithId(&HSTRING::from(APP_USER_MODEL_ID))
            .and_then(|notifier| notifier.Setting());
    match setting {
        Ok(NotificationSetting::Enabled) => ToastRegistration::Enabled,
        Ok(other) => ToastRegistration::Disabled(format!("setting {}", other.0)),
        Err(error) => ToastRegistration::NotRegistered(format!(
            "{} (0x{:08X})",
            error.message().trim(),
            error.code().0
        )),
    }
}

#[cfg(not(windows))]
fn toast_registration() -> ToastRegistration {
    ToastRegistration::NotRegistered("not Windows".to_owned())
}

#[cfg(windows)]
fn show_toast(body: &str) -> Result<(), String> {
    use windows::Data::Xml::Dom::XmlDocument;
    use windows::UI::Notifications::{ToastNotification, ToastNotificationManager};
    use windows::core::HSTRING;

    let shown = (|| -> windows::core::Result<()> {
        let document = XmlDocument::new()?;
        document.LoadXml(&HSTRING::from(toast_xml(TITLE, body)))?;
        let toast = ToastNotification::CreateToastNotification(&document)?;
        ToastNotificationManager::CreateToastNotifierWithId(&HSTRING::from(APP_USER_MODEL_ID))?
            .Show(&toast)
    })();
    shown.map_err(|error| format!("the toast could not be shown: {}", error.message().trim()))
}

#[cfg(not(windows))]
fn show_toast(_body: &str) -> Result<(), String> {
    Err("Windows toasts exist only on Windows".to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_registered_windows_app_gets_a_toast_and_nothing_else_does() {
        assert_eq!(
            choose(
                Platform::Windows,
                false,
                Some(&ToastRegistration::Enabled),
                false
            ),
            Route::WindowsToast
        );
        // Path A: the copy mcc-desktop fetched was never registered. A toast
        // from it would carry somebody else's name or none; the sentence is
        // shown in the window instead.
        let path_a = choose(
            Platform::Windows,
            false,
            Some(&ToastRegistration::NotRegistered(
                "Element not found".to_owned(),
            )),
            false,
        );
        assert!(matches!(path_a, Route::InWindowOnly(ref why) if why.contains("installer")));
        let off = choose(
            Platform::Windows,
            false,
            Some(&ToastRegistration::Disabled("setting 1".to_owned())),
            false,
        );
        assert!(matches!(off, Route::InWindowOnly(ref why) if why.contains("turned off")));
    }

    #[test]
    fn the_python_tray_speaking_means_the_app_does_not_speak_twice() {
        for platform in [Platform::Windows, Platform::Linux, Platform::MacOs] {
            let route = choose(platform, true, Some(&ToastRegistration::Enabled), true);
            assert!(matches!(route, Route::InWindowOnly(_)), "{platform:?}");
        }
        assert!(python_tray_speaks(Some("0")));
        assert!(python_tray_speaks(Some(" 0 ")));
        assert!(!python_tray_speaks(Some("1")));
        assert!(!python_tray_speaks(None));
    }

    #[test]
    fn linux_uses_notify_send_under_the_apps_name_when_it_exists() {
        assert_eq!(
            choose(Platform::Linux, false, None, true),
            Route::NotifySend
        );
        assert!(matches!(
            choose(Platform::Linux, false, None, false),
            Route::InWindowOnly(_)
        ));
    }

    #[test]
    fn macos_is_in_the_window_only_and_says_so() {
        assert!(matches!(
            choose(Platform::MacOs, false, None, true),
            Route::InWindowOnly(ref why) if why.contains("window only")
        ));
    }

    #[test]
    fn the_toast_document_escapes_the_sentence() {
        let xml = toast_xml(TITLE, "pid 42 <b>&</b> \"quoted\" 'single'");
        assert!(
            xml.contains("pid 42 &lt;b&gt;&amp;&lt;/b&gt; &quot;quoted&quot; &apos;single&apos;")
        );
        assert!(xml.starts_with("<toast><visual><binding template=\"ToastGeneric\">"));
        assert!(xml.contains("<text>My Claude Code</text>"));
    }

    #[test]
    fn delivering_in_the_window_only_does_nothing_outside_it() {
        let done = deliver(&Route::InWindowOnly("why".to_owned()), "a sentence").expect("ok");
        assert!(done.contains("in the window only"), "{done}");
    }
}
