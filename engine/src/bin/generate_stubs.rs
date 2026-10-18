//! Generate native Python type stubs, optionally into a temporary directory.

fn main() -> pyo3_stub_gen::Result<()> {
    let mut info = _core::stub_info()?;
    if let Some(output_directory) = std::env::args_os().nth(1) {
        info.python_root = output_directory.into();
    }
    info.generate()?;

    // The native extension is a single module, not a Python subpackage.
    let package = info.python_root.join("engine");
    std::fs::rename(
        package.join("_core/__init__.pyi"),
        package.join("_core.pyi"),
    )?;
    std::fs::remove_dir(package.join("_core"))?;
    Ok(())
}
