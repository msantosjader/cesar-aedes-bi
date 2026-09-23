# aedes-bi

Pipeline de Extração, Transformação e Carga para o primeiro módulo do projeto:
EDLs, ovitrampas, ciclos e observações de ovitrampas do Recife.

## Execução

Requisitos: Python 3.12+ e UV.

```bash
uv sync
uv run python main.py
```

O comando pode ser executado novamente. Ele recria o banco SQLite e substitui
as auditorias sem alterar os arquivos em `data/entradas/`.

Saídas:

- `database/aedes_bi.sqlite`;
- `data/auditoria/auditoria_coordenadas.csv`;
- `data/auditoria/auditoria_datas.csv`;
- `data/referencia/recife.geojson`, baixado do IBGE apenas quando ausente.

## Fontes

- `TOTAL DE EDL POR DS.xlsx`: oito abas de EDLs, com cabeçalhos introdutórios;
- `Georreferenciamento OVT 2026 ATUALIZAÇÃO.xlsx`: oito abas de localização;
- planilhas XLS de 2024 e 2025: uma aba por bairro, com ciclos em grupos de
  colunas;
- planilha XLSX de 2026: dados tabulares do ciclo 1 em diante.

A extração preserva arquivo, aba e linha de origem. As planilhas originais não
são modificadas.

## Tabelas

- `edls`: estações disseminadoras, coordenadas e qualidade geográfica;
- `ovitrampas`: cadastro georreferenciado, ID original e chave normalizada;
- `ciclos`: ano, ciclo e datas disponíveis;
- `observacoes_ovitrampas`: observações por ovitrampa e ciclo.

Coordenadas são preservadas nos valores originais e tratados. A classificação
geográfica usa o limite municipal do Recife em `dentro_recife`,
`fora_recife_proxima`, `fora_recife` ou `sem_coordenada`.

## CRISP-DM

1. **Entendimento do negócio:** organizar dados de EDLs e ovitrampas para
   análise operacional e vigilância ambiental.
2. **Entendimento dos dados:** identificar formatos XLS/XLSX, abas, cabeçalhos
   introdutórios, ciclos e inconsistências das fontes.
3. **Preparação dos dados:** normalizar textos, IDs, datas, números e
   coordenadas, mantendo os valores originais e a rastreabilidade.
4. **Modelagem das tabelas:** carregar EDLs, ovitrampas, ciclos e observações
   em SQLite com índices e chave entre ciclos e observações.
5. **Avaliação da qualidade:** gerar auditorias de coordenadas, datas, IDs
   normalizados e classificações contra o limite municipal.
6. **Implantação:** executar `uv run python main.py` a partir da raiz.

O escopo deste módulo não inclui clima, COMPESA, arboviroses ou H3.
