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
    pub port_conflict: Option<String>,
    pub server_mode: String,
    pub window_width: u32,
    pub window_height: u32,
    pub tray_enabled: bool,
    pub minimize_to_tray: bool,
    pub server_log: String,
    pub start_timeout_seconds: f64,
    pub health_check_interval_seconds: f64,
    pub health_poll_seconds: f64,
    pub health_failure_threshold: u32,
    pub activation_poll_seconds: f64,
    pub reconnect_timeout_seconds: f64,
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
        "port_conflict": serde_json::Value::Null,
        "server_mode": "spawn",
        "window": "auto",
        "window_open": true,
        "window_width": 1280,
        "window_height": 860,
        "tray_enabled": true,
        "minimize_to_tray": false,
        "start_at_login": false,
        "server_log": "/home/example/config/logs/server.log",
        "start_timeout_seconds": 30.0,
        "health_check_interval_seconds": 0.5,
        "health_poll_seconds": 5.0,
        "health_failure_threshold": 3,
        "activation_poll_seconds": 1.0,
        "reconnect_timeout_seconds": 1320.0
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
