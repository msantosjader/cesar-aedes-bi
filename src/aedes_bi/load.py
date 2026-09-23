from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd


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
        (coordinate_audit if coordinate_audit is not None else pd.DataFrame()).to_csv(
            self.audit_dir / "auditoria_coordenadas.csv", index=False, encoding="utf-8-sig"
        )
        (date_audit if date_audit is not None else pd.DataFrame()).to_csv(
            self.audit_dir / "auditoria_datas.csv", index=False, encoding="utf-8-sig"
        )

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
