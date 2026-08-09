//! Checked base-10 arithmetic for exchange filters and order boundaries.
//!
//! Binance publishes tick, lot, price and notional filters as decimal strings.
//! Converting those strings to binary floating point before validation can move
//! an order across a filter boundary. `ExactDecimal` retains the base-10 value
//! and provides only the small, checked operation set needed at the REST order
//! boundary. Arithmetic overflow is reported to the caller so live paths can
//! fail closed.

use std::cmp::Ordering;
use std::fmt;
use std::str::FromStr;

const MAX_PARSED_SCALE: u32 = 28;

#[derive(Clone, Copy, Eq, Hash, PartialEq)]
pub struct ExactDecimal {
    units: i128,
    scale: u32,
}

impl ExactDecimal {
    pub const ZERO: Self = Self { units: 0, scale: 0 };
    pub const MAX: Self = Self {
        units: i128::MAX,
        scale: 0,
    };

    pub const fn from_integer(units: i128) -> Self {
        Self { units, scale: 0 }
    }

    pub fn from_f64(value: f64) -> Option<Self> {
        if !value.is_finite() {
            return None;
        }
        value.to_string().parse().ok()
    }

    pub const fn is_positive(self) -> bool {
        self.units > 0
    }

    pub const fn scale(self) -> u32 {
        self.scale
    }

    pub fn to_f64(self) -> Option<f64> {
        self.to_string()
            .parse::<f64>()
            .ok()
            .filter(|v| v.is_finite())
    }

    pub fn checked_add(self, other: Self) -> Option<Self> {
        let (left, right, scale) = self.aligned_units(other)?;
        Some(Self::normalized(left.checked_add(right)?, scale))
    }

    pub fn checked_sub(self, other: Self) -> Option<Self> {
        let (left, right, scale) = self.aligned_units(other)?;
        Some(Self::normalized(left.checked_sub(right)?, scale))
    }

    pub fn checked_mul(self, other: Self) -> Option<Self> {
        let units = self.units.checked_mul(other.units)?;
        let scale = self.scale.checked_add(other.scale)?;
        Some(Self::normalized(units, scale))
    }

    /// Smallest positive decimal increment that is an integer multiple of
    /// both inputs. This is the exact intersection of two exchange grids.
    pub fn checked_common_increment(self, other: Self) -> Option<Self> {
        if self.units <= 0 || other.units <= 0 {
            return None;
        }
        let (left, right, scale) = self.aligned_units(other)?;
        let divisor = gcd(left, right);
        let units = left.checked_div(divisor)?.checked_mul(right)?;
        Some(Self::normalized(units, scale))
    }

    pub fn floor_to_increment(self, increment: Self) -> Option<Self> {
        self.quantize_to_increment(increment, false)
    }

    pub fn ceil_to_increment(self, increment: Self) -> Option<Self> {
        self.quantize_to_increment(increment, true)
    }

    pub fn format_to_scale(self, scale: u32) -> Option<String> {
        if scale < self.scale {
            return None;
        }
        let extra = scale - self.scale;
        let units = self.units.checked_mul(pow10(extra)?)?;
        Some(render_units(units, scale))
    }

    fn quantize_to_increment(self, increment: Self, ceil: bool) -> Option<Self> {
        if self.units < 0 || increment.units <= 0 {
            return None;
        }
        let (value_units, increment_units, scale) = self.aligned_units(increment)?;
        let quotient = value_units / increment_units;
        let remainder = value_units % increment_units;
        let lots = if ceil && remainder != 0 {
            quotient.checked_add(1)?
        } else {
            quotient
        };
        Some(Self::normalized(lots.checked_mul(increment_units)?, scale))
    }

    fn aligned_units(self, other: Self) -> Option<(i128, i128, u32)> {
        let scale = self.scale.max(other.scale);
        let left = self
            .units
            .checked_mul(pow10(scale.checked_sub(self.scale)?)?)?;
        let right = other
            .units
            .checked_mul(pow10(scale.checked_sub(other.scale)?)?)?;
        Some((left, right, scale))
    }

