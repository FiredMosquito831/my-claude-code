//! The status document, and the only place the shell learns anything.
//!
//! Contract C1: this shell never resolves the configuration directory, the
//! port or the admin URL. It runs `mcc-desktop --print-status`, parses the
//! JSON below, and uses the strings verbatim. There is deliberately no
//! fallback value for any of them -- a default would be a second source of
//! truth, and a second source of truth is how the design decays.
//!
//! Contract C3: unknown keys are tolerated (Python may add one without
//! bumping `schema`), and an unknown `schema` is refused loudly rather than
//! guessed at.
//!
//! Contract C9: every timing below is read from the document. None of them
//! has a compiled-in default, so a routine server update can never be painted
//! over with an error page because the shell disagreed about the budget.

use serde::Deserialize;

/// The one `schema` value this build understands.
pub const SUPPORTED_SCHEMA: u64 = 1;

/// The status document. `serde` tolerates unknown fields by default, and that
/// default is load-bearing here -- see C3.
#[derive(Debug, Clone, Deserialize)]
pub struct Status {
    pub schema: u64,
    pub version: String,
    pub config_dir: String,
    pub admin_url: String,
    pub health_url: String,
    pub server_presence: String,
    /// The startup stage a `starting` server named, when it named one. Optional
    /// because every wheel before 6.59.0 emits a document without it, and this
    /// shell has to keep working against those (C3).
    #[serde(default)]
    pub server_starting_stage: Option<String>,
    pub port_conflict: Option<String>,
    pub server_mode: String,
    pub window_width: u32,
    pub window_height: u32,
    pub tray_enabled: bool,
    pub minimize_to_tray: bool,
    /// Whether closing this window should hide it rather than end the app.
    ///
    /// Already resolved by Python, and that is load-bearing. It is NOT
    /// `minimize_to_tray && tray_enabled`: `tray_enabled` answers "should THIS
    /// window draw a tray icon", which is false on Windows and macOS precisely
    /// because a Python tray is already drawing one. Computing the close
    /// behaviour from it here is what made the close button end the app on the
    /// two platforms that actually have a tray.
    pub close_to_tray: bool,
    pub server_log: String,
    pub start_timeout_seconds: f64,
    /// How many further start attempts follow the first one before the window
    /// stops calling itself "starting". C9 again: the shell counts the
    /// attempts, but it never decides how many there are. A flat single
    /// attempt of `start_timeout_seconds` is what parked this window on a
    /// Retry button seven seconds before the server answered.
    pub server_start_retries: u32,
    pub health_check_interval_seconds: f64,
    pub health_poll_seconds: f64,
    pub health_failure_threshold: u32,
    pub activation_poll_seconds: f64,
    pub reconnect_timeout_seconds: f64,
    /// How often, while reconnecting, to re-read this whole document instead
    /// of only re-probing the health URL. C9: there is deliberately no default
    /// here either -- a compiled-in cadence would be this binary deciding how
    /// often to run a process on the user's machine.
    pub reconnect_restatus_seconds: f64,
    /// Which release of *this binary* the wheel on this machine pins.
    ///
    /// Emitted since 6.44.0 and read by nobody until 6.60.0, which is the
    /// whole of BUG-0: the pin was enforced only by `ShellWindow.create()`, so
    /// a window launched from the Start Menu could sit fifteen releases behind
    /// the wheel forever. The shell now compares it with the tag stamped into
    /// this build and asks `mcc-desktop --ensure-shell` to stage the right one.
    ///
    /// Optional, per C3: a window must keep working against a wheel that does
    /// not send it, and `None` simply means the comparison cannot be made.
    #[serde(default)]
    pub shell_release_tag: Option<String>,
    /// Where the wheel believes the shell it manages is installed, and `null`
    /// when there is not a verified one. A fallback for naming the binary to
    /// update when this process cannot read its own path.
    #[serde(default)]
    pub shell_binary: Option<String>,
    /// What the receipt beside that binary says. Added in 6.60.0; tolerated
    /// here, not required, per the two-release rule in `desktop_status.py`.
    #[serde(default)]
    pub shell_installed_tag: Option<String>,
}

/// Why a status document could not be used.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StatusError {
    /// `mcc-desktop` answered in a shape this build was not written against.
    /// Refusing is the whole point: guessing at an unknown schema is how a
    /// shell renders a stale URL after an upgrade.
    UnsupportedSchema { found: u64, supported: u64 },
    /// The bytes were not the document at all.
    Malformed(String),
}

