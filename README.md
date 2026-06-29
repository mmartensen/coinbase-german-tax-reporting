# Coinbase → Krypto-Steuerreport (Deutschland)

Erzeugt aus einem Coinbase-Transaktions-CSV einen Jahres-Steuerreport für
private Krypto-Veräußerungsgeschäfte (§ 23 EStG) – aufgebaut analog zum
Trade-Republic „Crypto Jahresauszug".

> ## ⚠️ Disclaimer / Haftungsausschluss
>
> **English:** This tool and its code were **generated with the help of AI**.
> It is **not an official tax document** and **not tax advice**. The
> calculations may contain errors or rely on simplifying assumptions, and tax
> rules change over time. Nothing here is endorsed by or affiliated with
> Coinbase, Trade Republic, or any tax authority. Use entirely at your own
> risk and **verify the results with a qualified tax advisor (Steuerberater)**
> before filing anything. Provided "as is", without any warranty.
>
> **Deutsch:** Dieses Tool und der Code wurden **mithilfe von KI erstellt**.
> Es handelt sich **weder um ein offizielles Steuerdokument noch um eine
> Steuerberatung**. Die Berechnungen können Fehler enthalten oder auf
> vereinfachenden Annahmen beruhen; steuerliche Regeln ändern sich. Keine
> Verbindung zu bzw. Freigabe durch Coinbase, Trade Republic oder eine
> Finanzbehörde. Nutzung **auf eigenes Risiko** – bitte die Ergebnisse vor der
> Abgabe von einer **Steuerberaterin/einem Steuerberater prüfen** lassen.
> Bereitstellung „wie besehen", ohne jegliche Gewähr.

## Was das Skript macht

- **FIFO (First-In-First-Out):** Jede Veräußerung wird gegen die ältesten
  Anschaffungen verrechnet.
- **Haltedauer:** Gewinne/Verluste werden in `< 1 Jahr` (steuerpflichtig) und
  `> 1 Jahr` (steuerfrei) aufgeteilt.
- **Anschaffungskosten:** gezahlter Betrag inkl. Gebühren/Spread; für
  Staking-/Reward-Zugänge der Marktwert zum Zeitpunkt des Zuflusses.
- **Staking & Rewards:** werden zusätzlich als sonstige Einkünfte (§ 22 EStG,
  Marktwert bei Zufluss) ausgewiesen.

### Behandlung der Coinbase-Transaktionstypen

| Typ | Behandlung |
|---|---|
| `Buy`, `Convert` (Zugang) | Anschaffung (Kostenbasis) |
| `Staking Income`, `Incentives Rewards Payout`, `Retail Simple Price Improvement` | Anschaffung **und** sonstige Einkünfte (Marktwert) |
| `Sell`, `Convert` (Abgang), `Retail Simple Dust` | Veräußerung |
| `Deposit`, `Withdrawal`, `Retail Staking/Unstaking Transfer` | ignoriert (Fiat bzw. interne Umbuchung) |

## Nutzung

```bash
python3 coinbase_tax_report.py input/2025.csv --year 2025 --out output
```

Alle Argumente sind optional (Standard: `input/2025.csv`, Jahr `2025`,
Ausgabe nach `output/`). Python ≥ 3.10.

Für die **PDF-Ausgabe** wird `fpdf2` benötigt:

```bash
pip install -r requirements.txt   # bzw. pip install fpdf2
```

Ohne `fpdf2` läuft das Skript trotzdem durch und erzeugt nur die `.txt`-/
`.csv`-Dateien (die PDF-Erstellung wird mit Hinweis übersprungen).

## Ausgabe

- `output/Krypto_Steuerreport_<jahr>.pdf` – formatierter Report (Layout analog
  Trade Republic, Tabellen je Asset, Gesamtsumme, Staking-Einkünfte).
- `output/Krypto_Steuerreport_<jahr>.txt` – derselbe Report als Text.
- `output/veraeusserungen_<jahr>.csv` – alle Veräußerungen (FIFO-Detail).
- `output/staking_einkommen_<jahr>.csv` – alle Staking-/Reward-Zugänge.

## Hinweise / Annahmen

- Das CSV muss die **vollständige Historie** enthalten, damit FIFO korrekt
  rechnet. Fehlt zu einer Veräußerung ein Anschaffungs-Lot (z. B. Bestand aus
  einem Vorjahr), wird die Kostenbasis mit 0 angesetzt und ein Hinweis im
  Report ausgegeben.
- Stablecoins (z. B. EURC) werden wie normale Krypto-Assets behandelt; ihre
  Gewinne/Verluste sind faktisch ~0.
- **Keine Steuerberatung.** Bitte vor Abgabe prüfen (siehe Disclaimer oben).

## Datenschutz

Persönliche Daten (Coinbase-Export, Trade-Republic-Auszug, generierte Reports)
werden über `.gitignore` ausgeschlossen und **nicht** ins Repository committet.
