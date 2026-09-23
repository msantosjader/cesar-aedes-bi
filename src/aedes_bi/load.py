from __future__ import annotations

import sqlite3
import logging
from pathlib import Path

import pandas as pd

from .logging_utils import format_number

logger = logging.getLogger("aedes_bi.load")


class Load:
    """Carga idempotente das tabelas tratadas e das auditorias."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or Path(__file__).resolve().parents[2])
        self.database_path = self.root / "database" / "aedes_bi.sqlite"
        self.audit_dir = self.root / "data" / "auditoria"

    def load_sqlite(
        self,
        edls: pd.DataFrame,
        locations: pd.DataFrame,
        observations: pd.DataFrame,
        cycles: pd.DataFrame,
        coordinate_audit: pd.DataFrame | None = None,
        date_audit: pd.DataFrame | None = None,
        record_audit: pd.DataFrame | None = None,
        id_audit: pd.DataFrame | None = None,
        geocode_inventory: pd.DataFrame | None = None,
        edl_name_dictionary: pd.DataFrame | None = None,
    ) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute('DROP TABLE IF EXISTS "observacoes_ovitrampas"')
            connection.execute('DROP TABLE IF EXISTS "ciclos"')
            connection.execute('DROP TABLE IF EXISTS "ovitrampas"')
            connection.execute('DROP TABLE IF EXISTS "edls"')
            self._replace_table(connection, "edls", edls, "id_edl TEXT PRIMARY KEY")
            self._replace_table(connection, "ovitrampas", locations, "id INTEGER PRIMARY KEY AUTOINCREMENT")
            self._replace_table(
                connection,
                "ciclos",
                cycles,
                "ano INTEGER NOT NULL, ciclo INTEGER NOT NULL, PRIMARY KEY (ano, ciclo)",
            )
            self._replace_table(
                connection,
                "observacoes_ovitrampas",
                observations,
                "id INTEGER PRIMARY KEY AUTOINCREMENT, FOREIGN KEY (ano, ciclo) REFERENCES ciclos (ano, ciclo)",
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_ovt_chave ON ovitrampas (id_ovt_chave)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_obs_ovt_ciclo ON observacoes_ovitrampas (id_ovt_chave, ano, ciclo)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_edls_distrito ON edls (distrito)")
            logger.info("tabelas SQLite carregadas | edls: %s | ovitrampas: %s | ciclos: %s | observações: %s", format_number(len(edls)), format_number(len(locations)), format_number(len(cycles)), format_number(len(observations)))
        (coordinate_audit if coordinate_audit is not None else pd.DataFrame()).to_csv(
            self.audit_dir / "auditoria_coordenadas.csv", index=False, encoding="utf-8-sig"
        )
        (date_audit if date_audit is not None else pd.DataFrame()).to_csv(
            self.audit_dir / "auditoria_datas.csv", index=False, encoding="utf-8-sig"
        )
        (record_audit if record_audit is not None else pd.DataFrame()).to_csv(
            self.audit_dir / "auditoria_registros.csv", index=False, encoding="utf-8-sig"
        )
        (id_audit if id_audit is not None else pd.DataFrame()).to_csv(
            self.audit_dir / "auditoria_ids.csv", index=False, encoding="utf-8-sig"
        )
        (geocode_inventory if geocode_inventory is not None else pd.DataFrame()).to_csv(
            self.root / "data" / "processados" / "inventario_geocodificacao.csv",
            index=False,
            encoding="utf-8-sig",
        )
        (edl_name_dictionary if edl_name_dictionary is not None else pd.DataFrame()).to_csv(
            self.root / "data" / "processados" / "nomes_locais_edl.csv",
            index=False,
            encoding="utf-8-sig",
        )
        logger.info("auditorias gravadas | diretório: %s", self.audit_dir)

    @staticmethod
    def _replace_table(connection: sqlite3.Connection, name: str, frame: pd.DataFrame, constraints: str) -> None:
        connection.execute(f'DROP TABLE IF EXISTS "{name}"')
        definitions: list[str] = []
        if name in {"ovitrampas", "observacoes_ovitrampas"}:
            definitions.append('"id" INTEGER PRIMARY KEY AUTOINCREMENT')
        for column in frame.columns:
            dtype = frame[column].dtype
            sql_type = "REAL" if pd.api.types.is_float_dtype(dtype) else "INTEGER" if pd.api.types.is_integer_dtype(dtype) else "TEXT"
            suffix = " PRIMARY KEY" if name == "edls" and column == "id_edl" else " NOT NULL" if name == "ciclos" and column in {"ano", "ciclo"} else ""
            definitions.append(f'"{column}" {sql_type}{suffix}')
        if name == "ciclos":
            definitions.append("PRIMARY KEY (ano, ciclo)")
        if name == "observacoes_ovitrampas":
            definitions.append("FOREIGN KEY (ano, ciclo) REFERENCES ciclos (ano, ciclo)")
        connection.execute(f'CREATE TABLE "{name}" ({", ".join(definitions)})')
        if not frame.empty:
            frame.to_sql(name, connection, if_exists="append", index=False)
