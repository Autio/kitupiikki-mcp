"""Read/draft access to a local Kitsas (kitupiikki) bookkeeping file (.kitsas, SQLite).

Design rules
------------
* Reads never modify the file (opened with ``mode=ro``).
* Writes only ever create or change *draft* vouchers (Tosite.tila = 50, "Luonnos").
  Posting (tila 100, gets a voucher number) is left to a human in the Kitsas app.
* Every write session first takes a consistent backup copy (SQLite backup API) and
  appends to an audit log (JSON lines) next to the backups; Kitsas's own
  ``Tositeloki`` table is also written, exactly like the app does.
* Writes are refused while the Kitsas app appears to have the file open
  (a ``-shm`` file exists), unless explicitly overridden.

The SQL mirrors the behaviour of kitsas/sqlite/routes/tositeroute.cpp and
saldotroute.cpp in the Kitsas source (GPL-3).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Iterable

from .codes import (
    ACCOUNT_TYPE_NAMES, ALV_NAMES, BASE_FOR_TOSITETYYPPI, TILA_NAMES, TOSITETYYPIT,
    TOSITETYYPPI_NAMES, Alv, Tila, VientiTyyppi,
)

SUPPORTED_DB_VERSIONS = {24}
AUDIT_MARKER = "kitupiikki-mcp"


class KitsasError(Exception):
    """A user-facing error (validation failed, file locked, ...)."""


# ---------------------------------------------------------------- money helpers

def to_cents(value: Any) -> int:
    if value in (None, ""):
        return 0
    d = Decimal(str(value).replace(",", ".").replace(" ", ""))
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_cents(cents: int | None) -> float:
    return round((cents or 0) / 100.0, 2)


def _date(value: Any, field: str = "date") -> dt.date:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError as e:
        raise KitsasError(f"Invalid {field} '{value}', use YYYY-MM-DD") from e


def _text(v: Any) -> str:
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return "" if v is None else str(v)


def _json(v: Any) -> dict:
    t = _text(v)
    if not t:
        return {}
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return {}


def _name(js: dict, lang: str = "fi") -> str:
    n = js.get("nimi")
    if isinstance(n, dict):
        return n.get(lang) or n.get("fi") or next(iter(n.values()), "")
    return _text(n)


@dataclass
class Account:
    number: int
    type: str
    name: str
    scope: int          # "laajuus" - chart-of-accounts detail level
    vat_code: int | None
    vat_percent: float | None

    @property
    def debit_normal(self) -> bool:
        """Assets and expenses grow on the debit side."""
        return self.type.startswith(("A", "D"))

    def as_dict(self) -> dict:
        return {
            "number": self.number, "name": self.name, "type": self.type,
            "type_name": ACCOUNT_TYPE_NAMES.get(self.type, self.type),
            "default_vat_code": self.vat_code, "default_vat_percent": self.vat_percent,
        }


class KitsasBook:
    def __init__(self, path: str | os.PathLike, backup_dir: str | os.PathLike | None = None,
                 lang: str = "fi"):
        self.path = Path(path).expanduser().resolve()
        if not self.path.exists():
            raise KitsasError(f"Kitsas file not found: {self.path}")
        self.backup_dir = Path(backup_dir).expanduser() if backup_dir else self.path.parent / "kitupiikki-mcp-backups"
        self.lang = lang
        self._accounts: dict[int, Account] | None = None
        self._backed_up = False

    # ------------------------------------------------------------ connections
    @contextmanager
    def _ro(self):
        """Read-only connection that is always closed (sqlite3's own context manager
        only commits, it does not close - an open handle would look like the app)."""
        uri = f"file:{self.path.as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()

    def app_seems_open(self) -> bool:
        """True if another process (the Kitsas app) appears to hold the database open.

        SQLite deletes the ``-wal``/``-shm`` side files when the *last* connection
        closes. So we open and close a connection ourselves: if the ``-shm`` file
        survives, somebody else still has the file open."""
        shm = Path(str(self.path) + "-shm")
        if not shm.exists():
            return False
        try:
            con = sqlite3.connect(str(self.path), timeout=2)
            con.execute("SELECT COUNT(*) FROM Asetus").fetchone()
            con.close()
        except sqlite3.Error:
            return True
        return shm.exists()

    @contextmanager
    def _rw(self, allow_when_open: bool = False):
        if self.app_seems_open() and not allow_when_open:
            raise KitsasError(
                "The Kitsas app seems to have this file open (another process holds the database). "
                "Close Kitsas before letting the agent write drafts.")
        self._check_version()
        self._backup()
        con = sqlite3.connect(str(self.path), timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _backup(self) -> Path | None:
        if self._backed_up:
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.backup_dir / f"{self.path.stem}-{stamp}.kitsas"
        dst = sqlite3.connect(str(target))
        try:
            with self._ro() as src:
                src.backup(dst)
        finally:
            dst.close()
        self._backed_up = True
        self._audit({"event": "backup", "file": str(target)})
        return target

    def _audit(self, entry: dict) -> None:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        entry = {"time": dt.datetime.now().isoformat(timespec="seconds"), "book": str(self.path), **entry}
        with open(self.backup_dir / "audit-log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def _check_version(self) -> None:
        v = self.settings().get("KpVersio")
        if v is None or int(v) not in SUPPORTED_DB_VERSIONS:
            raise KitsasError(
                f"Unsupported Kitsas database version {v}; writing is only enabled for "
                f"{sorted(SUPPORTED_DB_VERSIONS)}. Reads still work.")

    # ------------------------------------------------------------ settings & meta
    def settings(self) -> dict[str, str]:
        with self._ro() as con:
            return {r["avain"]: _text(r["arvo"]) for r in con.execute("SELECT avain, arvo FROM Asetus")}

    def periods(self) -> list[dict]:
        with self._ro() as con:
            rows = con.execute("SELECT alkaa, loppuu, json FROM Tilikausi ORDER BY alkaa").fetchall()
        locked = self.settings().get("TilitPaatetty")
        return [{"start": r["alkaa"], "end": r["loppuu"],
                 "locked": bool(locked and r["loppuu"] <= locked)} for r in rows]

    def period_for(self, day: dt.date) -> dict | None:
        d = day.isoformat()
        for p in self.periods():
            if p["start"] <= d <= p["end"]:
                return p
        return None

    def info(self) -> dict:
        s = self.settings()
        with self._ro() as con:
            counts = dict(con.execute(
                "SELECT CASE WHEN tila >= 100 THEN 'posted' WHEN tila >= 50 THEN 'drafts' "
                "WHEN tila = 0 THEN 'deleted' ELSE 'other' END, COUNT(*) FROM Tosite GROUP BY 1").fetchall())
            last_vat = con.execute(
                "SELECT MAX(pvm) FROM Tosite WHERE tyyppi = 9100 AND tila >= 100").fetchone()[0]
        return {
            "file": str(self.path),
            "company": s.get("Nimi"), "business_id": s.get("Ytunnus"), "form": s.get("muoto"),
            "address": ", ".join(x for x in (s.get("Katuosoite"), s.get("Postinumero"), s.get("Kaupunki")) if x),
            "practice_file": s.get("Harjoitus") == "ON",
            "db_version": s.get("KpVersio"), "created_with": s.get("LuotuVersiolla"),
            "vat_registered": s.get("AlvVelvollinen") == "1",
            "vat_period_months": s.get("AlvKausi"),
            "last_vat_report_date": last_vat,
            "books_locked_until": s.get("TilitPaatetty"),
            "fiscal_periods": self.periods(),
            "voucher_counts": counts,
            "app_seems_open": self.app_seems_open(),
            "writes_enabled": (s.get("KpVersio") and int(s.get("KpVersio")) in SUPPORTED_DB_VERSIONS),
        }

    # ------------------------------------------------------------ accounts
    def accounts(self) -> dict[int, Account]:
        if self._accounts is None:
            with self._ro() as con:
                rows = con.execute("SELECT numero, tyyppi, json FROM Tili ORDER BY numero").fetchall()
            accs = {}
            for r in rows:
                js = _json(r["json"])
                accs[r["numero"]] = Account(
                    number=r["numero"], type=r["tyyppi"], name=_name(js, self.lang),
                    scope=int(js.get("laajuus", 1) or 1),
                    vat_code=js.get("alvlaji"), vat_percent=js.get("alvprosentti"))
            self._accounts = accs
        return self._accounts

    def account(self, number: int) -> Account:
        a = self.accounts().get(int(number))
        if not a:
            raise KitsasError(f"Account {number} does not exist in this chart of accounts")
        return a

    def account_of_type(self, type_code: str) -> Account | None:
        for a in self.accounts().values():
            if a.type == type_code:
                return a
        return None

    def list_accounts(self, query: str | None = None, include_all: bool = False,
                      type_prefix: str | None = None) -> list[dict]:
        scope = int(self.settings().get("laajuus", "3") or 3)
        with self._ro() as con:
            used = {r[0] for r in con.execute("SELECT DISTINCT tili FROM Vienti")}
        q = (query or "").lower()
        out = []
        for a in self.accounts().values():
            if not include_all and a.scope > scope and a.number not in used:
                continue
            if type_prefix and not a.type.startswith(type_prefix):
                continue
            if q and q not in a.name.lower() and not str(a.number).startswith(q):
                continue
            out.append(a.as_dict())
        return out

    # ------------------------------------------------------------ partners
    def partners(self, query: str | None = None) -> list[dict]:
        with self._ro() as con:
            rows = con.execute("SELECT id, nimi, alvtunnus FROM Kumppani ORDER BY nimi").fetchall()
        q = (query or "").lower()
        return [{"id": r["id"], "name": r["nimi"], "vat_id": r["alvtunnus"]}
                for r in rows if not q or q in (r["nimi"] or "").lower()]

    # ------------------------------------------------------------ balances & ledger
    def balances(self, date: Any, start: Any | None = None, include_drafts: bool = False) -> dict:
        """Trial balance like Kitsas's saldot route: balance-sheet accounts cumulative to
        ``date``; income-statement accounts from the start of the fiscal period (or ``start``)."""
        day = _date(date)
        period = self.period_for(day)
        p_start = _date(start) if start else (_date(period["start"]) if period else dt.date(day.year, 1, 1))
        tila_min = Tila.LUONNOS if include_drafts else Tila.KIRJANPIDOSSA
        with self._ro() as con:
            bs = con.execute(
                "SELECT tili, SUM(debetsnt), SUM(kreditsnt) FROM Vienti JOIN Tosite ON Vienti.tosite=Tosite.id "
                "WHERE Vienti.pvm <= ? AND CAST(tili AS TEXT) < '3' AND Tosite.tila >= ? GROUP BY tili",
                (day.isoformat(), tila_min)).fetchall()
            pl = con.execute(
                "SELECT tili, SUM(debetsnt), SUM(kreditsnt) FROM Vienti JOIN Tosite ON Vienti.tosite=Tosite.id "
                "WHERE Vienti.pvm BETWEEN ? AND ? AND CAST(tili AS TEXT) >= '3' AND Tosite.tila >= ? GROUP BY tili",
                (p_start.isoformat(), day.isoformat(), tila_min)).fetchall()
            prev = con.execute(
                "SELECT SUM(kreditsnt) - SUM(debetsnt) FROM Vienti JOIN Tosite ON Vienti.tosite=Tosite.id "
                "WHERE CAST(tili AS TEXT) >= '3' AND Vienti.pvm < ? AND Tosite.tila >= ?",
                (p_start.isoformat(), tila_min)).fetchone()[0] or 0

        def row(r):
            a = self.accounts().get(r[0])
            d, c = r[1] or 0, r[2] or 0
            bal = (d - c) if (a.debit_normal if a else str(r[0]).startswith(("1", "4", "5", "6", "7", "8"))) else (c - d)
            return {"account": r[0], "name": a.name if a else "?", "debit": from_cents(d),
                    "credit": from_cents(c), "balance": from_cents(bal)}

        income = sum((r[2] or 0) - (r[1] or 0) for r in pl)
        return {
            "date": day.isoformat(), "income_statement_from": p_start.isoformat(),
            "includes_drafts": include_drafts,
            "balance_sheet": [row(r) for r in bs],
            "income_statement": [row(r) for r in pl],
            "profit_for_period": from_cents(income),
            "retained_earnings_from_previous_periods_unclosed": from_cents(prev),
            "note": "Balances are in natural sign: assets/expenses debit-positive, "
                    "liabilities/equity/income credit-positive.",
        }

    def ledger(self, account: int, start: Any, end: Any, include_drafts: bool = False) -> dict:
        a = self.account(account)
        s, e = _date(start, "start"), _date(end, "end")
        tila_min = Tila.LUONNOS if include_drafts else Tila.KIRJANPIDOSSA
        with self._ro() as con:
            if str(a.number) < "3":
                opening = con.execute(
                    "SELECT SUM(debetsnt), SUM(kreditsnt) FROM Vienti JOIN Tosite ON Vienti.tosite=Tosite.id "
                    "WHERE tili=? AND Vienti.pvm < ? AND Tosite.tila >= ?", (a.number, s.isoformat(), tila_min)).fetchone()
                ob = ((opening[0] or 0) - (opening[1] or 0)) * (1 if a.debit_normal else -1)
            else:
                ob = 0
            rows = con.execute(
                "SELECT Vienti.id, Vienti.pvm, Tosite.id AS tosite, Tosite.tunniste, Tosite.sarja, Tosite.tila, "
                "Vienti.selite, debetsnt, kreditsnt, alvkoodi, alvprosentti, Kumppani.nimi AS kumppani "
                "FROM Vienti JOIN Tosite ON Vienti.tosite=Tosite.id "
                "LEFT JOIN Kumppani ON Vienti.kumppani=Kumppani.id "
                "WHERE tili=? AND Vienti.pvm BETWEEN ? AND ? AND Tosite.tila >= ? "
                "ORDER BY Vienti.pvm, Tosite.tunniste, Vienti.rivi",
                (a.number, s.isoformat(), e.isoformat(), tila_min)).fetchall()
        run = ob
        lines = []
        for r in rows:
            d, c = r["debetsnt"] or 0, r["kreditsnt"] or 0
            run += (d - c) if a.debit_normal else (c - d)
            lines.append({
                "date": r["pvm"], "voucher_id": r["tosite"],
                "voucher_number": f"{r['sarja'] or ''}{r['tunniste']}" if r["tunniste"] else None,
                "status": TILA_NAMES.get(r["tila"], r["tila"]), "description": _text(r["selite"]),
                "partner": r["kumppani"], "debit": from_cents(d), "credit": from_cents(c),
                "vat_code": r["alvkoodi"], "vat_percent": r["alvprosentti"], "running_balance": from_cents(run)})
        return {"account": a.as_dict(), "start": s.isoformat(), "end": e.isoformat(),
                "opening_balance": from_cents(ob), "closing_balance": from_cents(run), "entries": lines}

    # ------------------------------------------------------------ vouchers
    def list_vouchers(self, start: Any | None = None, end: Any | None = None, status: str = "posted",
                      query: str | None = None, limit: int = 200) -> list[dict]:
        where, args = [], []
        if status == "posted":
            where.append("t.tila >= 100")
        elif status == "drafts":
            where.append("t.tila >= 50 AND t.tila < 100")
        elif status == "inbox":
            where.append("t.tila > 5 AND t.tila < 50")
        elif status == "deleted":
            where.append("t.tila = 0")
        elif status != "all":
            raise KitsasError("status must be posted, drafts, inbox, deleted or all")
        if start:
            where.append("t.pvm >= ?"); args.append(_date(start).isoformat())
        if end:
            where.append("t.pvm <= ?"); args.append(_date(end).isoformat())
        if query:
            where.append("(t.otsikko LIKE ? OR k.nimi LIKE ?)"); args += [f"%{query}%"] * 2
        sql = ("SELECT t.id, t.pvm, t.tyyppi, t.tila, t.tunniste, t.sarja, t.otsikko, k.nimi AS kumppani, "
               "(SELECT COUNT(*) FROM Liite l WHERE l.tosite=t.id) AS liitteita, "
               "(SELECT SUM(debetsnt) FROM Vienti v WHERE v.tosite=t.id) AS summa "
               "FROM Tosite t LEFT JOIN Kumppani k ON t.kumppani=k.id "
               + ("WHERE " + " AND ".join(where) if where else "") +
               " ORDER BY t.pvm, t.sarja, t.tunniste LIMIT ?")
        args.append(int(limit))
        with self._ro() as con:
            rows = con.execute(sql, args).fetchall()
        return [{"id": r["id"], "date": r["pvm"],
                 "number": f"{r['sarja'] or ''}{r['tunniste']}" if r["tunniste"] else None,
                 "type": TOSITETYYPPI_NAMES.get(r["tyyppi"], r["tyyppi"]),
                 "status": TILA_NAMES.get(r["tila"], r["tila"]), "title": _text(r["otsikko"]),
                 "partner": r["kumppani"], "attachments": r["liitteita"], "total": from_cents(r["summa"])}
                for r in rows]

    def get_voucher(self, voucher_id: int) -> dict:
        with self._ro() as con:
            t = con.execute("SELECT t.*, k.nimi AS kumppani_nimi FROM Tosite t "
                            "LEFT JOIN Kumppani k ON t.kumppani=k.id WHERE t.id=?", (voucher_id,)).fetchone()
            if not t:
                raise KitsasError(f"Voucher {voucher_id} not found")
            lines = con.execute(
                "SELECT v.*, k.nimi AS kumppani_nimi FROM Vienti v LEFT JOIN Kumppani k ON v.kumppani=k.id "
                "WHERE tosite=? ORDER BY rivi", (voucher_id,)).fetchall()
            att = con.execute("SELECT id, nimi, tyyppi, length(data) AS koko, luotu FROM Liite "
                              "WHERE tosite=? ORDER BY id", (voucher_id,)).fetchall()
            log = con.execute("SELECT aika, tila FROM Tositeloki WHERE tosite=? ORDER BY aika",
                              (voucher_id,)).fetchall()
        js = _json(t["json"])
        accs = self.accounts()
        return {
            "id": t["id"], "date": t["pvm"],
            "number": f"{t['sarja'] or ''}{t['tunniste']}" if t["tunniste"] else None,
            "type": TOSITETYYPPI_NAMES.get(t["tyyppi"], t["tyyppi"]),
            "status": TILA_NAMES.get(t["tila"], t["tila"]), "title": _text(t["otsikko"]),
            "partner": t["kumppani_nimi"], "invoice_date": t["laskupvm"], "due_date": t["erapvm"],
            "reference": t["viite"], "note": js.get("info"),
            "lines": [{
                "row": l["rivi"], "date": l["pvm"], "account": l["tili"],
                "account_name": accs[l["tili"]].name if l["tili"] in accs else "?",
                "description": _text(l["selite"]), "debit": from_cents(l["debetsnt"]),
                "credit": from_cents(l["kreditsnt"]), "vat_code": l["alvkoodi"],
                "vat_code_name": ALV_NAMES.get(l["alvkoodi"] or 0), "vat_percent": l["alvprosentti"],
                "partner": l["kumppani_nimi"]} for l in lines],
            "attachments": [{"id": a["id"], "name": a["nimi"], "mime": a["tyyppi"], "bytes": a["koko"],
                             "added": a["luotu"]} for a in att],
            "history": [{"time": h["aika"], "status": TILA_NAMES.get(h["tila"], h["tila"])} for h in log],
        }

    # ------------------------------------------------------------ VAT
    def vat_summary(self, start: Any, end: Any, include_drafts: bool = False) -> dict:
        s, e = _date(start, "start"), _date(end, "end")
        tila_min = Tila.LUONNOS if include_drafts else Tila.KIRJANPIDOSSA
        with self._ro() as con:
            rows = con.execute(
                "SELECT alvkoodi, alvprosentti, SUM(debetsnt), SUM(kreditsnt), COUNT(*) FROM Vienti "
                "JOIN Tosite ON Vienti.tosite=Tosite.id WHERE Vienti.pvm BETWEEN ? AND ? AND Tosite.tila >= ? "
                "AND alvkoodi > 0 AND Tosite.tyyppi <> 9100 GROUP BY alvkoodi, alvprosentti ORDER BY alvkoodi",
                (s.isoformat(), e.isoformat(), tila_min)).fetchall()
        groups, output_vat, input_vat = [], 0, 0
        for code, pct, d, c, n in rows:
            d, c = d or 0, c or 0
            if 100 <= code < 200:            # output VAT booked
                amount = c - d
                output_vat += amount
                kind = "output VAT"
            elif 200 <= code < 300:          # deductible VAT booked
                amount = d - c
                input_vat += amount
                kind = "deductible VAT"
            elif code < 20:                  # sales bases
                amount, kind = c - d, "sales base (net)"
            elif code < 30:                  # purchase bases
                amount, kind = d - c, "purchase base (net)"
            else:
                amount, kind = c - d, "other"
            groups.append({"vat_code": code, "name": ALV_NAMES.get(code, str(code)), "kind": kind,
                           "vat_percent": pct, "amount": from_cents(amount), "entries": n})
        return {"start": s.isoformat(), "end": e.isoformat(), "includes_drafts": include_drafts,
                "by_code": groups, "output_vat": from_cents(output_vat),
                "deductible_vat": from_cents(input_vat),
                "vat_payable": from_cents(output_vat - input_vat),
                "note": "Preview only. File the VAT return from Kitsas (ALV-ilmoitus) or OmaVero."}

    # ------------------------------------------------------------ drafts (writes)
    def _prepare_lines(self, lines: list[dict], day: dt.date, voucher_type: int) -> tuple[list[dict], list[str]]:
        """Validate agent-supplied lines and expand VAT helpers into Kitsas rows."""
        if not lines or len(lines) < 2:
            raise KitsasError("A voucher needs at least two lines (debit and credit)")
        base = BASE_FOR_TOSITETYYPPI.get(voucher_type, 0)
        vat_recv = self.account_of_type("AL")
        vat_pay = self.account_of_type("BL")
        rows, warnings = [], []
        for i, ln in enumerate(lines, 1):
            acc = self.account(ln.get("account"))
            debit, credit = to_cents(ln.get("debit")), to_cents(ln.get("credit"))
            gross = to_cents(ln.get("gross")) if ln.get("gross") not in (None, "") else None
            side = (ln.get("side") or "").lower()
            if gross is not None:
                if side not in ("debit", "credit"):
                    raise KitsasError(f"Line {i}: 'gross' needs side='debit' or 'credit'")
            if (debit and credit) or (not debit and not credit and gross is None):
                raise KitsasError(f"Line {i}: give exactly one of debit or credit (or gross+side)")
            if min(debit, credit) < 0 or (gross is not None and gross < 0):
                raise KitsasError(f"Line {i}: amounts must be positive; use the other side instead")
            code = int(ln.get("vat_code") or 0)
            pct = ln.get("vat_percent")
            pct_d = Decimal(str(pct)) if pct not in (None, "") else None
            desc = str(ln.get("description") or "")
            line_date = _date(ln.get("date") or day)
            if acc.type in ("AL", "BL") and code:
                raise KitsasError(f"Line {i}: do not book VAT lines by hand; use vat_code on the net line")
            if code in (Alv.OSTOT_NETTO, Alv.MYYNNIT_NETTO) and pct_d is None:
                raise KitsasError(f"Line {i}: vat_code {code} needs vat_percent (e.g. 25.5, 13.5, 10)")
            if code and code not in (Alv.ALV0, Alv.OSTOT_NETTO, Alv.MYYNNIT_NETTO, Alv.EIALV):
                raise KitsasError(f"Line {i}: vat_code {code} is not supported for drafts yet "
                                  "(supported: 0, 19, 21, 11)")
            sub = VientiTyyppi.KIRJAUS if code or acc.type.startswith(("C", "D")) else VientiTyyppi.VASTAKIRJAUS
            if base == VientiTyyppi.SIIRTO:   # Kitsas stores transfers as plain 400 on every row
                sub = 0
            if gross is not None:
                if code in (Alv.OSTOT_NETTO, Alv.MYYNNIT_NETTO):
                    if ln.get("vat_amount") not in (None, ""):
                        # exact VAT from the receipt (avoids 1-cent rounding differences)
                        net = gross - to_cents(ln.get("vat_amount"))
                    else:
                        net = int((Decimal(gross) * 100 / (100 + pct_d)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                else:
                    net = gross
                vat = gross - net
                if side == "debit":
                    debit, credit = net, 0
                else:
                    debit, credit = 0, net
            else:
                net = debit or credit
                vat = 0
                if code in (Alv.OSTOT_NETTO, Alv.MYYNNIT_NETTO):
                    vat = int((Decimal(net) * pct_d / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            row = {"tili": acc.number, "debet": debit, "kredit": credit, "selite": desc, "pvm": line_date,
                   "alvkoodi": code, "alvprosentti": float(pct_d) if (code and pct_d is not None) else None,
                   "tyyppi": base + sub if base else 0}
            rows.append(row)
            if vat:
                if code == Alv.OSTOT_NETTO:
                    if not vat_recv:
                        raise KitsasError("No VAT receivable account (type AL) in the chart of accounts")
                    vat_acc, vat_code = vat_recv, Alv.OSTOT_NETTO + Alv.ALVVAHENNYS
                else:
                    if not vat_pay:
                        raise KitsasError("No VAT payable account (type BL) in the chart of accounts")
                    vat_acc, vat_code = vat_pay, Alv.MYYNNIT_NETTO + Alv.ALVKIRJAUS
                rows.append({"tili": vat_acc.number, "debet": vat if debit else 0, "kredit": vat if credit else 0,
                             "selite": desc, "pvm": line_date, "alvkoodi": vat_code,
                             "alvprosentti": float(pct_d), "tyyppi": base + VientiTyyppi.ALVKIRJAUS if base else 0})
            if acc.vat_code and not code and acc.type.startswith(("C", "D")):
                warnings.append(f"Line {i}: account {acc.number} normally uses VAT code {acc.vat_code} "
                                f"({acc.vat_percent} %); booked without VAT as requested.")
        d = sum(r["debet"] for r in rows)
        c = sum(r["kredit"] for r in rows)
        if d != c:
            raise KitsasError(f"Voucher does not balance: debit {from_cents(d)} ≠ credit {from_cents(c)} "
                              f"(difference {from_cents(d - c)}). With gross+VAT lines, let the "
                              f"counter line be the gross amount.")
        return rows, warnings

    def _check_date(self, day: dt.date) -> list[str]:
        p = self.period_for(day)
        if not p:
            raise KitsasError(f"No fiscal period covers {day}. Create the period in Kitsas first "
                              f"(Asetukset → Tilikaudet).")
        if p["locked"]:
            raise KitsasError(f"The fiscal period {p['start']}–{p['end']} is closed/locked.")
        warns = []
        with self._ro() as con:
            last_vat = con.execute("SELECT MAX(pvm) FROM Tosite WHERE tyyppi=9100 AND tila>=100").fetchone()[0]
        if last_vat and day.isoformat() <= last_vat:
            warns.append(f"VAT has already been reported up to {last_vat}; Kitsas will not let you post "
                         f"this date without reopening that VAT period.")
        return warns

    def _partner_id(self, con: sqlite3.Connection, partner: Any) -> int | None:
        if partner in (None, ""):
            return None
        if isinstance(partner, int) or str(partner).isdigit():
            r = con.execute("SELECT id FROM Kumppani WHERE id=?", (int(partner),)).fetchone()
            if not r:
                raise KitsasError(f"Partner id {partner} not found")
            return r[0]
        name = str(partner).strip()
        r = con.execute("SELECT id FROM Kumppani WHERE nimi=?", (name,)).fetchone()
        if r:
            return r[0]
        cur = con.execute("INSERT INTO Kumppani (nimi, json) VALUES (?, ?)", (name, "{}"))
        return cur.lastrowid

    def create_draft(self, date: Any, title: str, lines: list[dict], voucher_type: str = "muu",
                     partner: Any = None, note: str | None = None, attachments: Iterable[str] = (),
                     invoice_date: Any = None, due_date: Any = None, reference: str | None = None,
                     dry_run: bool = False, allow_when_app_open: bool = False) -> dict:
        day = _date(date)
        vt = TOSITETYYPIT.get(str(voucher_type).lower()) if not str(voucher_type).isdigit() else int(voucher_type)
        if vt is None:
            raise KitsasError(f"Unknown voucher_type '{voucher_type}'. Use one of: {', '.join(TOSITETYYPIT)}")
        if not title:
            raise KitsasError("title is required")
        warnings = self._check_date(day)
        rows, w2 = self._prepare_lines(lines, day, vt)
        warnings += w2
        files = [Path(p).expanduser() for p in attachments]
        for f in files:
            if not f.is_file():
                raise KitsasError(f"Attachment not found: {f}")
        preview = {
            "date": day.isoformat(), "title": title, "type": TOSITETYYPPI_NAMES.get(vt, vt),
            "lines": [{"account": r["tili"], "account_name": self.account(r["tili"]).name,
                       "debit": from_cents(r["debet"]), "credit": from_cents(r["kredit"]),
                       "vat_code": r["alvkoodi"], "vat_percent": r["alvprosentti"],
                       "description": r["selite"]} for r in rows],
            "total": from_cents(sum(r["debet"] for r in rows)),
            "attachments": [f.name for f in files], "warnings": warnings,
        }
        if dry_run:
            return {"dry_run": True, **preview}

        tosite_json = {"info": note} if note else {}
        request_log = {
            "source": AUDIT_MARKER, "pvm": day.isoformat(), "tyyppi": vt, "tila": Tila.LUONNOS,
            "otsikko": title, "viennit": [{**r, "pvm": r["pvm"].isoformat(),
                                           "debet": from_cents(r["debet"]), "kredit": from_cents(r["kredit"])}
                                          for r in rows],
        }
        with self._rw(allow_when_app_open) as con:
            kid = self._partner_id(con, partner)
            cur = con.execute(
                "INSERT INTO Tosite (pvm, tyyppi, tila, tunniste, otsikko, kumppani, sarja, laskupvm, erapvm, viite, json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (day.isoformat(), vt, Tila.LUONNOS, 0, title, kid, None,
                 _date(invoice_date).isoformat() if invoice_date else day.isoformat(),
                 _date(due_date).isoformat() if due_date else None, reference,
                 json.dumps(tosite_json, ensure_ascii=False)))
            tid = cur.lastrowid
            for n, r in enumerate(rows, 1):
                con.execute(
                    "INSERT INTO Vienti (tosite, pvm, tili, kohdennus, selite, debetsnt, kreditsnt, eraid, json, "
                    "alvkoodi, alvprosentti, rivi, kumppani, jaksoalkaa, jaksoloppuu, tyyppi, arkistotunnus) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tid, r["pvm"].isoformat(), r["tili"], 0, r["selite"], r["debet"] or None, r["kredit"] or None,
                     None, "{}", r["alvkoodi"],
                     f"{r['alvprosentti']:.2f}" if r["alvkoodi"] and r["alvprosentti"] is not None else None,
                     n, kid, None, None, r["tyyppi"], None))
            for f in files:
                self._insert_attachment(con, tid, f)
            con.execute("INSERT INTO Tositeloki (tosite, tila, data) VALUES (?,?,?)",
                        (tid, Tila.LUONNOS, json.dumps(request_log, ensure_ascii=False)))
        self._audit({"event": "create_draft", "voucher_id": tid, **preview})
        return {"created_draft_id": tid, "status": "draft", **preview,
                "next_step": "Open Kitsas → Kirjaa → Luonnokset, review and save the draft to post it."}

    def _insert_attachment(self, con: sqlite3.Connection, tosite_id: int, f: Path) -> int:
        data = f.read_bytes()
        mime = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        sha = hashlib.sha256(data).hexdigest()
        cur = con.execute("INSERT INTO Liite (tosite, nimi, data, tyyppi, sha) VALUES (?,?,?,?,?)",
                          (tosite_id, f.name, data, mime, sha))
        return cur.lastrowid

    def _require_draft(self, con: sqlite3.Connection, voucher_id: int) -> None:
        r = con.execute("SELECT tila FROM Tosite WHERE id=?", (voucher_id,)).fetchone()
        if not r:
            raise KitsasError(f"Voucher {voucher_id} not found")
        if not (Tila.LUONNOS <= r[0] < Tila.KIRJANPIDOSSA):
            raise KitsasError(f"Voucher {voucher_id} is '{TILA_NAMES.get(r[0], r[0])}', not a draft. "
                              "This server only changes drafts; edit posted vouchers in Kitsas.")

    def attach_file(self, voucher_id: int, path: str, allow_when_app_open: bool = False) -> dict:
        f = Path(path).expanduser()
        if not f.is_file():
            raise KitsasError(f"File not found: {f}")
        with self._rw(allow_when_app_open) as con:
            self._require_draft(con, voucher_id)
            lid = self._insert_attachment(con, voucher_id, f)
        self._audit({"event": "attach_file", "voucher_id": voucher_id, "file": str(f), "attachment_id": lid})
        return {"voucher_id": voucher_id, "attachment_id": lid, "name": f.name}

    def delete_draft(self, voucher_id: int, allow_when_app_open: bool = False) -> dict:
        with self._rw(allow_when_app_open) as con:
            self._require_draft(con, voucher_id)
            con.execute("UPDATE Tosite SET tila=0 WHERE id=?", (voucher_id,))
            con.execute("INSERT INTO Tositeloki (tosite, tila, data) VALUES (?,0,?)",
                        (voucher_id, json.dumps({"source": AUDIT_MARKER, "action": "delete_draft"})))
        self._audit({"event": "delete_draft", "voucher_id": voucher_id})
        return {"voucher_id": voucher_id, "status": "deleted (recoverable in Kitsas)"}

    def export_attachment(self, attachment_id: int, target_dir: str) -> dict:
        with self._ro() as con:
            r = con.execute("SELECT nimi, data FROM Liite WHERE id=?", (attachment_id,)).fetchone()
        if not r:
            raise KitsasError(f"Attachment {attachment_id} not found")
        out = Path(target_dir).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        name = r["nimi"] or f"liite-{attachment_id}"
        p = out / name
        p.write_bytes(r["data"])
        return {"attachment_id": attachment_id, "saved_to": str(p), "bytes": len(r["data"])}
