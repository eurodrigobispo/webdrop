# WebDrop

Ferramenta web para baixar replicas completas de sites, incluindo conteudo renderizado por JavaScript.

![Python](https://img.shields.io/badge/Python-3.11--3.14-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.1-000000?logo=flask&logoColor=white)
![Playwright](https://img.shields.io/badge/Playwright-1.58-2EAD33?logo=playwright&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)
![Deploy](https://img.shields.io/badge/Render-Deployed-46E3B7?logo=render&logoColor=white)

## O que e

WebDrop captura sites completos — HTML, CSS, JS, imagens e fontes — renderizando JavaScript com Playwright/Chromium antes da captura. Ideal para sites construidos com frameworks como Next.js, Gatsby, Nuxt e outras SPAs.

O resultado e exportado em um arquivo ZIP com URLs reescritas para funcionar offline.

## Funcionalidades

- **Captura completa** — Playwright renderiza JavaScript antes de capturar, garantindo que SPAs e conteudo dinamico sejam incluidos
- **Logs em tempo real** — Server-Sent Events (SSE) via Flask transmitem cada etapa do processo ao vivo na interface
- **Exportacao em ZIP** — todos os assets empacotados com URLs reescritas para funcionar offline
- **Download em lote** — suporte a multiplas URLs processadas sequencialmente
- **Lazy load capture** — captura de imagens carregadas sob demanda
- **Upload para GitHub** — envio opcional do ZIP para um repositorio de referencias via GitHub API
- **Limpeza automatica** — sessoes e arquivos temporarios sao removidos automaticamente apos 30 minutos

## Arquitetura

```
web-downloader/
├── app.py              # Servidor Flask — rotas, SSE, sessoes, upload GitHub
├── downloader.py       # Motor de download — Playwright, parsing, rewrite de URLs
├── templates/
│   └── index.html      # Interface WebDrop — UI completa com animacoes
├── Dockerfile          # Container Docker com Playwright/Chromium
├── entrypoint.sh       # Script de inicializacao (gunicorn)
├── requirements.txt    # Dependencias Python (pip)
├── pyproject.toml      # Configuracao do projeto (uv)
├── render.yaml         # Configuracao de deploy no Render
├── Procfile            # Alternativa para plataformas Heroku-like
├── run-local.bat       # Execucao rapida no Windows (cmd)
├── run-local.ps1       # Execucao rapida no Windows (PowerShell)
└── downloads/          # Diretorio temporario (gitignored)
```

### Fluxo de funcionamento

```
Usuario insere URL
      │
      ▼
POST /start-download ──► Cria sessao + thread
      │
      ▼
WebsiteDownloader.process()
  ├── Playwright abre Chromium headless
  ├── Navega ate a URL e aguarda network idle
  ├── Intercepta recursos via CDP (CSS, JS, fontes, imagens)
  ├── Faz scroll para disparar lazy load
  ├── Captura HTML renderizado
  ├── Reescreve URLs para caminhos locais
  └── Salva todos os assets em /downloads/{session}/
      │
      ▼
ZIP gerado ──► (Opcional) Upload para GitHub via API
      │
      ▼
SSE envia evento "done" ──► Frontend dispara download automatico
```

### Stack

| Camada | Tecnologia |
|--------|------------|
| Backend | Python 3.11+, Flask 3.1, Gunicorn |
| Renderizacao | Playwright (Chromium headless) |
| Frontend | HTML/CSS/JS vanilla, Plus Jakarta Sans, Lucide Icons |
| Background | UnicornStudio (animacao WebGL Nexus Cloud) |
| Container | Docker (python:3.11-slim-bookworm) |
| Deploy | Render.com (Docker, auto-deploy) |

## Desenvolvimento local

**Requisitos:** Python 3.11 a 3.14, [uv](https://docs.astral.sh/uv/)

```bash
# Clonar
git clone https://github.com/eurodrigobispo/web-downloader.git
cd web-downloader

# Instalar dependencias
uv sync
uv run playwright install chromium

# Rodar
uv run python app.py
```

Acesse `http://localhost:5001`

### Windows (atalho)

- **CMD:** dois cliques em `run-local.bat`
- **PowerShell:** `.\run-local.ps1`

No primeiro uso, o script cria `.venv`, instala dependencias e o Chromium do Playwright. Depois disso, apenas sobe o servidor.

### Variaveis de ambiente

Copie `.env.local.example` para `.env.local`:

| Variavel | Descricao | Obrigatoria |
|----------|-----------|-------------|
| `GITHUB_UPLOAD_TOKEN` | Token com permissao `contents:write` para envio ao repo de referencias | Nao |
| `GITHUB_TARGET_OWNER` | Owner do repositorio alvo (default: `eurodrigobispo`) | Nao |
| `GITHUB_TARGET_REPO` | Nome do repositorio alvo (default: `referencias-html`) | Nao |
| `GITHUB_TARGET_BRANCH` | Branch alvo (default: `main`) | Nao |
| `GITHUB_TARGET_ROOT` | Pasta raiz no repo (default: `sites`) | Nao |

Sem `GITHUB_UPLOAD_TOKEN`, o download local funciona normalmente — a interface apenas informa que o envio ao GitHub esta indisponivel.

## Deploy

O projeto esta configurado para deploy no **Render.com** com Docker e auto-deploy.

Veja [DEPLOY.md](DEPLOY.md) para instrucoes completas de deploy no Render, Railway e outras plataformas.

### Deploy rapido no Render

1. Fork ou conecte este repositorio no [Render](https://render.com)
2. Crie um novo **Web Service** com environment **Docker**
3. Plano minimo: **Starter** ($7/mes) — precisa de 512MB+ RAM para Playwright
4. O Render detecta o `Dockerfile` automaticamente
5. Auto-deploy ativado: cada push na `main` atualiza o site

## API

| Endpoint | Metodo | Descricao |
|----------|--------|-----------|
| `/` | GET | Interface WebDrop |
| `/start-download` | POST | Inicia download de uma URL |
| `/start-batch-download` | POST | Inicia download de multiplas URLs |
| `/stream/{session_id}` | GET | Stream SSE de logs em tempo real |
| `/session-result/{session_id}` | GET | Resultado da sessao (status, links) |
| `/download-file/{session_id}` | GET | Baixa o ZIP gerado |
| `/download-batch-file/{session_id}/{index}` | GET | Baixa ZIP de item do lote |
| `/upload-to-repo/{session_id}` | POST | Envia ZIP para repo GitHub |

## Creditos

Baseado no projeto [Website-Downloader](https://github.com/asimov-academy/Website-Downloader) da Asimov Academy.
UI redesenhada com background animado do [Nexus Cloud](https://nexus-cloud.aura.build).

## Licenca

MIT
