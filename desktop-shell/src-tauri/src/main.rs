// The window subsystem on Windows, so launching the app from a shortcut does
// not also open a console behind it. `debug_assertions` keeps the console in
// a development build, where the output is the point.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    mcc_shell::run()
}
