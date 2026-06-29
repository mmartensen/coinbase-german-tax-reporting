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

    assets = sorted({d.asset for d in pf.disposals})
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

    w("=" * 78)
    w("  GESAMT")
    w("=" * 78)
    w(f"  Gesamtgewinn/-verlust aus Kryptogeschäften {year}: {fmt(grand_gain)} €")
    w(f"  davon Haltedauer < 1 Jahr (steuerpflichtig § 23): {fmt(grand_short)} €")
    w(f"  davon Haltedauer > 1 Jahr (steuerfrei):           {fmt(grand_gain - grand_short)} €")
    w("")

    # --- Staking / Rewards (sonstige Einkünfte) -----------------------------
    if pf.income:
        w("=" * 78)
        w("  STAKING- & REWARD-EINKÜNFTE (Marktwert bei Zufluss, § 22 EStG)")
        w("=" * 78)
        by_type: dict[str, Decimal] = defaultdict(Decimal)
        by_asset: dict[str, Decimal] = defaultdict(Decimal)
        for inc in pf.income:
            by_type[inc.tx_type] += inc.value_eur
            by_asset[inc.asset] += inc.value_eur
        for t in sorted(by_type):
            w(f"  {t:<35} {fmt(by_type[t]):>12} €")
        w("  " + "-" * 50)
        for a in sorted(by_asset):
            w(f"  davon {a:<29} {fmt(by_asset[a]):>12} €")
        total_income = sum(by_type.values(), Decimal(0))
        w("  " + "-" * 50)
        w(f"  {'Summe Einkünfte':<35} {fmt(total_income):>12} €")
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
    assets = sorted({d.asset for d in pf.disposals})
    per_asset: dict[str, list[dict]] = {}
    grand_gain = Decimal(0)
    grand_short = Decimal(0)
    for asset in assets:
        events = aggregate_disposals([d for d in pf.disposals if d.asset == asset])
        per_asset[asset] = events
        grand_gain += sum((e["gain"] for e in events), Decimal(0))
        grand_short += sum((e["gain_short"] for e in events), Decimal(0))
    return assets, per_asset, grand_gain, grand_short


