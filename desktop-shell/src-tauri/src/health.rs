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

/// How long a single probe may take. This is a socket timeout, not a policy:
/// the policies -- how often to poll, how long to keep trying -- are all read
/// from the status document (C9).
const PROBE_TIMEOUT: Duration = Duration::from_millis(1500);

/// The header MCC's shutdown gate stamps on the 503 it refuses with, spelled
/// exactly as `core/stop_deadline.py` spells it. It is the difference between
/// "MCC is going away on purpose" and "something on this port is unwell", and
/// it is the only thing in this file that names a name Python owns.
pub const SHUTDOWN_MARKER_HEADER: &str = "x-mcc-shutdown";

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

/// Probe `url` and keep the reason.
pub fn probe_outcome(raw: &str) -> ProbeOutcome {
    match probe(raw) {
        Ok(outcome) => outcome,
        Err(detail) => detail,
    }
}

fn probe(raw: &str) -> Result<ProbeOutcome, ProbeOutcome> {
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
    let mut stream = TcpStream::connect_timeout(&address, PROBE_TIMEOUT)
        .map_err(|error| ProbeOutcome::Refused(connection_phrase(&error)))?;
    stream
        .set_read_timeout(Some(PROBE_TIMEOUT))
        .map_err(|error| ProbeOutcome::Refused(error.to_string()))?;
    stream
        .set_write_timeout(Some(PROBE_TIMEOUT))
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
    if code == 503 && head_carries_shutdown_marker(head) {
        return ProbeOutcome::ShuttingDown;
    }
    ProbeOutcome::Http(code)
}

/// Whether the head carries MCC's shutdown marker.
fn head_carries_shutdown_marker(head: &str) -> bool {
    head.lines().skip(1).any(|line| {
        let Some((name, value)) = line.split_once(':') else {
            return false;
        };
        name.trim().eq_ignore_ascii_case(SHUTDOWN_MARKER_HEADER) && value.trim() == "1"
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
