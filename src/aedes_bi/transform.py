from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point


class Transform:
    """Padronização, validação e auditoria dos dados extraídos."""

    def __init__(self, boundary_path: str | Path | None = None) -> None:
        self.boundary_path = Path(boundary_path) if boundary_path else None
        self.coordinate_audit: list[dict[str, Any]] = []
        self.date_audit: list[dict[str, Any]] = []
        self._boundary = None
        self._boundary_metric = None
        if self.boundary_path and self.boundary_path.exists():
            boundary = gpd.read_file(self.boundary_path).to_crs(4326)
            self._boundary = boundary.geometry.union_all()
            self._boundary_metric = gpd.GeoSeries([self._boundary], crs=4326).to_crs(31985).iloc[0]

    @staticmethod
    def _norm(value: object) -> str:
        text = unicodedata.normalize("NFKD", str(value or ""))
        text = "".join(char for char in text if not unicodedata.combining(char))
        return re.sub(r"[^A-Z0-9]+", "", text.upper())

    @staticmethod
    def _text(value: object) -> str | None:
        if pd.isna(value) or str(value).strip() == "":
            return None
        return str(value).strip()

    @staticmethod
    def _number(value: object) -> float | None:
        if pd.isna(value) or str(value).strip() == "":
            return None
        text = str(value).strip().replace(" ", "")
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
        else:
            text = text.replace(",", ".")
        match = re.search(r"[-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?", text)
        return float(match.group()) if match else None

    def _date(self, value: object, row: pd.Series, field: str) -> str | None:
        original = self._text(value)
        if original is None:
            self._audit_date(row, field, original, None, "sem_valor", "ausente", "data ausente")
            return None
        try:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                parsed = pd.to_datetime(value, unit="D", origin="1899-12-30")
            else:
                parsed = pd.to_datetime(value, dayfirst=True, errors="raise")
            result = parsed.strftime("%Y-%m-%d")
            quality = "alta"
            rule = "conversao_excel" if isinstance(value, (int, float)) else "conversao_data"
        except (TypeError, ValueError, OverflowError):
            result, quality, rule = None, "invalida", "sem_conversao"
        if quality != "alta":
            self._audit_date(row, field, original, result, rule, quality, "data inválida")
        return result

    def _audit_date(self, row: pd.Series, field: str, original: object, treated: object, rule: str, quality: str, reason: str) -> None:
        self.date_audit.append({
            "campo": field,
            "valor_original": original,
            "valor_tratado": treated,
            "regra_data": rule,
            "qualidade_data": quality,
            "arquivo_origem": row.get("_arquivo_origem"),
            "aba_origem": row.get("_aba_origem"),
            "linha_origem": row.get("_linha_origem"),
            "motivo_revisao": reason,
        })

    @staticmethod
    def _find_column(frame: pd.DataFrame, *parts: str) -> str | None:
        for column in frame.columns:
            normalized = Transform._norm(column)
            if all(part in normalized for part in parts):
                return column
        return None

    def _coordinate(self, value: object) -> float | None:
        if pd.isna(value) or str(value).strip() == "":
            return None
        text = str(value).strip().upper().replace(",", ".")
        dms = re.search(r"(\d+(?:\.\d+)?)\D+(\d+(?:\.\d+)?)['’]?\D+(\d+(?:\.\d+)?)", text)
        if dms:
            result = float(dms.group(1)) + float(dms.group(2)) / 60 + float(dms.group(3)) / 3600
        else:
            result = self._number(text)
        if result is None:
            return None
        if "S" in text or "W" in text or "O" in text:
            result = -abs(result)
        return result

    def _coordinates(self, latitude: object, longitude: object, row: pd.Series) -> tuple[float | None, float | None, str]:
        original_lat, original_lon = self._text(latitude), self._text(longitude)
        lat, lon = self._coordinate(latitude), self._coordinate(longitude)
        rule = "sem_alteracao"
        if lat is not None and lon is not None:
            lat = self._fit_coordinate_scale(lat, 5, 10)
            lon = self._fit_coordinate_scale(lon, 30, 40)
            lat_range = 5 <= abs(lat) <= 10
            lon_range = 30 <= abs(lon) <= 40
            swapped_lat_range = 5 <= abs(lon) <= 10
            swapped_lon_range = 30 <= abs(lat) <= 40
            if swapped_lon_range and swapped_lat_range:
                lat, lon, rule = lon, lat, "latitude_longitude_trocadas"
            if (lat_range and lon_range) and (lat > 0 or lon > 0):
                lat, lon = -abs(lat), -abs(lon)
                rule = "sinais_recife" if rule == "sem_alteracao" else rule
        if lat is not None and lon is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180):
            rule = "faixa_invalida"
            lat, lon = None, None
        elif lat is not None and lon is not None and not (5 <= abs(lat) <= 10 and 30 <= abs(lon) <= 40):
            rule = "faixa_invalida"
            lat, lon = None, None
        if rule != "sem_alteracao" or lat is None or lon is None:
            self.coordinate_audit.append({
                "latitude_original": original_lat,
                "longitude_original": original_lon,
                "latitude_tratada": lat,
                "longitude_tratada": lon,
                "regra_aplicada": rule,
                "confianca": "alta" if rule in {"sinais_recife", "latitude_longitude_trocadas"} else "revisao",
                "arquivo_origem": row.get("_arquivo_origem"),
                "aba_origem": row.get("_aba_origem"),
                "linha_origem": row.get("_linha_origem"),
                "motivo_revisao": "coordenada ausente, inválida ou corrigida",
            })
        return lat, lon, rule

    @staticmethod
    def _fit_coordinate_scale(value: float, minimum: float, maximum: float) -> float:
        if minimum <= abs(value) <= maximum:
            return value
        for power in range(1, 10):
            candidate = value / (10**power)
            if minimum <= abs(candidate) <= maximum:
                return candidate
        return value

    def _geography(self, latitude: float | None, longitude: float | None) -> tuple[str, float | None]:
        if latitude is None or longitude is None:
            return "sem_coordenada", None
        if self._boundary is None:
            return "limite_indisponivel", None
        point = Point(longitude, latitude)
        if self._boundary.covers(point):
            return "dentro_recife", 0.0
        distance = gpd.GeoSeries([point], crs=4326).to_crs(31985).iloc[0].distance(self._boundary_metric)
        return ("fora_recife_proxima" if distance <= 500 else "fora_recife"), float(distance)

    @staticmethod
    def _source(row: pd.Series) -> dict[str, Any]:
        return {str(key): (None if pd.isna(value) else value) for key, value in row.items() if not str(key).startswith("_")}

    def transform_edls(self, frame: pd.DataFrame) -> pd.DataFrame:
        records = []
        for _, row in frame.iterrows():
            coordinate_column = next((c for c in frame.columns if self._norm(c) in {"LATITUDE", "COORDENADAS"}), None)
            coordinate_index = frame.columns.get_loc(coordinate_column) if coordinate_column else -1
            lat_value = row.get(coordinate_column) if coordinate_column else None
            lon_column = next((c for c in frame.columns if self._norm(c) == "LONGITUDE"), None)
            lon_value = row.get(lon_column) if lon_column else None
            if lon_value is None and coordinate_index >= 0 and coordinate_index + 1 < len(frame.columns):
                lon_value = row.get(frame.columns[coordinate_index + 1])
            candidates: list[tuple[object, object]] = []
            for value in row.iloc[: len(frame.columns) - 3].tolist():
                parts = str(value).split(",")
                if len(parts) == 2:
                    candidates.append((parts[0], parts[1]))
            for index in range(len(frame.columns) - 3):
                candidates.append((row.iloc[index], row.iloc[index + 1]))
            for candidate_lat, candidate_lon in candidates:
                parsed_lat = self._coordinate(candidate_lat)
                parsed_lon = self._coordinate(candidate_lon)
                if parsed_lat is not None:
                    parsed_lat = self._fit_coordinate_scale(parsed_lat, 5, 10)
                if parsed_lon is not None:
                    parsed_lon = self._fit_coordinate_scale(parsed_lon, 30, 40)
                if parsed_lat is not None and parsed_lon is not None and (
                    5 <= abs(parsed_lat) <= 10 and 30 <= abs(parsed_lon) <= 40
                    or 30 <= abs(parsed_lat) <= 40 and 5 <= abs(parsed_lon) <= 10
                ):
                    lat_value, lon_value = candidate_lat, candidate_lon
                    break
            lat, lon, _ = self._coordinates(lat_value, lon_value, row)
            status, distance = self._geography(lat, lon)
            district = re.search(r"DS\s*[- ]?\s*([IVX]+)", str(row.get("_aba_origem", "")), re.I)
            records.append({
                "id_edl": f"{row.get('_arquivo_origem')}:{row.get('_aba_origem')}:{row.get('_linha_origem')}",
                "distrito": district.group(1) if district else None,
                "identificacao_original": self._text(row.get("Nº")),
                "responsavel": self._text(row.get("RESP. PELO PE")),
                "tipo_pe": self._text(row.get("TIPO PE.")),
                "nome_local": self._text(row.get("NOME FANTASIA/COMERCIAL")),
                "endereco": self._text(row.get("LOGRADOURO")),
                "bairro": self._text(row.get("BAIRRO")),
                "latitude_original": self._text(lat_value),
                "longitude_original": self._text(lon_value),
                "latitude": lat,
                "longitude": lon,
                "classificacao_geografica": status,
                "distancia_limite_m": distance,
                "dados_originais": json.dumps(self._source(row), ensure_ascii=False, default=str),
                "arquivo_origem": row.get("_arquivo_origem"),
                "aba_origem": row.get("_aba_origem"),
                "linha_origem": row.get("_linha_origem"),
            })
        return pd.DataFrame(records)

    def transform_locations(self, frame: pd.DataFrame) -> pd.DataFrame:
        id_column = self._find_column(frame, "ID", "OVT") or "ID OVT"
        lat_column = self._find_column(frame, "LATITUDE") or "LATITUDE"
        lon_column = self._find_column(frame, "LONGITUDE") or "LONGITUDE"
        records = []
        for _, row in frame.iterrows():
            lat, lon, _ = self._coordinates(row.get(lat_column), row.get(lon_column), row)
            status, distance = self._geography(lat, lon)
            original_id = self._text(row.get(id_column))
            district = re.search(r"DS\s+([IVX]+)", str(row.get("_aba_origem", "")), re.I)
            records.append({
                "id_ovt_original": original_id,
                "id_ovt_chave": self._norm(original_id),
                "distrito": district.group(1) if district else None,
                "bairro": self._text(row.get("BAIRRO")),
                "endereco": self._text(row.get("ENDEREÇO")),
                "latitude_original": self._text(row.get(lat_column)),
                "longitude_original": self._text(row.get(lon_column)),
                "latitude": lat,
                "longitude": lon,
                "classificacao_geografica": status,
                "distancia_limite_m": distance,
                "dados_originais": json.dumps(self._source(row), ensure_ascii=False, default=str),
                "arquivo_origem": row.get("_arquivo_origem"),
                "aba_origem": row.get("_aba_origem"),
                "linha_origem": row.get("_linha_origem"),
            })
        result = pd.DataFrame(records)
        if not result.empty:
            duplicates = result["id_ovt_chave"].duplicated(keep=False) & result["id_ovt_chave"].ne("")
            for _, item in result[duplicates].iterrows():
                self.coordinate_audit.append({
                    "latitude_original": item["id_ovt_original"],
                    "longitude_original": item["id_ovt_chave"],
                    "latitude_tratada": None,
                    "longitude_tratada": None,
                    "regra_aplicada": "colisao_id_normalizado",
                    "confianca": "revisao",
                    "arquivo_origem": item["arquivo_origem"],
                    "aba_origem": item["aba_origem"],
                    "linha_origem": item["linha_origem"],
                    "motivo_revisao": "IDs diferentes geraram a mesma chave",
                })
        return result

    def transform_observations(self, frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        if frame.empty:
            return pd.DataFrame(), pd.DataFrame()
        id_column = self._find_column(frame, "ID", "OVT") or self._find_column(frame, "ID")
        year_column = self._find_column(frame, "ANO")
        cycle_column = self._find_column(frame, "CICLO")
        date_columns = [column for column in frame.columns if self._norm(column).startswith("DT") or "DATA" in self._norm(column)]
        observations = []
        cycles = {}
        for _, row in frame.iterrows():
            year = self._number(row.get(year_column)) if year_column else None
            cycle = self._number(row.get(cycle_column)) if cycle_column else None
            year = int(year) if year is not None else None
            cycle = int(cycle) if cycle is not None else None
            cycle_key = (year, cycle)
            parsed_dates = {self._norm(column): self._date(row.get(column), row, str(column)) for column in date_columns}
            if cycle_key not in cycles:
                cycles[cycle_key] = {
                    "ano": year, "ciclo": cycle,
                    "data_inicio": next((value for key, value in parsed_dates.items() if "COLETA" in key), None),
                    "data_fim": next((value for key, value in parsed_dates.items() if "LEITURA" in key), None),
                    "data_referencia": next(iter(parsed_dates.values()), None),
                }
            observations.append({
                "ano": year,
                "ciclo": cycle,
                "id_ovt_original": self._text(row.get(id_column)) if id_column else None,
                "id_ovt_chave": self._norm(row.get(id_column)) if id_column else None,
                "data_coleta": next((value for key, value in parsed_dates.items() if "COLETA" in key), None),
                "quantidade_ovos": self._number(row.get(next((c for c in frame.columns if "OVOS" in self._norm(c)), "__missing__"))),
                "quantidade_palhetas": self._number(row.get(next((c for c in frame.columns if "PALHETA" in self._norm(c)), "__missing__"))),
                "status": self._text(row.get(next((c for c in frame.columns if self._norm(c) in {"STATUS", "SITUACAO"}), "__missing__"))),
                "dados_originais": json.dumps(self._source(row), ensure_ascii=False, default=str),
                "arquivo_origem": row.get("_arquivo_origem"),
                "aba_origem": row.get("_aba_origem"),
                "linha_origem": row.get("_linha_origem"),
            })
        return pd.DataFrame(observations), pd.DataFrame(cycles.values())

    def audits(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        return pd.DataFrame(self.coordinate_audit), pd.DataFrame(self.date_audit)
