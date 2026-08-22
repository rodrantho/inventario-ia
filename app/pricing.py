from __future__ import annotations

import re
import statistics
import urllib.parse
from dataclasses import dataclass
from functools import lru_cache

import httpx
from bs4 import BeautifulSoup

MARKETS = {
    "AR": ("mercadolibre.com.ar", "usado", "precio"),
    "UY": ("mercadolibre.com.uy", "usado", "precio"),
    "CL": ("mercadolibre.cl", "usado", "precio"),
    "MX": ("mercadolibre.com.mx", "usado", "precio"),
    "BR": ("mercadolivre.com.br", "usado", "preço"),
    "DE": ("kleinanzeigen.de", "gebraucht", "Preis"),
    "ES": ("wallapop.com", "segunda mano", "precio"),
    "US": ("ebay.com", "used", "price"),
}

GERMAN_TERMS = {
    "silla": "Stuhl", "sillas": "Stühle", "mesa": "Tisch", "sofá": "Sofa", "sillon": "Sessel",
    "sillón": "Sessel", "armario": "Schrank", "ropero": "Kleiderschrank", "cama": "Bett",
    "espejo": "Spiegel", "cuadro": "Bild", "florero": "Vase", "lámpara": "Lampe",
    "estatua": "Statue", "escultura": "Skulptur", "alfombra": "Teppich", "mesa de centro": "Couchtisch",
    "mesa auxiliar": "Beistelltisch", "estantería": "Regal", "televisor": "Fernseher", "heladera": "Kühlschrank",
}

CURRENCY_PATTERNS = {
    "EUR": re.compile(r"(?:€\s*([0-9][0-9., ]*)|([0-9][0-9., ]*)\s*€|EUR\s*([0-9][0-9., ]*))", re.I),
    "USD": re.compile(r"(?:US\$|USD|\$)\s*([0-9][0-9., ]*)", re.I),
    "ARS": re.compile(r"(?:AR\$|ARS|\$)\s*([0-9][0-9., ]*)", re.I),
    "UYU": re.compile(r"(?:UY\$|UYU|\$)\s*([0-9][0-9., ]*)", re.I),
    "CLP": re.compile(r"(?:CL\$|CLP|\$)\s*([0-9][0-9., ]*)", re.I),
    "MXN": re.compile(r"(?:MX\$|MXN|\$)\s*([0-9][0-9., ]*)", re.I),
    "BRL": re.compile(r"R\$\s*([0-9][0-9., ]*)", re.I),
}


@dataclass(frozen=True)
class PriceEstimate:
    suggested: float | None
    low: float | None
    high: float | None
    source_url: str
    note: str
    confidence: str
    comparable_count: int


def _localized_term(name: str, country: str) -> str:
    if country.upper() != "DE":
        return name
    lowered = name.casefold()
    for spanish in sorted(GERMAN_TERMS, key=len, reverse=True):
        if spanish in lowered:
            return GERMAN_TERMS[spanish]
    return name


def _parse_number(raw: str, currency: str) -> float | None:
    value = re.sub(r"[^0-9.,]", "", raw)
    if not value:
        return None
    decimal_currencies = currency in {"EUR", "USD", "BRL"}
    if "," in value and "." in value:
        last_comma, last_dot = value.rfind(","), value.rfind(".")
        decimal = "," if last_comma > last_dot else "."
        other = "." if decimal == "," else ","
        value = value.replace(other, "").replace(decimal, ".")
    elif "," in value or "." in value:
        separator = "," if "," in value else "."
        tail = value.rsplit(separator, 1)[1]
        if decimal_currencies and len(tail) in (1, 2):
            value = value.replace(separator, ".")
        else:
            value = value.replace(separator, "")
    try:
        number = float(value)
    except ValueError:
        return None
    return number if 1 <= number <= 100_000_000 else None


def _result_url(href: str) -> str:
    parsed = urllib.parse.urlparse(href)
    query = urllib.parse.parse_qs(parsed.query)
    return query.get("uddg", [href])[0]


