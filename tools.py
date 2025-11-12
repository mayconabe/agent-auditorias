# tools.py
import duckdb
import datetime
import json
import logging
import streamlit as st
from openai import OpenAI

# =========================================
# Configuração básica
# =========================================
PARQUET_FILE = 'data.parquet'

con = duckdb.connect(database=':memory:', read_only=False)
logging.info(f'Conexão DuckDB criada. Lendo {PARQUET_FILE}...')

try:
    # View direta para o Parquet (predicate/projection pushdown)
    con.execute(f"CREATE OR REPLACE VIEW dados AS SELECT * FROM read_parquet('{PARQUET_FILE}')")
    logging.info("View 'dados' sobre o Parquet criada.")
except Exception as e:
    logging.error(f'Falha ao criar VIEW do DuckDB: {e}')


# =========================================
# Helpers de SQL
# =========================================
def extract_sql_from_text(text: str) -> str:
    """
    Extrai um SQL 'cru' de uma resposta do modelo:
    - Remove blocos ```sql ... ```
    - Remove prefixos 'SQL:' / 'Query:'
    - Mantém somente o primeiro statement até ';', se houver
    """
    if not text:
        return ''
    t = text.strip()
    if '```' in t:
        parts = t.split('```')
        if len(parts) >= 2:
            t = parts[1]
        t = t.lstrip('sql').lstrip('\n').strip()
    for pref in ('SQL:', 'Sql:', 'sql:', 'Query:', 'query:'):
        if t.startswith(pref):
            t = t[len(pref):].strip()
    if ';' in t:
        t = t.split(';')[0] + ';'
    return t.strip()


def _normalize_from_dados(sql: str) -> str:
    """
    Garante que consultas façam referência à view 'dados' (e não ao caminho do arquivo).
    """
    if not sql:
        return sql
    out = sql
    out = out.replace(f'FROM {PARQUET_FILE}', 'FROM dados')
    out = out.replace(f"FROM '{PARQUET_FILE}'", 'FROM dados')
    out = out.replace('from dados', 'FROM dados')
    return out

def _create_stable_view():
    """
    Cria/recria a VIEW 'dados' com casts estáveis para evitar rebind/binder errors.
    Ajuste a lista de colunas conforme seu Parquet (incluí as mais usadas aqui).
    """
    con.execute(f"""
        CREATE OR REPLACE VIEW dados AS
        SELECT
            * REPLACE (
                CAST(data_atendimento      AS TIMESTAMP) AS data_atendimento,
                CAST(data_emissao_guia     AS TIMESTAMP) AS data_emissao_guia,
                CAST(idade                 AS INTEGER)   AS idade,
                CAST(descricao_especialidade_medica AS VARCHAR) AS descricao_especialidade_medica,
                CAST(TIPO_PRESTADOR        AS VARCHAR)   AS TIPO_PRESTADOR,
                CAST(prestador_uf          AS VARCHAR)   AS prestador_uf
            )
        FROM read_parquet('{PARQUET_FILE}')
    """)
    logging.info("View 'dados' (estável) criada com casts.")

# chame na inicialização
try:
    _create_stable_view()
except Exception as e:
    logging.error(f'Falha ao criar VIEW estável do DuckDB: {e}')


def _run_query(sql: str) -> str:
    """
    Executa a query no DuckDB e retorna JSON.
    Se detectar o binder error da view, recria a view e tenta 1x novamente.
    """
    sql = _normalize_from_dados(sql)
    for attempt in (1, 2):  # 1ª tentativa + 1 retry
        try:
            logging.info(f'[Especialista] Executando query DuckDB (tentativa {attempt}): {sql}')
            result = con.execute(sql).fetchall()
            return json.dumps(result)
        except Exception as e:
            msg = str(e)
            logging.error(f'Erro na query DuckDB: {msg}')
            if attempt == 1 and 'Contents of view were altered' in msg:
                logging.warning('Detectado binder error; recriando a VIEW dados e tentando novamente...')
                try:
                    _create_stable_view()
                    continue  # tenta de novo
                except Exception as ee:
                    logging.error(f'Erro ao recriar VIEW: {ee}')
            # se não era o binder error, ou já tentamos o retry, retorna erro
            return json.dumps({'error': msg})



# =========================================
# 1) FUNÇÕES ESPECIALISTAS (rápidas e confiáveis)
# =========================================
def _get_consultas_hoje_logic() -> str:
    """
    Número total de consultas realizadas HOJE.
    """
    sql = """
    SELECT COALESCE(COUNT(*), 0)
    FROM dados
    WHERE CAST(data_atendimento AS DATE) = CURRENT_DATE
    """
    return _run_query(sql)


