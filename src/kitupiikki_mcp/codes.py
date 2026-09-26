"""Code tables mirrored from the Kitsas (kitupiikki) source code.

Sources (GPL-3, (c) Arto Hyvättinen / Kitsas Oy):
  kitsas/model/tosite.h          -> Tosite::Tila
  kitsas/db/tositetyyppimodel.h  -> TositeTyyppi::Tyyppi
  kitsas/model/tositevienti.h    -> TositeVienti::VientiTyyppi
  kitsas/kitsas.h                -> AlvKoodi::Koodi
  kitsas/db/tilityyppimodel.cpp  -> account type letters
"""

# Voucher (Tosite) states
class Tila:
    POISTETTU = 0        # deleted
    MALLIPOHJA = 5       # template
    HYLATTY = 10
    SAAPUNUT = 20        # inbox
    TARKASTETTU = 30
    HYVAKSYTTY = 40
    LUONNOS = 50         # draft  <- the only state this server writes
    VALMISLASKU = 80
    KIRJANPIDOSSA = 100  # posted


TILA_NAMES = {
    0: "deleted", 5: "template", 10: "rejected", 20: "inbox", 30: "checked",
    40: "approved", 50: "draft", 80: "invoice ready", 100: "posted",
    101: "posted (sending)", 102: "posted (send error)", 110: "posted (invoice sent)",
    113: "posted (delivered)", 114: "posted (opened)", 120: "posted (reminded)",
}

# Voucher types (Tosite.tyyppi)
TOSITETYYPIT = {
    "muu": 0,            # other / general journal
    "meno": 100,         # expense
    "kululasku": 120,    # employee expense claim
    "tulo": 200,         # income
    "myyntilasku": 210,  # sales invoice
    "siirto": 300,       # transfer
    "tiliote": 400,      # bank statement
    "palkka": 500,       # salary
    "muistio": 700,      # memo
}
TOSITETYYPPI_NAMES = {v: k for k, v in TOSITETYYPIT.items()} | {
    90: "tuonti", 110: "saapunut verkkolasku", 214: "hyvityslasku", 216: "maksumuistutus",
    800: "liitetieto", 1000: "järjestelmätosite", 9010: "tilinavaus", 9100: "alv-laskelma",
    9110: "yhteenvetoilmoitus", 9910: "poistolaskelma", 9920: "jaksotus", 9930: "tulovero",
}

# Entry (Vienti) types: base (OSTO/MYYNTI/...) + sub (KIRJAUS/VASTAKIRJAUS/ALVKIRJAUS)
class VientiTyyppi:
    TUNTEMATON = 0
    KIRJAUS = 1
    VASTAKIRJAUS = 2
    ALVKIRJAUS = 3
    OSTO = 100
    MYYNTI = 200
    SUORITUS = 300
    SIIRTO = 400


BASE_FOR_TOSITETYYPPI = {100: 100, 110: 100, 120: 100, 200: 200, 210: 200, 300: 400, 400: 400}

# VAT codes (Vienti.alvkoodi)
class Alv:
    EIALV = 0
    ALV0 = 19                      # 0 % sales (e.g. services sold outside the EU)
    MYYNNIT_NETTO = 11             # taxable sales, net booking
    OSTOT_NETTO = 21               # deductible purchases, net booking
    MYYNNIT_BRUTTO = 12
    OSTOT_BRUTTO = 22
    YHTEISOMYYNTI_PALVELUT = 15    # EU service sales (reverse charge)
    YHTEISOHANKINNAT_PALVELUT = 25 # EU service purchases (reverse charge)
    MAAHANTUONTI_PALVELUT = 29     # services bought from outside the EU (reverse charge)
    ALVKIRJAUS = 100               # + code => output VAT line
    ALVVAHENNYS = 200              # + code => deductible VAT line
    VAHENNYSKELVOTON = 932


ALV_NAMES = {
    0: "no VAT", 11: "taxable sales (net)", 12: "taxable sales (gross)", 13: "margin sales",
    14: "EU goods sales", 15: "EU service sales", 16: "construction sales (reverse)",
    18: "cash-basis sales", 19: "0 % sales", 21: "deductible purchases (net)",
    22: "deductible purchases (gross)", 23: "margin purchases", 24: "EU goods purchases",
    25: "EU service purchases", 26: "construction purchases (reverse)", 27: "import of goods",
    28: "cash-basis purchases", 29: "import of services (outside EU)",
    111: "output VAT on sales", 124: "VAT on EU goods purchases", 125: "VAT on EU service purchases",
    126: "VAT on construction purchases", 129: "VAT on imported services",
    221: "deductible VAT on purchases", 224: "deductible VAT, EU goods", 225: "deductible VAT, EU services",
    226: "deductible VAT, construction", 229: "deductible VAT, imported services",
    900: "VAT payable", 901: "VAT settlement", 932: "non-deductible VAT",
}

# Account type prefixes: 'A*' assets (vastaavaa), 'B*' liabilities & equity (vastattavaa),
# 'C*' income, 'D*' expenses, 'T' result of the period.
ACCOUNT_TYPE_NAMES = {
    "A": "asset", "APM": "depreciable asset (residual)", "APT": "depreciable asset (straight-line)",
    "AS": "receivable", "AO": "sales receivables", "AJ": "accrued income", "AL": "VAT receivable",
    "ALM": "cash-basis VAT receivable", "AV": "tax receivable", "ARK": "cash", "ARP": "bank account",
    "B": "liability/equity", "BE": "retained earnings", "T": "result of the period", "BS": "liability",
    "BSP": "credit account", "BO": "trade payables", "BJ": "accrued liabilities", "BL": "VAT payable",
    "BLM": "cash-basis VAT payable", "BV": "tax payable", "BY": "private accounts",
    "C": "income", "CL": "turnover (sales)", "CZ": "tax-free income", "CLZ": "VAT-free sales",
    "D": "expense", "DP": "depreciation", "DZ": "non-deductible expense",
    "DH": "half-deductible expense", "DPZ": "non-deductible depreciation", "DVE": "prepaid taxes",
}
