# Website Downloader

Ferramenta web para baixar replicas completas de sites, incluindo conteudo renderizado por JavaScript.

## Funcionalidades

- Download completo de sites com HTML, CSS, JS, imagens e fontes
- Renderizacao de JavaScript com Playwright/Chromium
- Captura de imagens lazy-loaded
- Exportacao em arquivo ZIP
- Upload opcional do ZIP para um repositorio GitHub de referencias
- Interface em tempo real com logs de progresso
- Limpeza automatica de arquivos temporarios
- Ajustes para visualizacao offline

## Deploy

Veja [DEPLOY.md](DEPLOY.md) para instrucoes completas de deploy no Render, Railway e outras plataformas.

## Upload para Repositorio de Referencias

O WebDrop pode enviar o ZIP gerado para um repositorio GitHub logo apos o download.

Variaveis de ambiente:

```bash
GITHUB_UPLOAD_TOKEN=seu_token_com_permissao_de_contents_write
GITHUB_TARGET_OWNER=eurodrigobispo
GITHUB_TARGET_REPO=referencias-html
GITHUB_TARGET_BRANCH=main
GITHUB_TARGET_ROOT=sites
```

Sem `GITHUB_UPLOAD_TOKEN`, o download local continua funcionando normalmente e a interface informa que o envio ao GitHub esta indisponivel.

## Desenvolvimento Local

Requisitos:

- Python 3.11 a 3.14
- uv

Instalacao:

```bash
uv sync
uv run playwright install chromium
uv run python app.py
```

Acesse `http://localhost:5001`.

Execucao rapida no Windows:

- Dois cliques em `run-local.bat`
- Ou rode `.\run-local.ps1` no PowerShell

No primeiro uso o script cria `.venv`, instala as dependencias e instala o Chromium do Playwright. Depois disso ele apenas sobe o servidor local.

Envio automatico local para o GitHub:

- Preencha `.env.local` com base em `.env.local.example`
- O `run-local.ps1` carrega esse arquivo automaticamente antes de subir o servidor