    fn normalized(mut units: i128, mut scale: u32) -> Self {
        while scale > 0 && units % 10 == 0 {
            units /= 10;
            scale -= 1;
        }
        Self { units, scale }
    }

    fn absolute_significand(self) -> String {
        self.units.unsigned_abs().to_string()
    }

    fn magnitude_exponent(self) -> i64 {
        self.absolute_significand().len() as i64 - i64::from(self.scale)
    }
}

fn pow10(power: u32) -> Option<i128> {
    10_i128.checked_pow(power)
}

fn gcd(mut left: i128, mut right: i128) -> i128 {
    while right != 0 {
        let remainder = left % right;
        left = right;
        right = remainder;
    }
    left.abs()
}

fn render_units(units: i128, scale: u32) -> String {
    let negative = units < 0;
    let digits = units.unsigned_abs().to_string();
    let rendered = if scale == 0 {
        digits
    } else if digits.len() > scale as usize {
        let split = digits.len() - scale as usize;
        format!("{}.{}", &digits[..split], &digits[split..])
    } else {
        format!("0.{}{}", "0".repeat(scale as usize - digits.len()), digits)
    };
    if negative && units != 0 {
        format!("-{rendered}")
    } else {
        rendered
    }
}

impl FromStr for ExactDecimal {
    type Err = &'static str;

    fn from_str(raw: &str) -> Result<Self, Self::Err> {
        let raw = raw.trim();
        if raw.is_empty() {
            return Err("empty decimal");
        }

        let (mantissa, exponent) = match raw.find(['e', 'E']) {
            Some(index) => {
                let exponent = raw[index + 1..]
                    .parse::<i32>()
                    .map_err(|_| "invalid exponent")?;
                (&raw[..index], exponent)
            }
            None => (raw, 0),
        };
        let (negative, unsigned) = match mantissa.as_bytes().first() {
            Some(b'-') => (true, &mantissa[1..]),
            Some(b'+') => (false, &mantissa[1..]),
            _ => (false, mantissa),
        };
        if unsigned.is_empty() {
            return Err("missing decimal digits");
        }

        let mut digits = String::with_capacity(unsigned.len());
        let mut fractional_digits = 0_u32;
        let mut saw_dot = false;
        let mut saw_digit = false;
        for byte in unsigned.bytes() {
            match byte {
                b'0'..=b'9' => {
                    saw_digit = true;
                    digits.push(char::from(byte));
                    if saw_dot {
                        fractional_digits = fractional_digits
                            .checked_add(1)
                            .ok_or("decimal scale overflow")?;
                    }
                }
                b'.' if !saw_dot => saw_dot = true,
                _ => return Err("invalid decimal character"),
            }
        }
        if !saw_digit {
            return Err("missing decimal digits");
        }

        let mut units = digits
            .parse::<i128>()
            .map_err(|_| "decimal significand overflow")?;
        if negative {
            units = units.checked_neg().ok_or("decimal significand overflow")?;
        }

        let target_scale = i64::from(fractional_digits) - i64::from(exponent);
        let scale = if target_scale < 0 {
            let shift = u32::try_from(-target_scale).map_err(|_| "decimal exponent overflow")?;
            units = units
                .checked_mul(pow10(shift).ok_or("decimal exponent overflow")?)
                .ok_or("decimal significand overflow")?;
            0
        } else {
            let scale = u32::try_from(target_scale).map_err(|_| "decimal scale overflow")?;
            if scale > MAX_PARSED_SCALE {
                return Err("decimal scale exceeds supported exchange precision");
            }
            scale
        };
        Ok(Self::normalized(units, scale))
    }
}