def _get_media_consultas_mes_logic() -> str:
    """
    Média diária de consultas no MÊS ATUAL.
    """
    sql = """
    WITH daily_counts AS (
        SELECT CAST(data_atendimento AS DATE) AS dia, COUNT(*) AS total
        FROM dados
        WHERE data_atendimento >= date_trunc('month', current_date)
        GROUP BY dia
    )
    SELECT COALESCE(AVG(total), 0) FROM daily_counts
    """
    return _run_query(sql)


def _get_ranking_especialidades_logic() -> str:
    """
    Ranking (TOP 5) das especialidades por contagem.
    """
    sql = """
    SELECT descricao_especialidade_medica, COUNT(*) AS total
    FROM dados
    WHERE descricao_especialidade_medica IS NOT NULL
    GROUP BY 1
    ORDER BY total DESC
    LIMIT 5
    """
    return _run_query(sql)


def _get_media_idade_logic(periodo_meses: int = None) -> str:
    """
    Média de idade com filtro opcional de últimos N meses.
    """
    base_sql = 'SELECT COALESCE(AVG(idade), 0) FROM dados WHERE idade IS NOT NULL AND idade BETWEEN 0 AND 120'
    if periodo_meses:
        try:
            meses = int(periodo_meses)
            base_sql += f" AND data_atendimento >= (CURRENT_DATE - INTERVAL '{meses} months')"
            logging.info(f'Aplicando filtro de {meses} meses para média de idade.')
        except (ValueError, TypeError):
            logging.warning(f"Valor inválido de 'periodo_meses': {periodo_meses}. Ignorando filtro.")
    return _run_query(base_sql)


def _get_ranking_tipo_atendimento_logic() -> str:
    """
    Ranking (TOP 5) por TIPO_PRESTADOR.
    Obs.: se desejar INDICACAO_ACIDENTE, troque a coluna abaixo.
    """
    sql = """
    SELECT TIPO_PRESTADOR, COUNT(*) AS total
    FROM dados
    WHERE TIPO_PRESTADOR IS NOT NULL
    GROUP BY 1
    ORDER BY total DESC
    LIMIT 5
    """
    return _run_query(sql)


def _get_schema_logic() -> str:
    """
    Lista colunas e tipos (DuckDB DESCRIBE).
    """
    sql = 'DESCRIBE SELECT * FROM dados'
    return _run_query(sql)


# =========================================
# 2) WRAPPERS DAS FERRAMENTAS (assinaturas para o app)
# =========================================
def get_consultas_hoje(json_arguments: str) -> str:
    logging.info(f'Wrapper: get_consultas_hoje chamado com {json_arguments}')
    return _get_consultas_hoje_logic()


def get_media_consultas_mes(json_arguments: str) -> str:
    logging.info(f'Wrapper: get_media_consultas_mes chamado com {json_arguments}')
    return _get_media_consultas_mes_logic()


def get_ranking_especialidades(json_arguments: str) -> str:
    logging.info(f'Wrapper: get_ranking_especialidades chamado com {json_arguments}')
    return _get_ranking_especialidades_logic()


def get_media_idade(json_arguments: str) -> str:
    logging.info(f'Wrapper: get_media_idade chamado com {json_arguments}')
    try:
        args = json.loads(json_arguments) if json_arguments else {}
        periodo = args.get('periodo_meses')
        return _get_media_idade_logic(periodo_meses=periodo)
    except Exception:
        return _get_media_idade_logic()


def get_ranking_tipo_atendimento(json_arguments: str) -> str:
    logging.info(f'Wrapper: get_ranking_tipo_atendimento chamado com {json_arguments}')
    return _get_ranking_tipo_atendimento_logic()


def get_schema(json_arguments: str) -> str:
    logging.info(f'Wrapper: get_schema chamado com {json_arguments}')
    return _get_schema_logic()


# =========================================
# 3) FERRAMENTA GENERALISTA (Text-to-SQL) — último recurso
# =========================================
SCHEMA_INFO = 'Esquema não carregado'
try:
    schema_df = con.execute('DESCRIBE SELECT * FROM dados').df()
    SCHEMA_INFO = "Arquivo 'dados' (do Parquet) com as colunas:\n"
    for _, row in schema_df.iterrows():
        SCHEMA_INFO += f"- {row['column_name']} ({row['column_type']})\n"
except Exception as e:
    logging.error(f'Erro ao carregar schema para o Generalista: {e}')

