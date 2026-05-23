use crate::xml_util::parse_attr;
use sim_types::{NetElementId, OpPointId, TrackId};
use std::collections::HashMap;
use std::path::Path;

const NS: &str = "https://www.railml.org/schemas/3.3";

#[derive(Debug, Clone, PartialEq)]
pub struct NetElement {
    pub id: NetElementId,
    pub length_m: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Track {
    pub id: TrackId,
    pub net_element_id: NetElementId,
}

#[derive(Debug, Clone, PartialEq)]
pub struct OperationalPoint {
    pub id: OpPointId,
    pub name: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Signal {
    pub id: String,
    pub net_element_id: NetElementId,
    pub offset_m: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Platform {
    pub id: String,
    pub op_id: OpPointId,
    pub track_id: TrackId,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Infrastructure {
    pub net_elements: HashMap<NetElementId, NetElement>,
    pub tracks: HashMap<TrackId, Track>,
    pub ops: HashMap<OpPointId, OperationalPoint>,
    pub signals: Vec<Signal>,
    pub platforms: Vec<Platform>,
}

// ---------------------------------------------------------------------------
// Parser
// ---------------------------------------------------------------------------

pub fn load_infrastructure(path: &Path) -> Result<Infrastructure, String> {
    let xml = std::fs::read_to_string(path)
        .map_err(|e| format!("cannot read '{}': {e}", path.display()))?;
    parse_infrastructure_xml(&xml, &path.display().to_string())
}

pub fn parse_infrastructure_xml(xml: &str, label: &str) -> Result<Infrastructure, String> {
    let doc = roxmltree::Document::parse(xml)
        .map_err(|e| format!("XML parse error in '{label}': {e}"))?;

    let infra_node = doc
        .descendants()
        .find(|n| n.has_tag_name((NS, "infrastructure")))
        .ok_or_else(|| format!("no <infrastructure> element in '{label}'"))?;

    // --- NetElements -----------------------------------------------------------

    let mut net_elements = HashMap::new();
    for ne in infra_node.descendants().filter(|n| n.has_tag_name((NS, "netElement"))) {
        let ctx = format!("netElement in '{label}'");
        let raw_id: String = parse_attr(ne, "id", &ctx)?;
        let length_m: f64 = if let Ok(v) = parse_attr::<f64>(ne, "length", &ctx) {
            v
        } else if let Some(child) = ne.children().find(|n| n.has_tag_name((NS, "length"))) {
            match child.attribute("quantity").and_then(|v| v.parse::<f64>().ok()) {
                Some(v) => v,
                None => {
                    eprintln!("Warning: netElement '{raw_id}' has no parseable length — skipped");
                    continue;
                }
            }
        } else {
            eprintln!("Warning: netElement '{raw_id}' has no parseable length — skipped");
            continue;
        };
        let id = NetElementId(raw_id);
        net_elements.insert(id.clone(), NetElement { id, length_m });
    }

    // --- Tracks ----------------------------------------------------------------

    let mut tracks = HashMap::new();
    for track in infra_node.descendants().filter(|n| n.has_tag_name((NS, "track"))) {
        let ctx = format!("track in '{label}'");
        let raw_id: String = parse_attr(track, "id", &ctx)?;
        let raw_ne: String = parse_attr(track, "netElementRef", &ctx)?;
        let id = TrackId(raw_id);
        let net_element_id = NetElementId(raw_ne);
        tracks.insert(id.clone(), Track { id, net_element_id });
    }

    // --- OperationalPoints (with nested platforms) -----------------------------

    let mut ops = HashMap::new();
    let mut platforms = Vec::new();
    for op in infra_node.descendants().filter(|n| n.has_tag_name((NS, "operationalPoint"))) {
        let raw_id = match op.attribute("id") {
            Some(v) => v.to_string(),
            None => continue,
        };
        let name = op.attribute("name").map(str::to_string);
        let op_id = OpPointId(raw_id);

        for plat in op.children().filter(|n| n.has_tag_name((NS, "platform"))) {
            let ctx = format!("platform in operationalPoint '{}' in '{label}'", op_id.0);
            let plat_id: String = parse_attr(plat, "id", &ctx)?;
            let raw_track: String = parse_attr(plat, "trackId", &ctx)?;
            platforms.push(Platform {
                id: plat_id,
                op_id: op_id.clone(),
                track_id: TrackId(raw_track),
            });
        }

        ops.insert(op_id.clone(), OperationalPoint { id: op_id, name });
    }

    // --- Signals ---------------------------------------------------------------

    let mut signals = Vec::new();
    for sig in infra_node.descendants().filter(|n| n.has_tag_name((NS, "signal"))) {
        let ctx = format!("signal in '{label}'");
        let raw_id: String = parse_attr(sig, "id", &ctx)?;
        if let Some(loc) = sig.descendants().find(|n| n.has_tag_name((NS, "spotLocation"))) {
            let ctx2 = format!("spotLocation of signal '{raw_id}' in '{label}'");
            let raw_ne: String = parse_attr(loc, "netElementRef", &ctx2)?;
            let offset_m: f64 = parse_attr(loc, "offset", &ctx2)?;
            signals.push(Signal {
                id: raw_id,
                net_element_id: NetElementId(raw_ne),
                offset_m,
            });
        } else {
            eprintln!("Warning: signal '{raw_id}' has no <spotLocation> — skipped");
        }
    }

    Ok(Infrastructure { net_elements, tracks, ops, signals, platforms })
}

// ---------------------------------------------------------------------------
// Serialiser
// ---------------------------------------------------------------------------

impl Infrastructure {
    pub fn to_xml(&self) -> String {
        let mut out = String::new();
        out.push_str("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n");
        out.push_str("<rail3:railml xmlns:rail3=\"https://www.railml.org/schemas/3.3\">\n");
        out.push_str("  <rail3:infrastructure>\n");

        // topology / netElements
        out.push_str("    <rail3:topology>\n      <rail3:netElements>\n");
        let mut nes: Vec<_> = self.net_elements.values().collect();
        nes.sort_by(|a, b| a.id.0.cmp(&b.id.0));
        for ne in nes {
            out.push_str(&format!(
                "        <rail3:netElement id=\"{}\" length=\"{}\"/>\n",
                ne.id.0, ne.length_m
            ));
        }
        out.push_str("      </rail3:netElements>\n    </rail3:topology>\n");

        // functionalInfrastructure
        out.push_str("    <rail3:functionalInfrastructure>\n");

        // tracks
        out.push_str("      <rail3:tracks>\n");
        let mut tracks: Vec<_> = self.tracks.values().collect();
        tracks.sort_by(|a, b| a.id.0.cmp(&b.id.0));
        for t in tracks {
            out.push_str(&format!(
                "        <rail3:track id=\"{}\" netElementRef=\"{}\"/>\n",
                t.id.0, t.net_element_id.0
            ));
        }
        out.push_str("      </rail3:tracks>\n");

        // operationalPoints (with nested platforms)
        out.push_str("      <rail3:operationalPoints>\n");
        let mut ops: Vec<_> = self.ops.values().collect();
        ops.sort_by(|a, b| a.id.0.cmp(&b.id.0));
        for op in ops {
            let op_plats: Vec<_> =
                self.platforms.iter().filter(|p| p.op_id == op.id).collect();
            let name_attr = op
                .name
                .as_ref()
                .map(|n| format!(" name=\"{n}\""))
                .unwrap_or_default();
            if op_plats.is_empty() {
                out.push_str(&format!(
                    "        <rail3:operationalPoint id=\"{}\"{name_attr}/>\n",
                    op.id.0
                ));
            } else {
                out.push_str(&format!(
                    "        <rail3:operationalPoint id=\"{}\"{name_attr}>\n",
                    op.id.0
                ));
                for p in op_plats {
                    out.push_str(&format!(
                        "          <rail3:platform id=\"{}\" trackId=\"{}\"/>\n",
                        p.id, p.track_id.0
                    ));
                }
                out.push_str("        </rail3:operationalPoint>\n");
            }
        }
        out.push_str("      </rail3:operationalPoints>\n");

        // signals
        if !self.signals.is_empty() {
            out.push_str("      <rail3:signals>\n");
            let mut sigs = self.signals.clone();
            sigs.sort_by(|a, b| a.id.cmp(&b.id));
            for s in &sigs {
                out.push_str(&format!(
                    "        <rail3:signal id=\"{}\">\n          <rail3:spotLocation netElementRef=\"{}\" offset=\"{}\"/>\n        </rail3:signal>\n",
                    s.id, s.net_element_id.0, s.offset_m
                ));
            }
            out.push_str("      </rail3:signals>\n");
        }

        out.push_str("    </rail3:functionalInfrastructure>\n");
        out.push_str("  </rail3:infrastructure>\n");
        out.push_str("</rail3:railml>\n");
        out
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    const NS_DECL: &str = r#"xmlns:rail3="https://www.railml.org/schemas/3.3""#;

    fn wrap(body: &str) -> String {
        format!(
            r#"<?xml version="1.0"?><rail3:railml {NS_DECL}><rail3:infrastructure>{body}</rail3:infrastructure></rail3:railml>"#
        )
    }

    #[test]
    fn net_elements_and_tracks() {
        let xml = wrap(
            r#"<rail3:topology><rail3:netElements>
               <rail3:netElement id="ne_1" length="1000.0"/>
               <rail3:netElement id="ne_2" length="500.5"/>
             </rail3:netElements></rail3:topology>
             <rail3:functionalInfrastructure><rail3:tracks>
               <rail3:track id="track_A" netElementRef="ne_1"/>
             </rail3:tracks></rail3:functionalInfrastructure>"#,
        );
        let infra = parse_infrastructure_xml(&xml, "test").unwrap();
        assert_eq!(infra.net_elements.len(), 2);
        assert!((infra.net_elements[&NetElementId::from("ne_1")].length_m - 1000.0).abs() < 1e-9);
        assert_eq!(infra.tracks[&TrackId::from("track_A")].net_element_id, NetElementId::from("ne_1"));
    }

    #[test]
    fn operational_points_with_platforms() {
        let xml = wrap(
            r#"<rail3:functionalInfrastructure>
               <rail3:operationalPoints>
                 <rail3:operationalPoint id="OP_A" name="Station A">
                   <rail3:platform id="plat_1" trackId="track_1"/>
                 </rail3:operationalPoint>
                 <rail3:operationalPoint id="OP_B"/>
               </rail3:operationalPoints>
             </rail3:functionalInfrastructure>"#,
        );
        let infra = parse_infrastructure_xml(&xml, "test").unwrap();
        assert_eq!(infra.ops.len(), 2);
        assert_eq!(infra.ops[&OpPointId::from("OP_A")].name.as_deref(), Some("Station A"));
        assert_eq!(infra.platforms.len(), 1);
        assert_eq!(infra.platforms[0].id, "plat_1");
        assert_eq!(infra.platforms[0].track_id, TrackId::from("track_1"));
    }

    #[test]
    fn signals() {
        let xml = wrap(
            r#"<rail3:functionalInfrastructure>
               <rail3:signals>
                 <rail3:signal id="sig_1">
                   <rail3:spotLocation netElementRef="ne_1" offset="500.0"/>
                 </rail3:signal>
                 <rail3:signal id="sig_2">
                   <rail3:spotLocation netElementRef="ne_2" offset="100.0"/>
                 </rail3:signal>
               </rail3:signals>
             </rail3:functionalInfrastructure>"#,
        );
        let infra = parse_infrastructure_xml(&xml, "test").unwrap();
        assert_eq!(infra.signals.len(), 2);
        let s1 = infra.signals.iter().find(|s| s.id == "sig_1").unwrap();
        assert_eq!(s1.net_element_id, NetElementId::from("ne_1"));
        assert!((s1.offset_m - 500.0).abs() < 1e-9);
    }

    #[test]
    fn length_as_child_element() {
        let xml = wrap(
            r#"<rail3:topology><rail3:netElements>
               <rail3:netElement id="ne_x"><rail3:length quantity="750.25"/></rail3:netElement>
             </rail3:netElements></rail3:topology>"#,
        );
        let infra = parse_infrastructure_xml(&xml, "test").unwrap();
        assert!((infra.net_elements[&NetElementId::from("ne_x")].length_m - 750.25).abs() < 1e-9);
    }

    #[test]
    fn missing_infrastructure_element_errors() {
        let xml = format!(
            r#"<?xml version="1.0"?><rail3:railml {NS_DECL}><rail3:rollingstock/></rail3:railml>"#
        );
        let err = parse_infrastructure_xml(&xml, "test").unwrap_err();
        assert!(err.contains("no <infrastructure>"));
    }

    #[test]
    fn round_trip() {
        let xml = wrap(
            r#"<rail3:topology><rail3:netElements>
               <rail3:netElement id="ne_1" length="2000.0"/>
               <rail3:netElement id="ne_2" length="1500.0"/>
               <rail3:netElement id="ne_3" length="1000.0"/>
             </rail3:netElements></rail3:topology>
             <rail3:functionalInfrastructure>
               <rail3:tracks>
                 <rail3:track id="track_1" netElementRef="ne_1"/>
                 <rail3:track id="track_2" netElementRef="ne_2"/>
                 <rail3:track id="track_3" netElementRef="ne_3"/>
               </rail3:tracks>
               <rail3:operationalPoints>
                 <rail3:operationalPoint id="OP_A" name="Station A">
                   <rail3:platform id="plat_1" trackId="track_1"/>
                 </rail3:operationalPoint>
                 <rail3:operationalPoint id="OP_B" name="Terminal B"/>
               </rail3:operationalPoints>
               <rail3:signals>
                 <rail3:signal id="sig_1">
                   <rail3:spotLocation netElementRef="ne_1" offset="500.0"/>
                 </rail3:signal>
                 <rail3:signal id="sig_2">
                   <rail3:spotLocation netElementRef="ne_2" offset="200.0"/>
                 </rail3:signal>
               </rail3:signals>
             </rail3:functionalInfrastructure>"#,
        );
        let first = parse_infrastructure_xml(&xml, "original").unwrap();
        let serialised = first.to_xml();
        let second = parse_infrastructure_xml(&serialised, "round-tripped").unwrap();
        assert_eq!(first, second);
    }
}
