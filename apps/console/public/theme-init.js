(function () {
  var key = 'terminal-mcp.console.theme'
  var theme = 'oled-dark'
  try {
    var stored = localStorage.getItem(key)
    if (stored === 'oled-dark' || stored === 'soft-light') theme = stored
  } catch (_) {}
  document.documentElement.dataset.theme = theme
  document.documentElement.style.colorScheme = theme === 'soft-light' ? 'light' : 'dark'
})()