PROMPT_GERADOR_GENERICO = f"""
Você é um gerador de DuckDB SQL. Sua única tarefa é traduzir a pergunta do usuário em UMA ÚNICA query SQL válida.
A tabela é 'dados'. Ela contém dados dos últimos 6 meses.
O schema é:
{SCHEMA_INFO}
REGRAS OBRIGATÓRIAS:
1. Use 'FROM dados'.
2. PERFORMANCE: Adicione 'LIMIT 20' a qualquer query que não seja uma agregação (COUNT, SUM, AVG).
3. Use sintaxe DuckDB (similar ao PostgreSQL).
4. Responda APENAS com a string SQL. Não inclua explicações.
5. IMPORTANTE: SEMPRE envolva agregações (COUNT, SUM, AVG) com COALESCE(..., 0). (Não use NVL.)
6. Regra de negócio: Para 'consultas', filtre 'tipo_guia = 'Consulta Eletiva'' se a pergunta exigir explicitamente.
Pergunta do Usuário:
{{user_question}}
SQL:
"""


def run_generic_text_to_sql_query(json_arguments: str) -> str:
    """
    Generalista: recebe uma pergunta, gera o SQL com o modelo e executa.
    """
    logging.info(f'Generalista (Text-to-SQL) ativado com args: {json_arguments}')
    try:
        args = json.loads(json_arguments) if json_arguments else {}
        user_question = args.get('user_question')
        if not user_question:
            return json.dumps({'error': 'Nenhuma pergunta fornecida ao Generalista.'})
    except Exception as e:
        return json.dumps({'error': f'Erro ao decodificar argumentos: {e}'})

    try:
        client = OpenAI(api_key=st.secrets['openai']['api_key'])
        prompt = PROMPT_GERADOR_GENERICO.format(user_question=user_question)
        response = client.chat.completions.create(
            model='gpt-4o-mini',
            messages=[{'role': 'user', 'content': prompt}],
            temperature=0
        )
        sql_query = response.choices[0].message.content
        sql_query = extract_sql_from_text(sql_query)

        if not sql_query:
            return json.dumps({'error': 'O Generalista não conseguiu gerar SQL.'})

        low = sql_query.lower()
        if ('limit' not in low) and ('count(' not in low) and ('avg(' not in low) and ('sum(' not in low):
            sql_query = sql_query.rstrip(';') + ' LIMIT 20;'

        logging.info(f'[Generalista] Query gerada: {sql_query}')
        return _run_query(sql_query)

    except Exception as e:
        logging.error(f'Erro no Generalista (Text-to-SQL): {e}', exc_info=True)
        return json.dumps({'error': f'Erro fatal no Generalista: {e}'})


# =========================================
# 4) MÉTRICAS GENÉRICAS — 1 ferramenta para tudo
# =========================================
def _mk_month_bounds_sql(months_ago: int = 0) -> str:
    """
    CTE mes_ref(inicio, fim) para um mês relativo:
      0 = mês atual, 1 = mês anterior, etc.
    Usa aritmética de INTERVAL do DuckDB: current_date - INTERVAL 'N months'
    """
    m = int(months_ago)
    return f"""
    WITH mes_ref AS (
        SELECT
            date_trunc('month', current_date - INTERVAL '{m} months') AS inicio,
            date_trunc('month', current_date - INTERVAL '{m} months') + INTERVAL '1 month' AS fim
    )
    """


def _mk_named_month_bounds_sql(month: int, year: int) -> str:
    """
    CTE mes_ref(inicio, fim) para mês/ano específicos (1–12).
    """
    m = int(month)
    y = int(year)
    return f"""
    WITH mes_ref AS (
        SELECT
            make_date({y}, {m}, 1) AS inicio,
            make_date({y}, {m}, 1) + INTERVAL '1 month' AS fim
    )
    """


def _validate_named_month(p: dict, max_months_back: int = 12, allow_future: bool = False):
    """
    Valida 'named_month' (month/year) contra uma janela móvel.
    Retorna (ok: bool, msg_erro: str)
    """
    try:
        y = int(p['year'])
        m = int(p['month'])
    except Exception:
        return False, 'Parâmetros inválidos para named_month (month/year).'

    import datetime as _dt
    try:
        target = _dt.date(y, m, 1)
    except ValueError:
        return False, 'Mês/ano inválidos.'

    today = _dt.date.today().replace(day=1)
    delta_months = (today.year - y) * 12 + (today.month - m)

    if delta_months < 0 and not allow_future:
        return False, 'Mês no futuro não é permitido.'
    if delta_months > max_months_back:
        return False, f'Mês fora da janela de {max_months_back} meses.'
    return True, ''


