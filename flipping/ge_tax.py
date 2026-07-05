"""Grand Exchange tax and flip-margin math.

Rules current as of the "Yama CAs & More!" update (29 May 2025), verified
against https://oldschool.runescape.wiki/w/Grand_Exchange_tax:
  - 2% of sale price per item, rounded down
  - capped at 5,000,000 gp per item
  - items sold below 50 gp incur no tax (2% rounds down to 0)
  - some items are fully exempt
"""

TAX_RATE = 0.02
TAX_CAP = 5_000_000

# Fully tax-exempt items (wiki: Grand_Exchange_tax#Exempt_items).
# Kept as names because the exemption list is curated by Jagex, not derivable
# from item metadata. Names must match the /mapping endpoint exactly.
EXEMPT_ITEM_NAMES = frozenset({
    "Old school bond",
    "Energy potion(1)", "Energy potion(2)", "Energy potion(3)", "Energy potion(4)",
    "Bronze arrow", "Iron arrow", "Steel arrow",
    "Bronze dart", "Iron dart", "Steel dart",
    "Mind rune",
    "Bread", "Cake", "Shrimps", "Lobster", "Salmon", "Tuna", "Bass",
    "Varrock teleport", "Falador teleport", "Lumbridge teleport",
    "Ardougne teleport", "Camelot teleport",
    "Hammer", "Spade", "Saw", "Chisel", "Shears", "Rake", "Needle",
    "Bucket", "Tinderbox", "Gardening trowel", "Glassblowing pipe",
    "Seed dibber", "Secateurs", "Watering can",
})


def sale_tax(unit_price: int, item_name: str | None = None) -> int:
    """Tax charged per item at the given sale price."""
    if item_name is not None and item_name in EXEMPT_ITEM_NAMES:
        return 0
    return min(int(unit_price * TAX_RATE), TAX_CAP)


def post_tax_proceeds(unit_price: int, item_name: str | None = None) -> int:
    """What the seller actually receives per item."""
    return unit_price - sale_tax(unit_price, item_name)


def flip_margin(buy_price: int, sell_price: int, item_name: str | None = None) -> int:
    """Per-item profit for buying at buy_price and selling at sell_price."""
    return post_tax_proceeds(sell_price, item_name) - buy_price
