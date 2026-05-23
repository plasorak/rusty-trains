pub fn parse_attr<T: std::str::FromStr>(
    node: roxmltree::Node,
    attr_name: &str,
    ctx: &str,
) -> Result<T, String> {
    let raw = node
        .attribute(attr_name)
        .ok_or_else(|| format!("{ctx}: missing attribute '{attr_name}'"))?;
    raw.parse().map_err(|_| format!("{ctx}: invalid value for '{attr_name}': {raw:?}"))
}
