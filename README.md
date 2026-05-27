# texocad-agents
TexoCAD Agents

## Anonymous Sessions
Set `ANONYMOUS_SESSION_SECRET` to a strong random value. Anonymous Lumi threads use signed session tokens/cookies scoped to `anon:<id>` owners.

Generate one with:

```shell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## TODO
- [ ] how to add skills?
- [ ] flesh out cadquery prompt with real best-practices guidance