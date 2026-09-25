// Saves text as a file. The API needs the bearer token, which a plain link
// cannot send, so downloads are fetched first and saved from memory.
export function saveFile(filename, text, type = 'text/csv') {
  const url = URL.createObjectURL(new Blob([text], { type }))
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}
