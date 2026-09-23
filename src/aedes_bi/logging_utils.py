import logging
from contextvars import ContextVar


PIPELINE_STAGES = 8
_current_stage = ContextVar("pipeline_stage", default=f"0/{PIPELINE_STAGES}")


def format_number(value: int | float) -> str:
    """Formata contagens no padrão brasileiro, sem alterar valores de dados."""
    return f"{value:,}".replace(",", ".")


def log_stage(logger: logging.Logger, number: int, name: str, completed: bool = False) -> None:
    """Atualiza a etapa corrente e registra sua transição."""
    _current_stage.set(f"{number}/{PIPELINE_STAGES}")
    state = "concluída" if completed else "iniciada"
    logger.info("%s %s", name, state)


def current_stage() -> str:
    return _current_stage.get()
