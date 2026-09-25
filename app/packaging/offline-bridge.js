// Private pipes replace HTTP/SSE; the shared page retains its original UI.
(() => {
  window.KAROSPACE_OFFLINE = true;
  const pending = new Map(), listeners = new Map();
  let sequence = 0;
  window.fetch = (path, options = {}) => new Promise((resolve, reject) => {
    if (!['/auth', '/opening', '/preview', '/send', '/interrupt', '/history', '/recovery/preview'].includes(path)) {
      reject(new Error('Network requests are disabled offline.')); return;
    }
    const id = ++sequence;
    pending.set(id, resolve);
    window.webkit.messageHandlers.offline.postMessage({id, path, body: options.body ? JSON.parse(options.body) : {}});
  });
  window.EventSource = class {
    addEventListener(type, callback) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(callback);
    }
    close() {}
  };
  window.karoOfflineReceive = message => {
    if ('id' in message) {
      const resolve = pending.get(message.id);
      if (resolve) { pending.delete(message.id); resolve({ok: message.status < 400, status: message.status, json: async () => message.body}); }
    } else if (message.type) {
      for (const callback of listeners.get(message.type) || []) callback({data: JSON.stringify(message)});
    }
  };
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelector('.kind').textContent = 'offline';
    document.querySelector('header .note').textContent = 'Local model · Network blocked · Files stay on this Mac.';
    document.querySelector('footer .hint').textContent = '/tools shows capabilities · /inspect checks your dataset · ⌘V pastes · Enter sends.';
    document.getElementById('auth').title = 'Local model; no cloud credentials';
    document.getElementById('send').textContent = 'Send locally';
    document.getElementById('input').spellcheck = false;
    document.getElementById('input').placeholder = 'Ask about your selected dataset, or type /tools for local KaroSpace capabilities.';
  });
})();
