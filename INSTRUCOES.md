# Projeto AEDES-BI

Documento inicial de orientação para o projeto de Engenharia de Dados. O
primeiro módulo do projeto será dedicado aos dados de EDLs e ovitrampas e deve
seguir o fluxo didático de Extração, Transformação e Carga.

## Objetivo

Construir o projeto `aedes-bi`, começando por um pipeline para ler, tratar,
validar e armazenar dados de:

- Estações Disseminadoras de Larvicida (EDLs);
- ovitrampas;
- ciclos e observações de ovitrampas.

O resultado deve ser um banco SQLite reproduzível, com dados tratados e
auditorias das correções e inconsistências encontradas.

O escopo do primeiro módulo não inclui clima, COMPESA, arboviroses ou H3. A
estrutura do projeto deve permitir que esses módulos sejam adicionados depois
sem alterar a identidade do projeto.

## Estrutura do Projeto

```text
aedes-bi/
├── .venv/
├── data/
│   ├── entradas/
│   │   ├── TOTAL DE EDL POR DS.xlsx
│   │   ├── Georreferenciamento OVT 2026 ATUALIZAÇÃO.xlsx
│   │   ├── OVITRAMPAS 2024/
│   │   ├── OVITRAMPAS 2025/
│   │   └── OVITRAMPAS 2026/
│   ├── referencia/
│   │   └── recife.geojson
│   ├── processados/
│   │   ├── geocodificacao_cache.json
│   │   ├── inventario_geocodificacao.csv
│   │   └── nomes_locais_edl.csv
│   └── auditoria/
│       ├── auditoria_coordenadas.csv
│       └── auditoria_datas.csv
├── database/
│   └── aedes_bi.sqlite
├── src/
│   └── aedes_bi/
│       ├── __init__.py
│       ├── extract.py
│       ├── transform.py
│       └── load.py
├── main.py
├── README.md
├── pyproject.toml
├── uv.lock
└── .gitignore
```

O pacote Python deve se chamar `aedes_bi`. Os arquivos originais devem
permanecer em `data/entradas/` e não podem ser
alterados pelo pipeline.

## Fluxo Principal

```text
extract.py -> transform.py -> load.py -> main.py
```

`main.py` apenas coordena as etapas. As regras de negócio não devem ficar no
arquivo de orquestração.

## `src/aedes_bi/extract.py`

Implementar a classe `Extract`.

Responsabilidades:

- ler arquivos `.xls` e `.xlsx`;
- localizar abas e cabeçalhos mesmo quando houver linhas introdutórias;
- carregar os dados brutos sem aplicar regras de negócio;
- baixar o limite do Recife pela API do IBGE quando
  `data/referencia/recife.geojson` não existir;
- preservar o caminho, nome do arquivo e aba de origem;
- retornar os dados para a etapa de transformação.

Métodos previstos:

```python
read_edls()
read_ovt_locations()
read_ovt_observations(year)
download_recife_boundary()
```

Código do município do Recife no IBGE: `2611606`.

## `src/aedes_bi/transform.py`

Implementar a classe `Transform`.

Responsabilidades:

- padronizar nomes de colunas;
- limpar textos e valores vazios;
- converter números;
- criar e padronizar IDs;
- tratar datas;
- corrigir anos inválidos somente quando a regra for segura;
- validar coordenadas;
- detectar latitude e longitude invertidas;
- validar pontos contra o limite municipal do Recife;
- calcular distância até o limite municipal quando necessário;
- separar endereço original em logradouro, número e complemento para consultas;
- preservar o número do endereço nas consultas alternativas;
- remover somente complementos como `CASA`, `EDF`, `AP` ou `BLOCO` em tentativas
  alternativas;
- gerar o inventário dos candidatos à geocodificação;
- ignorar linhas de localização sem identificador e sem dados de localização,
  registrando a decisão em `auditoria_registros.csv`;
- gerar os DataFrames tratados;
- gerar as tabelas de auditoria.

### IDs de ovitrampas

- preservar o ID original;
- criar uma chave derivada em maiúsculas;
- remover espaços, pontuação e qualquer caractere que não seja letra ou número;
- registrar colisões entre IDs normalizados;
- nunca sobrescrever silenciosamente o valor original.

### Datas

- preservar a data original;
- marcar datas ausentes, inválidas ou ambíguas;
- corrigir anos inválidos somente quando dia, mês, arquivo e ciclo forem
  compatíveis;
- registrar a regra aplicada em `regra_data`;
- registrar a qualidade em `qualidade_data`;
- encaminhar datas sem confiança para auditoria ou `NULL`.

### Coordenadas

- preservar latitude e longitude originais;
- criar valores tratados separadamente;
- detectar faixas inválidas;
- detectar sinais invertidos;
- detectar latitude e longitude trocadas;
- corrigir automaticamente apenas casos inequívocos;
- registrar toda correção na auditoria.

### Limite municipal

Validar coordenadas contra o limite do Recife obtido do IBGE. A classificação
deve ser:

- `dentro_recife`;
- `fora_recife_proxima`, quando estiver fora e a até 1 km do limite;
- `fora_recife`;
- `sem_coordenada` ou equivalente quando não houver ponto válido.

O ponto original e o resultado da validação devem permanecer rastreáveis.