impl Ord for ExactDecimal {
    fn cmp(&self, other: &Self) -> Ordering {
        if self.units.signum() != other.units.signum() {
            return self.units.signum().cmp(&other.units.signum());
        }
        if self.units == 0 {
            return Ordering::Equal;
        }

        let sign = self.units.signum();
        let exponent_cmp = self.magnitude_exponent().cmp(&other.magnitude_exponent());
        if exponent_cmp != Ordering::Equal {
            return if sign > 0 {
                exponent_cmp
            } else {
                exponent_cmp.reverse()
            };
        }

        let left = self.absolute_significand();
        let right = other.absolute_significand();
        let width = left.len().max(right.len());
        let digit_cmp = left
            .bytes()
            .chain(std::iter::repeat(b'0'))
            .zip(right.bytes().chain(std::iter::repeat(b'0')))
            .take(width)
            .find_map(|(left, right)| (left != right).then(|| left.cmp(&right)))
            .unwrap_or(Ordering::Equal);
        if sign > 0 {
            digit_cmp
        } else {
            digit_cmp.reverse()
        }
    }
}

impl PartialOrd for ExactDecimal {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl fmt::Display for ExactDecimal {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&render_units(self.units, self.scale))
    }
}

impl fmt::Debug for ExactDecimal {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "ExactDecimal({self})")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_plain_and_exponent_forms_without_binary_rounding() {
        assert_eq!(
            "0.01000000".parse::<ExactDecimal>().unwrap().to_string(),
            "0.01"
        );
        assert_eq!(
            "1e-8".parse::<ExactDecimal>().unwrap().to_string(),
            "0.00000001"
        );
        assert_eq!(
            "1.23E3".parse::<ExactDecimal>().unwrap().to_string(),
            "1230"
        );
        assert!("NaN".parse::<ExactDecimal>().is_err());
        assert!("1e-100".parse::<ExactDecimal>().is_err());
    }

    #[test]
    fn compares_different_scales_exactly_without_cross_multiplication() {
        let boundary = "10000000000000000.1".parse::<ExactDecimal>().unwrap();
        let below = "10000000000000000.099999999999"
            .parse::<ExactDecimal>()
            .unwrap();
        assert!(below < boundary);
        assert_eq!(
            "0.10".parse::<ExactDecimal>().unwrap(),
            "0.1".parse().unwrap()
        );
    }

    #[test]
    fn floors_and_ceils_hostile_decimal_boundaries() {
        let tick = "0.000001".parse::<ExactDecimal>().unwrap();
        let below = "1.2345609999999999".parse::<ExactDecimal>().unwrap();
        assert_eq!(
            below.floor_to_increment(tick).unwrap().to_string(),
            "1.23456"
        );
        assert_eq!(
            below.ceil_to_increment(tick).unwrap().to_string(),
            "1.234561"
        );
        let exact = "1.234561".parse::<ExactDecimal>().unwrap();
        assert_eq!(exact.floor_to_increment(tick).unwrap(), exact);
        assert_eq!(exact.ceil_to_increment(tick).unwrap(), exact);
    }

    #[test]
    fn multiplication_and_formatting_remain_base_ten_exact() {
        let quantity = "0.0003".parse::<ExactDecimal>().unwrap();
        let price = "16666.666666666667".parse::<ExactDecimal>().unwrap();
        let notional = quantity.checked_mul(price).unwrap();
        assert_eq!(notional.to_string(), "5.0000000000000001");
        assert_eq!(
            "1.2"
                .parse::<ExactDecimal>()
                .unwrap()
                .format_to_scale(4)
                .unwrap(),
            "1.2000"
        );
    }

    #[test]
    fn common_increment_intersects_non_nested_lot_grids() {
        let lot = "0.003".parse::<ExactDecimal>().unwrap();
        let market = "0.002".parse::<ExactDecimal>().unwrap();
        assert_eq!(
            lot.checked_common_increment(market).unwrap().to_string(),
            "0.006"
        );
    }

    #[test]
    fn tiny_tick_addition_is_not_lost_at_large_prices() {
        let price = "10000000000000000".parse::<ExactDecimal>().unwrap();
        let tick = "0.00000001".parse::<ExactDecimal>().unwrap();
        assert_eq!(
            price.checked_add(tick).unwrap().to_string(),
            "10000000000000000.00000001"
        );
    }
}
