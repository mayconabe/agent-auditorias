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

if 'show_suggestions' not in st.session_state:
    # mostra sugestões só se ainda não houve mensagem do usuário
    st.session_state.show_suggestions = not any(m.get('role') == 'user' for m in st.session_state.messages)

# Flags de controle (uma única vez)
st.session_state.setdefault('is_processing', False)   # trava input durante execução
st.session_state.setdefault('pending_prompt', None)   # buffer para clique/digitação

# =========================
# Utilitário para exibir resultados
# =========================
def _render_tool_result_as_markdown(content: str):
    """
    Exibe o resultado da tool de forma amigável.
    Ignora JSONs brutos e mensagens de depuração.
    """
    if not content or content.strip() in ('None', '[]', '{}'):
        return

    try:
        data = json.loads(content)
    except Exception:
        # se não for JSON, só mostra texto
        if not any(s in content for s in ['rows', 'value', 'error']):
            st.markdown(content)
        return

    # se veio {"error": "..."}
    if isinstance(data, dict) and 'error' in data:
        st.error(data['error'])
        return

    # se veio {"value": x}
    if isinstance(data, dict) and 'value' in data:
        st.info(f"Resultado numérico: **{round(data['value'], 2)}**")
        return

    # se veio {"rows": [...]} e não tem 'value'
    if isinstance(data, dict) and 'rows' in data:
        rows = data['rows']
        if not rows:
            st.info('Sem dados disponíveis.')
        elif len(rows) == 1 and len(rows[0]) == 1:
            st.info(f"Resultado: **{rows[0][0]}**")
        else:
            st.dataframe(rows)
        return

    # fallback genérico
    st.json(data)

# =========================
# Estado inicial
# =========================
if 'messages' not in st.session_state:
    st.session_state.messages = [{'role': 'system', 'content': SYSTEM_INSTRUCTIONS}]

# controla UI
if 'show_suggestions' not in st.session_state:
    st.session_state.show_suggestions = True      # some após 1ª interação
if 'is_processing' not in st.session_state:
    st.session_state.is_processing = False        # travar input durante execução
if 'pending_prompt' not in st.session_state:
    st.session_state.pending_prompt = None        # buffer de clique/digitação

# =========================
# Input SEMPRE visível (desabilita durante processamento)
# =========================
user_typed = st.chat_input(
    'Qual sua pergunta sobre os dados?',
    key='chat_box',
    disabled=st.session_state.is_processing
)

if user_typed and not st.session_state.is_processing:
    st.session_state.pending_prompt = user_typed
    st.session_state.show_suggestions = False
    st.session_state.is_processing = True
    st.rerun()

st.subheader('Sugestões rápidas')
cols = st.columns(5)
COMMON_QUESTIONS = [
    'Quantas consultas foram feitas hoje?',
    'Qual foi a média de consultas nesse mês?',
    'Qual é o ranking de especialidades no mês anterior?',
    'Qual a média de idade nos últimos 3 meses?',
    'Top 5 especialidades médicas por sexo no mês anterior'
]

if st.session_state.show_suggestions:
    for i, q in enumerate(COMMON_QUESTIONS):
        if cols[i].button(q, use_container_width=True, key=f'quick_q_{i}'):
            # NÃO adiciona no histórico aqui para não duplicar
            st.session_state.pending_prompt = q
            st.session_state.show_suggestions = False
            st.session_state.is_processing = True
            st.rerun()

# =========================
# Render do histórico (sem o system e sem tools)
# =========================
for message in st.session_state.messages:
    role = message.get('role')
    content = message.get('content')

    # Não exibe mensagens do sistema nem das tools, nem vazias/None
    if role in ('system', 'tool'):
        continue
    if content is None or str(content).strip().lower() == 'none' or str(content).strip() == '':
        continue

    with st.chat_message(role):
        st.markdown(content)

# =========================
# Processamento
# =========================
if st.session_state.is_processing and st.session_state.pending_prompt is not None:
    prompt = st.session_state.pending_prompt

    # Adiciona a mensagem do usuário UMA única vez aqui
    st.session_state.messages.append({'role': 'user', 'content': prompt})

    with st.chat_message('assistant'):
        with st.status('Analisando... 🧠', expanded=True) as status:
            try:
                st.empty()
                while True:
                    status.update(label='Avaliando a melhor ferramenta... 💡')

                    # monta o histórico para envio
                    messages_to_send = []
                    for msg in st.session_state.messages:
                        if isinstance(msg, dict):
                            messages_to_send.append(msg)
                        elif hasattr(msg, 'model_dump'):
                            messages_to_send.append(msg.model_dump())

                    response = client.chat.completions.create(
                        model='gpt-4o',
                        messages=messages_to_send,
                        tools=TOOL_SCHEMAS,
                        tool_choice='auto'
                    )
                    response_message = response.choices[0].message

                    if response_message.tool_calls:
                        status.update(label='Consultando os dados... 📊')
                        # adiciona a "mensagem de ferramenta" do modelo no histórico
                        st.session_state.messages.append(response_message.model_dump())

                        # executa as tools
                        for tool_call in response_message.tool_calls:
                            function_name = tool_call.function.name
                            function_args_json = tool_call.function.arguments
                            st.write(f'🛠️ Chamando: `{function_name}({function_args_json})`')

                            if function_name not in available_tools:
                                function_response = json.dumps({'error': f'Ferramenta desconhecida: {function_name}'})
                            else:
                                try:
                                    function_response = available_tools[function_name](function_args_json)
                                except Exception as tool_e:
                                    function_response = json.dumps({'error': f'Erro ao executar a ferramenta: {tool_e}'})

                                # registra a resposta da tool no histórico (para a LLM usar)
                                st.session_state.messages.append({
                                    'tool_call_id': tool_call.id,
                                    'role': 'tool',
                                    'name': function_name,
                                    'content': function_response
                                })

                                # 🧹 NÃO exibir o JSON bruto; apenas em modo debug, se quiser
                                if st.session_state.get('debug'):
                                    st.caption(f'🔍 Retorno da tool {function_name}:')
                                    st.json(json.loads(function_response))

                        # volta ao topo do while para o modelo consumir as tool responses
                        continue

                    # resposta final
                    status.update(label='Resposta recebida!', state='complete')
                    final_answer = response_message.content or ''

                    if final_answer and final_answer.strip().lower() != 'none':
                        st.markdown(final_answer)
                        st.session_state.messages.append({'role': 'assistant', 'content': final_answer})
                    break

            except Exception as e:
                if 'status' in locals():
                    status.update(label=f'Ocorreu um erro: {e}', state='error')
                logging.error(f'Erro no loop de chat: {e}', exc_info=True)
                st.error(f'Ocorreu um erro: {e}')

    # **Limpa flags** para voltar o input e evitar reprocesso/duplicação
    st.session_state.pending_prompt = None
    st.session_state.is_processing = False
    st.rerun()
