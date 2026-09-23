import logging


PIPELINE_STAGES = 8


def format_number(value: int | float) -> str:
    """Formata contagens no padrão brasileiro, sem alterar valores de dados."""
    return f"{value:,}".replace(",", ".")


def log_stage(logger: logging.Logger, number: int, name: str, completed: bool = False) -> None:
    """Registra o avanço geral sem substituir o progresso interno das etapas."""
    completed_stages = number if completed else number - 1
    percentage = completed_stages / PIPELINE_STAGES * 100
    remaining = PIPELINE_STAGES - completed_stages
    state = "concluída" if completed else "iniciada"
    logger.info(
        "ETAPA %d/%d | %.1f%% geral | %s %s | faltam %d etapas",
        number,
        PIPELINE_STAGES,
        percentage,
        name,
        state,
        remaining,
    )
