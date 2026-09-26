import json
import sqlite3
from pathlib import Path

import pytest

from kitupiikki_mcp.book import KitsasBook, KitsasError

HERE = Path(__file__).parent


def make_book(tmp_path: Path) -> Path:
    """Minimal .kitsas file built from Kitsas's own schema (tests/luo.sql, GPL-3)."""
    db = tmp_path / "Testi Oy.kitsas"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript((HERE / "luo.sql").read_text(encoding="utf-8"))
    settings = {"KpVersio": "24", "Nimi": "Testi Oy", "Ytunnus": "1234567-8", "muoto": "oy",
                "laajuus": "3", "AlvVelvollinen": "1", "AlvKausi": "1", "TilitPaatetty": "2025-12-31"}
    con.executemany("INSERT INTO Asetus (avain, arvo) VALUES (?,?)", settings.items())
    con.execute("INSERT INTO Tilikausi VALUES ('2025-01-01','2025-12-31','{}')")
    con.execute("INSERT INTO Tilikausi VALUES ('2026-01-01','2026-12-31','{}')")
    accounts = [
        (1763, "AL", {"nimi": {"fi": "Arvonlisäverosaamiset"}, "laajuus": 1}),
        (1910, "ARP", {"nimi": {"fi": "Pankkitili"}, "laajuus": 1}),
        (2871, "BO", {"nimi": {"fi": "Ostovelat"}, "laajuus": 1}),
        (2939, "BL", {"nimi": {"fi": "Arvonlisäverovelka"}, "laajuus": 1}),
        (2961, "BJ", {"nimi": {"fi": "Siirtovelat"}, "laajuus": 1}),
        (3000, "CL", {"nimi": {"fi": "Myynti"}, "laajuus": 1, "alvlaji": 11, "alvprosentti": 25.5}),
        (7640, "D", {"nimi": {"fi": "Matkaliput"}, "laajuus": 1, "alvlaji": 21, "alvprosentti": 13.5}),
        (7670, "D", {"nimi": {"fi": "Päivärahat, verovapaat"}, "laajuus": 1}),
        (8999, "D", {"nimi": {"fi": "Piilotettu tili"}, "laajuus": 6}),
    ]
    con.executemany("INSERT INTO Tili (numero, tyyppi, json) VALUES (?,?,?)",
                    [(n, t, json.dumps(j)) for n, t, j in accounts])
    # one posted sale: 1000 € to bank, 0 % (export of services)
    con.execute("INSERT INTO Tosite (id,pvm,tyyppi,tila,tunniste,otsikko,json) "
                "VALUES (1,'2026-01-15',200,100,1,'Myynti mWater','{}')")
    con.execute("INSERT INTO Vienti (rivi,tosite,tyyppi,pvm,tili,selite,debetsnt,alvkoodi) "
                "VALUES (1,1,202,'2026-01-15',1910,'mWater',100000,0)")
    con.execute("INSERT INTO Vienti (rivi,tosite,tyyppi,pvm,tili,selite,kreditsnt,alvkoodi,alvprosentti) "
                "VALUES (2,1,201,'2026-01-15',3000,'mWater',100000,19,0)")
    con.commit()
    con.close()
    return db


@pytest.fixture
def book(tmp_path):
    return KitsasBook(make_book(tmp_path), backup_dir=tmp_path / "backups")


def test_info_and_accounts(book):
    info = book.info()
    assert info["company"] == "Testi Oy"
    assert info["writes_enabled"]
    assert [p["locked"] for p in info["fiscal_periods"]] == [True, False]
    nums = [a["number"] for a in book.list_accounts()]
    assert 8999 not in nums and 7640 in nums
    assert 8999 in [a["number"] for a in book.list_accounts(include_all=True)]
    assert [a["number"] for a in book.list_accounts(type_prefix="ARP")] == [1910]


def test_balances_and_ledger(book):
    b = book.balances("2026-01-31")
    assert {r["account"]: r["balance"] for r in b["balance_sheet"]} == {1910: 1000.0}
    assert b["profit_for_period"] == 1000.0
    led = book.ledger(1910, "2026-01-01", "2026-01-31")
    assert led["closing_balance"] == 1000.0 and len(led["entries"]) == 1


def test_dry_run_gross_vat_split(book):
    r = book.create_draft("2026-08-21", "Juna Tampere–Turku", voucher_type="kululasku", lines=[
        {"account": 7640, "gross": 30.30, "side": "debit", "vat_code": 21, "vat_percent": 13.5},
        {"account": 2961, "credit": 30.30},
    ], dry_run=True)
    rows = {l["account"]: l for l in r["lines"]}
    assert rows[7640]["debit"] == 26.70
    assert rows[1763]["debit"] == 3.60
    assert rows[2961]["credit"] == 30.30
    assert book.list_vouchers(status="drafts") == []  # nothing written