Pontos fora do Recife até 1 km devem ser mantidos para revisão manual e
registrados na auditoria. Pontos sem coordenada ou fora do Recife a mais de 1
km podem ser consultados por endereço, usando nome do local, endereço e bairro.
Essa geocodificação é executada por padrão e pode ser desativada com
`--sem-geocodificar`.

Quando o endereço possui número, o número deve permanecer nas consultas. O
complemento pode ser removido apenas em consultas alternativas. Não se deve
buscar automaticamente apenas pelo nome da rua quando há número disponível.
Para todo candidato à geocodificação, o endereço informado deve ser consultado
primeiro, inclusive quando possui número. Se não houver resultado, o nome do
estabelecimento pode ser usado como fallback, mantendo bairro e Recife na
validação. O arquivo
`data/processados/nomes_locais_edl.csv` registra os nomes extraídos dos EDLs,
mas não bloqueia a consulta nominal.
O endereço original, as partes extraídas e todas as consultas devem permanecer
no inventário `data/processados/inventario_geocodificacao.csv`.

`regra_coordenada` e `status_geocodificacao` são decisões independentes. Uma
falha de geocodificação não invalida nem apaga uma coordenada tratada com regra
segura.

## `src/aedes_bi/load.py`

Implementar a classe `Load`.

Responsabilidades:

- criar o banco SQLite;
- criar as tabelas;
- inserir os DataFrames tratados;
- configurar chaves primárias e estrangeiras;
- criar índices úteis para consultas;
- salvar as auditorias em `data/auditoria/`, quando essa responsabilidade
  estiver centralizada na carga.
- salvar o inventário de geocodificação e o cache em `data/processados/`.

O carregamento deve usar `sqlite3` ou `DataFrame.to_sql()`.

## `main.py`

Responsável apenas por orquestrar o pipeline:

```python
from aedes_bi.extract import Extract
from aedes_bi.transform import Transform
from aedes_bi.load import Load

extractor = Extract()
transformer = Transform()
loader = Load()

edls = extractor.read_edls()
locations = extractor.read_ovt_locations()
observations = extractor.read_ovt_observations()

edls = transformer.transform_edls(edls)
locations = transformer.transform_locations(locations)
observations = transformer.transform_observations(observations)

loader.load_sqlite(edls, locations, observations)
```

O código acima é um fluxo de referência. Os nomes dos parâmetros podem ser
ajustados para refletir as fontes reais, sem misturar extração, transformação
e carga.

## Modelo Inicial do Banco

O banco deve conter, no mínimo, as tabelas:

### `edls`

Um registro por estação disseminadora, incluindo identificação, distrito,
endereço, coordenadas, situação, mês de retirada, dados originais e qualidade
dos dados. Linhas de total devem ser ignoradas. Locais reais marcados como
retirados devem permanecer com o mês de retirada quando essa informação existir.

### `ovitrampas`

Um registro por ovitrampa georreferenciada, incluindo ID original, chave
normalizada, endereço, coordenadas originais e tratadas e classificação
geográfica.

### `ciclos`

Um registro por ano e ciclo, com datas inicial, final e de referência quando
essas datas puderem ser calculadas com segurança.

### `observacoes_ovitrampas`

Um registro por ovitrampa e ciclo, incluindo datas de coleta, quantidade de
ovos, quantidade de palhetas, status, ID da ovitrampa e arquivo/aba de origem.

As tabelas devem preservar rastreabilidade para os arquivos de origem.

## Auditoria

Toda alteração ou decisão de qualidade deve ser verificável. As auditorias
devem registrar, quando aplicável:

- valor original;
- valor tratado;
- regra aplicada;
- nível de confiança;
- arquivo de origem;
- aba de origem;
- linha de origem;
- motivo para revisão manual.

Arquivos mínimos:

- `data/auditoria/auditoria_coordenadas.csv`;
- `data/auditoria/auditoria_datas.csv`.

Também são gerados, quando o pipeline é executado:

- `data/auditoria/auditoria_ids.csv`;
- `data/auditoria/auditoria_registros.csv`;
- `data/processados/inventario_geocodificacao.csv`;
- `data/processados/geocodificacao_cache.json`.

## O Que Não Será Utilizado

- MongoDB;
- `.env`;
- dados climáticos;
- dados da COMPESA;
- casos de arboviroses;
- scripts separados por ano;
- um único `importar_dados.py` concentrando todo o pipeline.

## Dependências

```text
pandas
requests
openpyxl
xlrd
geopandas
shapely
pyproj
```

`sqlite3` faz parte da biblioteca padrão do Python e não deve ser adicionado
ao arquivo de dependências.

## README e CRISP-DM

O `README.md` deve documentar o planejamento usando as etapas do CRISP-DM e
registrar que EDLs e ovitrampas são o primeiro módulo do `aedes-bi`:

1. entendimento do negócio;
2. entendimento dos dados;
3. preparação dos dados;
4. modelagem das tabelas;
5. avaliação da qualidade;
6. implantação ou execução do pipeline.

## Execução Esperada

Na primeira versão, o pipeline deverá ser executado a partir da raiz:

```bash
python main.py
```

O pipeline deve ser reexecutável sem modificar os arquivos de entrada e sem
duplicar registros no banco.

Este documento é o ponto de partida da implementação. As decisões específicas
de leitura das planilhas devem ser confirmadas observando as estruturas reais
dos arquivos de entrada e documentadas no `README.md`.
