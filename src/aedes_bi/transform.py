from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import Point

from .logging_utils import format_number

logger = logging.getLogger("aedes_bi.transform")


class Transform:
    """Padronização, validação e auditoria dos dados extraídos."""

    def __init__(self, boundary_path: str | Path | None = None, geocode: bool = False) -> None:
        self.boundary_path = Path(boundary_path) if boundary_path else None
        self.coordinate_audit: list[dict[str, Any]] = []
        self.date_audit: list[dict[str, Any]] = []
        self.record_audit: list[dict[str, Any]] = []
        self.id_audit: list[dict[str, Any]] = []
        self.geocode_inventory: list[dict[str, Any]] = []
        self.edl_name_keys: set[str] = set()
        self.edl_name_counts: Counter[str] = Counter()
        self.geocode_enabled = geocode
        self.geocode_cache_path = self.boundary_path.parent.parent / "processados" / "geocodificacao_cache.json" if self.boundary_path else None
        self.geocode_cache: dict[str, dict[str, Any]] = {}
        self.geocode_cache_hits = 0
        self.geocode_http_requests = 0
        self.name_dictionary_path = self.geocode_cache_path.parent / "nomes_locais_edl.csv" if self.geocode_cache_path else None
        self._last_geocode_request = 0.0
        if self.geocode_cache_path and self.geocode_cache_path.exists():
            self.geocode_cache = json.loads(self.geocode_cache_path.read_text(encoding="utf-8"))
        if self.name_dictionary_path and self.name_dictionary_path.exists():
            names = pd.read_csv(self.name_dictionary_path, encoding="utf-8-sig")
            name_column = "nome_chave" if "nome_chave" in names else "nome_local"
            self.edl_name_keys.update(self._norm(value) for value in names[name_column].dropna())
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

    @staticmethod
    def _status(value: object) -> tuple[str | None, str | None, str]:
        original = Transform._text(value)
        if original is None:
            return None, None, "ausente"
        normalized = original.upper().replace(" ", "")
        descriptions = {
            "F": "Fechado",
            "E": "Extraviado",
            "R": "Recusado",
            "D": "Desocupado",
            "REC": "Recuperada",
        }
        code = normalized
        quality = "alta"
        if re.fullmatch(r"\.?REC(?:-20\d{2})?", normalized):
            code = "REC"
            quality = "corrigida_alta" if normalized != "REC" else "alta"
        if code not in descriptions:
            return None, None, "invalida"
        return code, descriptions[code], quality

    @staticmethod
    def _parse_date(value: object) -> pd.Timestamp | None:
        if value is None or (not isinstance(value, (list, tuple)) and pd.isna(value)) or str(value).strip() == "":
            return None
        try:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return pd.to_datetime(value, unit="D", origin="1899-12-30")
            return pd.to_datetime(value, dayfirst=True, errors="raise")
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _expected_date_year(row: pd.Series, year: int | None, cycle: int | None, parsed: pd.Timestamp | None) -> int | None:
        if parsed is None:
            return year
        source = re.search(r"OVITRAMPAS\s+(202[456])", str(row.get("_arquivo_origem", "")), re.I)
        source_year = int(source.group(1)) if source else year
        if source_year is None:
            return None
        if cycle is not None and cycle >= 24 and parsed.year == source_year + 1 and parsed.month <= 2:
            return source_year + 1
        return source_year

    @staticmethod
    def _cycle_expected_year(anchor: pd.Timestamp | None, cycle: int | None, parsed: pd.Timestamp | None) -> int | None:
        if anchor is None or cycle is None or parsed is None or cycle < 1:
            return None
        start = anchor + timedelta(days=15 * (cycle - 1)) - timedelta(days=10)
        end = anchor + timedelta(days=15 * cycle) + timedelta(days=10)
        candidates = []
        for year in (parsed.year - 1, parsed.year, parsed.year + 1):
            try:
                candidate = parsed.replace(year=year)
            except ValueError:
                continue
            if start <= candidate <= end:
                candidates.append(year)
        return candidates[0] if len(candidates) == 1 else None
        try:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return pd.to_datetime(value, unit="D", origin="1899-12-30")
            return pd.to_datetime(value, dayfirst=True, errors="raise")
        except (TypeError, ValueError, OverflowError):
            return None

    def _date_info(
        self,
        value: object,
        row: pd.Series,
        field: str,
        expected_year: int | None = None,
        inferred: pd.Timestamp | None = None,
        allow_post_cycle: bool = False,
    ) -> tuple[str | None, str, str]:
        original = self._text(value)
        if original is None:
            return None, "sem_valor", "ausente"
        parsed = inferred or self._parse_date(value)
        if parsed is None:
            return None, "sem_conversao", "invalida"
        rule = "data_inferida_por_aba_coluna_ciclo" if inferred is not None else "conversao_excel" if isinstance(value, (int, float)) else "conversao_data"
        quality = "corrigida_alta" if inferred is not None else "alta"
        if allow_post_cycle and expected_year and parsed.year > expected_year:
            expected_year = None
            rule = "data_posterior_status_rec"
            quality = "aceita_contexto"
        if expected_year and parsed.year != expected_year and abs(parsed.year - expected_year) == 1:
            parsed = parsed.replace(year=expected_year)
            rule = "ano_ajustado_por_arquivo_e_ciclo"
            quality = "corrigida_alta"
        elif expected_year and parsed.year != expected_year:
            return None, "ano_incompativel", "invalida"
        result = parsed.strftime("%Y-%m-%d")
        if quality.startswith("corrigida"):
            reason = "data incompleta confirmada por aba, coluna e ciclo" if inferred is not None else "ano incompatível com a sequência do ciclo"
            self._audit_date(row, field, original, result, rule, quality, reason)
        return result, rule, quality

    def _audit_date(self, row: pd.Series, field: str, original: object, treated: object, rule: str, quality: str, reason: str) -> None:
        self.date_audit.append({
            "tipo_registro": "observacao_ovt",
            "id_ovt_original": row.get("ID_OVT") or row.get("ID_OVT"),
            "id_ovt_chave": self._norm(row.get("ID_OVT")) if row.get("ID_OVT") else None,
            "ano": row.get("Ano"),
            "ciclo": row.get("CICLOS"),
            "campo": field,
            "coluna_origem": field,
            "status_original": row.get("_status_original"),
            "status_codigo": row.get("_status_codigo"),
            "status_descricao": row.get("_status_descricao"),
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
    def _coordinate_context(row: pd.Series) -> dict[str, Any]:
        return {
            "tipo_registro": row.get("_tipo_registro"),
            "id_edl": row.get("_id_edl"),
            "id_ovt_original": row.get("_id_ovt_original"),
            "id_ovt_chave": row.get("_id_ovt_chave"),
            "identificacao_original": row.get("_identificacao_original"),
            "distrito": row.get("_distrito"),
            "tipo_pe": row.get("_tipo_pe"),
            "nome_local": row.get("_nome_local"),
            "endereco": row.get("_endereco"),
            "bairro": row.get("_bairro"),
            "coluna_latitude_origem": row.get("_coluna_latitude_origem"),
            "coluna_longitude_origem": row.get("_coluna_longitude_origem"),
            "valor_original_coordenadas": row.get("_valor_original_coordenadas"),
        }

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
        text = str(value).strip().upper()
        if any(letter not in {"N", "S", "E", "W", "O"} for letter in re.findall(r"[A-Z]", text)):
            return None
        has_dms_marker = bool(re.search(r"[°º'\"′″]", text))
        if not has_dms_marker:
            text = re.sub(r"\s+", "", text)
            if text.count(",") > 1 and "." not in text:
                first, *rest = text.split(",")
                text = f"{first}.{''.join(rest)}"
            elif text.count(".") > 1 and "," not in text:
                first, *rest = text.split(".")
                text = f"{first}.{''.join(rest)}"
            else:
                text = text.replace(",", ".")
        dms = re.search(r"(\d+(?:\.\d+)?)\D+(\d+(?:\.\d+)?)['’]?\D+(\d+(?:\.\d+)?)", text) if has_dms_marker else None
        if dms:
            result = float(dms.group(1)) + float(dms.group(2)) / 60 + float(dms.group(3)) / 3600
        else:
            result = self._number(text)
        if result is None:
            return None
        if "S" in text or "W" in text or "O" in text:
            result = -abs(result)
        return result

    @staticmethod
    def _fragmented_decimal(value: object) -> bool:
        text = str(value or "")
        return "." in text and text.count(".") > 1 and not re.search(r"[°º'\"′″]", text)

    def _coordinates(self, latitude: object, longitude: object, row: pd.Series) -> tuple[float | None, float | None, str]:
        original_lat, original_lon = self._text(latitude), self._text(longitude)
        lat, lon = self._coordinate(latitude), self._coordinate(longitude)
        rule = "numero_decimal_fragmentado" if self._fragmented_decimal(latitude) or self._fragmented_decimal(longitude) else "sem_alteracao"
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
                rule = "sinais_recife" if rule == "sem_alteracao" else f"{rule}_e_sinais_recife"
        if lat is not None and lon is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180):
            rule = "faixa_invalida"
            lat, lon = None, None
        elif lat is not None and lon is not None and not (5 <= abs(lat) <= 10 and 30 <= abs(lon) <= 40):
            rule = "faixa_invalida"
            lat, lon = None, None
        if lat is None or lon is None:
            if not original_lat and not original_lon:
                rule = "coordenada_ausente"
            elif not original_lat or not original_lon:
                rule = "coordenada_incompleta"
        if rule != "sem_alteracao" or lat is None or lon is None:
            reason = {
                "coordenada_ausente": "latitude e longitude não informadas na fonte",
                "coordenada_incompleta": "apenas uma coordenada foi informada na fonte",
                "faixa_invalida": "coordenada fora das faixas válidas",
                "sinais_recife": "sinais ajustados para o hemisfério do Recife",
                "numero_decimal_fragmentado": "pontos decimais fragmentados foram unidos",
                "numero_decimal_fragmentado_e_sinais_recife": "pontos decimais unidos e sinais ajustados para o hemisfério do Recife",
                "latitude_longitude_trocadas": "latitude e longitude estavam invertidas",
            }.get(rule, "coordenada corrigida ou requer revisão")
            audit = {
                "latitude_original": original_lat,
                "longitude_original": original_lon,
                "latitude_tratada": lat,
                "longitude_tratada": lon,
                "regra_aplicada": rule,
                "confianca": "alta" if rule in {"sinais_recife", "numero_decimal_fragmentado", "numero_decimal_fragmentado_e_sinais_recife", "latitude_longitude_trocadas"} else "revisao",
                "arquivo_origem": row.get("_arquivo_origem"),
                "aba_origem": row.get("_aba_origem"),
                "linha_origem": row.get("_linha_origem"),
                "motivo_revisao": reason,
            }
            audit.update(self._coordinate_context(row))
            self.coordinate_audit.append(audit)
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
        return ("fora_recife_proxima" if distance <= 1000 else "fora_recife"), float(distance)

    def _geocode_address(self, name: str | None, address: str | None, bairro: str | None) -> dict[str, Any]:
        queries = self._geocode_queries(name, address, bairro)
        if not queries:
            return {
                "resultado": "rejeitada",
                "status_geocodificacao": "endereco_ausente",
                "motivo": "nome, endereço e bairro ausentes",
                "query": None,
                "tentativas_geocodificacao": 0,
            }
        key = self._norm("|".join(queries))
        if key in self.geocode_cache:
            self.geocode_cache_hits += 1
            cached = dict(self.geocode_cache[key])
            cached.setdefault("status_geocodificacao", "sucesso" if cached.get("resultado") == "aceita" else "sem_sucesso")
            cached.setdefault("tentativas_geocodificacao", 0)
            cached.setdefault("consultas_geocodificacao", cached.get("query"))
            return cached
        attempts = []
        last_error = None
        result = None
        name_queries = {
            query for query in queries
            if name and query == name
            or name and query.startswith(f"{name},") and (not address or address not in query)
        }
        for query in queries:
            wait = 1.0 - (time.monotonic() - self._last_geocode_request)
            if wait > 0:
                time.sleep(wait)
            attempts.append(query)
            try:
                self.geocode_http_requests += 1
                response = requests.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={
                        "q": query,
                        "format": "jsonv2",
                        "addressdetails": 1,
                        "limit": 5 if query in name_queries else 1,
                        "countrycodes": "br",
                        "accept-language": "pt-BR",
                    },
                    headers={"User-Agent": "aedes-bi/0.1"},
                    timeout=30,
                )
                self._last_geocode_request = time.monotonic()
                response.raise_for_status()
                results = response.json()
            except requests.RequestException as exc:
                last_error = str(exc)
                continue
            if not results:
                continue
            for item in results:
                if not self._valid_geocode_result(item, name, bairro, query in name_queries):
                    continue
                result = {
                    "latitude": float(item["lat"]),
                    "longitude": float(item["lon"]),
                    "display_name": item.get("display_name"),
                    "importancia": item.get("importance"),
                    "resultado": "aceita",
                    "status_geocodificacao": "sucesso",
                    "motivo": None,
                    "query": query,
                    "provedor": "Nominatim/OpenStreetMap",
                    "estrategia_geocodificacao": "nome_local" if query in name_queries else "endereco",
                }
                break
            if result is not None:
                break
        if result is None:
            result = {
                "resultado": "rejeitada",
                "status_geocodificacao": "erro_consulta" if last_error and len(attempts) == 1 else "sem_sucesso",
                "motivo": last_error or "endereço não encontrado",
                "query": attempts[-1] if attempts else None,
                "provedor": "Nominatim/OpenStreetMap",
            }
        result["tentativas_geocodificacao"] = len(attempts)
        result["consultas_geocodificacao"] = " | ".join(attempts)
        self.geocode_cache[key] = result
        if self.geocode_cache_path:
            self.geocode_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.geocode_cache_path.write_text(json.dumps(self.geocode_cache, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def _valid_geocode_result(
        self,
        item: dict[str, Any],
        name: str | None,
        bairro: str | None,
        name_query: bool,
    ) -> bool:
        address_data = item.get("address", {})
        city = self._norm(address_data.get("city") or address_data.get("municipality") or address_data.get("town"))
        if city != "RECIFE":
            return False
        if not name_query:
            return True
        returned_name = self._norm(
            address_data.get("amenity")
            or address_data.get("name")
            or item.get("name")
            or item.get("display_name")
        )
        if not name or self._norm(name) not in returned_name:
            return False
        returned_bairro = self._norm(
            address_data.get("suburb")
            or address_data.get("city_district")
            or address_data.get("district")
        )
        return bool(bairro and returned_bairro and returned_bairro == self._norm(bairro))

    def _geocode_queries(self, name: str | None, address: str | None, bairro: str | None) -> list[str]:
        parts = self._address_parts(address)
        base_address = parts["endereco_sem_complemento"]
        queries = []
        if address and bairro:
            queries.extend([
                f"{address}, {bairro}, Recife, Pernambuco, Brasil",
                f"{address}, {bairro}, Recife",
            ])
            if base_address and base_address != address:
                queries.extend([
                    f"{base_address}, {bairro}, Recife, Pernambuco, Brasil",
                    f"{base_address}, {bairro}, Recife",
                ])
        if address:
            queries.append(f"{address}, Recife")
            if base_address and base_address != address:
                queries.append(f"{base_address}, Recife")
        if name:
            queries.extend([
                name,
                f"{name}, {bairro}, Recife, Pernambuco, Brasil" if bairro else f"{name}, Recife, Pernambuco, Brasil",
                f"{name}, {bairro}, Recife" if bairro else f"{name}, Recife",
                f"{name}, Recife",
            ])
        return list(dict.fromkeys(query for query in queries if query))

    @staticmethod
    def _address_parts(address: str | None) -> dict[str, str | None]:
        original = Transform._text(address)
        if not original:
            return {"logradouro": None, "numero": None, "complemento": None, "endereco_sem_complemento": None}
        text = re.sub(r"\s+", " ", original).strip(" ,")
        match = re.search(r",\s*(\d+[A-Z]?)(?:\s+(.*))?$", text, re.I)
        if not match:
            match = re.search(r"\s+(\d+[A-Z]?)(?:\s+(.*))?$", text, re.I)
        if not match:
            return {"logradouro": text, "numero": None, "complemento": None, "endereco_sem_complemento": text}
        logradouro = text[:match.start()].strip(" ,")
        numero = match.group(1).upper()
        complemento = Transform._text(match.group(2))
        endereco_sem_complemento = f"{logradouro}, {numero}"
        return {
            "logradouro": logradouro,
            "numero": numero,
            "complemento": complemento,
            "endereco_sem_complemento": endereco_sem_complemento,
        }

    def _audit_geography_review(self, record: dict[str, Any], status: str, distance: float | None) -> None:
        if not status.startswith("fora_recife") or distance is None:
            return
        decision = "revisao_manual_ate_1km" if distance <= 1000 else "candidato_geocodificacao"
        self.coordinate_audit.append({
            "tipo_registro": "edl" if record.get("id_edl") else "ovt",
            "id_edl": record.get("id_edl"),
            "id_ovt_original": record.get("id_ovt_original"),
            "id_ovt_chave": record.get("id_ovt_chave"),
            "identificacao_original": record.get("identificacao_original"),
            "distrito": record.get("distrito"),
            "bairro": record.get("bairro"),
            "nome_local": record.get("nome_local"),
            "endereco": record.get("endereco"),
            "arquivo_origem": record.get("arquivo_origem"),
            "aba_origem": record.get("aba_origem"),
            "linha_origem": record.get("linha_origem"),
            "latitude_original": record.get("latitude_original"),
            "longitude_original": record.get("longitude_original"),
            "latitude_tratada": record.get("latitude"),
            "longitude_tratada": record.get("longitude"),
            "regra_aplicada": decision,
            "confianca": "revisao",
            "classificacao_geografica": status,
            "distancia_limite_m": distance,
            "motivo_revisao": "ponto fora do Recife dentro do raio de revisão" if distance <= 1000 else "ponto fora do Recife acima do raio de revisão",
            "decisao_geocodificacao": decision,
        })

    def _geocode_records(self, result: pd.DataFrame, label: str) -> pd.DataFrame:
        if result.empty:
            return result
        candidates = result.index[
            result["classificacao_geografica"].eq("sem_coordenada")
            | (result["distancia_limite_m"].fillna(0).gt(1000) & result["classificacao_geografica"].str.startswith("fora_recife"))
        ]
        queries: dict[str, tuple[str | None, str | None, str | None]] = {}
        for index in candidates:
            row = result.loc[index]
            name, address, bairro = self._text(row.get("nome_local")), self._text(row.get("endereco")), self._text(row.get("bairro"))
            query = ", ".join(value for value in [name, address, bairro, "Recife", "Pernambuco", "Brasil"] if value)
            queries.setdefault(self._norm(query), (name, address, bairro))
        if not self.geocode_enabled:
            for index in candidates:
                row = result.loc[index]
                parts = self._address_parts(self._text(row.get("endereco")))
                self.geocode_inventory.append(self._geocode_inventory_row(row, parts, None, "nao_executada"))
            logger.info("geocodificação %s não executada | candidatos pendentes: %s | use sem --geocodificar para consultar endereços", label, format_number(len(candidates)))
            return result
        cache_hits_start = self.geocode_cache_hits
        http_requests_start = self.geocode_http_requests
        logger.info("geocodificação %s iniciada | candidatos: %s | endereços únicos: %s", label, format_number(len(candidates)), format_number(len(queries)))
        geocoded: dict[str, dict[str, Any]] = {}
        total = len(queries)
        for processed, (key, values) in enumerate(queries.items(), start=1):
            geocoded[key] = self._geocode_address(*values)
            if processed % 10 != 0 and processed != total:
                continue
            percentage = processed / total * 100 if total else 100
            filled = round(30 * processed / total) if total else 30
            logger.info("geocodificação %s | [%s%s] %s/%s | %.1f%%", label, "#" * filled, "-" * (30 - filled), format_number(processed), format_number(total), percentage)

        status_counts: Counter[str] = Counter()
        for index in candidates:
            row = result.loc[index]
            name, address, bairro = self._text(row.get("nome_local")), self._text(row.get("endereco")), self._text(row.get("bairro"))
            query = ", ".join(value for value in [name, address, bairro, "Recife", "Pernambuco", "Brasil"] if value)
            outcome = geocoded[self._norm(query)]
            is_accepted = outcome.get("resultado") == "aceita"
            geo_lat, geo_lon = outcome.get("latitude"), outcome.get("longitude")
            geo_status, geo_distance = self._geography(geo_lat, geo_lon) if is_accepted else (row["classificacao_geografica"], row["distancia_limite_m"])
            parts = self._address_parts(address)
            self.geocode_inventory.append(self._geocode_inventory_row(row, parts, outcome, outcome.get("status_geocodificacao")))
            geocode_audit = {
                "tipo_registro": "edl" if row.get("id_edl") else "ovt",
                "id_edl": row.get("id_edl"),
                "id_ovt_original": row.get("id_ovt_original"),
                "id_ovt_chave": row.get("id_ovt_chave"),
                "identificacao_original": row.get("identificacao_original"),
                "distrito": row.get("distrito"),
                "bairro": row.get("bairro"),
                "nome_local": row.get("nome_local"),
                "endereco": row.get("endereco"),
                "arquivo_origem": row.get("arquivo_origem"),
                "aba_origem": row.get("aba_origem"),
                "linha_origem": row.get("linha_origem"),
                "latitude_original": row.get("latitude_original"),
                "longitude_original": row.get("longitude_original"),
                "latitude_tratada": geo_lat if is_accepted else row.get("latitude"),
                "longitude_tratada": geo_lon if is_accepted else row.get("longitude"),
                "regra_coordenada": row.get("regra_coordenada"),
                "regra_aplicada": "geocodificacao_endereco" if is_accepted else "geocodificacao_rejeitada",
                "confianca": "alta" if is_accepted else "revisao",
                "classificacao_geografica": geo_status,
                "distancia_limite_m": geo_distance,
                "motivo_revisao": outcome.get("motivo"),
                "decisao_geocodificacao": outcome.get("status_geocodificacao"),
                "tipo_auditoria": "geocodificacao",
                "status_geocodificacao": outcome.get("status_geocodificacao"),
                "tentativas_geocodificacao": outcome.get("tentativas_geocodificacao"),
                "consultas_geocodificacao": outcome.get("consultas_geocodificacao"),
                "endereco_consultado": outcome.get("query"),
                "provedor_geocodificacao": outcome.get("provedor"),
                "resultado_geocodificacao": outcome.get("resultado"),
                "latitude_geocodificada": geo_lat,
                "longitude_geocodificada": geo_lon,
                "qualidade_coordenada": "alta" if is_accepted else "revisao",
                "fonte_coordenada": "geocodificacao_nominatim" if is_accepted else "planilha",
            }
            matching_audits = [
                audit for audit in self.coordinate_audit
                if audit.get("arquivo_origem") == row.get("arquivo_origem")
                and audit.get("aba_origem") == row.get("aba_origem")
                and audit.get("linha_origem") == row.get("linha_origem")
            ]
            if matching_audits:
                for audit in matching_audits:
                    audit["regra_original"] = audit.get("regra_aplicada")
                    audit.update(geocode_audit)
            else:
                self.coordinate_audit.append(geocode_audit)
            status_counts[outcome.get("status_geocodificacao", "desconhecido")] += 1
            if is_accepted:
                result.loc[index, ["latitude", "longitude", "latitude_geocodificada", "longitude_geocodificada", "classificacao_geografica", "distancia_limite_m", "fonte_coordenada", "qualidade_coordenada"]] = [geo_lat, geo_lon, geo_lat, geo_lon, geo_status, geo_distance, "geocodificacao_nominatim", "alta"]
        logger.log(25, "geocodificação %s concluída | resultados: %s", label, {key: format_number(value) for key, value in status_counts.items()})
        logger.info(
            "geocodificação %s | cache: %s | novas requisições HTTP: %s",
            label,
            format_number(self.geocode_cache_hits - cache_hits_start),
            format_number(self.geocode_http_requests - http_requests_start),
        )
        return result

    @staticmethod
    def _geocode_inventory_row(
        row: pd.Series,
        parts: dict[str, str | None],
        outcome: dict[str, Any] | None,
        status: str | None,
    ) -> dict[str, Any]:
        outcome = outcome or {}
        return {
            "tipo_registro": "edl" if row.get("id_edl") else "ovt",
            "arquivo_origem": row.get("arquivo_origem"),
            "aba_origem": row.get("aba_origem"),
            "linha_origem": row.get("linha_origem"),
            "id_edl": row.get("id_edl"),
            "id_ovt_original": row.get("id_ovt_original"),
            "id_ovt_chave": row.get("id_ovt_chave"),
            "nome_local": row.get("nome_local"),
            "endereco_original": row.get("endereco"),
            "logradouro": parts.get("logradouro"),
            "numero": parts.get("numero"),
            "complemento": parts.get("complemento"),
            "endereco_sem_complemento": parts.get("endereco_sem_complemento"),
            "bairro": row.get("bairro"),
            "latitude_original": row.get("latitude_original"),
            "longitude_original": row.get("longitude_original"),
            "latitude_tratada": row.get("latitude"),
            "longitude_tratada": row.get("longitude"),
            "classificacao_geografica": row.get("classificacao_geografica"),
            "distancia_limite_m": row.get("distancia_limite_m"),
            "status_geocodificacao": status,
            "resultado_geocodificacao": outcome.get("resultado"),
            "endereco_consultado": outcome.get("query"),
            "consultas_tentadas": outcome.get("consultas_geocodificacao"),
            "tentativas_geocodificacao": outcome.get("tentativas_geocodificacao"),
            "motivo_revisao": outcome.get("motivo"),
            "provedor_geocodificacao": outcome.get("provedor"),
        }

    @staticmethod
    def _source(row: pd.Series) -> dict[str, Any]:
        return {str(key): (None if pd.isna(value) else value) for key, value in row.items() if not str(key).startswith("_")}

    @staticmethod
    def _retirement_info(row: pd.Series) -> tuple[str, str | None, str | None]:
        months = {
            "JANEIRO": "01", "FEVEREIRO": "02", "MARCO": "03", "MARÇO": "03",
            "ABRIL": "04", "MAIO": "05", "JUNHO": "06", "JULHO": "07",
            "AGOSTO": "08", "SETEMBRO": "09", "OUTUBRO": "10", "NOVEMBRO": "11", "DEZEMBRO": "12",
        }
        for value in row.tolist():
            text = Transform._text(value)
            if not text:
                continue
            normalized = unicodedata.normalize("NFKD", text.upper())
            normalized = "".join(char for char in normalized if not unicodedata.combining(char))
            match = re.search(r"RETIRAD[AO]\s*(?:EM|:)?\s*(JANEIRO|FEVEREIRO|MARCO|ABRIL|MAIO|JUNHO|JULHO|AGOSTO|SETEMBRO|OUTUBRO|NOVEMBRO|DEZEMBRO)\s*(?:DE\s*)?(20\d{2})", normalized)
            if match:
                return "retirado", f"{match.group(2)}-{months[match.group(1)]}", text
            match = re.search(r"RETIRAD[AO].*?(0?[1-9]|1[0-2])\s*/\s*(20\d{2})", normalized)
            if match:
                return "retirado", f"{match.group(2)}-{int(match.group(1)):02d}", text
        return "ativo", None, None

    @staticmethod
    def _edl_has_location(row: pd.Series) -> bool:
        return any(Transform._text(row.get(column)) for column in ("NOME FANTASIA/COMERCIAL", "LOGRADOURO", "BAIRRO"))

    def transform_edls(self, frame: pd.DataFrame) -> pd.DataFrame:
        logger.info("transformação EDL iniciada | registros brutos: %s", format_number(len(frame)))
        record_audit_start = len(self.record_audit)
        coordinate_audit_start = len(self.coordinate_audit)
        records = []
        for _, row in frame.iterrows():
            identification = self._text(row.get("Nº"))
            if identification and identification.upper().startswith("EDL -"):
                self.record_audit.append({
                    "tipo_registro": "edl",
                    "regra_aplicada": "linha_estrutural_excluida",
                    "motivo_revisao": "título de seção, não representa um local",
                    "identificacao_original": identification,
                    "arquivo_origem": row.get("_arquivo_origem"),
                    "aba_origem": row.get("_aba_origem"),
                    "linha_origem": row.get("_linha_origem"),
                    "dados_originais": json.dumps(self._source(row), ensure_ascii=False, default=str),
                })
                continue
            tamanho = self._norm(row.get("TAMANHO"))
            responsible = self._norm(row.get("RESP. PELO PE"))
            has_location = self._edl_has_location(row)
            total_marker = any(
                self._norm(value) == "TOTAL"
                for value in row.tolist()
                if not (pd.isna(value) if not isinstance(value, (list, tuple)) else False)
            )
            if (tamanho == "TOTAL" or total_marker) and not identification and not has_location:
                self.record_audit.append({
                    "tipo_registro": "edl",
                    "regra_aplicada": "linha_total_ignorada",
                    "motivo_revisao": "linha de total, não representa um local",
                    "identificacao_original": identification,
                    "arquivo_origem": row.get("_arquivo_origem"),
                    "aba_origem": row.get("_aba_origem"),
                    "linha_origem": row.get("_linha_origem"),
                    "dados_originais": json.dumps(self._source(row), ensure_ascii=False, default=str),
                })
                continue
            if responsible == "RETIRADO" and not has_location:
                self.record_audit.append({
                    "tipo_registro": "edl",
                    "regra_aplicada": "marcador_retirado_ignorado",
                    "motivo_revisao": "marcador sem local, endereço ou registro EDL",
                    "identificacao_original": identification,
                    "arquivo_origem": row.get("_arquivo_origem"),
                    "aba_origem": row.get("_aba_origem"),
                    "linha_origem": row.get("_linha_origem"),
                    "dados_originais": json.dumps(self._source(row), ensure_ascii=False, default=str),
                })
                continue
            situacao_edl, mes_retirada, mes_retirada_original = self._retirement_info(row)
            coordinate_column = next((c for c in frame.columns if self._norm(c) in {"LATITUDE", "COORDENADAS"}), None)
            coordinate_index = frame.columns.get_loc(coordinate_column) if coordinate_column else -1
            lat_value = row.get(coordinate_column) if coordinate_column else None
            lon_column = next((c for c in frame.columns if self._norm(c) == "LONGITUDE"), None)
            lon_value = row.get(lon_column) if lon_column else None
            if lon_value is None and coordinate_index >= 0 and coordinate_index + 1 < len(frame.columns):
                lon_value = row.get(frame.columns[coordinate_index + 1])
            row = row.copy()
            row["_tipo_registro"] = "edl"
            row["_id_edl"] = f"{row.get('_arquivo_origem')}:{row.get('_aba_origem')}:{row.get('_linha_origem')}"
            row["_identificacao_original"] = row.get("Nº")
            row["_distrito"] = re.search(r"DS\s*[- ]?\s*([IVX]+|\d+)", str(row.get("_aba_origem", "")), re.I)
            row["_distrito"] = row["_distrito"].group(1) if row["_distrito"] else None
            row["_tipo_pe"] = row.get("TIPO PE.")
            row["_nome_local"] = row.get("NOME FANTASIA/COMERCIAL")
            row["_endereco"] = row.get("LOGRADOURO")
            row["_bairro"] = row.get("BAIRRO")
            row["_coluna_latitude_origem"] = coordinate_column
            row["_coluna_longitude_origem"] = lon_column or (frame.columns[coordinate_index + 1] if coordinate_index >= 0 and coordinate_index + 1 < len(frame.columns) else None)
            row["_valor_original_coordenadas"] = ", ".join(filter(None, [self._text(lat_value), self._text(lon_value)])) or None
            audit_start = len(self.coordinate_audit)
            lat, lon, coordinate_rule = self._coordinates(lat_value, lon_value, row)
            status, distance = self._geography(lat, lon)
            coordinate_source, coordinate_quality = "planilha", "alta" if status == "dentro_recife" else "revisao"
            for audit in self.coordinate_audit[audit_start:]:
                audit["classificacao_geografica"] = status
                audit["distancia_limite_m"] = distance
                audit["dados_originais"] = json.dumps(self._source(row), ensure_ascii=False, default=str)
            district = re.search(r"DS\s*[- ]?\s*([IVX]+|\d+)", str(row.get("_aba_origem", "")), re.I)
            records.append({
                "id_edl": f"{row.get('_arquivo_origem')}:{row.get('_aba_origem')}:{row.get('_linha_origem')}",
                "distrito": district.group(1) if district else None,
                "identificacao_original": self._text(row.get("Nº")),
                "responsavel": self._text(row.get("RESP. PELO PE")),
                "tipo_pe": self._text(row.get("TIPO PE.")),
                "nome_local": self._text(row.get("NOME FANTASIA/COMERCIAL")),
                "endereco": self._text(row.get("LOGRADOURO")),
                "bairro": self._text(row.get("BAIRRO")),
                "situacao_edl": situacao_edl,
                "mes_retirada_original": mes_retirada_original,
                "mes_retirada": mes_retirada,
                "ativo_ate": mes_retirada,
                "quantidade_edl_real": self._number(row.get(next((c for c in frame.columns if self._norm(c) == "QUANTDEEDLREAL"), "__missing__"))),
                "latitude_original": self._text(lat_value),
                "longitude_original": self._text(lon_value),
                "latitude": lat,
                "longitude": lon,
                "regra_coordenada": coordinate_rule,
                "latitude_geocodificada": lat if coordinate_source == "geocodificacao_nominatim" else None,
                "longitude_geocodificada": lon if coordinate_source == "geocodificacao_nominatim" else None,
                "fonte_coordenada": coordinate_source,
                "qualidade_coordenada": coordinate_quality,
                "classificacao_geografica": status,
                "distancia_limite_m": distance,
                "dados_originais": json.dumps(self._source(row), ensure_ascii=False, default=str),
                "arquivo_origem": row.get("_arquivo_origem"),
                "aba_origem": row.get("_aba_origem"),
                "linha_origem": row.get("_linha_origem"),
            })
            self._audit_geography_review(records[-1], status, distance)
        result = pd.DataFrame(records)
        for value in result.get("nome_local", pd.Series(dtype=object)).dropna():
            key = self._norm(value)
            if key:
                self.edl_name_keys.add(key)
                self.edl_name_counts[str(value).strip()] += 1
        result = self._geocode_records(result, "EDL")
        logger.info("linhas estruturais excluídas: %s", format_number(len(self.record_audit) - record_audit_start))
        logger.info("coordenadas EDL auditadas: %s", format_number(len(self.coordinate_audit) - coordinate_audit_start))
        logger.log(25, "transformação EDL concluída | locais: %s", format_number(len(result)))
        return result

    def transform_locations(self, frame: pd.DataFrame) -> pd.DataFrame:
        logger.info("transformação de localizações iniciada | registros: %s", format_number(len(frame)))
        id_audit_start = len(self.id_audit)
        coordinate_audit_start = len(self.coordinate_audit)
        id_column = self._find_column(frame, "ID", "OVT") or "ID OVT"
        lat_column = self._find_column(frame, "LATITUDE") or "LATITUDE"
        lon_column = self._find_column(frame, "LONGITUDE") or "LONGITUDE"
        records = []
        for _, row in frame.iterrows():
            row = row.copy()
            district = re.search(r"DS\s+([IVX]+)", str(row.get("_aba_origem", "")), re.I)
            original_id = self._text(row.get(id_column))
            raw_address = self._text(row.get("ENDEREÇO"))
            raw_bairro = self._text(row.get("BAIRRO"))
            raw_latitude = self._text(row.get(lat_column))
            raw_longitude = self._text(row.get(lon_column))
            if not any((original_id, raw_address, raw_bairro, raw_latitude, raw_longitude)):
                self.record_audit.append({
                    "tipo_registro": "ovt",
                    "regra_aplicada": "linha_localizacao_incompleta_ignorada",
                    "qualidade": "revisao",
                    "motivo_revisao": "linha sem identificador e sem dados de localização",
                    "arquivo_origem": row.get("_arquivo_origem"),
                    "aba_origem": row.get("_aba_origem"),
                    "linha_origem": row.get("_linha_origem"),
                    "dados_originais": json.dumps(self._source(row), ensure_ascii=False, default=str),
                })
                continue
            row["_tipo_registro"] = "ovt"
            row["_id_ovt_original"] = original_id
            row["_id_ovt_chave"] = self._norm(row.get(id_column))
            row["_identificacao_original"] = None
            row["_distrito"] = district.group(1) if district else None
            row["_bairro"] = row.get("BAIRRO")
            row["_endereco"] = row.get("ENDEREÇO")
            row["_coluna_latitude_origem"] = lat_column
            row["_coluna_longitude_origem"] = lon_column
            row["_valor_original_coordenadas"] = ", ".join(filter(None, [self._text(row.get(lat_column)), self._text(row.get(lon_column))])) or None
            normalized_id = self._norm(original_id)
            self.id_audit.append({
                "id_ovt_original": original_id,
                "id_ovt_chave": normalized_id,
                "regra_aplicada": "normalizacao_id" if original_id else "id_ausente",
                "qualidade_id": "alta" if original_id else "revisao",
                "houve_colisao": False,
                "arquivo_origem": row.get("_arquivo_origem"),
                "aba_origem": row.get("_aba_origem"),
                "linha_origem": row.get("_linha_origem"),
                "motivo_revisao": None if original_id else "ID da ovitrampa ausente",
            })
            audit_start = len(self.coordinate_audit)
            lat, lon, coordinate_rule = self._coordinates(row.get(lat_column), row.get(lon_column), row)
            status, distance = self._geography(lat, lon)
            coordinate_source, coordinate_quality = "planilha", "alta" if status == "dentro_recife" else "revisao"
            for audit in self.coordinate_audit[audit_start:]:
                audit["classificacao_geografica"] = status
                audit["distancia_limite_m"] = distance
                audit["dados_originais"] = json.dumps(self._source(row), ensure_ascii=False, default=str)
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
                "regra_coordenada": coordinate_rule,
                "latitude_geocodificada": lat if coordinate_source == "geocodificacao_nominatim" else None,
                "longitude_geocodificada": lon if coordinate_source == "geocodificacao_nominatim" else None,
                "fonte_coordenada": coordinate_source,
                "qualidade_coordenada": coordinate_quality,
                "classificacao_geografica": status,
                "distancia_limite_m": distance,
                "dados_originais": json.dumps(self._source(row), ensure_ascii=False, default=str),
                "arquivo_origem": row.get("_arquivo_origem"),
                "aba_origem": row.get("_aba_origem"),
                "linha_origem": row.get("_linha_origem"),
            })
            self._audit_geography_review(records[-1], status, distance)
        result = pd.DataFrame(records)
        if not result.empty:
            duplicates = result["id_ovt_chave"].duplicated(keep=False) & result["id_ovt_chave"].ne("")
            duplicate_keys = set(result.loc[duplicates, "id_ovt_chave"])
            for audit in self.id_audit:
                if audit["id_ovt_chave"] in duplicate_keys:
                    audit["regra_aplicada"] = "colisao_id_normalizado"
                    audit["qualidade_id"] = "revisao"
                    audit["houve_colisao"] = True
                    audit["motivo_revisao"] = "IDs diferentes geraram a mesma chave"
        collision_count = sum(1 for item in self.id_audit[id_audit_start:] if item["houve_colisao"])
        result = self._geocode_records(result, "OVT")
        logger.info("IDs normalizados: %s | colisões: %s", format_number(len(self.id_audit) - id_audit_start), format_number(collision_count))
        logger.info("coordenadas de localizações auditadas: %s", format_number(len(self.coordinate_audit) - coordinate_audit_start))
        logger.log(25, "transformação de localizações concluída | registros: %s", format_number(len(result)))
        return result

    def transform_observations(self, frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        if frame.empty:
            return pd.DataFrame(), pd.DataFrame()
        logger.info("transformação de observações iniciada | registros brutos: %s", format_number(len(frame)))
        total_records = len(frame)

        def log_progress(label: str, processed: int) -> None:
            if processed % 10_000 != 0 and processed != total_records:
                return
            percentage = processed / total_records * 100
            filled = round(30 * processed / total_records)
            bar = "#" * filled + "-" * (30 - filled)
            logger.info(
                "%s | [%s] %s/%s | %.1f%%",
                label,
                bar,
                format_number(processed),
                format_number(total_records),
                percentage,
            )

        date_audit_start = len(self.date_audit)
        id_column = self._find_column(frame, "ID", "OVT") or self._find_column(frame, "ID")
        year_column = self._find_column(frame, "ANO")
        cycle_column = self._find_column(frame, "CICLO")
        date_columns = [column for column in frame.columns if self._norm(column).startswith("DT") or "DATA" in self._norm(column)]
        observations = []
        cycles = {}
        collection_column = next((column for column in date_columns if "COLETA" in self._norm(column)), None)
        status_columns = [
            column for column in frame.columns
            if self._norm(column) in {"STATUS", "SITUACAO"} or self._norm(column).startswith("OBS")
        ]
        reference_dates: dict[tuple[object, object, int | None, int | None, str], list[pd.Timestamp]] = {}
        anchors: dict[object, list[pd.Timestamp]] = {}
        logger.info("preparação das referências temporais iniciada")
        for processed, (_, row) in enumerate(frame.iterrows(), start=1):
            year_value = self._number(row.get(year_column)) if year_column else self._number(row.get("Ano"))
            cycle_value = self._number(row.get(cycle_column)) if cycle_column else self._number(row.get("CICLOS"))
            row_year = int(year_value) if year_value is not None else None
            row_cycle = int(cycle_value) if cycle_value is not None else None
            for column in date_columns:
                parsed = self._parse_date(row.get(column))
                if parsed is None or parsed.year < 2000:
                    continue
                key = (row.get("_arquivo_origem"), row.get("_aba_origem"), row_year, row_cycle, self._norm(column))
                reference_dates.setdefault(key, []).append(parsed)
                if collection_column and column == collection_column and row_cycle == 1:
                    anchors.setdefault(row.get("_arquivo_origem"), []).append(parsed)
            log_progress("referências temporais", processed)
        anchor_dates = {source: pd.Series(values).sort_values().iloc[len(values) // 2] for source, values in anchors.items()}
        logger.info("preparação das referências temporais concluída")
        logger.info("processamento das observações iniciado")
        for processed, (_, row) in enumerate(frame.iterrows(), start=1):
            year = self._number(row.get(year_column)) if year_column else None
            cycle = self._number(row.get(cycle_column)) if cycle_column else None
            if year is None and "Ano" in frame.columns:
                year = self._number(row.get("Ano"))
            if cycle is None and "CICLOS" in frame.columns:
                cycle = self._number(row.get("CICLOS"))
            year = int(year) if year is not None else None
            cycle = int(cycle) if cycle is not None else None
            cycle_key = (year, cycle)
            status_original = next((self._text(row.get(column)) for column in status_columns if self._text(row.get(column)) is not None), None)
            status_code, status_description, status_quality = self._status(status_original)
            parsed_dates = {}
            date_metadata = {}
            for column in date_columns:
                raw_date = row.get(column)
                parsed = self._parse_date(raw_date)
                key = (row.get("_arquivo_origem"), row.get("_aba_origem"), year, cycle, self._norm(column))
                inferred = None
                if parsed is None and self._text(raw_date) is not None:
                    candidates = reference_dates.get(key, [])
                    if candidates:
                        counts = Counter(value.strftime("%Y-%m-%d") for value in candidates).most_common()
                        if len(counts) == 1 or (counts[0][1] >= 3 and counts[0][1] > counts[1][1]):
                            inferred = pd.Timestamp(counts[0][0])
                expected_year = self._cycle_expected_year(anchor_dates.get(row.get("_arquivo_origem")), cycle, inferred or parsed)
                expected_year = expected_year or self._expected_date_year(row, year, cycle, inferred or parsed)
                date_row = row.copy()
                date_row["_status_original"] = status_original
                date_row["_status_codigo"] = status_code
                date_row["_status_descricao"] = status_description
                result, rule, quality = self._date_info(
                    raw_date, date_row, str(column), expected_year, inferred, status_code == "REC"
                )
                parsed_dates[self._norm(column)] = result
                date_metadata[self._norm(column)] = (rule, quality)
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
                "data_coleta_original": self._text(row.get(collection_column)) if collection_column else None,
                "data_coleta": next((value for key, value in parsed_dates.items() if "COLETA" in key), None),
                "regra_data": date_metadata.get(self._norm(collection_column), (None, None))[0] if collection_column else None,
                "qualidade_data": date_metadata.get(self._norm(collection_column), (None, None))[1] if collection_column else None,
                "quantidade_ovos": self._number(row.get(next((c for c in frame.columns if "OVOS" in self._norm(c)), "__missing__"))),
                "quantidade_palhetas": self._number(row.get(next((c for c in frame.columns if "PALHETA" in self._norm(c)), "__missing__"))),
                "status_original": status_original,
                "status": status_code,
                "status_descricao": status_description,
                "qualidade_status": status_quality,
                "dados_originais": json.dumps(self._source(row), ensure_ascii=False, default=str),
                "arquivo_origem": row.get("_arquivo_origem"),
                "aba_origem": row.get("_aba_origem"),
                "linha_origem": row.get("_linha_origem"),
            })
            log_progress("observações processadas", processed)
        result = pd.DataFrame(observations)
        status_counts = result["status"].value_counts(dropna=False).to_dict() if not result.empty else {}
        corrections = self.date_audit[date_audit_start:]
        logger.info("ciclos identificados: %s", format_number(len(cycles)))
        logger.info("datas ajustadas/inferidas: %s", format_number(len(corrections)))
        logger.info("status normalizados: %s", {key: format_number(value) for key, value in status_counts.items()})
        logger.log(25, "transformação de observações concluída | registros: %s", format_number(len(result)))
        return result, pd.DataFrame(cycles.values())

    def reconcile_ovt_ids(self, locations: pd.DataFrame, observations: pd.DataFrame) -> None:
        """Compara o cadastro georreferenciado com os IDs observados nos ciclos."""
        location_keys = set(locations.get("id_ovt_chave", pd.Series(dtype=str)).dropna())
        observation_groups = (
            observations.dropna(subset=["id_ovt_chave"]).groupby("id_ovt_chave")
            if not observations.empty and "id_ovt_chave" in observations
            else None
        )
        observation_keys = set(observation_groups.groups) if observation_groups is not None else set()
        audit_by_key = {}
        for audit in self.id_audit:
            audit_by_key.setdefault(audit["id_ovt_chave"], []).append(audit)
        for key in location_keys | observation_keys:
            present_location = key in location_keys
            present_observation = key in observation_keys
            group = observation_groups.get_group(key) if present_observation and observation_groups is not None else pd.DataFrame()
            years = sorted({int(value) for value in group["ano"].dropna()}) if present_observation else []
            situation = "em_ambas" if present_location and present_observation else "somente_georreferencia" if present_location else "somente_observacoes"
            updates = audit_by_key.get(key, [])
            if not updates:
                first = group.iloc[0]
                updates = [{
                    "id_ovt_original": first["id_ovt_original"],
                    "id_ovt_chave": key,
                    "regra_aplicada": "reconciliacao_fontes",
                    "qualidade_id": "revisao",
                    "houve_colisao": False,
                    "arquivo_origem": first["arquivo_origem"],
                    "aba_origem": first["aba_origem"],
                    "linha_origem": first["linha_origem"],
                    "motivo_revisao": "ID presente nas observações e ausente no georreferenciamento",
                }]
                self.id_audit.extend(updates)
            for audit in updates:
                audit.update({
                    "presente_georreferencia": present_location,
                    "presente_observacoes": present_observation,
                    "anos_observacoes": ",".join(map(str, years)) or None,
                    "quantidade_observacoes": len(group),
                    "situacao_cadastro": situation,
                })
        logger.info(
            "reconciliação de IDs | no georreferenciamento: %s | nas observações: %s | somente observações: %s",
            format_number(len(location_keys)), format_number(len(observation_keys)),
            format_number(len(observation_keys - location_keys)),
        )

    def geocode_inventory_frame(self) -> pd.DataFrame:
        columns = [
            "tipo_registro", "arquivo_origem", "aba_origem", "linha_origem", "id_edl",
            "id_ovt_original", "id_ovt_chave", "nome_local", "endereco_original",
            "logradouro", "numero", "complemento", "endereco_sem_complemento", "bairro",
            "latitude_original", "longitude_original", "latitude_tratada", "longitude_tratada",
            "classificacao_geografica", "distancia_limite_m", "status_geocodificacao",
            "resultado_geocodificacao", "endereco_consultado", "consultas_tentadas",
            "tentativas_geocodificacao", "motivo_revisao", "provedor_geocodificacao",
        ]
        frame = pd.DataFrame(self.geocode_inventory)
        for column in columns:
            if column not in frame:
                frame[column] = None
        return frame[columns]

    def edl_name_dictionary_frame(self) -> pd.DataFrame:
        return pd.DataFrame([
            {"nome_local": name, "nome_chave": self._norm(name), "ocorrencias": count}
            for name, count in sorted(self.edl_name_counts.items())
        ])

    def audits(self) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        provenance_columns = [
            "tipo_auditoria", "tipo_registro", "arquivo_origem", "aba_origem", "linha_origem",
            "coluna_origem", "coluna_latitude_origem", "coluna_longitude_origem",
            "id_edl", "id_ovt_original", "id_ovt_chave", "identificacao_original", "ano", "ciclo",
        ]

        def ordered_audit(data: list[dict[str, Any]], kind: str, specific: list[str]) -> pd.DataFrame:
            frame = pd.DataFrame(data)
            columns = provenance_columns + [column for column in specific if column not in provenance_columns]
            for column in columns:
                if column not in frame:
                    frame[column] = None
            if frame["tipo_auditoria"].isna().all() if not frame.empty else True:
                frame["tipo_auditoria"] = kind
            else:
                frame["tipo_auditoria"] = frame["tipo_auditoria"].fillna(kind)
            return frame[columns]

        coordinate_audit = ordered_audit(self.coordinate_audit, "coordenadas", [
            "distrito", "bairro", "tipo_pe", "nome_local", "endereco", "valor_original_coordenadas",
            "latitude_original", "longitude_original", "latitude_tratada", "longitude_tratada",
            "regra_coordenada", "regra_original", "regra_aplicada", "confianca", "classificacao_geografica", "distancia_limite_m",
            "motivo_revisao", "endereco_consultado", "provedor_geocodificacao", "resultado_geocodificacao",
            "latitude_geocodificada", "longitude_geocodificada", "qualidade_coordenada", "fonte_coordenada",
            "decisao_geocodificacao", "status_geocodificacao", "tentativas_geocodificacao",
            "consultas_geocodificacao", "dados_originais",
        ])
        date_audit = ordered_audit(self.date_audit, "datas", [
            "campo", "status_original", "status_codigo", "status_descricao", "valor_original", "valor_tratado",
            "regra_data", "qualidade_data", "regra_aplicada", "qualidade", "motivo_revisao", "dados_originais",
        ])
        record_audit = ordered_audit(self.record_audit, "registros", [
            "valor_original", "valor_tratado", "regra_aplicada", "qualidade", "motivo_revisao", "dados_originais",
        ])
        id_audit = ordered_audit(self.id_audit, "ids", [
            "regra_aplicada", "qualidade_id", "houve_colisao", "presente_georreferencia",
            "presente_observacoes", "anos_observacoes", "quantidade_observacoes", "situacao_cadastro",
            "motivo_revisao", "dados_originais",
        ])
        return coordinate_audit, date_audit, record_audit, id_audit
