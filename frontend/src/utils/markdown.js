/**
 * Minimal markdown rendering for agent output.
 *
 * Mirrors `render_markdown` in `backend/app/agent/render.py` deliberately: the
 * same subset, the same order of operations. The trace page and the app should
 * show the same run the same way, and two diverging renderers is how that stops
 * being true.
 *
 * The security property is the order: **escape the whole string first, then add
 * structure**. Once every `<` is `&lt;`, nothing the model emits can become a tag,
 * so there is no sanitiser to get wrong and no dependency to keep patched. Links
 * are deliberately not converted — a `javascript:` URL then has nowhere to go.
 *
 * Unrecognised input passes through as escaped text, which is the honest failure
 * mode: showing a conclusion plainly beats showing it with literal `**`.
 */

function escapeHtml(text) {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#x27;')
}

export function renderMarkdown(source) {
  if (!source) return ''

  let text = escapeHtml(source)

  // Stash fenced blocks so their contents are not treated as markdown.
  const blocks = []
  text = text.replace(/```[a-zA-Z0-9]*\n([\s\S]*?)```/g, (_m, body) => {
    blocks.push(body)
    return `\u0000BLOCK${blocks.length - 1}\u0000`
  })

  const out = []
  let listStack = []
  let inTable = false
  let tableRows = []

  const closeList = () => {
    while (listStack.length) out.push(`</${listStack.pop()}>`)
  }

  const flushTable = () => {
    if (!inTable) return
    if (tableRows.length) {
      const [head, ...body] = tableRows
      out.push('<table><thead><tr>')
      head.forEach((cell) => out.push(`<th>${cell}</th>`))
      out.push('</tr></thead><tbody>')
      body.forEach((row) => {
        out.push('<tr>')
        row.forEach((cell) => out.push(`<td>${cell}</td>`))
        out.push('</tr>')
      })
      out.push('</tbody></table>')
    }
    inTable = false
    tableRows = []
  }

  for (const rawLine of text.split('\n')) {
    const line = rawLine.trim()

    if (line.startsWith('|') && line.endsWith('|')) {
      const cells = line.slice(1, -1).split('|').map((c) => c.trim())
      if (cells.every((c) => /^[-: ]*$/.test(c))) continue // separator row
      closeList()
      inTable = true
      tableRows.push(cells)
      continue
    }
    flushTable()

    const heading = line.match(/^(#{1,6})\s+(.*)$/)
    if (heading) {
      closeList()
      const level = Math.min(heading[1].length + 1, 6) // h1 stays the page title
      out.push(`<h${level}>${heading[2]}</h${level}>`)
      continue
    }

    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line)) {
      closeList()
      out.push('<hr/>')
      continue
    }

    const bullet = line.match(/^[-*+]\s+(.*)$/)
    if (bullet) {
      if (listStack[listStack.length - 1] !== 'ul') {
        closeList()
        listStack.push('ul')
        out.push('<ul>')
      }
      out.push(`<li>${bullet[1]}</li>`)
      continue
    }

    const numbered = line.match(/^\d+[.)]\s+(.*)$/)
    if (numbered) {
      if (listStack[listStack.length - 1] !== 'ol') {
        closeList()
        listStack.push('ol')
        out.push('<ol>')
      }
      out.push(`<li>${numbered[1]}</li>`)
      continue
    }

    closeList()
    if (!line) continue
    // A stashed code block is already a block element; <p><pre> is invalid.
    if (line.startsWith('\u0000BLOCK')) {
      out.push(line)
      continue
    }
    out.push(`<p>${line}</p>`)
  }

  closeList()
  flushTable()

  let result = out.join('\n')
  result = result.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
  result = result.replace(/(?<![\w*])\*([^*\n]+)\*(?![\w*])/g, '<em>$1</em>')
  result = result.replace(/`([^`\n]+)`/g, '<code>$1</code>')

  blocks.forEach((body, index) => {
    result = result.split(`\u0000BLOCK${index}\u0000`).join(`<pre class="md-block">${body}</pre>`)
  })

  return result
}
