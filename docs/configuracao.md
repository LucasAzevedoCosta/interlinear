# Configuração

Tudo se configura por variáveis num arquivo `.env` ao lado do `compose.yaml`.
O [`.env.example`](../.env.example) traz todas, comentadas: copie para `.env`
e preencha só o que for usar.

```bash
cp .env.example .env
docker compose up -d     # recria o container com os valores novos
```

## Variáveis

| Variável                    | Para quê                                          |
| --------------------------- | ------------------------------------------------- |
| `INTERLINEAR_IMAGE_TAG`     | `latest` (padrão) ou `claude`, a imagem com o Claude Code |
| `INTERLINEAR_WITH_CLAUDE`   | `1` inclui o Claude Code ao construir do código (`--build`) |
| `INTERLINEAR_PORT`          | Porta no seu computador (padrão `8000`)           |
| `INTERLINEAR_ALLOWED_HOSTS` | Nomes extras aceitos no `Host`, separados por vírgula (`*` desliga a checagem) |
| `INTERLINEAR_OPENAI_KEY`    | Chave da OpenAI, tem prioridade sobre a salva     |
| `INTERLINEAR_ANTHROPIC_KEY` | Idem, Anthropic                                   |
| `INTERLINEAR_GOOGLE_KEY`    | Idem, Google                                      |
| `INTERLINEAR_COMPAT_KEY`    | Idem, endpoint compatível (OpenRouter etc.)       |
| `INTERLINEAR_OLLAMA_URL`    | Endereço padrão do Ollama                         |
| `INTERLINEAR_LMSTUDIO_URL`  | Endereço padrão do LM Studio                      |
| `INTERLINEAR_LOG_LEVEL`     | `DEBUG` para ver o que o extrator decidiu         |
| `INTERLINEAR_OCR_LANGS`     | Idiomas do OCR (padrão `por+eng`; só os instalados na imagem valem) |

Usar as variáveis de ambiente evita gravar chave nenhuma em disco.

## Dados

Projetos e preferências (inclusive as chaves de API salvas pela tela) ficam no
volume `interlinear-data`, montado em `/data` no container. Sobrevivem a
atualizações e rebuilds; `docker compose down -v` apaga tudo.

O login do Claude Code fica no volume `interlinear-claude`.

## IA local

O container alcança o Ollama e o LM Studio do seu computador por
`host.docker.internal`, já configurado. O LM Studio precisa estar servindo na
rede (opção *Serve on Local Network*), não só em `localhost`.

## Expor na rede local

Por padrão a porta é publicada só em `127.0.0.1`: nada responde na rede. Para
abrir, troque a linha de `ports` do `compose.yaml` por `"8000:8000"` e liste o
IP ou o nome da máquina em `INTERLINEAR_ALLOWED_HOSTS`. Leia antes o
[SECURITY.md](../SECURITY.md): as chaves de API ficam acessíveis a quem
alcançar o app.

## OCR em outros idiomas

A imagem traz o Tesseract com português e inglês. Para outro idioma, acrescente
o pacote `tesseract-ocr-<código>` no `Dockerfile`, reconstrua com
`docker compose up -d --build` e inclua o código em `INTERLINEAR_OCR_LANGS`
(por exemplo `por+eng+spa`).
