def format_number(value: int | float) -> str:
    """Formata contagens no padrão brasileiro, sem alterar valores de dados."""
    return f"{value:,}".replace(",", ".")
