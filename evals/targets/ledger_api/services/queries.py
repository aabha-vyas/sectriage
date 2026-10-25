"""SQL fragments shared by the data layer."""

SORTABLE_COLUMNS = ("id", "customer", "amount_cents", "created")


def build_order_clause(sort: str, descending: bool = False) -> str:
    direction = "DESC" if descending else "ASC"
    return f" ORDER BY {sort} {direction}"


def checked_order_clause(sort: str, descending: bool = False) -> str:
    if sort not in SORTABLE_COLUMNS:
        raise ValueError("unsupported sort column")
    return build_order_clause(sort, descending)
