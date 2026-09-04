//! Running the projects own installer when My Claude Code is not installed.
//!
//! Decision Q4: the window does not merely offer to install; when
//! `mcc-desktop` is missing it runs the repositorys install script itself,
//! shows the exact command it is running and streams the output into the
//! page, then continues the ladder. It never bundles Python, and it never
//! carries a second copy of the server.
//!
//! The command is chosen from the target OS and nothing else, so the choice
//! is a table with a test rather than a `cfg!` scattered through the runtime.

/// The base the install scripts are fetched from. One string, so a fork can
/// change it in one place.
const SCRIPT_BASE: &str =
    "https://raw.githubusercontent.com/FiredMosquito831/my-claude-code/main/scripts";

/// A command to run, plus the exact text to show the user before running it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InstallCommand {
    /// The program to execute.
    pub program: String,
    /// Its arguments, already split -- never a shell string this code joins.
    pub args: Vec<String>,
    /// What to print in the window. This is the command a user could paste
    /// into their own terminal and get the same result.
    pub display: String,
}

/// The install command for a target OS, spelled as `std::env::consts::OS`.
///
/// Windows gets the PowerShell one-liner from the README; everything else
/// gets the POSIX one. There is no third branch: macOS and Linux install the
/// same way.
pub fn install_command(os: &str) -> InstallCommand {
    if os == "windows" {
        let script = format!("& ([scriptblock]::Create((irm \"{SCRIPT_BASE}/install.ps1\")))");
        return InstallCommand {
            program: "powershell.exe".to_owned(),
            args: vec![
                "-NoProfile".to_owned(),
                "-ExecutionPolicy".to_owned(),
                "Bypass".to_owned(),
                "-Command".to_owned(),
                script.clone(),
            ],
            display: script,
        };
    }
    let script = format!("curl -fsSL \"{SCRIPT_BASE}/install.sh\" | sh");
    InstallCommand {
        program: "sh".to_owned(),
        args: vec!["-c".to_owned(), script.clone()],
        display: script,
    }
}

/// The command for the machine this binary is running on.
pub fn install_command_for_this_machine() -> InstallCommand {
    install_command(std::env::consts::OS)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn windows_runs_the_powershell_one_liner() {
        let command = install_command("windows");
        assert_eq!(command.program, "powershell.exe");
        assert_eq!(
            command.args.last().expect("a script argument"),
            &command.display
        );
        assert!(command.args.contains(&"-NoProfile".to_owned()));
        assert!(
            command
                .display
                .starts_with("& ([scriptblock]::Create((irm \"https://"),
            "{}",
            command.display
        );
        assert!(command.display.ends_with("/scripts/install.ps1\")))"));
    }

    #[test]
    fn linux_and_macos_pipe_the_shell_script() {
        for os in ["linux", "macos", "freebsd"] {
            let command = install_command(os);
            assert_eq!(command.program, "sh", "{os}");
            assert_eq!(command.args[0], "-c", "{os}");
            assert_eq!(command.args[1], command.display, "{os}");
            assert!(command.display.starts_with("curl -fsSL "), "{os}");
            assert!(
                command.display.ends_with("/scripts/install.sh\" | sh"),
                "{os}"
            );
        }
    }

    #[test]
    fn the_displayed_command_is_the_command_that_runs() {
        // The window shows what it is about to do. If these ever drift, the
        // page is lying about what ran on the users machine.
        for os in ["windows", "linux", "macos"] {
            let command = install_command(os);
            assert!(
                command.args.contains(&command.display),
                "{os} shows a command it does not run"
            );
        }
    }

    #[test]
    fn both_scripts_come_from_the_projects_own_repository() {
        for os in ["windows", "linux"] {
            assert!(
                install_command(os)
                    .display
                    .contains("FiredMosquito831/my-claude-code"),
                "{os}"
            );
        }
    }
}
