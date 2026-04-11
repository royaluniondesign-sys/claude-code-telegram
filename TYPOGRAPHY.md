# AURA Typography System — Permanente

## Font Stack

| Font | File | Weight | Use case |
|------|------|--------|----------|
| AnthropicSerif Display | AnthropicSerif-Display-Semibold-Static.otf | Semibold | Hero display, H1 grande |
| AnthropicSerif | AnthropicSerif-Roman.woff2 + Italic | 400 | H2, quotes, body serif |  
| Styrene A | StyreneA-*.otf | 300/400/500/700 | H3, H4, labels, UI nav |
| Anthropic Sans | AnthropicSans-Roman.woff2 + Italic | 400 | Body copy, UI text |
| Anthropic Mono | AnthropicMono-Roman.woff2 | 400 | Code, data, metrics |

## Jerarquía

```
Display (hero/splash):  AnthropicSerif Display Semibold — 48–96px, tight tracking
H1 (page title):        AnthropicSerif Display Semibold — 32–48px
H2 (section):           AnthropicSerif Roman — 24–32px  
H3 (subsection):        Styrene A Bold — 18–22px
H4 (card title):        Styrene A Medium — 14–16px uppercase tracked
Label/Tag:              Styrene A Light — 10–12px uppercase +0.08em tracking
Body:                   Anthropic Sans — 14–16px, 1.6 line-height
Body italic/quote:      Anthropic Sans Italic — same size
Caption:                Anthropic Sans — 11–12px, color: muted
Code/data:              Anthropic Mono — 12–14px
```

## CSS Variables (para usar en todo)

```css
:root {
  --font-display: 'Anthropic Serif Display', 'AnthropicSerif', Georgia, serif;
  --font-serif:   'AnthropicSerif', 'Anthropic Serif', Georgia, serif;
  --font-head:    'Styrene A', -apple-system, BlinkMacSystemFont, sans-serif;
  --font-sans:    'Anthropic Sans', -apple-system, BlinkMacSystemFont, sans-serif;
  --font-mono:    'Anthropic Mono', 'JetBrains Mono', monospace;
}

/* Usage */
.display      { font-family: var(--font-display); font-size: clamp(40px,6vw,96px); font-weight: 600; letter-spacing: -0.03em; line-height: 1.05; }
h1            { font-family: var(--font-display); font-size: clamp(28px,4vw,48px); font-weight: 600; letter-spacing: -0.02em; line-height: 1.1; }
h2            { font-family: var(--font-serif);   font-size: clamp(22px,3vw,32px); font-weight: 400; letter-spacing: -0.01em; line-height: 1.2; }
h3            { font-family: var(--font-head);    font-size: clamp(16px,2vw,22px); font-weight: 700; line-height: 1.3; }
h4, .label    { font-family: var(--font-head);    font-size: 13px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.08em; }
body, p       { font-family: var(--font-sans);    font-size: 15px; font-weight: 400; line-height: 1.65; }
blockquote    { font-family: var(--font-serif);   font-style: italic; font-size: 17px; line-height: 1.6; }
code, pre     { font-family: var(--font-mono);    font-size: 13px; }
.caption      { font-family: var(--font-sans);    font-size: 11px; opacity: 0.6; }
```

## Uso por medio

### Posts Instagram/Social
- **Headline**: AnthropicSerif Display Semibold — grande, impacto
- **Subtítulo**: Styrene A Medium — complementa
- **Body text**: Anthropic Sans — legible en mobile
- **Stats/data**: Anthropic Mono — credibilidad

### PDFs / Reportes  
- **Cover title**: AnthropicSerif Display — presencia
- **Chapter H1**: AnthropicSerif Roman
- **Section H2**: Styrene A Bold
- **Body**: Anthropic Sans Regular
- **Code blocks**: Anthropic Mono
- **Captions**: Anthropic Sans, smaller

### Emails
- **Subject preview**: Styrene A Bold (si soporta embed)
- **Header**: AnthropicSerif Display o Styrene A Bold
- **Body**: Anthropic Sans (safe for email)
- **CTA button**: Styrene A Medium uppercase
- **Footer**: Anthropic Sans small

### Dashboard (web)
- **KPI numbers**: Styrene A Bold (ya en uso)
- **Panel headers**: Styrene A Medium uppercase (ya en uso)  
- **Body text**: Anthropic Sans (ya en uso)
- **Live feed/logs**: Anthropic Mono (ya en uso)
- **Page header**: AnthropicSerif Display Semibold (NUEVO)

## @font-face para web

```css
@font-face {
  font-family: 'Anthropic Serif Display';
  src: url('/fonts/AnthropicSerif-Display-Semibold-Static.otf') format('opentype');
  font-weight: 600;
  font-style: normal;
  font-display: swap;
}
@font-face {
  font-family: 'AnthropicSerif';
  src: url('/fonts/AnthropicSerif-Roman.woff2') format('woff2');
  font-weight: 400;
  font-style: normal;
  font-display: swap;
}
@font-face {
  font-family: 'AnthropicSerif';
  src: url('/fonts/AnthropicSerif-Italic.woff2') format('woff2');
  font-weight: 400;
  font-style: italic;
  font-display: swap;
}
@font-face {
  font-family: 'Styrene A';
  src: url('/fonts/StyreneA-Light.otf') format('opentype');
  font-weight: 300;
  font-style: normal;
}
@font-face {
  font-family: 'Styrene A';
  src: url('/fonts/StyreneA-Regular.otf') format('opentype');
  font-weight: 400;
  font-style: normal;
}
@font-face {
  font-family: 'Styrene A';
  src: url('/fonts/StyreneA-Medium.otf') format('opentype');
  font-weight: 500;
  font-style: normal;
}
@font-face {
  font-family: 'Styrene A';
  src: url('/fonts/StyreneA-Bold.otf') format('opentype');
  font-weight: 700;
  font-style: normal;
}
@font-face {
  font-family: 'Anthropic Sans';
  src: url('/fonts/AnthropicSans-Roman.woff2') format('woff2');
  font-weight: 400;
  font-style: normal;
}
@font-face {
  font-family: 'Anthropic Sans';
  src: url('/fonts/AnthropicSans-Italic.woff2') format('woff2');
  font-weight: 400;
  font-style: italic;
}
@font-face {
  font-family: 'Anthropic Mono';
  src: url('/fonts/AnthropicMono-Roman.woff2') format('woff2');
  font-weight: 400;
  font-style: normal;
}
```
