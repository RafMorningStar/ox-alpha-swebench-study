# Publishing Checklist

## Before Making The Repository Public

- [ ] Read the full Study 01 report and confirm that it matches your intended claims
- [ ] Confirm the author name in `CITATION.cff`
- [ ] Confirm the repository name and GitHub URL
- [ ] Review `drafts/linkedin-id.md` in your own voice
- [ ] Run the validation commands in this file
- [ ] Inspect the private repository rendering on GitHub
- [ ] Confirm no API keys, private endpoints, home paths, or raw provider headers are present
- [ ] Create a `study-01` GitHub Release if trajectories or raw logs will be attached
- [ ] Change repository visibility to public
- [ ] Replace `[LINK REPOSITORY]` in the LinkedIn draft
- [ ] Publish LinkedIn only after the public link works in an incognito window

## Local Validation

From the repository root:

```bash
python scripts/validate_study.py
python -m unittest -v tests/test_benchmark.py
git status --short
```

The runner tests require an environment with mini-SWE-agent and SWE-bench
installed. Activate that environment, then run:

```bash
python -m unittest -v tests/test_benchmark.py
```

## Suggested GitHub Settings

- Description: `Reproducible offline-agent SWE-bench studies of Ox Alpha, Gemini, and Qwen.`
- Topics: `swe-bench`, `llm-evaluation`, `coding-agents`, `ox-alpha`, `reproducible-research`
- Issues: enabled, for protocol questions and future study planning
- Discussions: optional
- Wiki: disabled
- Default branch: `main`

## Study Update Policy

- Do not overwrite Study 01 predictions or evaluator reports.
- Fix prose with a changelog entry.
- Publish a materially different benchmark as Study 02.
- Assign a new protocol ID when task selection, agent prompt, budgets, network policy, or evaluator policy changes.
