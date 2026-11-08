//! Native document compatibility belongs to Arctic, not to its HTTP clients.
use pyo3::{exceptions::PyValueError, prelude::*, types::PyBytes};
use std::panic::{AssertUnwindSafe, catch_unwind};
use wright_crdt::CrdtDocument;
use wright_document::{serialize, title};
use wright_model::{Schema, Slice};
use wright_state::{AllSelection, EditorState, EditorStateConfig};
use yrs::ReadTxn;

#[cfg_attr(feature = "stubgen", pyo3_stub_gen::derive::gen_stub_pyclass)]
#[pyclass(frozen)]
pub struct NativeDocument {
    state: Vec<u8>,
    update: Vec<u8>,
    projection: String,
    inferred_title: Option<String>,
}

#[cfg_attr(feature = "stubgen", pyo3_stub_gen::derive::gen_stub_pymethods)]
#[pymethods]
impl NativeDocument {
    #[getter]
    fn state<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.state)
    }
    #[getter]
    fn update<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.update)
    }
    #[getter]
    fn projection(&self) -> &str {
        &self.projection
    }
    #[getter]
    fn inferred_title(&self) -> Option<String> {
        self.inferred_title.clone()
    }
}

fn schema(name: &str) -> Result<Schema, String> {
    match name {
        "basic" => Ok(wright_model::basic_schema()),
        "wright-rich-text" => Ok(wright_rich_text::wright_schema()),
        _ => Err("unsupported_native_profile".into()),
    }
}

fn load(name: &str, updates: &[Vec<u8>]) -> Result<CrdtDocument, String> {
    if updates.is_empty()
        || updates.len() > 16384
        || updates.iter().map(Vec::len).sum::<usize>() > 128 * 1024 * 1024
    {
        return Err("native_state_too_large".into());
    }
    let doc = CrdtDocument::from_updates(schema(name)?, updates.iter().map(Vec::as_slice))
        .map_err(|_| "invalid_native_state".to_string())?;
    if doc.read_transaction().has_missing_updates() {
        return Err("missing_dependencies".into());
    }
    Ok(doc)
}

fn finish(doc: &CrdtDocument, update: Vec<u8>) -> Result<NativeDocument, String> {
    let state = doc.encode_state_as_update();
    if state.len() > 64 * 1024 * 1024 {
        return Err("native_state_too_large".into());
    }
    Ok(NativeDocument {
        state,
        update,
        projection: serde_json::to_string(&serialize::serialize_document(
            doc.pm_doc(),
            doc.schema(),
        ))
        .map_err(|_| "invalid_native_state".to_string())?,
        inferred_title: title::infer_document_title(doc.pm_doc(), doc.schema()),
    })
}

fn guarded(work: impl FnOnce() -> Result<NativeDocument, String>) -> PyResult<NativeDocument> {
    catch_unwind(AssertUnwindSafe(work))
        .map_err(|_| PyValueError::new_err("invalid_native_state"))?
        .map_err(PyValueError::new_err)
}

/// Reconstruct native Yrs bytes without inventing replacement identities.
#[cfg_attr(feature = "stubgen", pyo3_stub_gen::derive::gen_stub_pyfunction)]
#[pyfunction]
pub fn native_merge(
    py: Python<'_>,
    schema_name: String,
    updates: Vec<Vec<u8>>,
) -> PyResult<NativeDocument> {
    py.detach(move || guarded(|| finish(&load(&schema_name, &updates)?, Vec::new())))
}

/// Create a new lineage from a validated current model, or a schema-valid empty document.
#[cfg_attr(feature = "stubgen", pyo3_stub_gen::derive::gen_stub_pyfunction)]
#[pyfunction]
#[pyo3(signature = (schema_name, content=None))]
pub fn native_create(
    py: Python<'_>,
    schema_name: String,
    content: Option<String>,
) -> PyResult<NativeDocument> {
    py.detach(move || {
        guarded(|| {
            let schema = schema(&schema_name)?;
            let node = if let Some(content) = content {
                let (node, found) = serialize::deserialize_document(&content)
                    .map_err(|_| "invalid_native_model".to_string())?;
                if found.name() != schema.name() {
                    return Err("unsupported_native_profile".into());
                }
                node
            } else {
                schema
                    .top_node_type()
                    .create_and_fill(&schema, None, None, None)
                    .ok_or_else(|| "invalid_native_model".to_string())?
            };
            let doc = CrdtDocument::from_pm_doc(schema, &node);
            if !doc.pm_doc().eq_node(&node) {
                return Err("invalid_native_model".into());
            }
            finish(&doc, Vec::new())
        })
    })
}

/// Restore visible content by creating a new transaction in the CURRENT lineage.
#[cfg_attr(feature = "stubgen", pyo3_stub_gen::derive::gen_stub_pyfunction)]
#[pyfunction]
pub fn native_restore(
    py: Python<'_>,
    schema_name: String,
    current: Vec<u8>,
    historical: Vec<u8>,
) -> PyResult<NativeDocument> {
    py.detach(move || {
        guarded(|| {
            let mut doc = load(&schema_name, &[current])?;
            let old = load(&schema_name, &[historical])?;
            let state = EditorState::create(EditorStateConfig {
                schema: doc.schema().clone(),
                doc: Some(doc.pm_doc().clone()),
                selection: None,
                plugins: vec![],
            });
            let mut transaction = state.transaction();
            transaction.set_selection(Box::new(AllSelection::new(doc.pm_doc())));
            if !transaction.try_replace_selection(Slice::new(old.pm_doc().content().clone(), 0, 0))
            {
                return Err("invalid_native_restore".into());
            }
            for (name, value) in old.pm_doc().attrs().iter() {
                transaction.set_doc_attribute(name, value.clone());
            }
            transaction
                .rejection()
                .map_err(|_| "invalid_native_restore".to_string())?;
            doc.apply_transaction(&transaction);
            if !doc.pm_doc().eq_node(old.pm_doc()) {
                return Err("invalid_native_restore".into());
            }
            let mut update = Vec::new();
            for event in doc.pending_updates().updates {
                // A replacement is one native transaction. Never merge in old bytes.
                if !update.is_empty() {
                    return Err("invalid_native_restore".into());
                }
                update.extend_from_slice(&event.bytes);
            }
            if update.is_empty() {
                update = vec![0, 0];
            }
            finish(&doc, update)
        })
    })
}
