//! Minimal native engine exposed to Python through PyO3.

use pyo3::prelude::*;
mod documents;

/// Return a greeting from the Rust engine.
#[cfg_attr(feature = "stubgen", pyo3_stub_gen::derive::gen_stub_pyfunction)]
#[pyfunction]
fn hello(name: &str) -> String {
    format!("Hello, {name}! Greetings from Rust.")
}

#[pymodule]
fn _core(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(hello, module)?)?;
    module.add_class::<documents::NativeDocument>()?;
    module.add_function(wrap_pyfunction!(documents::native_merge, module)?)?;
    module.add_function(wrap_pyfunction!(documents::native_create, module)?)?;
    module.add_function(wrap_pyfunction!(documents::native_restore, module)?)?;
    Ok(())
}

#[cfg(feature = "stubgen")]
pyo3_stub_gen::define_stub_info_gatherer!(stub_info);
