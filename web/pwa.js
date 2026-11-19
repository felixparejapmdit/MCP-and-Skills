(() => {
  'use strict';
  const installButton = document.getElementById('install-app');
  const status = document.getElementById('pwa-status');
  const standalone = () => matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  let installPrompt = null;
  let offlineReady = false;

  function announce(message) {
    if (status) status.textContent = message;
  }
  function render() {
    if (!navigator.onLine) announce(offlineReady ? 'Offline · saved inventory' : 'Offline');
    else if (standalone()) announce('App installed');
    else if (offlineReady) announce('Offline ready');
    else announce('Local workspace');
    if (installButton) installButton.hidden = !installPrompt || standalone();
  }

  window.addEventListener('beforeinstallprompt', event => {
    event.preventDefault();
    installPrompt = event;
    render();
  });
  window.addEventListener('appinstalled', () => {
    installPrompt = null;
    render();
  });
  if (installButton) installButton.addEventListener('click', async () => {
    if (!installPrompt) return;
    const prompt = installPrompt;
    installPrompt = null;
    installButton.disabled = true;
    try {
      await prompt.prompt();
      await prompt.userChoice;
    } catch {
      announce('Use your browser menu to install this app.');
    } finally {
      installButton.disabled = false;
      render();
    }
  });
  for (const event of ['online', 'offline']) window.addEventListener(event, () => {
    render();
    window.dispatchEvent(new CustomEvent('mcp-skills:network', {detail: {online: navigator.onLine}}));
  });
  const display = matchMedia('(display-mode: standalone)');
  if (display.addEventListener) display.addEventListener('change', render);

  render();
  if (!['http:', 'https:'].includes(location.protocol) || !isSecureContext || !('serviceWorker' in navigator)) return;
  navigator.serviceWorker.register('/service-worker.js', {scope: '/', updateViaCache: 'none'})
    .then(async registration => {
      await navigator.serviceWorker.ready;
      offlineReady = true;
      render();
      if (registration.active) registration.update().catch(() => {});
    })
    .catch(() => {
      // The dashboard remains usable even if this browser blocks app storage.
      announce('Browser mode · offline storage unavailable');
    });
})();
