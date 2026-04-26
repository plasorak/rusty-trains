use crate::core::model::{Infrastructure, Route, RouteElement};
use std::collections::HashMap;
use std::path::Path;

const NS: &str = "https://www.railml.org/schemas/3.3";

/// Parse routes for all operational trains from the `<timetable>` section of a
/// RailML 3.3 file.
///
/// Only the **route geometry** is extracted — no timing, stops, or validity.
///
/// ## Resolution chain
///
/// ```text
/// operationalTrain
///   operationalTrainVariant[@itineraryRef]
///     itinerary
///       range[@baseItineraryRef, @sequenceNumber, @start?, @end?]
///         baseItinerary
///           baseItineraryPoint[@sequenceNumber]  (sorted ascending)
///             followupSections/followupSection   (lowest priority value wins)
///               trackRefs/trackRef[@ref, @sequenceNumber]
/// ```
///
/// Track IDs are resolved to `RouteElement`s via the supplied `Infrastructure`.
/// Tracks or net elements that are absent from the infrastructure are skipped
/// with a warning — they do not abort the parse.
///
/// Returns a map from **operational train id** → `Route`.
pub fn load_routes(
    path: &Path,
    infra: &Infrastructure,
) -> Result<HashMap<String, Route>, String> {
    let xml = std::fs::read_to_string(path)
        .map_err(|e| format!("cannot read '{}': {e}", path.display()))?;
    let doc = roxmltree::Document::parse(&xml)
        .map_err(|e| format!("XML parse error in '{}': {e}", path.display()))?;

    let tt_node = match doc.descendants().find(|n| n.has_tag_name((NS, "timetable"))) {
        Some(n) => n,
        None => {
            println!("No <timetable> element in '{}' — no routes loaded", path.display());
            return Ok(HashMap::new());
        }
    };

    // -----------------------------------------------------------------------
    // Step 1: Parse every baseItinerary into an ordered list of track IDs.
    //
    // A baseItinerary is a sequence of baseItineraryPoints.  The tracks that
    // connect point N to point N+1 live in point N's followupSections.
    // The last point has no followupSections (no onward section).
    // -----------------------------------------------------------------------

    // bip_id → sequence_number (for range slicing later)
    let mut bip_seq: HashMap<String, u32> = HashMap::new();
    // base_itinerary_id → (bip_id, seq, Vec<(seq, track_id)>)
    let mut base_itineraries: HashMap<String, Vec<(u32, String, Vec<(u32, String)>)>> =
        HashMap::new();

    for bi in tt_node.descendants().filter(|n| n.has_tag_name((NS, "baseItinerary"))) {
        let bi_id = match bi.attribute("id") {
            Some(v) => v.to_string(),
            None => continue,
        };

        let mut points: Vec<(u32, String, Vec<(u32, String)>)> = Vec::new();

        for bip in bi.children().filter(|n| n.has_tag_name((NS, "baseItineraryPoint"))) {
            let bip_id = match bip.attribute("id") {
                Some(v) => v.to_string(),
                None => continue,
            };
            let seq: u32 = bip
                .attribute("sequenceNumber")
                .and_then(|v| v.parse().ok())
                .unwrap_or(0);

            bip_seq.insert(bip_id.clone(), seq);

            // Collect tracks from the best-priority followupSection.
            // Priority 0 (or absent) = highest priority; pick lowest numeric value.
            let followup_sections = bip
                .children()
                .find(|n| n.has_tag_name((NS, "followupSections")));

            let track_ids = match followup_sections {
                None => Vec::new(),
                Some(fs_container) => {
                    let best_section = fs_container
                        .children()
                        .filter(|n| n.has_tag_name((NS, "followupSection")))
                        .min_by_key(|n| {
                            n.attribute("priority")
                                .and_then(|v| v.parse::<i64>().ok())
                                .unwrap_or(0)
                        });

                    match best_section {
                        None => Vec::new(),
                        Some(section) => {
                            let track_refs_container = section
                                .children()
                                .find(|n| n.has_tag_name((NS, "trackRefs")));
                            match track_refs_container {
                                None => Vec::new(),
                                Some(trc) => {
                                    let mut refs: Vec<(u32, String)> = trc
                                        .children()
                                        .filter(|n| n.has_tag_name((NS, "trackRef")))
                                        .filter_map(|tr| {
                                            let track_ref = tr.attribute("ref")?.to_string();
                                            let sn: u32 = tr
                                                .attribute("sequenceNumber")
                                                .and_then(|v| v.parse().ok())
                                                .unwrap_or(0);
                                            Some((sn, track_ref))
                                        })
                                        .collect();
                                    refs.sort_by_key(|(sn, _)| *sn);
                                    refs
                                }
                            }
                        }
                    }
                }
            };

            points.push((seq, bip_id, track_ids));
        }

        points.sort_by_key(|(seq, _, _)| *seq);
        base_itineraries.insert(bi_id, points);
    }

    // -----------------------------------------------------------------------
    // Step 2: Parse every itinerary as an ordered list of ItineraryRange
    //         references into base itineraries (with optional BIP start/end).
    // -----------------------------------------------------------------------

    struct ItineraryRange {
        base_itinerary_ref: String,
        start_bip: Option<String>, // inclusive first BIP id
        end_bip: Option<String>,   // inclusive last BIP id
        seq: u32,
    }

    let mut itineraries: HashMap<String, Vec<ItineraryRange>> = HashMap::new();

    for iti in tt_node.descendants().filter(|n| n.has_tag_name((NS, "itinerary"))) {
        let iti_id = match iti.attribute("id") {
            Some(v) => v.to_string(),
            None => continue,
        };

        let mut ranges: Vec<ItineraryRange> = iti
            .children()
            .filter(|n| n.has_tag_name((NS, "range")))
            .filter_map(|r| {
                let base_ref = r.attribute("baseItineraryRef")?.to_string();
                let seq: u32 =
                    r.attribute("sequenceNumber").and_then(|v| v.parse().ok()).unwrap_or(0);
                Some(ItineraryRange {
                    base_itinerary_ref: base_ref,
                    start_bip: r.attribute("start").map(str::to_string),
                    end_bip: r.attribute("end").map(str::to_string),
                    seq,
                })
            })
            .collect();
        ranges.sort_by_key(|r| r.seq);
        itineraries.insert(iti_id, ranges);
    }

    // -----------------------------------------------------------------------
    // Step 3: Parse operationalTrain elements and resolve their routes.
    // -----------------------------------------------------------------------

    let mut routes: HashMap<String, Route> = HashMap::new();

    for ot in tt_node.descendants().filter(|n| n.has_tag_name((NS, "operationalTrain"))) {
        let ot_id = match ot.attribute("id") {
            Some(v) => v.to_string(),
            None => continue,
        };

        // Take the first operationalTrainVariant.
        let variant = match ot
            .children()
            .find(|n| n.has_tag_name((NS, "operationalTrainVariant")))
        {
            Some(v) => v,
            None => continue,
        };

        let itinerary_ref = match variant.attribute("itineraryRef") {
            Some(v) => v.to_string(),
            None => {
                eprintln!(
                    "Warning: operationalTrainVariant in train '{ot_id}' has no @itineraryRef — skipped"
                );
                continue;
            }
        };

        let iti_ranges = match itineraries.get(&itinerary_ref) {
            Some(r) => r,
            None => {
                eprintln!(
                    "Warning: itinerary '{itinerary_ref}' referenced by train '{ot_id}' not found — skipped"
                );
                continue;
            }
        };

        // Flatten all ranges into an ordered list of track IDs.
        let mut all_track_ids: Vec<String> = Vec::new();

        for range in iti_ranges {
            let bi_points = match base_itineraries.get(&range.base_itinerary_ref) {
                Some(p) => p,
                None => {
                    eprintln!(
                        "Warning: baseItinerary '{}' not found (train '{}') — skipped",
                        range.base_itinerary_ref, ot_id
                    );
                    continue;
                }
            };

            // Determine the BIP sequence-number window.
            let start_seq = range
                .start_bip
                .as_deref()
                .and_then(|id| bip_seq.get(id).copied())
                .unwrap_or(0);
            let end_seq = range
                .end_bip
                .as_deref()
                .and_then(|id| bip_seq.get(id).copied())
                .unwrap_or(u32::MAX);

            for (bip_seq_n, _bip_id, track_ids) in bi_points {
                if *bip_seq_n < start_seq || *bip_seq_n > end_seq {
                    continue;
                }
                // The last BIP of the range has no onward section — skip its
                // track list (which will be empty anyway, but be explicit).
                if *bip_seq_n == end_seq {
                    continue;
                }
                for (_, tid) in track_ids {
                    all_track_ids.push(tid.clone());
                }
            }
        }

        // Resolve track IDs → RouteElements via the infrastructure.
        let elements: Vec<RouteElement> = all_track_ids
            .into_iter()
            .filter_map(|tid| {
                let track = match infra.tracks.get(&tid) {
                    Some(t) => t,
                    None => {
                        eprintln!(
                            "Warning: track '{tid}' in route for train '{ot_id}' not found in infrastructure — skipped"
                        );
                        return None;
                    }
                };
                let ne = match infra.net_elements.get(&track.net_element_id) {
                    Some(ne) => ne,
                    None => {
                        eprintln!(
                            "Warning: netElement '{}' (track '{tid}', train '{ot_id}') not found in infrastructure — skipped",
                            track.net_element_id
                        );
                        return None;
                    }
                };
                Some(RouteElement {
                    track_id: tid,
                    net_element_id: ne.id.clone(),
                    length_m: ne.length_m,
                })
            })
            .collect();

        if elements.is_empty() {
            eprintln!(
                "Warning: train '{ot_id}' resolved to an empty route — no Route created"
            );
            continue;
        }

        let route = Route::new(elements);
        println!(
            "Route for train '{ot_id}': {} elements, total {:.0} m",
            route.elements.len(),
            route.total_length_m,
        );
        routes.insert(ot_id, route);
    }

    Ok(routes)
}