def _infer_year_for_month_only(month: int, max_months_back: int = 12):
    """
    Dado apenas um mês (1-12), escolhe o ano mais recente que não é futuro
    e que está dentro da janela de 'max_months_back' meses.
    Retorna (year:int) ou levanta ValueError se impossível.
    """
    import datetime as _dt
    m = int(month)
    if not (1 <= m <= 12):
        raise ValueError('Mês inválido.')

    today = _dt.date.today().replace(day=1)
    candidate_current_year = _dt.date(today.year, m, 1)
    if candidate_current_year <= today:
        candidate = candidate_current_year
    else:
        candidate = _dt.date(today.year - 1, m, 1)

    delta_months = (today.year - candidate.year) * 12 + (today.month - candidate.month)
    if delta_months < 0 or delta_months > max_months_back:
        raise ValueError('Mês fora da janela permitida.')
    return candidate.year


def _resolve_period(period: dict | None) -> dict:
    """
    Normaliza o período:
      - 'today'
      - 'current_month'
      - 'months_ago'   (months_ago >= 0)
      - 'named_month'  (month, year)
      - 'none'
    """
    if not period:
        return {'mode': 'current_month'}
    mode = period.get('mode', 'current_month')
    if mode == 'months_ago':
        return {'mode': 'months_ago', 'months_ago': max(0, int(period.get('months_ago', 1)))}
    if mode == 'named_month':
        out = {'mode': 'named_month', 'month': int(period.get('month')), 'year': period.get('year')}
        # ano pode vir ausente => será inferido mais abaixo
        return out
    if mode in ('today', 'current_month', 'none'):
        return {'mode': mode}
    return {'mode': 'current_month'}


def _sql_where_period(mode: str) -> str:
    """
    Predicado WHERE para data_atendimento conforme o mode.
    Pressupõe CTE mes_ref quando aplicável.
    """
    if mode == 'today':
        return 'CAST(data_atendimento AS DATE) = CURRENT_DATE'
    if mode in ('current_month', 'months_ago', 'named_month'):
        return 'data_atendimento >= mes_ref.inicio AND data_atendimento < mes_ref.fim'
    return '1=1'  # none


