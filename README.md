# kitupiikki-mcp

**An MCP server (agent interface) for Kitsas bookkeeping files.**
Lets AI agents such as Claude read your books and prepare *draft* vouchers in a local
`.kitsas` file, which a human then reviews and posts in the Kitsas app.

> Unofficial community project. Not affiliated with, endorsed by or supported by Kitsas Oy.
> Kitsas / kitupiikki is © Arto Hyvättinen and Kitsas Oy – <https://github.com/artoh/kitupiikki>.

*Suomeksi alla ↓*

## Why

Small Finnish companies (one-person osakeyhtiö, associations) have a few dozen documents a
month. A capable agent can do the bookkeeping, as long as it has a safe, well-defined way to
talk to a real double-entry ledger that produces the statutory Finnish reports. Kitsas already
does the reports (tilinpäätös, tase-erittelyt, ALV-ilmoitus, päiväkirja/pääkirja). This
project gives it an agent interface.

## Safety model

| Rule | How |
|---|---|
| Agent never posts | All writes create **drafts** (`Tosite.tila = 50`, *Luonnos*). You post them in Kitsas, which then assigns the voucher number. |
| Nothing is lost | A full backup (SQLite backup API) is taken before the first write of every session. |
| Everything is traceable | Every write is logged to `audit-log.jsonl` and to Kitsas's own voucher history (`Tositeloki`). |
| No collisions with the app | Writes are refused while another process (the Kitsas app) holds the file open. Close Kitsas first. |
| Books stay consistent | Vouchers must balance to the cent; accounts must exist; the date must be in an open fiscal period; VAT rows are generated, never typed by hand; posted vouchers cannot be changed or deleted. |
| Known schema only | Writes are enabled only for database version 24 (current Kitsas). Reads work regardless. |

## Tools

| Tool | What it does |
|---|---|
| `book_info` | Company, fiscal periods, VAT settings, lock dates, voucher counts |
| `list_accounts` | Search the chart of accounts (by name, number, type) |
| `list_partners` | Customers and suppliers |
| `account_balances` | Trial balance on a date (optionally including drafts) |
| `account_ledger` | Ledger of one account with running balance |
| `list_vouchers` / `get_voucher` | Browse vouchers, rows, VAT codes, attachments, history |
| `vat_summary` | VAT bases and VAT by code for a period (preview of the VAT return) |
| `create_draft_voucher` | Create a draft with rows, VAT, partner, note and receipt attachments (`dry_run` preview by default) |
| `attach_file_to_draft` | Add a receipt to a draft |
| `delete_draft` | Move a draft to Kitsas's deleted list (recoverable) |
| `export_attachment` | Save a stored receipt to disk |
| `open_book` | Switch to another `.kitsas` file |

### VAT made easy for agents

Put `vat_code` and `vat_percent` on the net row and the server adds the VAT row to the
VAT receivable/payable account, exactly like Kitsas does. Or give the **gross** amount:

```json
{"account": 7640, "gross": 30.30, "side": "debit", "vat_code": 21, "vat_percent": 13.5}
```
→ 26.70 to 7640 + 3.60 to 1763 (deductible VAT). Add `"vat_amount": 3.60` to match a receipt to the cent.

VAT codes: `0` no VAT · `21` deductible purchase · `11` taxable sale · `19` 0 % sale (e.g. services sold outside the EU).

## Install

Requires Python 3.10+.

```bash
pip install git+https://github.com/<your-account>/kitupiikki-mcp
```

### Claude Desktop / Claude Code / any MCP client

```json
{
  "mcpServers": {
    "kitsas": {
      "command": "kitupiikki-mcp",
      "args": ["--file", "C:\\Users\\me\\Kirjanpito\\Yritys Oy.kitsas"]
    }
  }
}
```

Optional: `--backup-dir` (default: `kitupiikki-mcp-backups` next to the book).

Works with the **local (free) Kitsas file**. Cloud books (Kitsas pilvi) are not supported —
their API is not publicly available for writing.

## Typical monthly workflow

1. Close Kitsas.
2. Ask the agent: *"Book September: here are the bank statement and receipts."*
3. The agent previews each voucher (`dry_run`), you confirm, it creates drafts with receipts attached.
4. Open Kitsas → **Kirjaa → Luonnokset**, check and save each draft (it gets a number and is posted).
5. File VAT from Kitsas as usual.

## Development

```bash
pip install -e ".[test]"
pytest
```

Tests build a book from Kitsas's own schema (`tests/luo.sql`, copied unmodified from
kitupiikki, GPL-3). The SQL mirrors `kitsas/sqlite/routes/tositeroute.cpp` and
`saldotroute.cpp`; code tables are in `src/kitupiikki_mcp/codes.py` with source references.

Ideas welcome: bank statement import (CAMT/CSV) as drafts, open-item (erä) matching,
reverse-charge VAT codes, salary vouchers from palkka.fi, a proper upstream API in Kitsas.

## License

GPL-3.0-or-later, like Kitsas. See `LICENSE`.

---

## Suomeksi

**kitupiikki-mcp** on MCP-palvelin, jonka avulla tekoälyagentti (esim. Claude) voi lukea
Kitsaan paikallista kirjanpitotiedostoa ja tehdä siihen **tositeluonnoksia**. Ihminen
tarkistaa ja kirjaa luonnokset Kitsaassa (Kirjaa → Luonnokset).

- Agentti ei koskaan kirjaa suoraan kirjanpitoon – vain luonnoksia.
- Ennen ensimmäistä kirjoitusta otetaan varmuuskopio, ja jokainen muutos lokitetaan.
- Kirjoitus estetään, jos Kitsas-ohjelma on auki samassa tiedostossa.
- Tositteen pitää täsmätä sentilleen, tilin on oltava tilikartassa ja päivämäärän avoimella tilikaudella.
- ALV-rivit muodostetaan automaattisesti (esim. brutto 30,30 € / 13,5 % → 26,70 + 3,60).

Epävirallinen yhteisöprojekti, ei Kitsas Oy:n tuote eikä sen tukema.
