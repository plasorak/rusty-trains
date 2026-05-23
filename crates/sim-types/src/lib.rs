pub use geo::Point;
pub use uom::si::f64::Length;
pub use uom::si::length::meter;

/// Length quantity in SI units (meters).
pub type Metres = Length;

/// Geographic position as (longitude, latitude) in decimal degrees.
pub type GeoPoint = Point<f64>;

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct NetElementId(pub String);

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct TrackId(pub String);

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct OpPointId(pub String);

impl From<&str> for NetElementId {
    fn from(s: &str) -> Self { Self(s.to_string()) }
}
impl From<&str> for TrackId {
    fn from(s: &str) -> Self { Self(s.to_string()) }
}
impl From<&str> for OpPointId {
    fn from(s: &str) -> Self { Self(s.to_string()) }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn id_equality() {
        let a = NetElementId::from("ne_1");
        let b = NetElementId("ne_1".to_string());
        assert_eq!(a, b);
    }

    #[test]
    fn meters_arithmetic() {
        let a = Metres::new::<meter>(100.0);
        let b = Metres::new::<meter>(50.0);
        assert!((a + b).get::<meter>() - 150.0 < 1e-9);
    }
}
