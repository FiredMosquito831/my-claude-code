//! Taking over from a staged replacement, at the one moment that is safe.
//!
//! Contract C5 says this shell is not an updater, and it still is not: it
//! downloads nothing, verifies nothing and chooses nothing. Python does all
//! three, in `config/desktop_shell.py`, when the window asks it to with
//! `mcc-desktop --ensure-shell` (decision Q5). What Python cannot do is write
//! over a *running* executable, so it leaves the verified new binary beside
//! the old one as `MyClaudeCode.exe.new`, with the receipt that describes it
//! as `MyClaudeCode.receipt.json.new`, and stops.
//!
//! This module is the other half: three renames, run before the window, the
//! tray or the ladder exist, because process start is the only moment at which
//! nobody is holding either file.
//!
//! Two shapes of start reach it, and they are told apart by nothing more than
//! the name of the file being run:
//!
//! * **Running as `MyClaudeCode.exe.new`.** The old window's "Restart now"
//!   launched us. The old binary is moved to `MyClaudeCode.old-<stamp>.exe`,
//!   we rename ourselves onto `MyClaudeCode.exe`, and the receipt follows.
//!   Renaming a running executable is allowed on every platform this ships to
//!   -- it is the same move `_install_atomically` makes in Python -- so no
//!   second launch is needed: the process carries on, under the right name.
//! * **Running as `MyClaudeCode.exe` with a `.new` beside us.** The user did
//!   not press the button; they just quit and started the app again. That is
//!   the promise the banner made ("it updates the next time you restart the
//!   app"), so we start the staged binary and exit before building anything.
//!   The staged process then takes the branch above.
//!
//! The rename-aside copy is deliberately kept for one run rather than deleted
//! here: if the new build cannot start at all, the previous one is still on
//! disk under a name a person can rename back. It is swept on the next start
//! that does not create one, and `config/desktop_shell.py` sweeps the same
//! glob.

use std::ffi::OsStr;
use std::io;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// What Python appends to the file it staged. Must equal
/// `DESKTOP_SHELL_STAGED_SUFFIX` in `config/desktop_shell.py`.
pub const STAGED_SUFFIX: &str = ".new";

/// The receipt Python writes beside the binary. Must equal
/// `DESKTOP_SHELL_RECEIPT_FILENAME` in `config/desktop_shell.py`.
pub const RECEIPT_FILENAME: &str = "MyClaudeCode.receipt.json";

/// How many times a rename is retried, and how long between attempts.
///
/// The old process exits and this one starts at very nearly the same instant,
/// and on Windows a file can stay open for a few milliseconds after the
/// process that held it is gone (antivirus reads the image on close). One
/// failed rename must not cost the user their update.
const RENAME_ATTEMPTS: u32 = 25;
const RENAME_PAUSE: Duration = Duration::from_millis(200);

/// What this start has to do about a staged binary, before anything else.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Plan {
    /// No staged replacement is involved. Carry on and build the window.
    Nothing,
    /// This process *is* the staged binary: take over the canonical name.
    Adopt { canonical: PathBuf },
    /// A staged replacement is waiting and this process is the old one: start
    /// it and exit.
    Relaunch { staged: PathBuf },
}

/// Decide, from the path being run and whether a `.new` sits beside it.
///
/// Pure, so the rule is a unit test rather than a hand-run of two binaries.
pub fn plan(current_exe: &Path, staged_exists: bool) -> Plan {
    let name = current_exe
        .file_name()
        .and_then(OsStr::to_str)
        .unwrap_or_default();
    if let Some(stem) = name.strip_suffix(STAGED_SUFFIX) {
        if stem.is_empty() {
            return Plan::Nothing;
        }
        return Plan::Adopt {
            canonical: current_exe.with_file_name(stem),
        };
    }
    if staged_exists {
        return Plan::Relaunch {
            staged: staged_path(current_exe),
        };
    }
    Plan::Nothing
}

/// Where a staged replacement for `binary` lives.
pub fn staged_path(binary: &Path) -> PathBuf {
    let name = binary
        .file_name()
        .and_then(OsStr::to_str)
        .unwrap_or_default();
    binary.with_file_name(format!("{name}{STAGED_SUFFIX}"))
}