impl std::fmt::Display for StatusError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::UnsupportedSchema { found, supported } => write!(
                f,
                "mcc-desktop --print-status reported schema {found}, and this \
                 window only understands schema {supported}. Update the desktop \
                 window, or run the dashboard in a browser tab until you can."
            ),
            Self::Malformed(detail) => write!(
                f,
                "mcc-desktop --print-status did not print a status document: \
                 {detail}"
            ),
        }
    }
}

/// Parse a status document, checking `schema` before anything else.
///
/// The two-step parse is deliberate. Deserializing straight into [`Status`]
/// would report a missing field from a *future* schema as a field error, and
/// the user would read "missing field `admin_url`" instead of "this window is
/// too old". The schema is therefore read on its own first.
pub fn parse_status(raw: &str) -> Result<Status, StatusError> {
    let value: serde_json::Value =
        serde_json::from_str(raw).map_err(|error| StatusError::Malformed(error.to_string()))?;
    let schema = value
        .get("schema")
        .and_then(serde_json::Value::as_u64)
        .ok_or_else(|| StatusError::Malformed("no numeric `schema` key".to_owned()))?;
    if schema != SUPPORTED_SCHEMA {
        return Err(StatusError::UnsupportedSchema {
            found: schema,
            supported: SUPPORTED_SCHEMA,
        });
    }
    serde_json::from_value(value).map_err(|error| StatusError::Malformed(error.to_string()))
}

