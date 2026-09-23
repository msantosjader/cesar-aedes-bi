from __future__ import annotations

from pathlib import Path
import re
import logging
from typing import Iterable

import pandas as pd
import requests

from .logging_utils import format_number

logger = logging.getLogger("aedes_bi.extract")


class Extract:
    """Leitura bruta das fontes, sem aplicar regras de negócio."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or Path(__file__).resolve().parents[2])
        self.input_dir = self.root / "data" / "entradas"
        self.reference_dir = self.root / "data" / "referencia"
        self.boundary_path = self.reference_dir / "recife.geojson"

    def _read_sheet(self, path: Path, sheet: str, marker: Iterable[str]) -> pd.DataFrame | None:
        engine = "xlrd" if path.suffix.lower() == ".xls" else "openpyxl"
        raw = pd.read_excel(path, sheet_name=sheet, header=None, engine=engine)
        marker = [self._clean(value) for value in marker]
        header_index = None
        for index, row in raw.iterrows():
            values = {self._clean(value) for value in row.tolist()}
            if all(any(token in value for value in values) for token in marker):
                header_index = index
                break
        if header_index is None:
            logger.warning("cabeçalho não localizado | arquivo: %s | aba: %s", path.name, sheet)
            return None
        frame = raw.iloc[header_index + 1 :].copy()
        frame.columns = self._unique_columns(raw.iloc[header_index].tolist())
        frame = frame.dropna(how="all")
        frame["_arquivo_origem"] = str(path.relative_to(self.root))
        frame["_aba_origem"] = sheet
        frame["_linha_origem"] = frame.index.astype(int) + 1
        frame = frame.reset_index(drop=True)
        logger.debug("cabeçalho localizado | arquivo: %s | aba: %s | linha: %d", path.name, sheet, header_index + 1)
        logger.info("arquivo lido | arquivo: %s | aba: %s | registros: %s", path.name, sheet, format_number(len(frame)))
        return frame

    @staticmethod
    def _clean(value: object) -> str:
        return " ".join(str(value or "").strip().upper().split())

    @staticmethod
    def _unique_columns(values: list[object]) -> list[str]:
        columns: list[str] = []
        counts: dict[str, int] = {}
        for position, value in enumerate(values):
            name = "" if pd.isna(value) else str(value).strip()
            name = name or f"coluna_{position + 1}"
            counts[name] = counts.get(name, 0) + 1
            columns.append(name if counts[name] == 1 else f"{name}_{counts[name]}")
        return columns

    def _read_matching(self, paths: Iterable[Path], marker: Iterable[str]) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        path_list = list(paths)
        for path in sorted(path_list):
            engine = "xlrd" if path.suffix.lower() == ".xls" else "openpyxl"
            book = pd.ExcelFile(path, engine=engine)
            for sheet in book.sheet_names:
                frame = self._read_sheet(path, sheet, marker)
                if frame is not None:
                    frames.append(frame)
        result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        logger.info("extração consolidada | arquivos: %s | registros: %s", format_number(len(path_list)), format_number(len(result)))
        return result

    def read_edls(self) -> pd.DataFrame:
        path = self.input_dir / "TOTAL DE EDL POR DS.xlsx"
        book = pd.ExcelFile(path, engine="openpyxl")
        frames: list[pd.DataFrame] = []
        for sheet in book.sheet_names:
            if not self._clean(sheet).startswith("DS"):
                logger.info("aba auxiliar ignorada | arquivo: %s | aba: %s", path.name, sheet)
                continue
            frame = self._read_sheet(path, sheet, ["Nº", "RESP. PELO PE"])
            if frame is not None:
                frames.append(frame)
        result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        logger.info("extração EDL consolidada | abas: %s | registros: %s", format_number(len(frames)), format_number(len(result)))
        return result

    def read_ovt_locations(self) -> pd.DataFrame:
        path = self.input_dir / "Georreferenciamento OVT 2026 ATUALIZAÇÃO.xlsx"
        return self._read_matching([path], ["ID OVT", "LATITUDE", "LONGITUDE"])

    def read_ovt_observations(self, year: int | None = None) -> pd.DataFrame:
        years = [year] if year else [2024, 2025, 2026]
        paths: list[Path] = []
        frames: list[pd.DataFrame] = []
        for current_year in years:
            directory = self.input_dir / f"OVITRAMPAS {current_year}"
            for path in sorted(directory.glob("*.xls")):
                frames.extend(self._read_legacy_observations(path, current_year))
            for path in sorted(directory.glob("*.xlsx")):
                if "CONSOLIDADOS" in path.name.upper():
                    logger.info("arquivo auxiliar ignorado | arquivo: %s", path.name)
                    continue
                book = pd.ExcelFile(path, engine="openpyxl")
                for sheet in book.sheet_names:
                    if current_year == 2026 and sheet != "CICLO 1":
                        logger.info("aba auxiliar ignorada | arquivo: %s | aba: %s", path.name, sheet)
                        continue
                    frame = self._read_sheet(path, sheet, ["ID", "CICLO"])
                    if frame is not None:
                        frames.append(frame)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def _read_legacy_observations(self, path: Path, year: int) -> list[pd.DataFrame]:
        """Achata os grupos DATA/OVOS/OBS das planilhas XLS antigas."""
        engine = "xlrd"
        result: list[pd.DataFrame] = []
        book = pd.ExcelFile(path, engine=engine)
        for sheet in book.sheet_names:
            raw = pd.read_excel(path, sheet_name=sheet, header=None, engine=engine)
            header = next(
                (index for index, row in raw.iterrows() if any("ID. OVT" in self._clean(value) for value in row)),
                None,
            )
            if header is None:
                continue
            cycle_row = raw.iloc[header - 1].tolist()
            columns = raw.iloc[header].tolist()
            id_index = next(index for index, value in enumerate(columns) if "ID. OVT" in self._clean(value))
            cycle_starts = {
                index: int(re.search(r"\d+", self._clean(value)).group())
                for index, value in enumerate(cycle_row)
                if "CICLO" in self._clean(value)
            }
            records: list[dict[str, object]] = []
            for row_index, values in raw.iloc[header + 1 :].iterrows():
                identifier = values.iloc[id_index] if id_index < len(values) else None
                if pd.isna(identifier) or str(identifier).strip() == "":
                    continue
                base = {
                    "Ano": year,
                    "DS": self._district(path.name),
                    "BAIRRO": values.iloc[0] if len(values) else None,
                    "AGENTE / MATRICULA": values.iloc[1] if len(values) > 1 else None,
                    "QT": values.iloc[3] if len(values) > 3 else None,
                    "ID_OVT": identifier,
                    "_arquivo_origem": str(path.relative_to(self.root)),
                    "_aba_origem": sheet,
                    "_linha_origem": int(row_index) + 1,
                }
                for start, cycle in cycle_starts.items():
                    if start + 2 >= len(values):
                        continue
                    record = base | {
                        "CICLOS": cycle,
                        "DT_COLETA": values.iloc[start],
                        "N_OVOS": values.iloc[start + 1],
                        "STATUS": values.iloc[start + 2],
                    }
                    if not all(pd.isna(record[key]) for key in ("DT_COLETA", "N_OVOS", "STATUS")):
                        records.append(record)
            if records:
                result.append(pd.DataFrame(records))
                logger.info("arquivo lido | arquivo: %s | aba: %s | registros: %s", path.name, sheet, format_number(len(records)))
        return result

    @staticmethod
    def _district(filename: str) -> str | None:
        match = re.search(r"DS-([IVX]+)", filename.upper())
        return match.group(1) if match else None

    def download_recife_boundary(self) -> Path:
        if self.boundary_path.exists():
            logger.info("limite do Recife encontrado | arquivo: %s", self.boundary_path)
            return self.boundary_path
        self.reference_dir.mkdir(parents=True, exist_ok=True)
        url = "https://servicodados.ibge.gov.br/api/v3/malhas/municipios/2611606?formato=application/vnd.geo+json"
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        self.boundary_path.write_bytes(response.content)
        logger.info("limite do Recife baixado | arquivo: %s", self.boundary_path)
        return self.boundary_path