def _summarize(values: list[float], source_url: str, domain: str) -> PriceEstimate:
    values = sorted(set(values))
    if len(values) >= 4:
        quartiles = statistics.quantiles(values, n=4, method="inclusive")
        low, high = quartiles[0], quartiles[2]
        confidence = "media"
    else:
        low, high = min(values), max(values)
        confidence = "baja"
    suggested = statistics.median(values)
    note = f"Estimación automática sobre {len(values)} precios publicados en {domain}; revisá estado, marca y modelo antes de fijar el valor final."
    return PriceEstimate(round(suggested, 2), round(low, 2), round(high, 2), source_url, note, confidence, len(values))


def _kleinanzeigen_price(term: str, timeout: float) -> PriceEstimate | None:
    slug = urllib.parse.quote_plus(term).replace("+", "-")
    search_url = f"https://www.kleinanzeigen.de/s-{slug}/k0"
    response = httpx.get(search_url, headers={"User-Agent": "Mozilla/5.0 (compatible; InventarioIA/0.2)"},
                         timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    values: list[float] = []
    first_url = search_url
    for price in soup.select(".aditem-main--middle--price-shipping--price"):
        number_match = re.search(r"([0-9][0-9.,]*)\s*€", price.get_text(" ", strip=True))
        if not number_match:
            continue
        number = _parse_number(number_match.group(1), "EUR")
        if number is None:
            continue
        values.append(number)
        if first_url == search_url:
            item = price.find_parent(class_=re.compile(r"aditem"))
            anchor = item.select_one("a[href]") if item else None
            if anchor and anchor.get("href"):
                first_url = urllib.parse.urljoin("https://www.kleinanzeigen.de", anchor["href"])
        if len(values) >= 30:
            break
    return _summarize(values, first_url, "kleinanzeigen.de") if values else None


@lru_cache(maxsize=512)
def estimate_used_price(name: str, country: str, currency: str, timeout: float = 15.0) -> PriceEstimate:
    country = country.upper()
    currency = currency.upper()
    domain, qualifier, price_word = MARKETS.get(country, ("ebay.com", "used", "price"))
    term = _localized_term(name, country)
    query = f"site:{domain} {term} {qualifier} {price_word}"
    search_url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    fallback_url = "https://duckduckgo.com/?" + urllib.parse.urlencode({"q": query})
    if country == "DE" and currency == "EUR":
        try:
            direct = _kleinanzeigen_price(term, timeout)
            if direct:
                return direct
        except Exception:
            pass
    try:
        response = httpx.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (compatible; InventarioIA/0.2)"},
            timeout=timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
    except Exception:
        return PriceEstimate(None, None, None, fallback_url, "No se pudo consultar el mercado en este momento.", "sin datos", 0)

    pattern = CURRENCY_PATTERNS.get(currency)
    if not pattern:
        return PriceEstimate(None, None, None, fallback_url, f"Moneda {currency} aún no compatible con comparables automáticos.", "sin datos", 0)

    soup = BeautifulSoup(response.text, "html.parser")
    values: list[float] = []
    links: list[str] = []
    for result in soup.select(".result"):
        anchor = result.select_one(".result__a")
        snippet = result.select_one(".result__snippet")
        text = " ".join(filter(None, [anchor.get_text(" ", strip=True) if anchor else "", snippet.get_text(" ", strip=True) if snippet else ""]))
        found: list[float] = []
        for match in pattern.finditer(text):
            raw = next((group for group in match.groups() if group), match.group(0))
            number = _parse_number(raw, currency)
            if number is not None:
                found.append(number)
        if found:
            values.append(found[0])
            if anchor and anchor.get("href"):
                links.append(_result_url(anchor["href"]))

    if not values:
        return PriceEstimate(None, None, None, search_url, "No se encontraron precios comparables con moneda verificable.", "sin datos", 0)
    source_url = links[0] if links else search_url
    return _summarize(values, source_url, domain)
