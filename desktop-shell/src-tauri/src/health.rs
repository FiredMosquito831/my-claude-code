//! A loopback health check, written by hand.
//!
//! An HTTP client crate would be several hundred kilobytes and a TLS stack
//! for one request to `127.0.0.1`. The URL always comes from the status
//! document (C1) and is always loopback, so a bare `GET` over a `TcpStream`
//! is both sufficient and honest about what it is doing.

use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

use url::Url;

/// How long a single probe may take. This is a socket timeout, not a policy:
/// the policies -- how often to poll, how long to keep trying -- are all read
/// from the status document (C9).
const PROBE_TIMEOUT: Duration = Duration::from_millis(1500);

/// Whether `url` answered with a `2xx`.
///
/// Any failure -- refused, timed out, garbled -- is a `false`. The caller
/// counts consecutive falses against the documents debounce threshold, so
/// distinguishing them here would be discarded information.
pub fn is_healthy(url: &str) -> bool {
    probe(url).unwrap_or(false)
}

fn probe(raw: &str) -> Option<bool> {
    let url = Url::parse(raw).ok()?;
    let host = url.host_str()?;
    let port = url.port_or_known_default()?;
    let mut path = url.path().to_owned();
    if let Some(query) = url.query() {
        path.push('?');
        path.push_str(query);
    }

    let address = (host, port).to_socket_addrs().ok()?.next()?;
    let mut stream = TcpStream::connect_timeout(&address, PROBE_TIMEOUT).ok()?;
    stream.set_read_timeout(Some(PROBE_TIMEOUT)).ok()?;
    stream.set_write_timeout(Some(PROBE_TIMEOUT)).ok()?;

    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\
         User-Agent: my-claude-code-shell\r\nAccept: */*\r\n\r\n"
    );
    stream.write_all(request.as_bytes()).ok()?;
    stream.flush().ok()?;

    // The status line is all that is read. Draining the body would mean
    // waiting on a server that is, by hypothesis, possibly unwell.
    let mut buffer = [0_u8; 64];
    let read = stream.read(&mut buffer).ok()?;
    let head = String::from_utf8_lossy(&buffer[..read]);
    Some(status_line_is_2xx(&head))
}

/// Whether an HTTP status line reports success. Split out so the parsing is
/// testable without a socket.
pub fn status_line_is_2xx(head: &str) -> bool {
    let line = head.lines().next().unwrap_or_default();
    let mut parts = line.split_whitespace();
    let Some(version) = parts.next() else {
        return false;
    };
    if !version.starts_with("HTTP/") {
        return false;
    }
    parts
        .next()
        .and_then(|code| code.parse::<u16>().ok())
        .is_some_and(|code| (200..300).contains(&code))
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
}