/// Where the previous binary is kept for one run.
///
/// `MyClaudeCode.old-<stamp>.exe` and not `MyClaudeCode.exe.old-<stamp>`, so
/// the glob `config/desktop_shell.py` already sweeps (`MyClaudeCode.old-*`)
/// picks it up, and so the file keeps an extension Windows understands if
/// somebody has to rename it back by hand.
pub fn aside_name(file_name: &str, stamp: u64) -> String {
    match file_name.rsplit_once('.') {
        Some((stem, extension)) if !stem.is_empty() => {
            format!("{stem}.old-{stamp}.{extension}")
        }
        _ => format!("{file_name}.old-{stamp}"),
    }
}

fn stamp() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|since| since.as_secs())
        .unwrap_or_default()
}

fn rename_with_retries(from: &Path, to: &Path) -> io::Result<()> {
    let mut last = None;
    for attempt in 0..RENAME_ATTEMPTS {
        match std::fs::rename(from, to) {
            Ok(()) => return Ok(()),
            Err(error) => {
                last = Some(error);
                if attempt + 1 < RENAME_ATTEMPTS {
                    std::thread::sleep(RENAME_PAUSE);
                }
            }
        }
    }
    Err(last.unwrap_or_else(|| io::Error::other("rename failed")))
}

/// Delete rename-aside copies from previous updates. Best effort, always.
pub fn sweep_aside(directory: &Path) {
    let Ok(entries) = std::fs::read_dir(directory) else {
        return;
    };
    for entry in entries.flatten() {
        let name = entry.file_name();
        let Some(name) = name.to_str() else { continue };
        if name.contains(".old-") && name.starts_with("MyClaudeCode") {
            let _ = std::fs::remove_file(entry.path());
        }
    }
}

