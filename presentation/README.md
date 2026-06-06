# Presentation

Quarto project for the AgentCOP policy-agent talk.

- **`slides.qmd`** — 5-minute RevealJS deck (the overall flow + components).
- **`documentation.qmd`** — background documentation (deeper detail on each component + the contracts).

## Build / view

```bash
quarto render presentation                 # -> presentation/_output/{slides,documentation}.html
quarto preview presentation/slides.qmd     # live preview in browser
```

Open `_output/slides.html` and press **F** for fullscreen, **S** for speaker notes, **arrow keys**
to navigate. Rendered output is gitignored — regenerate with the command above.

Requires the Quarto CLI (https://quarto.org). Diagrams use Quarto's built-in Mermaid; no extra deps.
