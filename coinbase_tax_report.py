#!/usr/bin/env python3
"""Krypto-Steuerreport (Deutschland) aus einem Coinbase Transaktions-CSV.

Berechnet Gewinne/Verluste aus Veräußerungsgeschäften nach dem
First-In-First-Out-Prinzip (§ 23 EStG) und stuft sie nach Haltedauer ein
(< 1 Jahr steuerpflichtig, > 1 Jahr steuerfrei). Zusätzlich werden Staking-
und Reward-Einkünfte (§ 22 EStG, Marktwert bei Zufluss) ausgewiesen.

Aufbau analog zum Trade-Republic "Crypto Jahresauszug".

Nur Standardbibliothek – keine externen Abhängigkeiten.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, getcontext
from pathlib import Path

getcontext().prec = 40

# --- Transaktionstypen ------------------------------------------------------
# Zugänge (bilden Anschaffungs-"Lots" für FIFO)
ACQUISITION_TYPES = {
    "Buy",
    "Convert",  # positive Menge = erhaltenes Asset
    "Staking Income",
    "Incentives Rewards Payout",
    "Retail Simple Price Improvement",
}
# Abgänge (Veräußerungen)
DISPOSAL_TYPES = {
    "Sell",
    "Convert",  # negative Menge = abgegebenes Asset
    "Retail Simple Dust",
}
# Reine Einkünfte (Marktwert bei Zufluss zählt als sonstige Einkünfte)
INCOME_TYPES = {
    "Staking Income",
    "Incentives Rewards Payout",
    "Retail Simple Price Improvement",
}
# Interne / Fiat-Buchungen ohne steuerliche Wirkung -> ignorieren
IGNORE_TYPES = {
    "Deposit",
    "Withdrawal",
    "Retail Staking Transfer",
    "Retail Unstaking Transfer",
}

# Fiat-Assets, die keine Krypto-Veräußerung darstellen
FIAT_ASSETS = {"EUR"}


def parse_money(raw: str) -> Decimal:
    """Wandelt '€1.234,56'-artige Coinbase-Geldfelder in Decimal um.

    Coinbase nutzt im Export Punkt als Dezimaltrennzeichen und keine
    Tausendertrennzeichen, schreibt aber ein €-Symbol davor (auch '-€0.00').
    """
    if raw is None:
        return Decimal(0)
    s = raw.strip().replace("\u20ac", "").replace("€", "").replace(",", "").strip()
    if s in ("", "-"):
        return Decimal(0)
    return Decimal(s)


@dataclass
class Lot:
    """Ein Anschaffungs-Posten für die FIFO-Verrechnung."""

    qty: Decimal
    cost_per_unit: Decimal
    acquired: datetime


@dataclass
class Disposal:
    """Ein realisiertes Veräußerungsgeschäft (eine FIFO-Teilabrechnung)."""

    asset: str
    disposed_on: datetime
    acquired_on: datetime
    qty: Decimal
    proceeds: Decimal
    cost: Decimal
    fee: Decimal
    price_per_unit: Decimal
    tx_type: str

    @property
    def gain(self) -> Decimal:
        return self.proceeds - self.cost

    @property
    def short_term(self) -> bool:
        """True, wenn Haltedauer <= 1 Jahr (steuerpflichtig nach § 23 EStG)."""
        return not held_more_than_one_year(self.acquired_on, self.disposed_on)


@dataclass
class IncomeEntry:
    asset: str
    received_on: datetime
    qty: Decimal
    value_eur: Decimal
    tx_type: str


def held_more_than_one_year(acquired: datetime, disposed: datetime) -> bool:
    """Steuerfrei, wenn zwischen Anschaffung und Veräußerung > 1 Jahr liegt."""
    try:
        anniversary = acquired.replace(year=acquired.year + 1)
    except ValueError:  # 29. Februar
        anniversary = acquired.replace(year=acquired.year + 1, month=2, day=28)
    return disposed > anniversary


@dataclass
class Portfolio:
    """FIFO-Bestand je Asset plus Erfassung aller Veräußerungen/Einkünfte."""

    lots: dict[str, deque[Lot]] = field(default_factory=lambda: defaultdict(deque))
    disposals: list[Disposal] = field(default_factory=list)
    income: list[IncomeEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def acquire(self, asset: str, qty: Decimal, total_cost: Decimal, when: datetime) -> None:
        if qty <= 0:
            return
        self.lots[asset].append(Lot(qty=qty, cost_per_unit=total_cost / qty, acquired=when))

    def dispose(
        self,
        asset: str,
        qty: Decimal,
        proceeds: Decimal,
        fee: Decimal,
        price_per_unit: Decimal,
        when: datetime,
        tx_type: str,
    ) -> None:
        if qty <= 0:
            return
        remaining = qty
        lots = self.lots[asset]
        while remaining > 0:
            if not lots:
                # Kein Anschaffungs-Lot vorhanden (z.B. Bestand aus Vorjahr
                # oder externer Transfer). Mit Kostenbasis 0 abrechnen + Warnung.
                self.warnings.append(
                    f"{when:%Y-%m-%d} {asset}: {remaining} ohne Anschaffungs-Lot "
                    f"(Kostenbasis 0 angesetzt)."
                )
                lots.append(Lot(qty=remaining, cost_per_unit=Decimal(0), acquired=when))
            lot = lots[0]
            take = min(lot.qty, remaining)
            share = take / qty
            self.disposals.append(
                Disposal(
                    asset=asset,
                    disposed_on=when,
                    acquired_on=lot.acquired,
                    qty=take,
                    proceeds=proceeds * share,
                    cost=lot.cost_per_unit * take,
                    fee=fee * share,
                    price_per_unit=price_per_unit,
                    tx_type=tx_type,
                )
            )
            lot.qty -= take
            remaining -= take
            if lot.qty <= 0:
                lots.popleft()

    def add_income(self, asset: str, qty: Decimal, value_eur: Decimal, when: datetime, tx_type: str) -> None:
        self.income.append(IncomeEntry(asset=asset, received_on=when, qty=qty, value_eur=value_eur, tx_type=tx_type))


def read_transactions(csv_path: Path) -> list[dict[str, str]]:
    """Liest das Coinbase-CSV und überspringt die Kopfzeilen vor der Tabelle."""
    text = csv_path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    header_idx = next(
        (i for i, ln in enumerate(lines) if ln.startswith("ID,Timestamp,Transaction Type")),
        None,
    )
    if header_idx is None:
        raise SystemExit(f"Konnte Tabellenkopf in {csv_path} nicht finden.")
    reader = csv.DictReader(lines[header_idx:])
    return [row for row in reader if row.get("ID")]


def transaction_value_eur(row: dict[str, str]) -> Decimal:
    """EUR-Wert einer Buchung: bevorzugt 'Total', sonst Menge * Preis."""
    total = abs(parse_money(row.get("Total (inclusive of fees and/or spread)", "")))
    if total > 0:
        return total
    qty = abs(parse_money(row.get("Quantity Transacted", "")))
    price = parse_money(row.get("Price at Transaction", ""))
    return qty * price


def build_portfolio(rows: list[dict[str, str]]) -> Portfolio:
    """Verarbeitet alle Buchungen chronologisch (ältester Eintrag zuerst)."""
    def ts(row: dict[str, str]) -> datetime:
        return datetime.strptime(row["Timestamp"], "%Y-%m-%d %H:%M:%S %Z")

    rows = sorted(rows, key=ts)
    pf = Portfolio()

    for row in rows:
        tx_type = row["Transaction Type"].strip()
        asset = row["Asset"].strip()
        when = ts(row)
        qty = parse_money(row["Quantity Transacted"])
        price = parse_money(row.get("Price at Transaction", ""))
        fee = abs(parse_money(row.get("Fees and/or Spread", "")))
        value = transaction_value_eur(row)

        if tx_type in IGNORE_TYPES or asset in FIAT_ASSETS:
            continue

        # Reine Einkünfte separat erfassen (Marktwert bei Zufluss).
        if tx_type in INCOME_TYPES:
            pf.add_income(asset, abs(qty), value, when, tx_type)

        # Zu- bzw. Abgang fürs FIFO bestimmen sich über das Vorzeichen.
        if qty > 0 and tx_type in ACQUISITION_TYPES:
            pf.acquire(asset, qty, value, when)
        elif qty < 0 and tx_type in DISPOSAL_TYPES:
            pf.dispose(asset, abs(qty), value, fee, price, when, tx_type)

    return pf


# --- Ausgabe ----------------------------------------------------------------

def aggregate_disposals(disposals: list[Disposal]) -> list[dict]:
    """Fasst FIFO-Teilabrechnungen je Veräußerungs-Ereignis zusammen.

    Ein Verkauf wird beim FIFO oft auf viele Anschaffungs-Lots aufgeteilt.
    Für die Darstellung (analog Trade Republic) wird pro Ereignis (Asset +
    Zeitpunkt + Typ) eine Zeile gebildet; der Anteil mit Haltedauer < 1 Jahr
    wird separat ausgewiesen.
    """
    groups: dict[tuple, dict] = {}
    for d in disposals:
        key = (d.disposed_on, d.tx_type)
        g = groups.setdefault(
            key,
            {
                "date": d.disposed_on,
                "type": d.tx_type,
                "qty": Decimal(0),
                "proceeds": Decimal(0),
                "cost": Decimal(0),
                "fee": Decimal(0),
                "gain": Decimal(0),
                "gain_short": Decimal(0),
                "price": d.price_per_unit,
            },
        )
        g["qty"] += d.qty
        g["proceeds"] += d.proceeds
        g["cost"] += d.cost
        g["fee"] += d.fee
        g["gain"] += d.gain
        if d.short_term:
            g["gain_short"] += d.gain
    return sorted(groups.values(), key=lambda g: g["date"])


def filing_group(asset: str) -> str:
    """Gruppierung für die Steuererklärung: BTC, ETH, alles andere 'Divers'."""
    return asset if asset in ("BTC", "ETH") else "Divers"


def filing_summary(pf: Portfolio) -> list[dict]:
    """Je Position (ETH / BTC / Divers): G/V des Jahres, erstes
    Anschaffungsdatum der veräußerten Bestände und letzter Verkauf.

    Alle drei Positionen werden immer ausgewiesen, auch ohne Verkäufe
    (dann mit 0,00 und ohne Datumsangaben).
    """
    groups: dict[str, dict] = {
        name: {
            "name": name,
            "assets": set(),
            "gain": Decimal(0),
            "gain_short": Decimal(0),
            "proceeds": Decimal(0),
            "cost": Decimal(0),
            "first_acquired": None,
            "last_sold": None,
        }
        for name in ("ETH", "BTC", "Divers")
    }
    for d in pf.disposals:
        g = groups[filing_group(d.asset)]
        g["assets"].add(d.asset)
        g["gain"] += d.gain
        if d.short_term:
            g["gain_short"] += d.gain
        g["proceeds"] += d.proceeds
        g["cost"] += d.cost
        if g["first_acquired"] is None or d.acquired_on < g["first_acquired"]:
            g["first_acquired"] = d.acquired_on
        if g["last_sold"] is None or d.disposed_on > g["last_sold"]:
            g["last_sold"] = d.disposed_on
    return [groups["ETH"], groups["BTC"], groups["Divers"]]


def fmt(d: Decimal, places: str = "0.01") -> str:
    q = d.quantize(Decimal(places))
    s = f"{q:,.{len(places.split('.')[1])}f}"
    # deutsche Schreibweise: Tausenderpunkt, Dezimalkomma
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def render_report(pf: Portfolio, year: int) -> str:
    out: list[str] = []
    w = out.append

    w("=" * 78)
    w(f"  KRYPTO-STEUERREPORT {year}  (Coinbase)".center(78))
    w("=" * 78)
    w("")
    w("!! KI-generiert. Kein offizielles Steuerdokument, keine Steuerberatung.")
    w("!! Ohne Gewähr - bitte vor Abgabe von einer/einem Steuerberater:in prüfen.")
    w("")
    w("Gewinne/Verluste nach First-In-First-Out (§ 23 EStG, private")
    w("Veräußerungsgeschäfte). Haltedauer > 1 Jahr ist steuerfrei.")
    w("Anschaffungskosten = gezahlter Betrag inkl. Gebühren/Spread;")
    w("für Staking/Rewards der Marktwert bei Zufluss.")
    w("")

    # --- Zusammenfassung für die Steuererklärung -----------------------------
    summary = filing_summary(pf)
    w("=" * 78)
    w("  ZUSAMMENFASSUNG FÜR DIE STEUERERKLÄRUNG")
    w("=" * 78)
    w(
        f"{'POSITION':<8}{'ERSTE ANSCH.':>14}{'LETZTER VERK.':>15}"
        f"{'ERLÖS':>11}{'KOSTEN':>11}{'G/V':>10}{'G/V <1J':>10}"
    )
    for g in summary:
        fa = f"{g['first_acquired']:%d.%m.%Y}" if g["first_acquired"] else "-"
        ls = f"{g['last_sold']:%d.%m.%Y}" if g["last_sold"] else "-"
        w(
            f"{g['name']:<8}"
            + fa.rjust(14)
            + ls.rjust(15)
            + fmt(g["proceeds"]).rjust(11)
            + fmt(g["cost"]).rjust(11)
            + fmt(g["gain"]).rjust(10)
            + fmt(g["gain_short"]).rjust(10)
        )
        if g["name"] == "Divers" and g["assets"]:
            w(f"{'':8}enthält: {', '.join(sorted(g['assets']))}")
    total = sum((g["gain"] for g in summary), Decimal(0))
    total_short = sum((g["gain_short"] for g in summary), Decimal(0))
    w("-" * 79)
    w(
        f"{'GESAMT':<8}{'':>14}{'':>15}{'':>11}{'':>11}"
        + fmt(total).rjust(10) + fmt(total_short).rjust(10)
    )
    w("")
    w(f"Gesamtgewinn/-verlust aus Cryptogeschäften in {year} bei Coinbase: {fmt(total)} €")
    w(f"Davon mit einer Haltedauer von weniger als einem Jahr:            {fmt(total_short)} €")
    w("")
    w("Erste Ansch. = ältestes Anschaffungsdatum der in " f"{year}")
    w("veräußerten Bestände (FIFO). Letzter Verk. = letzte Veräußerung in "
      f"{year}.")
    w("")

    assets = sorted(
        {d.asset for d in pf.disposals},
        key=lambda a: ({"ETH": 0, "BTC": 1}.get(a, 2), a),
    )
    grand_gain = Decimal(0)
    grand_short = Decimal(0)

    for asset in assets:
        ds = [d for d in pf.disposals if d.asset == asset]
        events = aggregate_disposals(ds)
        a_gain = sum((e["gain"] for e in events), Decimal(0))
        a_short = sum((e["gain_short"] for e in events), Decimal(0))
        grand_gain += a_gain
        grand_short += a_short

        w("-" * 78)
        w(f"{asset}")
        w("-" * 78)
        w(
            f"{'DATUM':<11}{'MENGE':>16}{'PREIS/STK':>13}{'GEBÜHR':>9}"
            f"{'ERLÖS':>11}{'KOSTEN':>11}{'G/V':>11}{'G/V <1J':>11}"
        )
        for e in events:
            w(
                f"{e['date']:%d.%m.%Y} "
                f"{fmt(e['qty'], '0.00000001'):>15}"
                f"{fmt(e['price']):>13}"
                f"{fmt(e['fee']):>9}"
                f"{fmt(e['proceeds']):>11}"
                f"{fmt(e['cost']):>11}"
                f"{fmt(e['gain']):>11}"
                f"{fmt(e['gain_short']):>11}"
            )
        w("")
        w(f"  Summe {asset}: G/V {fmt(a_gain)} €  |  davon < 1 Jahr {fmt(a_short)} €")
        w("")

    # --- Staking / Rewards (Fußnote) -----------------------------------------
    if pf.income:
        total_income = sum((i.value_eur for i in pf.income), Decimal(0))
        w("-" * 78)
        w(f"Staking- und Reward-Einkünfte {year}: {fmt(total_income)} € (Marktwert bei")
        w("Zufluss; zugleich Anschaffungskosten der erhaltenen Coins).")
        if total_income < Decimal(256):
            w("Unter der Freigrenze von 256 € (§ 22 Nr. 3 EStG), damit nicht")
            w("einkommensteuerpflichtig.")
        else:
            w("Die Freigrenze von 256 € (§ 22 Nr. 3 EStG) ist überschritten - als")
            w("sonstige Einkünfte anzugeben.")
        w(f"Einzelaufstellung: staking_einkommen_{year}.csv")
        w("")

    if pf.warnings:
        w("=" * 78)
        w("  HINWEISE")
        w("=" * 78)
        for msg in pf.warnings:
            w(f"  ! {msg}")
        w("")

    w("Hinweis: Keine Steuerberatung. Bitte vor Abgabe prüfen.")
    return "\n".join(out)


def write_csv_outputs(pf: Portfolio, out_dir: Path, year: int) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    disp_path = out_dir / f"veraeusserungen_{year}.csv"
    with disp_path.open("w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f, delimiter=";")
        wr.writerow(
            [
                "Asset", "Veraeussert_am", "Angeschafft_am", "Menge",
                "Preis_pro_Stk_EUR", "Erloes_EUR", "Anschaffungskosten_EUR",
                "Gebuehr_EUR", "Gewinn_Verlust_EUR", "Haltedauer",
            ]
        )
        for d in sorted(pf.disposals, key=lambda x: (x.asset, x.disposed_on)):
            wr.writerow(
                [
                    d.asset,
                    f"{d.disposed_on:%Y-%m-%d}",
                    f"{d.acquired_on:%Y-%m-%d}",
                    f"{d.qty:.8f}",
                    f"{d.price_per_unit:.2f}",
                    f"{d.proceeds:.2f}",
                    f"{d.cost:.2f}",
                    f"{d.fee:.2f}",
                    f"{d.gain:.2f}",
                    "<1 Jahr" if d.short_term else ">1 Jahr",
                ]
            )

    inc_path = out_dir / f"staking_einkommen_{year}.csv"
    with inc_path.open("w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f, delimiter=";")
        wr.writerow(["Asset", "Zugang_am", "Menge", "Marktwert_EUR", "Typ"])
        for i in sorted(pf.income, key=lambda x: (x.asset, x.received_on)):
            wr.writerow([i.asset, f"{i.received_on:%Y-%m-%d}", f"{i.qty:.12f}", f"{i.value_eur:.6f}", i.tx_type])


def compute_totals(pf: Portfolio):
    """Liefert (assets, je-Asset-Events, Gesamt-G/V, Gesamt-G/V<1J)."""
    assets = sorted(
        {d.asset for d in pf.disposals},
        key=lambda a: ({"ETH": 0, "BTC": 1}.get(a, 2), a),
    )
    per_asset: dict[str, list[dict]] = {}
    grand_gain = Decimal(0)
    grand_short = Decimal(0)
    for asset in assets:
        events = aggregate_disposals([d for d in pf.disposals if d.asset == asset])
        per_asset[asset] = events
        grand_gain += sum((e["gain"] for e in events), Decimal(0))
        grand_short += sum((e["gain_short"] for e in events), Decimal(0))
    return assets, per_asset, grand_gain, grand_short


ASSET_NAMES = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "ADA": "Cardano", "AVAX": "Avalanche",
    "DOT": "Polkadot", "EURC": "Euro Coin", "HBAR": "Hedera", "LINK": "Chainlink",
    "LTC": "Litecoin", "SOL": "Solana", "SUI": "Sui", "SWFTC": "SwftCoin",
    "UNI": "Uniswap", "XLM": "Stellar", "XRP": "XRP", "DOGE": "Dogecoin",
    "MATIC": "Polygon", "SHIB": "Shiba Inu", "BCH": "Bitcoin Cash",
}

TX_LABELS = {"Sell": "VERKAUF", "Convert": "TAUSCH", "Retail Simple Dust": "VERKAUF"}


def fmt_qty(d: Decimal) -> str:
    """Stückzahl deutsch formatiert, ohne überflüssige Nachkomma-Nullen."""
    s = f"{d:,.8f}".rstrip("0").rstrip(".")
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def write_pdf(pf: Portfolio, out_path: Path, year: int) -> bool:
    """Erzeugt einen PDF-Report im Stil des Trade-Republic Crypto Jahresauszugs.

    Gibt False zurück, falls fpdf2 nicht installiert ist.
    """
    try:
        from fpdf import FPDF
    except ImportError:
        return False

    assets, per_asset, grand_gain, grand_short = compute_totals(pf)
    summary = filing_summary(pf)
    total_income = sum((i.value_eur for i in pf.income), Decimal(0))

    GREY = (120, 120, 120)
    RED = (176, 0, 32)

    class TRStylePDF(FPDF):
        def footer(self):
            self.set_y(-12)
            self.set_font("Helvetica", "", 7)
            self.set_text_color(*GREY)
            self.cell(0, 4, f"Seite {self.page_no()} von {{nb}}", align="R")

    pdf = TRStylePDF(orientation="P", unit="mm", format="A4")
    # cp1252 statt latin-1, damit das Euro-Zeichen verfügbar ist
    pdf.core_fonts_encoding = "windows-1252"
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.set_margins(12, 14, 12)
    pdf.add_page()

    content_w = pdf.w - pdf.l_margin - pdf.r_margin  # 186 mm

    def hline(weight: float = 0.2):
        pdf.set_line_width(weight)
        pdf.set_draw_color(0, 0, 0)
        y = pdf.get_y()
        pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)

    def eur(d: Decimal) -> str:
        return f"{fmt(d)} \u20ac"

    def table_header(cols: list[tuple[str, str, float, str]]):
        """Zweizeilige Spaltenköpfe im TR-Stil mit Linie darunter."""
        pdf.set_font("Helvetica", "B", 6.3)
        pdf.set_text_color(0, 0, 0)
        x = pdf.l_margin
        y = pdf.get_y()
        for line1, line2, wcol, align in cols:
            pdf.set_xy(x, y)
            pdf.cell(wcol, 3.1, line1, align=align)
            pdf.set_xy(x, y + 3.1)
            pdf.cell(wcol, 3.1, line2, align=align)
            x += wcol
        pdf.set_y(y + 7.2)
        hline(0.35)
        pdf.ln(1.6)

    # ============================ KOPF ============================
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(100, 5, "KRYPTO-STEUERREPORT", align="L")
    pdf.set_font("Helvetica", "", 8)
    pdf.cell(0, 5, f"DATUM {datetime.now():%d.%m.%Y}    STEUERJAHR {year}",
             align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*GREY)
    pdf.cell(0, 4, "Quelle: Coinbase Transaktions-Export", align="L",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(6)

    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 7, "CRYPTO JAHRESAUSZUG", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, f"zum 31.12.{year}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # ======================= EINLEITUNG =======================
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, "CRYPTO TRANSAKTIONEN", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    pdf.set_font("Helvetica", "", 8)
    for para in (
        f"Nachfolgend findest du eine Aufstellung über die Crypto-Transaktionen in deinem "
        f"Coinbase-Konto im Jahr {year}.",
        "Die Gewinne und Verluste wurden nach dem First-In-First-Out-Prinzip ermittelt. "
        "Veräußerungen mit einer Haltedauer von mehr als einem Jahr sind steuerfrei (§ 23 EStG).",
        "Als Anschaffungskosten für Staking-Rewards wurde der Marktwert zum Zeitpunkt der "
        "Einbuchung angesetzt. Krypto-zu-Krypto-Tausch (Convert) gilt als Veräußerung.",
        "Bitte überprüfe, ob die Aufstellung korrekt ist.",
    ):
        pdf.multi_cell(0, 3.8, para)
        pdf.ln(1)

    pdf.set_text_color(*RED)
    pdf.set_font("Helvetica", "B", 7.5)
    pdf.multi_cell(
        0, 3.6,
        "KI-generiert. Kein offizielles Steuerdokument und keine Steuerberatung. Ohne Gewähr - "
        "bitte vor Abgabe von einer/einem Steuerberater:in prüfen lassen.",
    )
    pdf.set_text_color(0, 0, 0)
    pdf.ln(5)

    # ======================= ZUSAMMENFASSUNG =======================
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, "ZUSAMMENFASSUNG", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)

    sum_cols = [
        ("", "POSITION", 24, "L"),
        ("ERSTE", "ANSCHAFFUNG", 30, "R"),
        ("LETZTER", "VERKAUF", 28, "R"),
        ("ERLÖS", "IN EUR", 26, "R"),
        ("KOSTEN", "IN EUR", 26, "R"),
        ("GEWINN/VERLUST", "IN EUR", 26, "R"),
        ("GEWINN/VERLUST", "<1 JAHR IN EUR", 26, "R"),
    ]
    table_header(sum_cols)

    pdf.set_font("Helvetica", "", 8)
    for g in summary:
        row = [
            g["name"],
            f"{g['first_acquired']:%d.%m.%Y}" if g["first_acquired"] else "-",
            f"{g['last_sold']:%d.%m.%Y}" if g["last_sold"] else "-",
            eur(g["proceeds"]),
            eur(g["cost"]),
            eur(g["gain"]),
            eur(g["gain_short"]),
        ]
        for val, (_, _, wcol, align) in zip(row, sum_cols):
            pdf.cell(wcol, 5.5, val, align=align)
        pdf.ln(5.5)
        if g["name"] == "Divers" and g["assets"]:
            pdf.set_font("Helvetica", "I", 6.5)
            pdf.set_text_color(*GREY)
            pdf.cell(sum_cols[0][2], 3.6, "")
            pdf.cell(0, 3.6, "enthält: " + ", ".join(sorted(g["assets"])),
                     align="L", new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Helvetica", "", 8)
    pdf.ln(0.5)
    hline(0.35)
    pdf.ln(1.2)
    pdf.set_font("Helvetica", "B", 8)
    pdf.cell(sum(c[2] for c in sum_cols[:-2]), 5.5, "", align="R")
    pdf.cell(sum_cols[-2][2], 5.5, eur(grand_gain), align="R")
    pdf.cell(sum_cols[-1][2], 5.5, eur(grand_short), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # TR-Stil: Gesamtzeilen
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(150, 5.5, f"Gesamtgewinn/-verlust aus Cryptogeschäften in {year} bei Coinbase:", align="L")
    pdf.cell(0, 5.5, eur(grand_gain), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.multi_cell(
        150, 4.4,
        "Davon Gewinn / Verlust aus Cryptogeschäften mit einer Haltedauer von "
        "weniger als einem Jahr",
        new_x="RIGHT", new_y="TOP",
    )
    pdf.cell(0, 4.4, eur(grand_short), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)

    # Staking-Hinweis (Fußnote)
    if pf.income:
        pdf.set_font("Helvetica", "", 7)
        pdf.set_text_color(*GREY)
        note = (
            f"Staking- und Reward-Einkünfte {year}: {eur(total_income)} (Marktwert bei Zufluss; "
            "zugleich Anschaffungskosten der erhaltenen Coins). "
        )
        if total_income < Decimal(256):
            note += (
                "Der Betrag liegt unter der Freigrenze von 256 \u20ac (§ 22 Nr. 3 EStG) "
                "und ist damit nicht einkommensteuerpflichtig."
            )
        else:
            note += (
                "Die Freigrenze von 256 \u20ac (§ 22 Nr. 3 EStG) ist überschritten - "
                "der Betrag ist als sonstige Einkünfte anzugeben."
            )
        note += f" Einzelaufstellung: staking_einkommen_{year}.csv."
        pdf.multi_cell(0, 3.4, note)
        pdf.set_text_color(0, 0, 0)

    # =================== AB SEITE 2: DETAILS JE ASSET ===================
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, "CRYPTO TRANSAKTIONEN IM DETAIL", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*GREY)
    pdf.cell(0, 4.5, "Alle Veräußerungen nach FIFO, zusammengefasst je Transaktion.",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)

    det_cols = [
        ("", "TRANSAKTION", 30, "L"),
        ("NOMINALE", "IN STK.", 26, "R"),
        ("PREIS PRO STÜCK", "IN EUR", 30, "R"),
        ("GEBÜHREN", "IN EUR", 22, "R"),
        ("GEBUCHT", "IN EUR", 26, "R"),
        ("GEWINN/VERLUST", "IN EUR", 26, "R"),
        ("GEWINN/VERLUST", "<1 JAHR IN EUR", 26, "R"),
    ]

    for asset in assets:
        events = per_asset[asset]
        a_gain = sum((e["gain"] for e in events), Decimal(0))
        a_short = sum((e["gain_short"] for e in events), Decimal(0))

        # Platz für Titel + Kopf + mind. eine Zeile + Summe
        if pdf.get_y() > pdf.h - 60:
            pdf.add_page()

        long_name = ASSET_NAMES.get(asset)
        title = f"{long_name.upper()} ({asset})" if long_name else asset
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.cell(0, 6, title, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(0.5)
        table_header(det_cols)

        for e in events:
            if pdf.get_y() > pdf.h - 30:
                pdf.add_page()
                table_header(det_cols)
            row = [
                f"{e['date']:%d.%m.%Y}",
                fmt_qty(e["qty"]),
                eur(e["price"]),
                eur(e["fee"]),
                eur(e["proceeds"]),
                eur(e["gain"]),
                eur(e["gain_short"]),
            ]
            pdf.set_font("Helvetica", "", 7.5)
            for val, (_, _, wcol, align) in zip(row, det_cols):
                pdf.cell(wcol, 3.8, val, align=align)
            pdf.ln(3.8)
            pdf.set_font("Helvetica", "", 6.5)
            pdf.cell(det_cols[0][2], 3.4, TX_LABELS.get(e["type"], e["type"].upper()),
                     align="L", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1.2)

        # Summe je Asset (TR-Stil: Beträge unter den G/V-Spalten)
        pdf.ln(0.3)
        hline(0.35)
        pdf.ln(1.2)
        pdf.set_font("Helvetica", "B", 8)
        pdf.cell(sum(c[2] for c in det_cols[:-2]), 5, "", align="R")
        pdf.cell(det_cols[-2][2], 5, eur(a_gain), align="R")
        pdf.cell(det_cols[-1][2], 5, eur(a_short), align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(6)

    pdf.output(str(out_path))
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Coinbase -> deutscher Krypto-Steuerreport (FIFO).")
    parser.add_argument("csv", nargs="?", default="input/2025.csv", help="Pfad zum Coinbase-CSV")
    parser.add_argument("--year", type=int, default=2025, help="Steuerjahr (für Beschriftung/Dateinamen)")
    parser.add_argument("--out", default="output", help="Ausgabeverzeichnis")
    args = parser.parse_args(argv)

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"Datei nicht gefunden: {csv_path}", file=sys.stderr)
        return 1

    rows = read_transactions(csv_path)
    pf = build_portfolio(rows)
    report = render_report(pf, args.year)
    print(report)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"Krypto_Steuerreport_{args.year}.txt").write_text(report + "\n", encoding="utf-8")
    write_csv_outputs(pf, out_dir, args.year)

    pdf_path = out_dir / f"Krypto_Steuerreport_{args.year}.pdf"
    if write_pdf(pf, pdf_path, args.year):
        print(f"PDF erstellt: {pdf_path}")
    else:
        print("Hinweis: fpdf2 nicht installiert - PDF uebersprungen. "
              "Installation: pip install fpdf2", file=sys.stderr)

    print(f"\nDateien geschrieben nach: {out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