/// Take the canonical name, moving whatever holds it aside first.
///
/// `current` is this process's own path (the staged one). Returns the path the
/// process now answers to, which is `canonical` on success and `current` when
/// the rename could not be made -- a shell that could not rename itself is
/// still a working shell, and refusing to start would turn a cosmetic failure
/// into an outage.
pub fn adopt(current: &Path, canonical: &Path) -> PathBuf {
    let stamp = stamp();
    if canonical.exists() {
        let aside = canonical
            .file_name()
            .and_then(OsStr::to_str)
            .map(|name| canonical.with_file_name(aside_name(name, stamp)));
        if let Some(aside) = aside {
            if rename_with_retries(canonical, &aside).is_err() {
                return current.to_path_buf();
            }
        }
    }
    if rename_with_retries(current, canonical).is_err() {
        return current.to_path_buf();
    }
    // The receipt follows the binary it describes. Best effort: a binary
    // without its receipt is one `--ensure-shell` will simply stage again.
    let staged_receipt = canonical.with_file_name(format!("{RECEIPT_FILENAME}{STAGED_SUFFIX}"));
    if staged_receipt.is_file() {
        let _ = std::fs::rename(&staged_receipt, canonical.with_file_name(RECEIPT_FILENAME));
    }
    canonical.to_path_buf()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn scratch(name: &str) -> PathBuf {
        let directory = std::env::temp_dir().join(format!("mcc-swap-{name}-{}", stamp_nanos()));
        fs::create_dir_all(&directory).expect("scratch directory");
        directory
    }

    fn stamp_nanos() -> u128 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("a clock after 1970")
            .as_nanos()
    }

    #[test]
    fn an_ordinary_start_with_nothing_staged_does_nothing() {
        assert_eq!(
            plan(Path::new("C:/apps/MyClaudeCode.exe"), false),
            Plan::Nothing
        );
    }

    #[test]
    fn an_ordinary_start_with_something_staged_relaunches_into_it() {
        // The promise the banner makes: it updates the next time you restart
        // the app, whether or not you pressed the button.
        assert_eq!(
            plan(Path::new("C:/apps/MyClaudeCode.exe"), true),
            Plan::Relaunch {
                staged: PathBuf::from("C:/apps/MyClaudeCode.exe.new"),
            }
        );
    }

    #[test]
    fn the_staged_binary_takes_the_canonical_name() {
        assert_eq!(
            plan(Path::new("C:/apps/MyClaudeCode.exe.new"), false),
            Plan::Adopt {
                canonical: PathBuf::from("C:/apps/MyClaudeCode.exe"),
            }
        );
        // And it does not matter that a `.new` "exists": it is us.
        assert_eq!(
            plan(Path::new("C:/apps/MyClaudeCode.exe.new"), true),
            Plan::Adopt {
                canonical: PathBuf::from("C:/apps/MyClaudeCode.exe"),
            }
        );
    }

    #[test]
    fn a_posix_binary_with_no_extension_swaps_the_same_way() {
        assert_eq!(
            plan(Path::new("/usr/bin/MyClaudeCode"), true),
            Plan::Relaunch {
                staged: PathBuf::from("/usr/bin/MyClaudeCode.new"),
            }
        );
        assert_eq!(
            plan(Path::new("/usr/bin/MyClaudeCode.new"), false),
            Plan::Adopt {
                canonical: PathBuf::from("/usr/bin/MyClaudeCode"),
            }
        );
    }

    #[test]
    fn the_aside_name_is_the_one_python_already_sweeps() {
        // `config/desktop_shell.py::_sweep_renamed_aside` globs
        // `MyClaudeCode.old-*`, so the stamp goes before the extension.
        assert_eq!(
            aside_name("MyClaudeCode.exe", 1757000000),
            "MyClaudeCode.old-1757000000.exe"
        );
        assert_eq!(
            aside_name("MyClaudeCode", 1757000000),
            "MyClaudeCode.old-1757000000"
        );
    }

    #[test]
    fn adopting_moves_the_old_binary_aside_and_brings_the_receipt_across() {
        let directory = scratch("adopt");
        let canonical = directory.join("MyClaudeCode.exe");
        let staged = directory.join("MyClaudeCode.exe.new");
        fs::write(&canonical, b"old").expect("old binary");
        fs::write(&staged, b"new").expect("staged binary");
        fs::write(directory.join(RECEIPT_FILENAME), b"{\"tag\":\"v1\"}").expect("receipt");
        fs::write(
            directory.join(format!("{RECEIPT_FILENAME}{STAGED_SUFFIX}")),
            b"{\"tag\":\"v2\"}",
        )
        .expect("staged receipt");

        let now = adopt(&staged, &canonical);

        assert_eq!(now, canonical);
        assert_eq!(fs::read(&canonical).expect("the new binary"), b"new");
        assert!(!staged.exists(), "the staged file was consumed");
        assert_eq!(
            fs::read_to_string(directory.join(RECEIPT_FILENAME)).expect("the receipt"),
            "{\"tag\":\"v2\"}",
            "the receipt has to name what is now on disk, or nothing can tell \
             whether the pin reached this machine"
        );
        let aside: Vec<_> = fs::read_dir(&directory)
            .expect("read the directory")
            .flatten()
            .filter(|entry| entry.file_name().to_string_lossy().contains(".old-"))
            .collect();
        assert_eq!(aside.len(), 1, "the previous build is kept for one run");
        assert_eq!(fs::read(aside[0].path()).expect("the old binary"), b"old");

        sweep_aside(&directory);
        assert!(
            !aside[0].path().exists(),
            "and swept on a later start, so they do not accumulate"
        );
        fs::remove_dir_all(&directory).ok();
    }

    #[test]
    fn adopting_onto_a_name_nothing_holds_still_works() {
        // The old binary was deleted by hand between the stage and the start.
        let directory = scratch("adopt-fresh");
        let canonical = directory.join("MyClaudeCode.exe");
        let staged = directory.join("MyClaudeCode.exe.new");
        fs::write(&staged, b"new").expect("staged binary");
        assert_eq!(adopt(&staged, &canonical), canonical);
        assert_eq!(fs::read(&canonical).expect("the new binary"), b"new");
        fs::remove_dir_all(&directory).ok();
    }
}