#[cfg(test)]
pub(crate) fn sample_json() -> serde_json::Value {
    serde_json::json!({
        "schema": 1,
        "version": "6.43.0",
        "config_dir": "/home/example/config",
        "config_dir_source": "current",
        "config_dir_is_legacy": false,
        "host": "127.0.0.1",
        "port": 9999,
        "root_url": "http://127.0.0.1:9999",
        "admin_url": "http://127.0.0.1:9999/admin",
        "health_url": "http://127.0.0.1:9999/health",
        "server_presence": "healthy",
        "server_starting_stage": serde_json::Value::Null,
        "port_conflict": serde_json::Value::Null,
        "server_mode": "spawn",
        "window": "auto",
        "window_open": true,
        "window_width": 1280,
        "window_height": 860,
        "tray_enabled": true,
        "minimize_to_tray": true,
        "close_to_tray": true,
        "start_at_login": false,
        "server_log": "/home/example/config/logs/server.log",
        "start_timeout_seconds": 30.0,
        "server_start_retries": 2,
        "health_check_interval_seconds": 0.5,
        "health_poll_seconds": 5.0,
        "health_failure_threshold": 3,
        "activation_poll_seconds": 1.0,
        "reconnect_timeout_seconds": 1320.0,
        "reconnect_restatus_seconds": 30.0
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_the_documented_document() {
        let status = parse_status(&sample_json().to_string()).expect("sample parses");
        assert_eq!(status.schema, 1);
        assert_eq!(status.server_presence, "healthy");
        assert_eq!(status.health_failure_threshold, 3);
        assert!((status.reconnect_timeout_seconds - 1320.0).abs() < f64::EPSILON);
    }

    #[test]
    fn a_document_carrying_a_key_this_build_has_never_heard_of_still_parses() {
        // C9's two-release rule, from the tolerating side. 6.58.3 emits
        // `update` -- null unless an update helper is installing right now --
        // and this shell reads its own fields by name rather than
        // deserializing the whole document, so an unknown key costs nothing.
        // That is what lets a wheel ship a key one release before a shell is
        // allowed to require it.
        let mut document = sample_json();
        document["update"] = serde_json::json!({
            "stage": "installing",
            "message": "Installing the new version.",
            "version": "6.58.3",
            "helper_pid": 4242,
            "elapsed_seconds": 12.0,
        });
        document["a_key_from_some_later_release"] = serde_json::json!(["anything"]);
        let status = parse_status(&document.to_string()).expect("unknown keys are fine");
        assert_eq!(status.schema, 1);
        assert_eq!(status.server_presence, "healthy");
    }

    #[test]
    fn close_to_tray_is_read_and_is_not_recomputed_from_tray_enabled() {
        // The defect: on Windows and macOS `tray_enabled` is false in this
        // document *because* a tray exists and belongs to Python. A window
        // that ANDs the two ends the app on exactly the platforms where
        // closing should have hidden it.
        let mut document = sample_json();
        document["tray_enabled"] = serde_json::json!(false);
        document["close_to_tray"] = serde_json::json!(true);
        let status = parse_status(&document.to_string()).expect("parses");
        assert!(!status.tray_enabled);
        assert!(status.close_to_tray);

        let mut opted_out = sample_json();
        opted_out["close_to_tray"] = serde_json::json!(false);
        assert!(
            !parse_status(&opted_out.to_string())
                .expect("parses")
                .close_to_tray
        );
    }

    #[test]
    fn a_document_without_close_to_tray_is_malformed() {
        // Same rule as every other budget and switch here: no compiled-in
        // default, because a default is a second source of truth.
        let mut document = sample_json();
        document
            .as_object_mut()
            .expect("an object")
            .remove("close_to_tray");
        let error = parse_status(&document.to_string()).expect_err("refused");
        assert!(matches!(error, StatusError::Malformed(_)));
    }

    #[test]
    fn parses_reconnect_restatus_seconds() {
        let status = parse_status(&sample_json().to_string()).expect("sample parses");
        assert!((status.reconnect_restatus_seconds - 30.0).abs() < f64::EPSILON);
        let moved = parse_status(
            &{
                let mut document = sample_json();
                document["reconnect_restatus_seconds"] = serde_json::json!(7.5);
                document
            }
            .to_string(),
        )
        .expect("parses");
        assert!((moved.reconnect_restatus_seconds - 7.5).abs() < f64::EPSILON);
    }

    #[test]
    fn a_document_without_reconnect_restatus_seconds_is_malformed() {
        // C9. A default here would be this binary deciding how often to run a
        // process on the user's machine, and the whole design of this shell is
        // that it decides nothing.
        let mut document = sample_json();
        document
            .as_object_mut()
            .expect("an object")
            .remove("reconnect_restatus_seconds");
        let error = parse_status(&document.to_string()).expect_err("refused");
        assert!(matches!(error, StatusError::Malformed(_)));
    }

    #[test]
    fn parses_server_start_retries() {
        let status = parse_status(&sample_json().to_string()).expect("sample parses");
        assert_eq!(status.server_start_retries, 2);
        let mut document = sample_json();
        document["server_start_retries"] = serde_json::json!(0);
        assert_eq!(
            parse_status(&document.to_string())
                .expect("parses")
                .server_start_retries,
            0
        );
    }

    #[test]
    fn a_document_without_server_start_retries_is_malformed() {
        // C9. Defaulting it here would be this binary deciding how long a
        // user's machine gets to start a server, which is precisely the
        // decision the status document exists to carry.
        let mut document = sample_json();
        document
            .as_object_mut()
            .expect("an object")
            .remove("server_start_retries");
        let error = parse_status(&document.to_string()).expect_err("refused");
        assert!(matches!(error, StatusError::Malformed(_)));
    }

    #[test]
    fn tolerates_unknown_keys() {
        // C3: Python may add a key without bumping `schema`, so an older
        // window has to keep working against a newer wheel.
        let mut document = sample_json();
        document["a_key_from_a_later_wheel"] = serde_json::json!({"nested": [1, 2, 3]});
        let status = parse_status(&document.to_string()).expect("unknown keys are tolerated");
        assert_eq!(status.admin_url, "http://127.0.0.1:9999/admin");
    }

    #[test]
    fn unknown_schema_refuses_loudly() {
        let mut document = sample_json();
        document["schema"] = serde_json::json!(2);
        let error = parse_status(&document.to_string()).expect_err("schema 2 is refused");
        assert_eq!(
            error,
            StatusError::UnsupportedSchema {
                found: 2,
                supported: 1
            }
        );
        // The message has to say what to do, not merely that something is wrong.
        assert!(error.to_string().contains("Update the desktop window"));
    }

    #[test]
    fn a_schema_bump_is_refused_before_a_missing_field_is_noticed() {
        // A future schema that also dropped a field must still read as "this
        // window is too old", never as "missing field `admin_url`".
        let document = serde_json::json!({"schema": 7, "whatever": true});
        let error = parse_status(&document.to_string()).expect_err("refused");
        assert!(matches!(
            error,
            StatusError::UnsupportedSchema { found: 7, .. }
        ));
    }

    #[test]
    fn malformed_bytes_are_reported_as_malformed() {
        let error = parse_status("not json at all").expect_err("refused");
        assert!(matches!(error, StatusError::Malformed(_)));
    }

    #[test]
    fn a_document_without_a_schema_is_malformed() {
        let error = parse_status("{\"admin_url\": \"http://x/admin\"}").expect_err("refused");
        assert!(matches!(error, StatusError::Malformed(_)));
    }
}
