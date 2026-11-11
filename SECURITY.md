# Segurança

## Como relatar uma falha

**Não abra uma issue pública.** Use o relato privado do GitHub:
[Report a vulnerability](https://github.com/LucasAzevedoCosta/interlinear/security/advisories/new)
(aba **Security** do repositório).

Inclua o que for possível:

- o que a falha permite (ler chaves, ler arquivos, executar código...);
- os passos para reproduzir;
- a versão (commit) e o sistema operacional;
- se o app estava rodando direto ou no Docker, e com quais variáveis
  `INTERLINEAR_*`.

Você recebe uma resposta assim que possível. Depois da correção, a falha é
publicada com crédito a quem relatou, se quiser.

## O que conta como falha

O Interlinear é um app local, de um usuário só. O modelo de ameaça parte daí:

- **Dentro do escopo:**
  - outro site aberto no navegador conseguir falar com o app (DNS rebinding,
    CSRF, checagem de `Host`/`Origin` contornada);
  - chaves de API vazando pela API, pelos logs ou pelos arquivos exportados;
  - um PDF ou EPUB malicioso conseguir ler ou gravar arquivos fora da pasta
    de dados, ou executar código;
  - texto do livro sendo enviado para qualquer lugar além do provedor de IA
    configurado.
- **Fora do escopo:**
  - quem já tem acesso à sua conta no computador conseguir ler a pasta de
    dados;
  - riscos de expor o app na rede com `INTERLINEAR_ALLOWED_HOSTS=*` ou
    publicando a porta em `0.0.0.0`: o padrão é escutar só em `127.0.0.1`,
    e mudar isso é escolha de quem configura;
  - o conteúdo das respostas dos provedores de IA.
