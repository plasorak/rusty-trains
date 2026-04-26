use crate::core::model::{Infrastructure, NetElement, OperationalPoint, Track};
use crate::io::xml_util::parse_attr;
use std::collections::HashMap;
use std::path::Path;

const NS: &str = "https://www.railml.org/schemas/3.3";

/// Parse the `<infrastructure>` section of a RailML 3.3 file.
///
/// Extracts:
/// - `netElement` elements from `topology/netElements` (id, length in metres)
/// - `track` elements from `functionalInfrastructure/tracks` (id, netElementRef)
/// - `operationalPoint` elements from `functionalInfrastructure/operationalPoints` (id, name)
///
/// NetElements without a `@length` attribute are skipped with a warning.
pub fn load_infrastructure(path: &Path) -> Result<Infrastructure, String> {
    let xml = std::fs::read_to_string(path)
        .map_err(|e| format!("cannot read '{}': {e}", path.display()))?;
    let doc = roxmltree::Document::parse(&xml)
        .map_err(|e| format!("XML parse error in '{}': {e}", path.display()))?;

    let infra_node = doc
        .descendants()
        .find(|n| n.has_tag_name((NS, "infrastructure")))
        .ok_or_else(|| format!("no <infrastructure> element in '{}'", path.display()))?;

    // --- NetElements -----------------------------------------------------------

    let mut net_elements = HashMap::new();
    for ne in infra_node.descendants().filter(|n| n.has_tag_name((NS, "netElement"))) {
        let ctx = format!("netElement in '{}'", path.display());
        let id: String = parse_attr(ne, "id", &ctx)?;
        match parse_attr::<f64>(ne, "length", &ctx) {
            Ok(length_m) => {
                net_elements.insert(id.clone(), NetElement { id, length_m });
            }
            Err(_) => {
                eprintln!("Warning: netElement '{id}' has no parseable @length — skipped");
            }
        }
    }

    // --- Tracks ----------------------------------------------------------------

    let mut tracks = HashMap::new();
    for track in infra_node.descendants().filter(|n| n.has_tag_name((NS, "track"))) {
        let ctx = format!("track in '{}'", path.display());
        let id: String = parse_attr(track, "id", &ctx)?;
        let net_element_id: String = parse_attr(track, "netElementRef", &ctx)?;
        tracks.insert(id.clone(), Track { id, net_element_id });
    }

    // --- OperationalPoints (informational only) ---------------------------------

    let mut ops = HashMap::new();
    for op in infra_node.descendants().filter(|n| n.has_tag_name((NS, "operationalPoint"))) {
        if let Some(id) = op.attribute("id") {
            let name = op.attribute("name").map(str::to_string);
            ops.insert(id.to_string(), OperationalPoint { id: id.to_string(), name });
        }
    }

    println!(
        "Loaded infrastructure from '{}': {} net elements, {} tracks, {} operational points",
        path.display(),
        net_elements.len(),
        tracks.len(),
        ops.len(),
    );

    Ok(Infrastructure { net_elements, tracks, ops })
}
