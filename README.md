# aedes-bi

Pipeline de Extração, Transformação e Carga para o primeiro módulo do projeto:
EDLs, ovitrampas, ciclos e observações de ovitrampas do Recife.

## Execução

Requisitos: Python 3.12+ e UV.

```bash
uv sync
uv run python main.py
# execução sem consultas externas
uv run python main.py --sem-geocodificar
```

O comando pode ser executado novamente. Ele recria o banco SQLite e substitui
as auditorias sem alterar os arquivos em `data/entradas/`.

Os logs são exibidos no console no formato:

```text
DD/MM/AAAA HH:MM:SS | HH:MM:SS | FASE      | TIPO    | mensagem
```

As fases são `EXTRACT`, `TRANSFORM`, `LOAD` e `PIPELINE`. As contagens usam
separador de milhar no padrão brasileiro.

Saídas:

- `database/aedes_bi.sqlite`;
- `data/auditoria/auditoria_coordenadas.csv`;
- `data/auditoria/auditoria_datas.csv`;
- `data/auditoria/auditoria_ids.csv`;
- `data/auditoria/auditoria_registros.csv`;
- `data/processados/inventario_geocodificacao.csv`;
- `data/processados/geocodificacao_cache.json`;
- `data/referencia/recife.geojson`, baixado do IBGE apenas quando ausente.

O pipeline usa Nominatim/OpenStreetMap em bloco, por padrão, somente para
registros sem coordenada ou fora do Recife a mais de 1 km do limite. As
consultas são deduplicadas, limitadas, cacheadas em
`data/processados/geocodificacao_cache.json` e registradas na auditoria de
coordenadas. Use `--sem-geocodificar` para uma execução sem chamadas externas.
Cada tentativa registra `status_geocodificacao` como `sucesso`, `sem_sucesso`,
`endereco_ausente` ou `erro_consulta`, além das consultas realizadas.
O inventário de geocodificação registra cada candidato, suas coordenadas,
origem, endereço original, logradouro, número, complemento, consultas tentadas
e motivo da revisão. O endereço original nunca é alterado.

Quando existe número no endereço, ele é mantido em todas as consultas. Apenas
complementos como `CASA B`, `EDF`, `AP` ou `BLOCO` podem ser removidos em uma
tentativa alternativa. Não é feita automaticamente uma consulta somente pelo
nome da rua quando o número está disponível.

Para todo candidato à geocodificação, o endereço informado é consultado
primeiro, inclusive quando possui número. Se não houver resultado, o nome do
estabelecimento é usado como fallback, mantendo bairro e Recife na validação.
O arquivo
`data/processados/nomes_locais_edl.csv` é um inventário dos nomes extraídos dos
EDLs, não uma exigência para executar a consulta.

## Fontes

- `TOTAL DE EDL POR DS.xlsx`: oito abas de EDLs, com cabeçalhos introdutórios;
- `Georreferenciamento OVT 2026 ATUALIZAÇÃO.xlsx`: oito abas de localização;
- planilhas XLS de 2024 e 2025: uma aba por bairro, com ciclos em grupos de
  colunas;
- planilha XLSX de 2026: dados tabulares da aba `CICLO 1`.

As seguintes fontes auxiliares não são carregadas como observações:

- `CONSOLIDADOS DISTRITOS.xlsx` de 2024 e 2025;
- aba `Plan1` da planilha de 2026;
- aba `TOTAL DE PE E EDL` da planilha de EDLs.

Essas fontes contêm consolidações, tabelas dinâmicas ou resumos, não registros
individuais de locais ou ovitrampas.

A extração preserva arquivo, aba e linha de origem. As planilhas originais não
são modificadas.

## Tabelas

- `edls`: um registro por local com EDL, incluindo `QUANT. DE EDL REAL`,
  coordenadas, qualidade geográfica e situação temporal da estação;
- `ovitrampas`: cadastro georreferenciado, ID original e chave normalizada;
- `ciclos`: ano, ciclo e datas disponíveis;
- `observacoes_ovitrampas`: observações por ovitrampa e ciclo.

O modelo detalhado e o diagrama estão em [`docs/erd.md`](docs/erd.md). O grão
e as chaves das tabelas são:

| Tabela | Grão | Chave principal |
| --- | --- | --- |
| `edls` | um local EDL | `id_edl` |
| `ovitrampas` | uma ovitrampa do cadastro georreferenciado atual | `id` (`id_ovt_chave` é chave de negócio) |
| `ciclos` | um ano e ciclo | `(ano, ciclo)` |
| `observacoes_ovitrampas` | uma observação por ovitrampa e ciclo | `id` |

`observacoes_ovitrampas` possui chave estrangeira real para `ciclos` por
`(ano, ciclo)`. A relação com `ovitrampas` ocorre por `id_ovt_chave` e é
intencionalmente lógica: observações históricas podem não existir no cadastro
georreferenciado atual.

Em `edls`, linhas de total e marcadores `RETIRADO` sem local são ignorados.
Locais reais com indicação como `RETIRADA EM JANEIRO 2026` são preservados com
`situacao_edl = retirado`, `mes_retirada = 2026-01` e `ativo_ate = 2026-01`.
O mês é mantido sem inventar um dia. Linhas de total e marcadores ignorados são
registrados em `auditoria_registros.csv`.

