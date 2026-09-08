fn main() {
    // The release this binary is built from, read by `crate::RELEASE_TAG`.
    // Cargo does not know a compile depends on an environment variable unless
    // it is told, so without this line a rebuild in a warm CI cache would keep
    // the tag of the release before it -- and the one thing this constant must
    // never be is wrong.
    println!("cargo:rerun-if-env-changed=MCC_SHELL_RELEASE_TAG");
    tauri_build::build()
}
