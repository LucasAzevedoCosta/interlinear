# Provedores de IA

O Interlinear traduz com o provedor que você cadastrar em **Configurações**.
Cada livro pode ter o seu (painel "Tradução deste livro"); sem isso, vale o
provedor ativo.

| Tipo | Como cobra | Precisa de |
|------|------------|------------|
| OpenAI | por token, na sua conta da API | chave de API |
| Anthropic | por token, na sua conta da API | chave de API |
| Google Gemini | por token (tem camada gratuita) | chave de API |
| Endpoint compatível (OpenRouter, DeepSeek, Groq…) | por token, no serviço | chave de API e endereço |
| LM Studio, Ollama | nada: roda na sua máquina | o programa aberto, com um modelo |
| **Claude pela assinatura** (experimental) | sai dos limites do seu plano Pro ou Max | Claude Code instalado e logado |

## Claude pela assinatura (experimental)

Traduz usando o seu plano Claude Pro ou Max, sem chave de API e sem cobrança
por token: o uso sai dos limites do plano, como o uso do próprio Claude.

### Como funciona

O Interlinear não fala com a API da Anthropic. Ele roda o **Claude Code**
oficial, instalado e logado por você, em modo não interativo (`claude -p`), e
lê a resposta. O Interlinear nunca vê nem guarda o token do seu login.

Cada chamada roda isolada:

- com o prompt de tradução do Interlinear no lugar do prompt do Claude Code;
- sem ferramentas (`--tools ""`): o modelo só lê e responde;
- sem as suas configurações, hooks, servidores MCP e skills;
- numa pasta temporária vazia, sem `CLAUDE.md`;
- sem `ANTHROPIC_API_KEY` no ambiente, para a CLI não cair na cobrança por
  token.

Antes do primeiro lote de cada tradução, o Interlinear confere com
`claude auth status` que a CLI está logada **com a conta Claude** (e não com
uma chave de API). "Testar conexão" mostra o plano.

### Requisitos

1. [Claude Code](https://claude.com/claude-code) instalado. Rode `claude` uma
   vez no terminal e entre com a sua conta Claude.
2. O Interlinear rodando **direto na máquina**, não no Docker: a CLI e o seu
   login estão na máquina, fora do container. Isso pede Python 3.11 ou mais
   novo (veja "Instalação" no README).
3. Se o `claude` não estiver no `PATH`, aponte `INTERLINEAR_CLAUDE` para ele.

### Cadastro

Em **Configurações → Adicionar provedor**, escolha "Claude pela assinatura".
Não há chave nem endereço. O modelo é um apelido: `haiku` (mais rápido e
econômico), `sonnet` (o padrão) ou `opus`.

### Limites do plano

- Cada lote leva de 2 a 4 segundos a mais que pela API: a CLI abre um
  processo novo por chamada.
- Um romance de 100 mil palavras gasta na casa de 250 a 400 mil tokens de
  entrada e outro tanto de saída. No Pro, isso provavelmente passa da janela
  de uso de 5 horas; no Max, há mais folga. A Anthropic não publica os
  números exatos.
- Quando o limite acaba, a tradução **pausa** em vez de falhar: a barra de
  progresso mostra "Limite do plano atingido. Continua às HH:MM", e os lotes
  continuam sozinhos quando a janela renova. Se a CLI não disser a hora, o
  Interlinear tenta de novo a cada 10 minutos. Parar a tradução funciona
  durante a pausa.
- Deixe **Pedidos simultâneos** em 1 ou 2: mais que isso só gasta a janela
  mais rápido.

### Riscos

As regras da Anthropic para o uso programático da assinatura mudaram várias
vezes em 2026. A página
[Legal and compliance do Claude Code](https://code.claude.com/docs/en/legal-and-compliance)
(lida em novembro de 2026) diz que:

- terceiros não podem oferecer login do Claude.ai nem "route requests through
  Free, Pro, Max, or Ultra plan credentials on behalf of their users";
- terceiros não podem coletar nem guardar o token do login;
- isso não impede "an end user from signing in to the unmodified Claude Code
  binary with their own Claude subscription";
- os limites do plano pressupõem "ordinary, individual usage", e a Anthropic
  pode agir sem aviso.

O Interlinear segue o caminho que a página permite: chama o binário oficial,
sem modificação, logado por você, e nunca toca no token. Ainda assim, traduzir
um livro inteiro em lote é um uso pesado, e a leitura das regras é sua. O
risco prático é a Anthropic restringir a conta. Na dúvida, use a chave de API
(provedor Anthropic).