Linhas da planilha de localização de ovitrampas sem identificador e sem
endereço, bairro ou coordenadas também são ignoradas antes da geocodificação e
registradas em `auditoria_registros.csv`.

Coordenadas são preservadas nos valores originais e tratados. A classificação
geográfica usa o limite municipal do Recife em `dentro_recife`,
`fora_recife_proxima`, `fora_recife` ou `sem_coordenada`.

As fontes podem apresentar coordenadas em duas colunas ou em uma única célula,
incluindo formatos decimais, graus/minutos/segundos e indicadores `S`, `W` ou
`O`. O parser usa somente as colunas de coordenadas e não interpreta números
encontrados em metadados, como nomes de abas ou linhas de origem. Registros
fora do Recife permanecem no banco. Aqueles até 1 km do limite são registrados
para revisão manual; os mais distantes podem ser encaminhados para
geocodificação por endereço. Coordenadas ausentes ou ambíguas ficam nulas e
são auditadas.

O tratamento da coordenada é separado da geocodificação: `regra_coordenada`
descreve correções seguras, como unir pontos decimais ou ajustar sinais, e
`status_geocodificacao` descreve a consulta de endereço. Uma geocodificação sem
sucesso não apaga uma coordenada tratada da planilha.

IDs de ovitrampas mantêm o valor original e recebem uma chave normalizada em
maiúsculas, sem espaços ou pontuação. As colisões são registradas em
`auditoria_ids.csv`. A mesma auditoria reconcilia o cadastro georreferenciado
com as observações, indicando se cada ID aparece em ambas as fontes, somente
no cadastro ou somente nas observações.

Os status das observações são preservados em `status_original` e normalizados
em `status`: `F` (Fechado), `E` (Extraviado), `R` (Recusado), `D`
(Desocupado) e `REC` (Recuperada). Valores inválidos não são convertidos
silenciosamente.

A auditoria de datas registra apenas datas corrigidas ou inferidas. Campos
vazios e datas válidas sem alteração não são incluídos no CSV de auditoria.
As decisões consideram o arquivo, a aba, a coluna, o ciclo, a sequência de
aproximadamente 15 dias e o status da observação. Datas posteriores associadas
a `REC` podem ser mantidas quando forem compatíveis com uma recuperação após o
ciclo.

As linhas estruturais de títulos das seções de EDLs são excluídas da tabela
`edls` e registradas em `auditoria_registros.csv`.

## Avaliação da qualidade

A execução do pipeline não encerra a análise. Para um exame fino dos dados, é
necessário consultar o banco SQLite e revisar os arquivos de auditoria:

- `database/aedes_bi.sqlite`;
- `data/auditoria/auditoria_coordenadas.csv`;
- `data/auditoria/auditoria_datas.csv`;
- `data/auditoria/auditoria_ids.csv`;
- `data/auditoria/auditoria_registros.csv`.

A avaliação deve verificar, no mínimo:

- contagens por tabela, ano, ciclo, distrito e classificação geográfica;
- duplicidades de IDs e de observações;
- registros sem coordenada ou com coordenada inválida;
- coordenadas corrigidas, invertidas ou obtidas por geocodificação;
- datas efetivamente corrigidas ou inferidas;
- IDs presentes somente no cadastro georreferenciado ou somente nas
  observações;
- registros estruturais excluídos das tabelas tratadas.
- candidatos e tentativas no `data/processados/inventario_geocodificacao.csv`;
- quantidade de resultados recuperados do cache e de novas consultas HTTP.

Todos os CSVs de auditoria seguem a mesma ordem inicial de rastreabilidade:
`tipo_auditoria`, `tipo_registro`, `arquivo_origem`, `aba_origem`,
`linha_origem`, colunas de origem, IDs, ano e ciclo. Os campos específicos e a
decisão de qualidade aparecem depois dessa identificação.

### Limitação temporal do georreferenciamento

O cadastro georreferenciado disponível atualmente é o arquivo de 2026:
`Georreferenciamento OVT 2026 ATUALIZAÇÃO.xlsx`. As observações possuem
histórico de 2024, 2025 e 2026, mas não há, até o momento, um cadastro
georreferenciado histórico equivalente para cada ano.

Por isso, uma ovitrampa pode aparecer nas observações de 2024 ou 2025 e não
estar no cadastro georreferenciado atual. Essa situação é registrada como
`somente_observacoes` em `auditoria_ids.csv` e não deve ser interpretada
automaticamente como erro da observação.

Também é necessário distinguir:

```text
sem cadastro georreferenciado
sem coordenada na fonte
coordenada inválida
coordenada corrigida
coordenada obtida por geocodificação
```

Coordenadas não devem ser copiadas de outro ano sem evidência. A geocodificação
por endereço é executada por padrão para registros sem coordenada e para pontos
fora do Recife a mais de 1 km do limite. Ela fica identificada separadamente da
coordenada original. Use `--sem-geocodificar` para uma execução sem consultas
externas.

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
    normalizados, classificações contra o limite municipal e inventário de
    geocodificação.
6. **Implantação:** executar `uv run python main.py` a partir da raiz.

O escopo deste módulo não inclui clima, COMPESA, arboviroses ou H3.
