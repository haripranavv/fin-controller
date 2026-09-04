"""Static pools used by the synthetic data generator: merchants, customer
names, bank codes, narration templates, refund reason codes.

Kept intentionally modest in size — this is a synthetic dataset for a
reconciliation agent demo, not a realism benchmark. What matters is that the
*shapes* of the noise are realistic (PROJECT_SPEC.md section 15's "a judge
cannot dismiss it as trivial toy rows"), not the size of the name pool.
"""
from __future__ import annotations

MERCHANTS: list[tuple[str, str]] = [
    ("mch_0001", "Raj Trading Co"),
    ("mch_0002", "Bluepeak Retail"),
    ("mch_0003", "Sundar Textiles"),
    ("mch_0004", "Orbit Electronics"),
    ("mch_0005", "Green Leaf Grocers"),
    ("mch_0006", "Nimbus Logistics"),
    ("mch_0007", "Kaveri Foods"),
    ("mch_0008", "Silverline Apparel"),
    ("mch_0009", "Northwind Traders"),
    ("mch_0010", "Meridian Books"),
    ("mch_0011", "Aster Home Decor"),
    ("mch_0012", "Coral Bay Hardware"),
]

CUSTOMER_NAMES: list[str] = [
    "Aditi Sharma", "Rahul Verma", "Priya Nair", "Karan Mehta",
    "Sneha Iyer", "Vikram Rao", "Ananya Das", "Arjun Pillai",
    "Meera Joshi", "Rohit Kapoor", "Divya Menon", "Sanjay Gupta",
    "Neha Reddy", "Amit Choudhary", "Pooja Bhat", "Manish Kumar",
]

BANK_CODES: list[str] = ["HDFC", "ICIC", "SBIN", "AXIS", "KOTAK", "YESB", "PUNB"]

PAYMENT_METHODS: list[str] = ["upi", "card", "netbanking", "wallet"]

REFUND_REASON_CODES: list[str] = [
    "customer_request", "order_cancelled", "product_return",
    "service_issue", "duplicate_charge",
]

# Clean narrations: formats a deterministic parser is assumed to already
# know (PROJECT_SPEC.md section 8.1-8.2: normalize / exact match).
CLEAN_NARRATION_TEMPLATES: list[str] = [
    "UPI/{payment_id}/{merchant_short}/{customer_token}",
    "POS PURCHASE {merchant_upper} {order_id}",
    "CARD TXN {order_id} {merchant_short}",
    "NETBANKING {merchant_short} {order_id}",
]

# Messy/unseen narrations: PROJECT_SPEC.md section 9's whole premise — formats
# the deterministic parser has NOT seen, that narration_extractor (an AI
# tool, built in a later milestone) must turn into structured fields. Each
# embeds a reference/invoice token and a counterparty token so a human (or an
# LLM) can still make sense of it, just not via a fixed regex. The first
# template is deliberately the spec's own example shape (section 9:
# "NEFT-HDFC-RAJTRADCO-INV88213-PARTIAL").
MESSY_NARRATION_TEMPLATES: list[str] = [
    "NEFT-{bank_code}-{counterparty_token}-INV{invoice_num}-PARTIAL",
    "RTGS/{counterparty_token}/{ref_token}/SETTLE",
    "IMPS:{counterparty_token}:{ref_token}:{amount_hint}",
    "{counterparty_token}*{ref_token}*PAYMENT",
    "Payment received from {counterparty_name} against invoice {invoice_num}",
    "TRF-{counterparty_token}-{ref_token}-{date_token}",
]


def short_code(name: str) -> str:
    """'Raj Trading Co' -> 'RAJTRAD' (first 7 alnum chars, uppercased)."""
    return "".join(ch for ch in name.upper() if ch.isalnum())[:7]


def token(name: str) -> str:
    """'Raj Trading Co' -> 'RAJTRADCO' — the shape the spec's own example
    narration uses (NEFT-HDFC-RAJTRADCO-INV88213-PARTIAL)."""
    return "".join(ch for ch in name.upper() if ch.isalnum())
