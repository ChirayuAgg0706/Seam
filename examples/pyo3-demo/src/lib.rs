use pyo3::prelude::*;

/// Sum of squares of 0..n, computed in Rust.
#[pyfunction]
fn sum_squares(n: u64) -> u64 {
    let mut total = 0;
    for i in 0..n {
        total += i * i; // break here
    }
    total
}

#[pymodule]
fn seam_demo(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(sum_squares, m)?)?;
    Ok(())
}
