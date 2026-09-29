// Conservative multilingual estimate, not a provider-specific tokenizer.
// Shared policy: ASCII ~3 chars/token, BMP non-ASCII ~1.2 tokens/char,
// supplementary characters ~3 tokens/char. Never turn 28k into 112k Chinese chars.
export function textTokens(text = '') {
  let units = 0
  for (const char of String(text)) {
    const point = char.codePointAt(0)
    units += point <= 127 ? 5 : point <= 65535 ? 18 : 45
  }
  return Math.ceil(units / 15)
}

export function contentTokens(blocks = []) {
  return blocks.reduce((sum, block) => {
    if (block.type === 'image') return sum + 2048 // never price base64 as prose
    if (block.type === 'text' || block.type === 'reasoning') return sum + textTokens(block.text) + 4
    if (block.type === 'tool-call') return sum + textTokens(block.name) + textTokens(block.arguments) + 4
    if (block.type === 'tool-result') return sum + contentTokens(block.content) + 4
    return sum + textTokens(JSON.stringify(block)) + 4
  }, 0)
}

export function messageTokens(message) {
  return message ? contentTokens(message.content) + 4 : 0
}

export function headerTokens(header) {
  return textTokens(header?.system || '') + textTokens(JSON.stringify(header?.tools || [])) + 8
}
