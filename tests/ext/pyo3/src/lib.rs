// PyO3 extension used by the binding-layer scenarios.
use pyo3::prelude::*;

#[pyfunction]
fn add(a: i64, b: i64) -> i64 {
    let sum = a + b; // add-body
    sum
}

#[pyfunction]
fn call_back(fn_: &Bound<'_, PyAny>, x: i64) -> PyResult<Py<PyAny>> {
    let res = fn_.call1((x,))?; // callback-call
    Ok(res.unbind()) // callback-after
}

#[pymodule]
fn seam_pyo3(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(add, m)?)?;
    m.add_function(wrap_pyfunction!(call_back, m)?)?;
    Ok(())
}
