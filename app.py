# app.py
import streamlit as st
import json
import logging
from openai import OpenAI

import tools  # carrega o tools.py (view 'dados', execuções DuckDB, etc.)
from tools import available_tools  # dicionário { nome_tool: função_wrapper }

# =========================
# Configuração de logging
# =========================
logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')

# =========================
# Página
# =========================
st.set_page_config(
    page_title='Agente Híbrido SQL',
    page_icon='🚀',
    layout='wide'
)
st.title('Saw Chat')

# =========================
# Cliente OpenAI
# =========================
try:
    client = OpenAI(api_key=st.secrets['openai']['api_key'])
except KeyError:
    st.error('API key da OpenAI não encontrada. Configure .streamlit/secrets.toml')
    st.stop()

# =========================
# Ferramentas (schemas)
# =========================
TOOL_SCHEMAS = [
    # ---- 1) FERRAMENTA ÚNICA E INTELIGENTE (PRIORITÁRIA) ----
    {
        'type': 'function',
        'function': {
            'name': 'get_metrics',
            'description': (
                'Calcula métricas (consultas_hoje, media_consultas_mes, media_idade, '
                'ranking_especialidades, ranking_tipo_atendimento) com período opcional.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'metric': {
                        'type': 'string',
                        'description': (
                            "Uma das: 'consultas_hoje', 'media_consultas_mes', 'media_idade', "
                            "'ranking_especialidades', 'ranking_tipo_atendimento'."
                        )
                    },
                    'period': {
                        'type': 'object',
                        'description': (
                            "Filtro temporal: {'mode': 'today'|'current_month'|'months_ago'|'named_month'|'none', "
                            "'months_ago': 1, 'month': 9, 'year': 2025}"
                        ),
                        'properties': {
                            'mode': {'type': 'string'},
                            'months_ago': {'type': 'integer'},
                            'month': {'type': 'integer'},
                            'year': {'type': 'integer'}
                        }
                    },
                    'top_n': {'type': 'integer', 'description': 'Quantidade de linhas para rankings.'}
                },
                'required': ['metric']
            }
        }
    },

    # ---- 2) ESPECIALISTAS LEGADOS (mantidos para compatibilidade) ----
    {
        'type': 'function',
        'function': {
            'name': 'get_consultas_hoje',
            'description': 'Número total de consultas realizadas HOJE (data_atendimento = hoje).'
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_media_consultas_mes',
            'description': 'Média diária de consultas no MÊS ATUAL.'
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_ranking_especialidades',
            'description': 'Ranking (TOP 5) das especialidades mais utilizadas.'
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_media_idade',
            'description': 'Média de idade geral ou filtrada por últimos N meses.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'periodo_meses': {
                        'type': 'integer',
                        'description': 'Filtrar últimos N meses (opcional).'
                    }
                }
            }
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_ranking_tipo_atendimento',
            'description': 'Ranking (TOP 5) por TIPO_PRESTADOR.'
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_schema',
            'description': 'Lista colunas e tipos da view dados (Parquet).'
        }
    },

    # ---- 3) GENERALISTA (último recurso) ----
    {
        'type': 'function',
        'function': {
            'name': 'run_generic_text_to_sql_query',
            'description': 'Gerar uma SQL via LLM e executar (use apenas se nenhuma especialista/metrics cobrir).',
            'parameters': {
                'type': 'object',
                'properties': {
                    'user_question': {
                        'type': 'string',
                        'description': 'Pergunta completa do usuário para gerar a SQL.'
                    }
                },
                'required': ['user_question']
            }
        }
    }
]

# =========================
# Instruções do sistema
# =========================
SYSTEM_INSTRUCTIONS = """
Você é um analista de dados sênior, líder de uma equipe.

REGRAS DE ORQUESTRAÇÃO:
1) SEMPRE prefira a ferramenta 'get_metrics' passando o período adequado:
   - current_month: mês atual
   - months_ago: mês relativo (ex.: 1 = mês anterior)
   - named_month: mês/ano específicos
   - today: hoje
   - none: sem filtro temporal
2) Use as ferramentas especialistas legadas apenas se a pergunta for exatamente a que elas cobrem
   e fizer mais sentido chamá-las diretamente.
3) Use o 'run_generic_text_to_sql_query' apenas para perguntas fora do escopo das métricas acima.
4) Se uma ferramenta retornar [[None]] ou [] para agregações, interprete como 0/sem dados e responda objetivamente.
5) Responda sempre em português, de forma direta e com números/datas explícitas quando relevante.
"""