def _get_metrics_logic(metric: str, period: dict | None = None, top_n: int = 5, include_na: bool = False) -> str:
    """
    Constrói e executa a SQL ideal para a métrica pedida.
    Métricas suportadas:
      - 'consultas_hoje'
      - 'media_consultas_mes'         (usa period para escolher mês)
      - 'media_idade'                 (period opcional)
      - 'ranking_especialidades'      (period opcional)
      - 'ranking_tipo_atendimento'    (period opcional; usa TIPO_PRESTADOR)
      - 'ranking_especialidades_por_sexo' (top_n por sexo; period opcional)
    """
    p = _resolve_period(period)
    mode = p['mode']

    # Normalização/validação do período
    if mode == 'named_month':
        if 'year' not in p or p['year'] is None:
            try:
                p['year'] = _infer_year_for_month_only(p['month'], max_months_back=12)
            except Exception:
                return json.dumps({'error': 'Mês fora da janela de 12 meses.'})
        ok, msg = _validate_named_month(p, max_months_back=12, allow_future=False)
        if not ok:
            return json.dumps({'error': msg})
    elif mode == 'months_ago':
        try:
            ma = int(p.get('months_ago', 1))
        except Exception:
            ma = 1
        if ma < 0:
            return json.dumps({'error': 'months_ago não pode ser negativo.'})
        if ma > 12:
            return json.dumps({'error': 'months_ago acima do limite (12).'})

    # Parte temporal (CTE quando necessário)
    cte = ''
    if mode == 'months_ago':
        cte = _mk_month_bounds_sql(p.get('months_ago', 1))
    elif mode == 'current_month':
        cte = _mk_month_bounds_sql(0)
    elif mode == 'named_month':
        cte = _mk_named_month_bounds_sql(p['month'], p['year'])

    where_period = _sql_where_period(mode)
    from_suffix = ', mes_ref' if cte else ''

    # --------- Métricas ----------
    if metric == 'consultas_hoje':
        sql = f"""
        SELECT COALESCE(COUNT(*), 0)
        FROM dados
        WHERE {where_period}
        """
        return _run_query(sql)

    if metric == 'media_consultas_mes':
        sql = f"""
        {cte}
        , daily_counts AS (
            SELECT CAST(data_atendimento AS DATE) AS dia, COUNT(*) AS total
            FROM dados{from_suffix}
            WHERE {where_period}
            GROUP BY 1
        )
        SELECT COALESCE(AVG(total), 0) FROM daily_counts
        """
        return _run_query(sql)

    if metric == 'media_idade':
        sql = f"""
        {cte}
        SELECT COALESCE(AVG(idade), 0)
        FROM dados{from_suffix}
        WHERE {where_period} AND idade IS NOT NULL AND idade BETWEEN 0 AND 120
        """
        return _run_query(sql)

    if metric == 'ranking_especialidades':
        sql = f"""
        {cte}
        SELECT COALESCE(descricao_especialidade_medica, 'Sem descrição') AS especialidade,
               COUNT(*) AS total
        FROM dados{from_suffix}
        WHERE {where_period}
        GROUP BY 1
        ORDER BY total DESC, especialidade ASC
        LIMIT {int(top_n)}
        """
        return _run_query(sql)

    if metric == 'ranking_tipo_atendimento':
        # Troque TIPO_PRESTADOR por INDICACAO_ACIDENTE se desejar esse conceito.
        sql = f"""
        {cte}
        SELECT COALESCE(TIPO_PRESTADOR, 'Não informado') AS tipo_atendimento,
               COUNT(*) AS total
        FROM dados{from_suffix}
        WHERE {where_period}
        GROUP BY 1
        ORDER BY total DESC, tipo_atendimento ASC
        LIMIT {int(top_n)}
        """
        return _run_query(sql)

    # ---- Nova métrica: ranking de especialidades por sexo (top_n por sexo)
    if metric == 'ranking_especialidades_por_sexo':
        filtro_sexo = '' if include_na else 'AND sexo IS NOT NULL'
        sql = f"""
        {cte}
        , base AS (
            SELECT
                CASE
                    WHEN upper(sexo) IN ('F','FEMININO') THEN 'Feminino'
                    WHEN upper(sexo) IN ('M','MASCULINO') THEN 'Masculino'
                    ELSE 'Não informado'
                END AS sexo_norm,
                COALESCE(descricao_especialidade_medica, 'Sem descrição') AS especialidade
            FROM dados{from_suffix}
            WHERE {where_period}
            {filtro_sexo}
        ),
        agg AS (
            SELECT sexo_norm AS sexo, especialidade, COUNT(*) AS atendimentos
            FROM base
            GROUP BY 1, 2
        ),
        ranked AS (
            SELECT *,
                   row_number() OVER (PARTITION BY sexo ORDER BY atendimentos DESC, especialidade ASC) AS rn
            FROM agg
        )
        SELECT sexo, especialidade, atendimentos
        FROM ranked
        WHERE rn <= {int(top_n)}
        ORDER BY (sexo = 'Feminino') DESC, atendimentos DESC, especialidade ASC
        """
        return _run_query(sql)

    return json.dumps({'error': f"Métrica desconhecida: {metric}"})


def get_metrics(json_arguments: str) -> str:
    """
    Wrapper único de métricas.
    Payload:
      {
        'metric': 'media_consultas_mes' | 'consultas_hoje' | 'media_idade' |
                  'ranking_especialidades' | 'ranking_tipo_atendimento' |
                  'ranking_especialidades_por_sexo',
        'period': {'mode': 'today'|'current_month'|'months_ago'|'named_month'|'none',
                   'months_ago': 1,
                   'month': 9, 'year': 2025},
        'top_n': 10,
        'include_na': false
      }
    """
    logging.info(f'Wrapper: get_metrics chamado com {json_arguments}')
    try:
        args = json.loads(json_arguments) if json_arguments else {}
        metric = args.get('metric')
        period = args.get('period')
        top_n = int(args.get('top_n', 5))
        include_na = bool(args.get('include_na', False))
        if not metric:
            return json.dumps({'error': 'Parâmetro "metric" é obrigatório.'})
        return _get_metrics_logic(metric=metric, period=period, top_n=top_n, include_na=include_na)
    except Exception as e:
        logging.error(f'Erro em get_metrics: {e}', exc_info=True)
        return json.dumps({'error': str(e)})


# =========================================
# 5) Registro de ferramentas exportadas
# =========================================
available_tools = {
    'get_consultas_hoje': get_consultas_hoje,
    'get_media_consultas_mes': get_media_consultas_mes,
    'get_ranking_especialidades': get_ranking_especialidades,
    'get_media_idade': get_media_idade,
    'get_ranking_tipo_atendimento': get_ranking_tipo_atendimento,
    'get_schema': get_schema,
    'run_generic_text_to_sql_query': run_generic_text_to_sql_query,
    'get_metrics': get_metrics,  # ferramenta única e inteligente
}
