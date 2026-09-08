//! A loopback health check, written by hand.
//!
//! An HTTP client crate would be several hundred kilobytes and a TLS stack
//! for one request to `127.0.0.1`. The URL always comes from the status
//! document (C1) and is always loopback, so a bare `GET` over a `TcpStream`
//! is both sufficient and honest about what it is doing.
//!
//! The probe answers two questions with one request. `is_healthy` is the
//! boolean the reconnect ladder counts, and it is unchanged: anything that is
//! not a `2xx` is a failure, because a server that is refusing work is not a
//! server you can show a dashboard from. [`probe_outcome`] is the same answer
//! with its reason kept, and it exists only so the reconnect banner can say
//! *why* the last probe failed. A window that says "still trying -- connection
//! refused" and a window that says "still trying -- the server is shutting
//! down" are describing very different situations to the person watching, and
//! before 6.50.0 both of them said nothing at all.

use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

use url::Url;

/// How long a single probe may take when the status document does not say.
///
/// It is a socket timeout, not a policy -- the policies (how often to poll,
/// how long to keep trying) are all read from the document (C9). From 6.61.0
/// the timeout itself is in the document too, as
/// `health_probe_timeout_seconds`, because two constants that were meant to be
/// the same number (`launchers/common.py:18` and this one) are two numbers.
/// This value stays as the one 6.61.0 ships with, for the one release in which
/// the shell only tolerates the key.
pub const DEFAULT_PROBE_TIMEOUT: Duration = Duration::from_millis(1500);

/// The header MCC's shutdown gate stamps on the 503 it refuses with, spelled
/// exactly as `core/stop_deadline.py` spells it. It is the difference between
/// "MCC is going away on purpose" and "something on this port is unwell", and
/// it is the only thing in this file that names a name Python owns.
pub const SHUTDOWN_MARKER_HEADER: &str = "x-mcc-shutdown";

/// The header MCC's *startup* gate stamps on its 503, spelled exactly as
/// `core/startup_state.py` spells it. Shipped server-side in 6.59.0: the
/// listener moved in front of the twenty seconds of startup work, so a server
/// that is coming up now answers rather than refusing the connection. Reading
/// it is the difference between waiting for a server that is nearly ready and
/// spawning a second one into the bind race it is about to win.
pub const STARTING_MARKER_HEADER: &str = "x-mcc-starting";

/// How much of the response head to read. Enough for the status line and the
/// handful of headers the gate sends; never the body, because a server stuck
/// mid-body must not be able to stall the reconnect loop.
const HEAD_BYTES: usize = 2048;

/// What one probe saw. Only [`ProbeOutcome::Healthy`] is a healthy server;
/// the rest are named failures, and the names exist for the banner.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProbeOutcome {
    /// A `2xx`. The dashboard can be shown.
    Healthy,
    /// MCC's own shutdown gate: a 503 carrying the marker header. This is a
    /// server that is leaving on purpose and will be back.
    ShuttingDown,
    /// MCC's own startup gate: a 503 carrying `x-mcc-starting`. The server has
    /// the port and is working through its lifespan.
    StartingUp,
    /// Answered HTTP, but not with a 2xx and not as a drain.
    Http(u16),
    /// Nothing accepted the connection, or it timed out.
    Refused(String),
    /// Something answered, but not with anything this could read as HTTP.
    Unreadable(String),
}

impl ProbeOutcome {
    /// Whether the dashboard can be shown. The one question the ladder asks.
    pub fn is_healthy(&self) -> bool {
        matches!(self, Self::Healthy)
    }

    /// A short phrase for the banner, in the reader's terms rather than the
    /// protocol's. Never a stack of jargon: this is shown to someone who
    /// pressed Update and is watching a window.
    pub fn describe(&self) -> String {
        match self {
            Self::Healthy => "answering".to_owned(),
            Self::ShuttingDown => "shutting down".to_owned(),
            Self::StartingUp => "starting".to_owned(),
            Self::Http(code) => format!("HTTP {code}"),
            Self::Refused(detail) => detail.clone(),
            Self::Unreadable(detail) => detail.clone(),
        }
    }
}