# =========================
# Estado de conversa
# =========================
if 'messages' not in st.session_state:
    st.session_state.messages = [{'role': 'system', 'content': SYSTEM_INSTRUCTIONS}]

# Render do histórico (sem o system)
for message in st.session_state.messages:
    if message['role'] != 'system':
        with st.chat_message(message['role']):
            st.markdown(message.get('content', ''))

# =========================
# Utilitário para exibir resultados
# =========================
def _render_tool_result_as_markdown(content: str):
    """
    content é JSON (string) retornado pela função Python da tool.
    Exibe de maneira amigável.
    """
    try:
        data = json.loads(content)
    except Exception:
        st.markdown(content)
        return

    # Casos comuns
    if isinstance(data, dict):
        # tente formatos conhecidos
        if 'rows' in data and isinstance(data['rows'], list):
            if not data['rows']:
                st.info('Sem dados.')
                return
            # Exibir como tabela simples
            st.write(data['rows'])
            return
        # Caso dicionário simples, formata chave/valor
        st.json(data)
        return

    if isinstance(data, list):
        if not data:
            st.info('Sem dados.')
            return
        st.write(data)
        return

    st.write(data)

# =========================
# Loop principal de chat
# =========================
if prompt := st.chat_input('Qual sua pergunta sobre os dados?'):
    # 1) Guarda pergunta
    st.session_state.messages.append({'role': 'user', 'content': prompt})
    with st.chat_message('user'):
        st.markdown(prompt)

    # 2) Execução
    with st.chat_message('assistant'):
        with st.status('Analisando... 🧠', expanded=True) as status:
            try:
                while True:
                    status.update(label='Avaliando a melhor ferramenta... 💡')

                    # Prepara histórico
                    messages_to_send = []
                    for msg in st.session_state.messages:
                        if isinstance(msg, dict):
                            messages_to_send.append(msg)
                        elif hasattr(msg, 'model_dump'):
                            messages_to_send.append(msg.model_dump())

                    # Chamada ao modelo
                    response = client.chat.completions.create(
                        model='gpt-5-mini',
                        messages=messages_to_send,
                        tools=TOOL_SCHEMAS,
                        tool_choice='auto'
                    )
                    response_message = response.choices[0].message

                    # 3) Se houver chamadas de ferramenta
                    if response_message.tool_calls:
                        status.update(label='Consultando os dados... 📊')

                        # Salva a "mensagem assistant" com tool_calls no histórico
                        st.session_state.messages.append(response_message.model_dump())

                        # Para cada tool-call
                        for tool_call in response_message.tool_calls:
                            function_name = tool_call.function.name
                            function_args_json = tool_call.function.arguments

                            st.write(f'🛠️ Chamando: `{function_name}({function_args_json})`')

                            if function_name not in available_tools:
                                logging.error(f'LLM tentou chamar ferramenta desconhecida: {function_name}')
                                function_response = json.dumps({'error': f'Ferramenta desconhecida: {function_name}'})
                            else:
                                func = available_tools[function_name]
                                try:
                                    function_response = func(function_args_json)
                                except Exception as tool_e:
                                    logging.error(f'Erro ao executar ferramenta {function_name}: {tool_e}')
                                    function_response = json.dumps({'error': f'Erro ao executar a ferramenta: {tool_e}'})

                            # Adiciona a resposta da tool ao histórico
                            st.session_state.messages.append({
                                'tool_call_id': tool_call.id,
                                'role': 'tool',
                                'name': function_name,
                                'content': function_response
                            })

                        # Continua o loop para permitir que o modelo use o resultado das tools
                        continue

                    # 4) Sem tool_calls: resposta final
                    status.update(label='Resposta recebida!', state='complete')
                    final_answer = response_message.content or ''
                    st.markdown(final_answer)
                    st.session_state.messages.append({'role': 'assistant', 'content': final_answer})
                    break

            except Exception as e:
                if 'status' in locals():
                    status.update(label=f'Ocorreu um erro: {e}', state='error')
                logging.error(f'Erro no loop de chat: {e}', exc_info=True)
                st.error(f'Ocorreu um erro: {e}')
