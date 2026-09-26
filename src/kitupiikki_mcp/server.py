"""MCP server exposing a local Kitsas (kitupiikki) bookkeeping file to AI agents.

Run:  kitupiikki-mcp --file "/path/to/Company.kitsas"
  or  KITSAS_FILE=/path/to/Company.kitsas kitupiikki-mcp
"""
from __future__ import annotations

import argparse
import os
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from .book import KitsasBook, KitsasError

INSTRUCTIONS = """\
You are working with a Finnish double-entry bookkeeping file made by the Kitsas app.
Rules:
- Everything you write is a DRAFT (Luonnos). A human reviews and posts it in Kitsas.
- Always call book_info first; check the fiscal periods and whether the app is open.
- Look up account numbers with list_accounts; never guess.
- Use create_draft_voucher with dry_run=true first and show the preview to the user.
- For VAT, put vat_code + vat_percent on the net expense/income line; the server adds the
  VAT row itself. Finnish rates 2026: 25.5 (general), 13.5 (food, transport, accommodation,
  books, medicines), 10 (some items), 0. Services sold outside the EU: vat_code 19.
- With {gross, side, vat_code, vat_percent} the server splits gross into net + VAT.
- Amounts are euros with two decimals; one voucher must balance exactly.
"""

mcp = FastMCP("kitupiikki-mcp", instructions=INSTRUCTIONS)
_state: dict[str, KitsasBook | None] = {"book": None}


def _book() -> KitsasBook:
    b = _state["book"]
    if b is None:
        path = os.environ.get("KITSAS_FILE")
        if not path:
            raise KitsasError("No Kitsas file open. Call open_book(path) or set KITSAS_FILE.")
        b = _state["book"] = KitsasBook(path, os.environ.get("KITSAS_BACKUP_DIR"))
    return b


def _run(fn, *a, **kw) -> Any:
    try:
        return fn(*a, **kw)
    except KitsasError as e:
        raise ValueError(str(e)) from e


class Line(BaseModel):
    account: int = Field(description="Account number from list_accounts, e.g. 7910 or 1910")
    debit: float | None = Field(None, description="Debit amount in euros (net if vat_code given)")
    credit: float | None = Field(None, description="Credit amount in euros (net if vat_code given)")
    gross: float | None = Field(None, description="Alternative: gross amount incl. VAT; requires side")
    side: Literal["debit", "credit"] | None = Field(None, description="Side for a gross amount")
    vat_code: int | None = Field(None, description="0 none, 21 deductible purchase, 11 taxable sale, 19 zero-rated sale")
    vat_percent: float | None = Field(None, description="VAT rate, e.g. 25.5 or 13.5")
    vat_amount: float | None = Field(None, description="Exact VAT from the receipt when using gross (optional)")
    description: str | None = Field(None, description="Row text (selite)")
    date: str | None = Field(None, description="Row date YYYY-MM-DD if different from voucher date")


@mcp.tool()
def open_book(path: str, backup_dir: str | None = None) -> dict:
    """Open a .kitsas bookkeeping file (the local SQLite file used by the Kitsas app)."""
    _state["book"] = _run(KitsasBook, path, backup_dir)
    return _run(_state["book"].info)


@mcp.tool()
def book_info() -> dict:
    """Company details, fiscal periods, VAT settings, lock dates and whether writes are possible."""
    return _run(_book().info)


@mcp.tool()
def list_accounts(query: str | None = None, type_prefix: str | None = None,
                  include_all: bool = False) -> list[dict]:
    """Search the chart of accounts by name or number prefix. type_prefix e.g. 'D' expenses,
    'C' income, 'ARP' bank accounts, 'B' liabilities. include_all shows hidden detail accounts."""
    return _run(_book().list_accounts, query, include_all, type_prefix)


@mcp.tool()
def list_partners(query: str | None = None) -> list[dict]:
    """Customers/suppliers (kumppanit) known to the book."""
    return _run(_book().partners, query)


@mcp.tool()
def account_balances(date: str, start: str | None = None, include_drafts: bool = False) -> dict:
    """Trial balance on a date: balance-sheet accounts cumulative, income statement from the
    start of the fiscal period (or `start`)."""
    return _run(_book().balances, date, start, include_drafts)


@mcp.tool()
def account_ledger(account: int, start: str, end: str, include_drafts: bool = False) -> dict:
    """General-ledger entries of one account with opening and running balance."""
    return _run(_book().ledger, account, start, end, include_drafts)


@mcp.tool()
def list_vouchers(start: str | None = None, end: str | None = None,
                  status: Literal["posted", "drafts", "inbox", "deleted", "all"] = "posted",
                  query: str | None = None, limit: int = 200) -> list[dict]:
    """List vouchers (tositteet) by date range, status and text search."""
    return _run(_book().list_vouchers, start, end, status, query, limit)


@mcp.tool()
def get_voucher(voucher_id: int) -> dict:
    """Full voucher with rows, VAT codes, attachments and history."""
    return _run(_book().get_voucher, voucher_id)


@mcp.tool()
def vat_summary(start: str, end: str, include_drafts: bool = False) -> dict:
    """VAT bases and booked VAT by code for a period (preview of the VAT return)."""
    return _run(_book().vat_summary, start, end, include_drafts)


@mcp.tool()
def create_draft_voucher(date: str, title: str, lines: list[Line],
                         voucher_type: Literal["muu", "meno", "kululasku", "tulo", "myyntilasku",
                                               "siirto", "tiliote", "palkka", "muistio"] = "muu",
                         partner: str | None = None, note: str | None = None,
                         attachments: list[str] | None = None, invoice_date: str | None = None,
                         due_date: str | None = None, reference: str | None = None,
                         dry_run: bool = True) -> dict:
    """Create a DRAFT voucher (never posted). Validates balance, accounts, open fiscal period
    and VAT; expands VAT rows automatically. Keep dry_run=true to preview, then repeat with
    dry_run=false after the user agrees. attachments = local file paths (PDF/JPG receipts)."""
    return _run(_book().create_draft, date, title, [l.model_dump() for l in lines], voucher_type,
                partner, note, attachments or [], invoice_date, due_date, reference, dry_run)


@mcp.tool()
def attach_file_to_draft(voucher_id: int, path: str) -> dict:
    """Attach a receipt/file (local path) to an existing draft voucher."""
    return _run(_book().attach_file, voucher_id, path)


@mcp.tool()
def delete_draft(voucher_id: int) -> dict:
    """Move a draft voucher to Kitsas's deleted list (recoverable). Posted vouchers are refused."""
    return _run(_book().delete_draft, voucher_id)


@mcp.tool()
def export_attachment(attachment_id: int, target_dir: str) -> dict:
    """Save an attachment stored in the book to a local folder (e.g. to read a receipt)."""
    return _run(_book().export_attachment, attachment_id, target_dir)


def main() -> None:
    ap = argparse.ArgumentParser(description="MCP server for Kitsas (.kitsas) bookkeeping files")
    ap.add_argument("--file", help="Path to the .kitsas file (or set KITSAS_FILE)")
    ap.add_argument("--backup-dir", help="Where backups and the audit log go")
    args = ap.parse_args()
    if args.file:
        os.environ["KITSAS_FILE"] = args.file
    if args.backup_dir:
        os.environ["KITSAS_BACKUP_DIR"] = args.backup_dir
    mcp.run()


if __name__ == "__main__":
    main()