/// Whether `url` answered with a `2xx`.
///
/// Any failure -- refused, timed out, garbled -- is a `false`. The caller
/// counts consecutive falses against the document's debounce threshold, so the
/// distinction is not what the ladder branches on; [`probe_outcome`] carries it
/// for the banner instead.
pub fn is_healthy(url: &str) -> bool {
    probe_outcome(url).is_healthy()
}

/// Probe `url` and keep the reason, with this build's default timeout.
pub fn probe_outcome(raw: &str) -> ProbeOutcome {
    probe_outcome_within(raw, DEFAULT_PROBE_TIMEOUT)
}

/// Probe `url` with the timeout the status document asked for.
///
/// One implementation, one timeout, one vocabulary -- audit §5.1. Every caller
/// in this binary goes through here, so there is no second place a probe can
/// disagree about what a 503 means.
pub fn probe_outcome_within(raw: &str, timeout: Duration) -> ProbeOutcome {
    match probe(raw, timeout) {
        Ok(outcome) => outcome,
        Err(detail) => detail,
    }
}

fn probe(raw: &str, timeout: Duration) -> Result<ProbeOutcome, ProbeOutcome> {
    let unreadable = |detail: &str| ProbeOutcome::Unreadable(detail.to_owned());
    let url = Url::parse(raw).map_err(|error| unreadable(&error.to_string()))?;
    let host = url
        .host_str()
        .ok_or_else(|| unreadable("the health address names no host"))?;
    let port = url
        .port_or_known_default()
        .ok_or_else(|| unreadable("the health address names no port"))?;
    let mut path = url.path().to_owned();
    if let Some(query) = url.query() {
        path.push('?');
        path.push_str(query);
    }

    let address = (host, port)
        .to_socket_addrs()
        .map_err(|error| ProbeOutcome::Refused(error.to_string()))?
        .next()
        .ok_or_else(|| {
            ProbeOutcome::Refused("the health address resolved to nothing".to_owned())
        })?;
    let mut stream = TcpStream::connect_timeout(&address, timeout)
        .map_err(|error| ProbeOutcome::Refused(connection_phrase(&error)))?;
    stream
        .set_read_timeout(Some(timeout))
        .map_err(|error| ProbeOutcome::Refused(error.to_string()))?;
    stream
        .set_write_timeout(Some(timeout))
        .map_err(|error| ProbeOutcome::Refused(error.to_string()))?;

    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\
         User-Agent: my-claude-code-shell\r\nAccept: */*\r\n\r\n"
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|error| ProbeOutcome::Refused(connection_phrase(&error)))?;
    stream
        .flush()
        .map_err(|error| ProbeOutcome::Refused(connection_phrase(&error)))?;

    // The head is all that is read. Draining the body would mean waiting on a
    // server that is, by hypothesis, possibly unwell -- and the marker header
    // that distinguishes a drain from a failure is in the head anyway.
    let mut buffer = [0_u8; HEAD_BYTES];
    let mut filled = 0_usize;
    while filled < buffer.len() {
        match stream.read(&mut buffer[filled..]) {
            Ok(0) => break,
            Ok(read) => {
                filled += read;
                if head_is_complete(&buffer[..filled]) {
                    break;
                }
            }
            Err(error) => {
                if filled == 0 {
                    return Err(ProbeOutcome::Refused(connection_phrase(&error)));
                }
                break;
            }
        }
    }
    Ok(outcome_from_head(&String::from_utf8_lossy(
        &buffer[..filled],
    )))
}

/// Whether the blank line that ends an HTTP head has arrived.
fn head_is_complete(bytes: &[u8]) -> bool {
    // Both spellings: the canonical CRLF pair, and the bare LF pair a
    // hand-rolled server on loopback may well send.
    bytes.windows(4).any(|window| window == b"\r\n\r\n")
        || bytes.windows(2).any(|window| window == b"\n\n")
}

/// A phrase a person can read, for the commonest socket failures. `io::Error`'s
/// own Display on Windows is a sentence with an OS error number in it, which is
/// noise in a banner that is trying to say "nothing is listening yet".
fn connection_phrase(error: &std::io::Error) -> String {
    match error.kind() {
        std::io::ErrorKind::ConnectionRefused => "connection refused".to_owned(),
        std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock => {
            "no answer within 1.5s".to_owned()
        }
        std::io::ErrorKind::ConnectionReset | std::io::ErrorKind::ConnectionAborted => {
            "the connection was closed".to_owned()
        }
        _ => error.to_string(),
    }
}

/// Read an HTTP response head into an outcome. Split out so every branch is
/// testable without a socket.
pub fn outcome_from_head(head: &str) -> ProbeOutcome {
    let Some(code) = status_code(head) else {
        return ProbeOutcome::Unreadable("the answer was not HTTP".to_owned());
    };
    if (200..300).contains(&code) {
        return ProbeOutcome::Healthy;
    }
    if code == 503 && head_carries_marker(head, SHUTDOWN_MARKER_HEADER) {
        return ProbeOutcome::ShuttingDown;
    }
    // Deliberately after the shutdown marker: a server asked to stop during a
    // slow start answers with both gates' behaviour, and a caller told
    // "starting" about a server that is on its way out would wait for
    // something that is never coming back. `cli/desktop.py` orders it the same
    // way, and a contract test pins the two orderings together.
    if code == 503 && head_carries_marker(head, STARTING_MARKER_HEADER) {
        return ProbeOutcome::StartingUp;
    }
    ProbeOutcome::Http(code)
}

/// Whether the head carries one of MCC's own gate markers, set to `1`.
fn head_carries_marker(head: &str, marker: &str) -> bool {
    head.lines().skip(1).any(|line| {
        let Some((name, value)) = line.split_once(':') else {
            return false;
        };
        name.trim().eq_ignore_ascii_case(marker) && value.trim() == "1"
    })
}

/// The numeric status of an HTTP status line, if the line is one.
fn status_code(head: &str) -> Option<u16> {
    let line = head.lines().next().unwrap_or_default();
    let mut parts = line.split_whitespace();
    let version = parts.next()?;
    if !version.starts_with("HTTP/") {
        return None;
    }
    parts.next().and_then(|code| code.parse::<u16>().ok())
}

/// Whether an HTTP status line reports success. Split out so the parsing is
/// testable without a socket.
pub fn status_line_is_2xx(head: &str) -> bool {
    status_code(head).is_some_and(|code| (200..300).contains(&code))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Response heads, spelled with real CRLF escapes because that is what a
    /// socket delivers.
    const HEAD_STARTING: &str = "HTTP/1.1 503 Service Unavailable\r\nx-mcc-starting: 1\r\n";
    const HEAD_SHUTDOWN: &str = "HTTP/1.1 503 Service Unavailable\r\nx-mcc-shutdown: 1\r\n";
    const HEAD_BOTH: &str =
        "HTTP/1.1 503 Service Unavailable\r\nx-mcc-starting: 1\r\nx-mcc-shutdown: 1\r\n";
    const HEAD_PLAIN: &str = "HTTP/1.1 503 Service Unavailable\r\nretry-after: 5\r\n";

    #[test]
    fn a_starting_server_is_told_apart_from_a_draining_one() {
        // 6.59.0's startup gate. Before it existed a starting server refused
        // the connection outright, and the window read that as a free port --
        // which is how a second server came to be spawned into the bind race
        // the first one was about to win.
        assert_eq!(outcome_from_head(HEAD_STARTING), ProbeOutcome::StartingUp);
        assert_eq!(outcome_from_head(HEAD_SHUTDOWN), ProbeOutcome::ShuttingDown);
        // Both markers means going away: a server asked to stop mid-start is
        // not coming back, and Python's `probe_server_state` orders the two
        // the same way.
        assert_eq!(outcome_from_head(HEAD_BOTH), ProbeOutcome::ShuttingDown);
        // And a plain 503 from something that is not MCC stays a plain 503.
        assert_eq!(outcome_from_head(HEAD_PLAIN), ProbeOutcome::Http(503));
    }

    #[test]
    fn a_200_is_healthy() {
        assert!(status_line_is_2xx("HTTP/1.1 200 OK\r\nDate: now\r\n"));
        assert!(status_line_is_2xx("HTTP/1.0 204 No Content\r\n"));
    }

    #[test]
    fn anything_else_is_not() {
        assert!(!status_line_is_2xx(
            "HTTP/1.1 500 Internal Server Error\r\n"
        ));
        assert!(!status_line_is_2xx("HTTP/1.1 404 Not Found\r\n"));
        assert!(!status_line_is_2xx("HTTP/1.1 302 Found\r\n"));
        assert!(!status_line_is_2xx(""));
        assert!(!status_line_is_2xx("SSH-2.0-OpenSSH_9.6\r\n"));
        assert!(!status_line_is_2xx("HTTP/1.1 not-a-number\r\n"));
    }

    #[test]
    fn a_closed_port_is_unhealthy_rather_than_a_panic() {
        // Port 1 on loopback: nothing is there, and nothing may be started.
        assert!(!is_healthy("http://127.0.0.1:1/health"));
    }

    #[test]
    fn an_unparseable_url_is_unhealthy() {
        assert!(!is_healthy("not a url"));
    }

    #[test]
    fn a_503_reports_shutting_down_rather_than_a_bare_false() {
        // The exact head MCC's shutdown gate sends: 503, connection: close,
        // retry-after, and the marker.
        let head = "HTTP/1.1 503 Service Unavailable\r\n\
                    content-type: application/json\r\n\
                    connection: close\r\n\
                    retry-after: 5\r\n\
                    x-mcc-shutdown: 1\r\n\r\n";
        assert_eq!(outcome_from_head(head), ProbeOutcome::ShuttingDown);
        assert_eq!(outcome_from_head(head).describe(), "shutting down");
        // It is still not healthy: the ladder's boolean must not move.
        assert!(!outcome_from_head(head).is_healthy());
        assert!(!status_line_is_2xx(head));
    }

    #[test]
    fn a_503_without_the_marker_is_not_claimed_as_ours() {
        // Some other service answering 503 on the configured port must not be
        // reported as "My Claude Code is restarting".
        let head = "HTTP/1.1 503 Service Unavailable\r\nserver: nginx\r\n\r\n";
        assert_eq!(outcome_from_head(head), ProbeOutcome::Http(503));
    }

    #[test]
    fn a_refusal_and_a_503_are_both_unhealthy_but_are_named_differently() {
        let refused = probe_outcome("http://127.0.0.1:1/health");
        let draining =
            outcome_from_head("HTTP/1.1 503 Service Unavailable\r\nx-mcc-shutdown: 1\r\n\r\n");
        assert!(!refused.is_healthy());
        assert!(!draining.is_healthy());
        assert_ne!(refused, draining);
        assert_ne!(refused.describe(), draining.describe());
        assert!(
            !refused.describe().is_empty(),
            "a banner with an empty reason explains nothing"
        );
    }

    #[test]
    fn a_marker_on_something_that_is_not_a_503_changes_nothing() {
        // Defensive: the marker means "this refusal is a drain", not "this
        // response is a drain". A 500 carrying it is still a 500.
        let head = "HTTP/1.1 500 Internal Server Error\r\nx-mcc-shutdown: 1\r\n\r\n";
        assert_eq!(outcome_from_head(head), ProbeOutcome::Http(500));
    }

    #[test]
    fn the_head_ends_at_the_blank_line() {
        assert!(head_is_complete(b"HTTP/1.1 200 OK\r\n\r\n"));
        assert!(!head_is_complete(b"HTTP/1.1 200 OK\r\n"));
    }
}