def write_pdf(pf: Portfolio, out_path: Path, year: int) -> bool:
    """Erzeugt einen PDF-Report analog zum Trade-Republic Crypto Jahresauszug.

    Gibt False zurück, falls fpdf2 nicht installiert ist.
    """
    try:
        from fpdf import FPDF
    except ImportError:
        return False

    assets, per_asset, grand_gain, grand_short = compute_totals(pf)

    # Spaltenbreiten (mm), Summe ~190 (A4 hoch, Rand 10)
    cols = [
        ("DATUM", 22, "L"),
        ("MENGE", 30, "R"),
        ("PREIS/STK", 24, "R"),
        ("GEBÜHR", 20, "R"),
        ("ERLÖS", 24, "R"),
        ("KOSTEN", 24, "R"),
        ("G/V", 23, "R"),
        ("G/V <1J", 23, "R"),
    ]

    NAVY = (15, 32, 64)
    GREY = (235, 237, 241)
    RED = (176, 0, 32)
    GREEN = (0, 110, 60)

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.set_margins(10, 12, 10)
    pdf.add_page()

    def gv_color(val: Decimal):
        pdf.set_text_color(*(RED if val < 0 else GREEN if val > 0 else (0, 0, 0)))

    def header_row():
        pdf.set_font("Helvetica", "B", 7.5)
        pdf.set_fill_color(*NAVY)
        pdf.set_text_color(255, 255, 255)
        for title, wcol, _ in cols:
            pdf.cell(wcol, 6, title, border=0, align="C", fill=True)
        pdf.ln(6)
        pdf.set_text_color(0, 0, 0)

    # --- Titel ---
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(*NAVY)
    pdf.cell(0, 9, f"Krypto-Steuerreport {year}", align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(0, 5, "Coinbase - private Veraeusserungsgeschaefte (FIFO)", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*RED)
    pdf.set_font("Helvetica", "B", 8)
    pdf.multi_cell(
        0, 4,
        "KI-generiert. Kein offizielles Steuerdokument und keine Steuerberatung. "
        "Ohne Gewaehr - bitte vor Abgabe von einer/einem Steuerberater:in pruefen lassen.",
    )
    pdf.ln(1)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 8)
    intro = (
        "Gewinne/Verluste nach First-In-First-Out (Paragraf 23 EStG). Haltedauer ueber 1 Jahr "
        "ist steuerfrei. Anschaffungskosten = gezahlter Betrag inkl. Gebuehren/Spread; fuer "
        "Staking/Rewards der Marktwert bei Zufluss."
    )
    pdf.multi_cell(0, 4, intro)
    pdf.ln(2)

    # --- Asset-Tabellen ---
    for asset in assets:
        events = per_asset[asset]
        a_gain = sum((e["gain"] for e in events), Decimal(0))
        a_short = sum((e["gain_short"] for e in events), Decimal(0))

        if pdf.get_y() > 250:
            pdf.add_page()

        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(*NAVY)
        pdf.cell(0, 6, asset, new_x="LMARGIN", new_y="NEXT")
        header_row()

        pdf.set_font("Helvetica", "", 7.5)
        fill = False
        for e in events:
            row = [
                f"{e['date']:%d.%m.%Y}",
                fmt(e["qty"], "0.00000001"),
                fmt(e["price"]),
                fmt(e["fee"]),
                fmt(e["proceeds"]),
                fmt(e["cost"]),
                fmt(e["gain"]),
                fmt(e["gain_short"]),
            ]
            pdf.set_fill_color(*GREY)
            x0, y0 = pdf.get_x(), pdf.get_y()
            for i, (val, (_, wcol, align)) in enumerate(zip(row, cols)):
                if i == 6:
                    gv_color(e["gain"])
                elif i == 7:
                    gv_color(e["gain_short"])
                else:
                    pdf.set_text_color(0, 0, 0)
                pdf.cell(wcol, 5, val, border=0, align=align, fill=fill)
            pdf.set_text_color(0, 0, 0)
            pdf.ln(5)
            fill = not fill

        # Asset-Summe
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(0, 0, 0)
        label_w = cols[0][1] + cols[1][1] + cols[2][1] + cols[3][1] + cols[4][1] + cols[5][1]
        pdf.cell(label_w, 5.5, f"Summe {asset}", border="T", align="R")
        gv_color(a_gain)
        pdf.cell(cols[6][1], 5.5, fmt(a_gain), border="T", align="R")
        gv_color(a_short)
        pdf.cell(cols[7][1], 5.5, fmt(a_short), border="T", align="R")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(8)

    # --- Gesamtsumme ---
    if pdf.get_y() > 245:
        pdf.add_page()
    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 7, "  GESAMT", fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 9)

    def total_line(label: str, value: Decimal, bold: bool = False):
        pdf.set_font("Helvetica", "B" if bold else "", 9)
        pdf.set_text_color(0, 0, 0)
        pdf.cell(140, 6, label, align="L")
        gv_color(value)
        pdf.cell(0, 6, f"{fmt(value)} EUR", align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

    total_line(f"Gesamtgewinn/-verlust aus Kryptogeschaeften {year}", grand_gain, bold=True)
    total_line("davon Haltedauer < 1 Jahr (steuerpflichtig Paragraf 23)", grand_short)
    total_line("davon Haltedauer > 1 Jahr (steuerfrei)", grand_gain - grand_short)
    pdf.ln(4)

    # --- Staking / Rewards ---
    if pf.income:
        by_type: dict[str, Decimal] = defaultdict(Decimal)
        by_asset: dict[str, Decimal] = defaultdict(Decimal)
        for inc in pf.income:
            by_type[inc.tx_type] += inc.value_eur
            by_asset[inc.asset] += inc.value_eur

        if pdf.get_y() > 230:
            pdf.add_page()
        pdf.set_fill_color(*NAVY)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 7, "  STAKING- & REWARD-EINKUENFTE (Marktwert bei Zufluss, Paragraf 22 EStG)",
                 fill=True, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "", 9)
        for t in sorted(by_type):
            pdf.cell(140, 5.5, t, align="L")
            pdf.cell(0, 5.5, f"{fmt(by_type[t])} EUR", align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(90, 90, 90)
        for a in sorted(by_asset):
            pdf.cell(140, 5, f"   davon {a}", align="L")
            pdf.cell(0, 5, f"{fmt(by_asset[a])} EUR", align="R", new_x="LMARGIN", new_y="NEXT")
        total_income = sum(by_type.values(), Decimal(0))
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(0, 0, 0)
        pdf.cell(140, 6, "Summe Einkuenfte", border="T", align="L")
        pdf.cell(0, 6, f"{fmt(total_income)} EUR", border="T", align="R", new_x="LMARGIN", new_y="NEXT")

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