def test_create_draft_is_draft_with_attachment_and_backup(book, tmp_path):
    receipt = tmp_path / "kuitti.pdf"
    receipt.write_bytes(b"%PDF-1.4 test")
    r = book.create_draft("2026-09-03", "Päivärahat Water Forum", voucher_type="kululasku",
                          partner="Petri Autio", note="2 × kokopäiväraha 54 €",
                          attachments=[str(receipt)], dry_run=False, lines=[
        {"account": 7670, "debit": 108, "description": "Kokopäiväraha 2 × 54 €"},
        {"account": 2961, "credit": 108, "description": "Velka Petri Autiolle"},
    ])
    vid = r["created_draft_id"]
    v = book.get_voucher(vid)
    assert v["status"] == "draft" and v["number"] is None
    assert v["note"] == "2 × kokopäiväraha 54 €"
    assert [a["name"] for a in v["attachments"]] == ["kuitti.pdf"]
    assert v["history"][0]["status"] == "draft"
    # drafts do not affect balances unless asked
    assert book.balances("2026-12-31")["profit_for_period"] == 1000.0
    assert book.balances("2026-12-31", include_drafts=True)["profit_for_period"] == 892.0
    backups = list((tmp_path / "backups").glob("*.kitsas"))
    assert backups, "backup must be taken before writing"
    assert (tmp_path / "backups" / "audit-log.jsonl").exists()
    # Kitsas-compatible raw data
    con = sqlite3.connect(book.path)
    t = con.execute("SELECT tila, tunniste, tyyppi FROM Tosite WHERE id=?", (vid,)).fetchone()
    assert t == (50, 0, 120)
    types = [r[0] for r in con.execute("SELECT tyyppi FROM Vienti WHERE tosite=? ORDER BY rivi", (vid,))]
    assert types == [101, 102]
    sha = con.execute("SELECT sha FROM Liite WHERE tosite=?", (vid,)).fetchone()[0]
    assert len(sha) == 64


def test_validation(book):
    with pytest.raises(KitsasError, match="does not balance"):
        book.create_draft("2026-02-01", "x", [{"account": 7640, "debit": 10}, {"account": 1910, "credit": 9}])
    with pytest.raises(KitsasError, match="closed"):
        book.create_draft("2025-06-01", "x", [{"account": 7640, "debit": 10}, {"account": 1910, "credit": 10}])
    with pytest.raises(KitsasError, match="No fiscal period"):
        book.create_draft("2027-06-01", "x", [{"account": 7640, "debit": 10}, {"account": 1910, "credit": 10}])
    with pytest.raises(KitsasError, match="does not exist"):
        book.create_draft("2026-02-01", "x", [{"account": 7641, "debit": 10}, {"account": 1910, "credit": 10}])
    with pytest.raises(KitsasError, match="needs vat_percent"):
        book.create_draft("2026-02-01", "x", [{"account": 7640, "debit": 10, "vat_code": 21},
                                              {"account": 1910, "credit": 10}])


def test_only_drafts_can_change(book):
    with pytest.raises(KitsasError, match="not a draft"):
        book.delete_draft(1)
    r = book.create_draft("2026-02-01", "Poistettava", dry_run=False, lines=[
        {"account": 7640, "debit": 10}, {"account": 1910, "credit": 10}])
    book.delete_draft(r["created_draft_id"])
    assert book.list_vouchers(status="deleted")[0]["id"] == r["created_draft_id"]


def test_refuses_when_app_open(book):
    other = sqlite3.connect(book.path)          # simulates the Kitsas app holding the file
    other.execute("SELECT COUNT(*) FROM Asetus").fetchone()
    try:
        assert book.app_seems_open()
        with pytest.raises(KitsasError, match="seems to have this file open"):
            book.create_draft("2026-02-01", "x", dry_run=False, lines=[
                {"account": 7640, "debit": 10}, {"account": 1910, "credit": 10}])
    finally:
        other.close()
    assert not book.app_seems_open()


def test_vat_summary(book):
    book.create_draft("2026-08-21", "Juna", dry_run=False, voucher_type="meno", lines=[
        {"account": 7640, "gross": 30.30, "side": "debit", "vat_code": 21, "vat_percent": 13.5},
        {"account": 1910, "credit": 30.30}])
    s = book.vat_summary("2026-08-01", "2026-08-31", include_drafts=True)
    assert s["deductible_vat"] == 3.60 and s["vat_payable"] == -3.60
    assert book.vat_summary("2026-08-01", "2026-08-31")["deductible_vat"] == 0


def test_exact_vat_amount_from_receipt(book):
    r = book.create_draft("2026-08-20", "HSL", voucher_type="meno", lines=[
        {"account": 7640, "gross": 9.00, "side": "debit", "vat_code": 21, "vat_percent": 13.5, "vat_amount": 1.08},
        {"account": 1910, "credit": 9.00}])
    rows = {l["account"]: l for l in r["lines"]}
    assert rows[7640]["debit"] == 7.92 and rows[1763]["debit"] == 1.08
