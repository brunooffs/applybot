"""ISO country code -> the English name eligibility.py understands.

Some boards give a place in the company's own language ("Nederland"); adding the English name next to it
lets the plain-code location check recognise the country."""
from __future__ import annotations

ISO = {
    "AT": "Austria", "BE": "Belgium", "BG": "Bulgaria", "HR": "Croatia", "CY": "Cyprus", "CZ": "Czechia",
    "DK": "Denmark", "EE": "Estonia", "FI": "Finland", "FR": "France", "DE": "Germany", "GR": "Greece",
    "HU": "Hungary", "IE": "Ireland", "IT": "Italy", "LV": "Latvia", "LT": "Lithuania", "LU": "Luxembourg",
    "MT": "Malta", "NL": "Netherlands", "PL": "Poland", "PT": "Portugal", "RO": "Romania", "SK": "Slovakia",
    "SI": "Slovenia", "ES": "Spain", "SE": "Sweden", "NO": "Norway", "IS": "Iceland", "LI": "Liechtenstein",
    "GB": "United Kingdom", "CH": "Switzerland", "RS": "Serbia", "UA": "Ukraine", "AL": "Albania",
    "BA": "Bosnia", "MD": "Moldova", "ME": "Montenegro", "MK": "North Macedonia", "TR": "Turkey",
    "GE": "Georgia", "AM": "Armenia", "BY": "Belarus", "XK": "Kosovo",
    "US": "United States", "CA": "Canada", "MX": "Mexico", "BR": "Brazil", "AR": "Argentina", "CO": "Colombia",
    "CL": "Chile", "PE": "Peru", "CR": "Costa Rica",
    "IN": "India", "CN": "China", "JP": "Japan", "SG": "Singapore", "AU": "Australia", "NZ": "New Zealand",
    "PH": "Philippines", "VN": "Vietnam", "ID": "Indonesia", "KR": "South Korea", "MY": "Malaysia",
    "TH": "Thailand", "PK": "Pakistan", "BD": "Bangladesh", "TW": "Taiwan", "HK": "Hong Kong",
    "IL": "Israel", "AE": "United Arab Emirates", "SA": "Saudi Arabia", "EG": "Egypt", "ZA": "South Africa",
    "NG": "Nigeria", "KE": "Kenya", "MA": "Morocco", "QA": "Qatar",
}


def with_country(location: str, code: str | None) -> str:
    """'Amsterdam, Noord-Holland, Nederland' + 'NL' -> '... , Netherlands' (unless already there)."""
    name = ISO.get((code or "").upper())
    if name and name.lower() not in (location or "").lower():
        return f"{location}, {name}" if location else name
    return location or ""
