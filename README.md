# TexoCAD Agents

[LangGraph](https://www.langchain.com/langgraph) backend for [TexoCAD](https://www.texocad.ai): AI design agents that turn product ideas into requirements, concept images, 3D models, and CAD.

The web app lives in **[texocad-ui](https://github.com/torayeff/texocad-ui)**.

Developed by **Robominder**.

## Agents

| Graph | Agent | What it does |
| --- | --- | --- |
| `clari` | Clari, requirements engineer | Turns an idea into a product brief with requirements, constraints, and success criteria |
| `lumi` | Lumi, concept engineer | Generates concept images and turns them into 3D models (TRELLIS.2 on Modal) |
| `forma` | Forma, CAD engineer | Writes CadQuery code, runs it in a sandbox, and exports STEP/GLB with rendered previews |
| `sketch` | Sketch (legacy) | Generates and edits images |
| `cad` | CAD (legacy) | Earlier CadQuery CAD agent |

Graphs and custom auth are registered in [`langgraph.json`](langgraph.json).

## Requirements

- Python 3.12+
- An [OpenAI](https://platform.openai.com) API key. The chat model is `gpt-6-astra` and images use `gpt-image-2`.
- An Azure Storage account for designs, revisions, and files. The tables are created automatically.
- A [Daytona](https://www.daytona.io) account, which runs the generated CadQuery code. The sandbox snapshot `texocad-cadquery-v1` is created on first use.
- A [Clerk](https://clerk.com) application. Use the same one as the UI.
- A [Modal](https://modal.com) account. Only needed for Lumi's image-to-3D.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp env.example .env   # then fill in the values below
langgraph dev
```

The API runs at http://localhost:2024. Set `NEXT_PUBLIC_LANGGRAPH_API_URL` in the UI to this URL.

### Environment variables

| Variable | Description |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI API key |
| `OPENAI_API_BASE` | Optional. Custom OpenAI-compatible endpoint. |
| `AZURE_STORAGE_CONNECTION_STRING`, `AZURE_STORAGE_CONTAINER_NAME` | Azure Storage connection string and private blob container. Must match the UI. |
| `DAYTONA_API_URL`, `DAYTONA_API_KEY` | Daytona credentials |
| `CLERK_JWKS_URL` | `https://<your-clerk-frontend-api>/.well-known/jwks.json` |
| `CLERK_ISSUER` | `https://<your-clerk-frontend-api>` |
| `ALLOWED_TOKEN_AZP` | Comma-separated UI origins allowed to call the API, e.g. `http://localhost:3000` |
| `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`, `MODAL_PROFILE` | Modal credentials (`MODAL_PROFILE` is optional) |
| `HF_TOKEN` | Hugging Face token, used only when building the Modal image |

### Auth and access

Every request must carry a Clerk session token (v2) whose `azp` is listed in `ALLOWED_TOKEN_AZP`. Running an agent also requires one of these:

- the `early_bird` Clerk Billing plan (the `pla` claim)
- a `freeAccess` claim
- a `vipAccess` claim, which also unlocks every registered model

If a user has none of these, their runs are rejected. To set up the `freeAccess`/`vipAccess` claims in Clerk, see [texocad-ui → Studio access](https://github.com/torayeff/texocad-ui#studio-access). Plans and policies live in [`texocad_agents/auth/`](texocad_agents/auth/). Models are listed in [`texocad_agents/models.py`](texocad_agents/models.py).

### Image-to-3D (Modal)

Lumi calls a TRELLIS.2 app deployed on Modal. It runs on H100 and L4 GPUs. Deploy it once:

```bash
modal setup   # or export MODAL_TOKEN_ID / MODAL_TOKEN_SECRET
HF_TOKEN=... modal deploy texocad_agents/sandbox/modal/trellis.py
```

`HF_TOKEN` must have access to the gated Hugging Face models that are baked into the image (see [`build_image.py`](texocad_agents/sandbox/modal/build_image.py)).

## Deployment

`langgraph dev` runs an in-memory server for local development. For production, deploy using `langgraph.json` (for example with `langgraph build` or LangGraph Platform). See the [LangGraph deployment docs](https://docs.langchain.com/langgraph-platform).

## License

Copyright 2026 Robominder Ltd.

Licensed under the [Apache License, Version 2.0](LICENSE).
