from __future__ import annotations

from datetime import datetime
import argparse
import logging
import sys
from pathlib import Path
from time import monotonic

from aedes_bi.extract import Extract
from aedes_bi.load import Load
from aedes_bi.logging_utils import current_stage, format_number, log_stage
from aedes_bi.transform import Transform


SUCCESS = 25
logging.addLevelName(SUCCESS, "SUCESSO")


class PipelineFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__()
        self.started = monotonic()

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created).strftime("%d/%m/%Y %H:%M:%S")
        elapsed = int(monotonic() - self.started)
        phase = {
            "extract": "EXTRACT",
            "transform": "TRANSFORM",
            "load": "LOAD",
        }.get(record.name.rsplit(".", 1)[-1], "PIPELINE")
        level = {
            logging.WARNING: "AVISO",
            logging.ERROR: "ERRO",
            logging.CRITICAL: "FATAL",
            SUCCESS: "SUCESSO",
        }.get(record.levelno, "INFO")
        return f"{timestamp} | {elapsed // 3600:02d}:{elapsed % 3600 // 60:02d}:{elapsed % 60:02d} | {current_stage()} | {phase:<9} | {level:<7} | {record.getMessage()}"


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("aedes_bi")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = PipelineFormatter()
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(log_dir / "pipeline.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


def main(geocode: bool = True) -> None:
    logger = configure_logging()
    extractor = Extract()
    try:
        log_stage(logger, 1, "extração")
        logger.info("EXTRAÇÃO iniciada")
        boundary = extractor.download_recife_boundary()
        transformer = Transform(boundary, geocode=geocode)
        if geocode:
            logger.info("geocodificação automática ativada | provedor: Nominatim/OpenStreetMap")
        loader = Load()

        raw_edls = extractor.read_edls()
        raw_locations = extractor.read_ovt_locations()
        raw_observations = extractor.read_ovt_observations()
        logger.log(SUCCESS, "EXTRAÇÃO concluída | EDLs: %s | localizações: %s | observações brutas: %s", format_number(len(raw_edls)), format_number(len(raw_locations)), format_number(len(raw_observations)))
        log_stage(logger, 1, "extração", completed=True)

        logger.info("TRANSFORMAÇÃO iniciada")
        edls = transformer.transform_edls(raw_edls)
        locations = transformer.transform_locations(raw_locations)
        observations, cycles = transformer.transform_observations(raw_observations)
        transformer.reconcile_ovt_ids(locations, observations)
        logger.log(SUCCESS, "TRANSFORMAÇÃO concluída | locais EDL: %s | ovitrampas: %s | observações: %s | ciclos: %s", format_number(len(edls)), format_number(len(locations)), format_number(len(observations)), format_number(len(cycles)))
        coordinate_audit, date_audit, record_audit, id_audit = transformer.audits()
        geocode_inventory = transformer.geocode_inventory_frame()
        logger.info("AUDITORIAS preparadas | coordenadas: %s | datas: %s | registros: %s | IDs: %s", format_number(len(coordinate_audit)), format_number(len(date_audit)), format_number(len(record_audit)), format_number(len(id_audit)))

        log_stage(logger, 8, "carga")
        logger.info("CARGA iniciada | banco: %s", loader.database_path)
        loader.load_sqlite(edls, locations, observations, cycles, coordinate_audit, date_audit, record_audit, id_audit, geocode_inventory)
        logger.log(SUCCESS, "CARGA concluída | banco e auditorias atualizados")
        log_stage(logger, 8, "carga", completed=True)
    except Exception:
        logger.exception("PIPELINE interrompido por erro")
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Executa o pipeline aedes-bi")
    parser.add_argument("--geocodificar", dest="geocode", action="store_true", help="mantido por compatibilidade; já é o comportamento padrão")
    parser.add_argument("--sem-geocodificar", dest="geocode", action="store_false", help="não consulta endereços externos")
    parser.set_defaults(geocode=True)
    main(geocode=parser.parse_args().geocode)
