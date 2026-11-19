(() => {
  'use strict';
  const key = 'mcp-skills:theme';
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  let preference = 'system';
  try { const saved = localStorage.getItem(key); if (['system','light','dark'].includes(saved)) preference = saved; } catch {}
  function apply() {
    const theme = preference === 'system' ? (media.matches ? 'dark' : 'light') : preference;
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    const color = document.querySelector('meta[name="theme-color"]');
    if (color) color.content = theme === 'dark' ? '#101114' : '#f7f8fa';
    const select = document.getElementById('theme-select');
    if (select) select.value = preference;
  }
  window.AgentTheme = {
    getPreference: () => preference,
    setPreference(value) {
      if (!['system','light','dark'].includes(value)) return;
      preference = value;
      try { localStorage.setItem(key, value); } catch {}
      apply();
    }
  };
  apply();
  if (media.addEventListener) media.addEventListener('change', apply);
  else if (media.addListener) media.addListener(apply);
  document.addEventListener('DOMContentLoaded', () => {
    apply();
    document.getElementById('theme-select')?.addEventListener('change', event => window.AgentTheme.setPreference(event.target.value));
  });
})();
