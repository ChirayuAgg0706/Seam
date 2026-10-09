use pyo3::prelude::*;

#[pyfunction]
fn apply_discount(price_cents: u64, discount_percent: u64) -> u64 {
    let discount_cents = price_cents * discount_percent / 100;
    assert!(discount_cents <= price_cents);
    discount_cents
}

#[pymodule]
fn seam_checkout(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(apply_discount, m)?)?;
    Ok(())
}
